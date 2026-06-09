#!/usr/bin/env python3
"""
pm_argus_agent.py — Argus Investigation Agent

LLM が自律的にツールを選択・呼び出して段階的にプロジェクトデータを分析する
マルチステップ Agent。/argus-investigate Slack コマンドおよび CLI から利用する。

Usage:
    # CLI モード（標準出力のみ）
    python3 scripts/pm_argus_agent.py --investigate "M3の遅延原因を調査" --dry-run
    python3 scripts/pm_argus_agent.py --investigate "先週の決定事項の実行状況" --max-steps 5

環境変数:
    LOCAL_LLM_URL / RIVAULT_URL — LLM バックエンド（pm_argus.py と同じ）
    SLACK_BOT_TOKEN — Slack 返信用（Slack コマンド時のみ）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import (
    open_pm_db,
    fetch_milestone_progress,
    fetch_assignee_workload,
    fetch_overdue_items,
    fetch_weekly_trends,
    fetch_unacknowledged_decisions,
    fetch_summary_stats,
)
from format_utils import (
    format_milestone_table,
    format_overdue_list,
    format_assignee_table,
    format_weekly_trends as format_trends_table,
    format_decisions_list,
)

import yaml

logger = logging.getLogger("pm_argus_agent")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_ARGUS_CONFIG = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_LEGACY = _DATA_DIR / "qa_config.yaml"
_DEFAULT_SINCE_DAYS = 30
_DEFAULT_MAX_STEPS = 5
_DEFAULT_TIMEOUT = 480.0
_CONTEXT_CHAR_LIMIT = 100_000


# =========================================================================== #
#  argus_config.yaml からインデックスDB・チャンネルリスト解決
# =========================================================================== #

def _resolve_index_and_channels(
    channel_id: str | None = None,
) -> tuple[Path, list[str], list[Path], str]:
    """argus_config.yaml を読み、channel_id に対応する index_db (統合)・channels・pm_db_paths・index_name を返す。

    全インデックスは統合 data/qa_index.db を共有し、検索時に index_name でフィルタする。
    """
    qa_index = _DATA_DIR / "qa_index.db"
    config_path = _ARGUS_CONFIG if _ARGUS_CONFIG.exists() else _QA_CONFIG_LEGACY
    if not config_path.exists():
        return qa_index, [], [_PM_DB], "pm"

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    indices = cfg.get("indices") or {}
    channel_map = cfg.get("channel_map") or {}
    default_index = cfg.get("default_index", "pm")

    index_name = channel_map.get(channel_id, default_index) if channel_id else default_index
    index_cfg = indices.get(index_name, {})
    channels = index_cfg.get("channels", [])
    pm_db_list = index_cfg.get("pm_db", ["data/pm.db"])
    pm_db_paths = [_REPO_ROOT / p for p in pm_db_list]
    return qa_index, channels, pm_db_paths, index_name


# =========================================================================== #
#  AgentContext
# =========================================================================== #

@dataclass
class AgentContext:
    conns: list[Any]
    today: str
    since: str
    no_encrypt: bool = False
    data_dir: Path = field(default_factory=lambda: _DATA_DIR)
    minutes_dir: Path = field(default_factory=lambda: _MINUTES_DIR)
    index_db: Path = field(default_factory=lambda: _DATA_DIR / "qa_index.db")
    index_name: str = "pm"
    channels: list[str] = field(default_factory=list)
    cited_chunks: list[dict] = field(default_factory=list)
    cited_knowledge_ids: set[str] = field(default_factory=set)


# =========================================================================== #
#  ToolDef & Registry
# =========================================================================== #

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, str]
    fn: Callable[[dict, AgentContext], str]


def _query_all(ctx: AgentContext, fn, *args, **kwargs) -> list:
    """全 conns に対して fn(conn, ...) を実行し結果リストを結合する。"""
    out: list = []
    for conn in ctx.conns:
        out.extend(fn(conn, *args, **kwargs))
    return out


def _tool_get_milestone_progress(args: dict, ctx: AgentContext) -> str:
    rows = _query_all(ctx, fetch_milestone_progress)
    if not rows:
        return "（マイルストーンが登録されていません）"
    return format_milestone_table(rows, ctx.today)


def _tool_get_overdue_items(args: dict, ctx: AgentContext) -> str:
    items = _query_all(ctx, fetch_overdue_items, ctx.today, ctx.since)
    assignee = args.get("assignee")
    milestone = args.get("milestone")
    if assignee:
        items = [i for i in items if assignee in (i.get("assignee") or "")]
    if milestone:
        items = [i for i in items if i.get("milestone_id") == milestone]
    limit = int(args.get("limit", 20))
    items = items[:limit]
    if not items:
        filt = []
        if assignee:
            filt.append(f"assignee={assignee}")
        if milestone:
            filt.append(f"milestone={milestone}")
        return f"（該当する期限超過アイテムなし{' (' + ', '.join(filt) + ')' if filt else ''}）"
    return format_overdue_list(items, limit=limit)


def _tool_get_assignee_workload(args: dict, ctx: AgentContext) -> str:
    from pm_argus import merge_pm_stats
    all_rows: list = []
    for conn in ctx.conns:
        all_rows.extend(fetch_assignee_workload(conn, ctx.today))
    wl_map: dict[str, dict] = {}
    for w in all_rows:
        name = w["assignee"]
        if name in wl_map:
            wl_map[name]["total_open"] += w["total_open"]
            wl_map[name]["overdue"] += w["overdue"]
            wl_map[name]["no_due_date"] += w.get("no_due_date", 0)
        else:
            wl_map[name] = {**w}
    rows = sorted(wl_map.values(), key=lambda x: (-x["overdue"], -x["total_open"]))
    if not rows:
        return "（担当者データなし）"
    return format_assignee_table(rows)


def _tool_get_weekly_trends(args: dict, ctx: AgentContext) -> str:
    weeks = int(args.get("weeks", 4))
    trend_map: dict[str, dict] = {}
    for conn in ctx.conns:
        for t in fetch_weekly_trends(conn, weeks=weeks):
            k = t["week_start"]
            if k in trend_map:
                trend_map[k]["created"] += t["created"]
                trend_map[k]["closed"] += t["closed"]
            else:
                trend_map[k] = {**t}
    rows = sorted(trend_map.values(), key=lambda x: x["week_start"])
    if not rows:
        return "（トレンドデータなし）"
    return format_trends_table(rows)


def _tool_get_unacknowledged_decisions(args: dict, ctx: AgentContext) -> str:
    since = args.get("since", ctx.since)
    rows = _query_all(ctx, fetch_unacknowledged_decisions, since)
    if not rows:
        return "（未確認決定事項なし）"
    return format_decisions_list(rows)


def _tool_search_action_items(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import _query_action_items
    items: list = []
    for conn in ctx.conns:
        items.extend(_query_action_items(
            conn,
            assignee=args.get("assignee"),
            status=args.get("status"),
            milestone=args.get("milestone"),
            keyword=args.get("keyword"),
            limit=int(args.get("limit", 20)),
        ))
    if not items:
        return "（該当するアクションアイテムなし）"
    lines = []
    for i in items:
        parts = [
            f"ID:{i['id']}",
            f"[{i.get('status', '?')}]",
            i.get("content", "")[:80],
        ]
        if i.get("assignee"):
            parts.append(f"担当:{i['assignee']}")
        if i.get("due_date"):
            parts.append(f"期限:{i['due_date']}")
        if i.get("milestone_id"):
            parts.append(f"MS:{i['milestone_id']}")
        if i.get("requested_by"):
            parts.append(f"依頼者:{i['requested_by']}")
        lines.append(" | ".join(parts))
        if i.get("rationale"):
            lines.append(f"  背景: {i['rationale'][:150]}")
        if i.get("related_ids"):
            lines.append(f"  関連: {i['related_ids']}")
    return "\n".join(lines)


def _tool_search_decisions(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import _query_decisions
    items: list = []
    for conn in ctx.conns:
        items.extend(_query_decisions(
            conn,
            keyword=args.get("keyword"),
            limit=int(args.get("limit", 20)),
        ))
    if not items:
        return "（該当する決定事項なし）"
    lines = []
    for d in items:
        header = f"ID:{d['id']} [{d.get('decided_at', '?')}]"
        if d.get("decided_by"):
            header += f" 判断者:{d['decided_by']}"
        lines.append(f"{header} {d.get('content', '')[:100]}")
        if d.get("rationale"):
            lines.append(f"  根拠: {d['rationale'][:150]}")
        if d.get("related_ids"):
            lines.append(f"  関連: {d['related_ids']}")
    return "\n".join(lines)


def _tool_search_text(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import retrieve_chunks_hyde, rerank_chunks, _format_source_label
    query = args.get("query", "")
    if not query:
        return "（検索クエリが空です）"
    merged = retrieve_chunks_hyde(query, ctx.index_db, k=30, index_name=ctx.index_name)
    if not merged:
        return f"（「{query}」に一致する情報なし）"
    reranked = rerank_chunks(query, merged)
    # グローバル番号でチャンクを蓄積し、最終回答に出典セクションを自動付与する
    lines = []
    for chunk in reranked:
        ctx.cited_chunks.append(chunk)
        idx = len(ctx.cited_chunks)
        label = _format_source_label(chunk)
        ref = chunk.get("source_ref") or ""
        source_type = chunk.get("source_type", "")
        if source_type == "slack_raw" and ref:
            ref_str = f" | <{ref}|スレッドを開く>"
        elif source_type == "minutes_content" and ref:
            held_at = chunk.get("held_at") or ""
            ref_str = f" | {held_at} {ref}" if held_at else f" | {ref}"
        elif source_type == "web" and ref:
            ref_str = f" | <{ref}|リンク>"
        elif source_type == "box_document" and ref:
            ref_str = f" | <{ref}|Boxで開く>"
            slack_links = _fetch_slack_references_for_box(chunk.get("record_id") or "")
            if slack_links:
                ref_str += " / Slack共有: " + ", ".join(slack_links)
        else:
            ref_str = f" | {ref}" if ref else ""
        lines.append(f"[{idx}] 出典: {label}{ref_str}")
        lines.append(f"    {chunk['content'].strip()}")
        lines.append("")
    return "\n".join(lines)


def _open_knowledge_db(ctx: AgentContext):
    """data/knowledge.db を開く。存在しなければ None。"""
    from db_utils import open_knowledge_db
    db_path = ctx.data_dir / "knowledge.db"
    if not db_path.exists():
        return None
    try:
        return open_knowledge_db(db_path, no_encrypt=ctx.no_encrypt)
    except Exception:
        return None


def _tool_search_knowledge(args: dict, ctx: AgentContext) -> str:
    """knowledge.db を topic / current_state / tags で検索する。
    現役レコード（deleted=0 AND superseded_by IS NULL）を優先。
    """
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    include_superseded = bool(args.get("include_superseded") or False)

    conn = _open_knowledge_db(ctx)
    if conn is None:
        return "（knowledge.db が未構築です）"
    try:
        where = ["COALESCE(deleted, 0) = 0"]
        if not include_superseded:
            where.append("superseded_by IS NULL")
        params: list = []
        if query:
            where.append("(topic LIKE ? OR current_state LIKE ? OR tags LIKE ? OR rationale LIKE ?)")
            kw = f"%{query}%"
            params.extend([kw, kw, kw, kw])
        sql = (
            "SELECT id, kind, topic, current_state, confidence,"
            " last_validated_at, decided_at, superseded_by"
            " FROM knowledge WHERE " + " AND ".join(where) +
            " ORDER BY CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
            "          COALESCE(last_validated_at, decided_at, '') DESC"
            " LIMIT ?"
        )
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()

    if not rows:
        return f"（「{query}」に一致するナレッジなし）" if query else "（ナレッジレコードなし）"

    lines = [f"# ナレッジ検索結果 ({len(rows)} 件)"]
    for r in rows:
        ctx.cited_knowledge_ids.add(r["id"])
        validated = r["last_validated_at"] or r["decided_at"] or "-"
        super_mark = f" [superseded_by {r['superseded_by']}]" if r["superseded_by"] else ""
        lines.append(
            f"- **{r['id']}** [{r['kind']}/{r['confidence']}] {r['topic']}"
            f"{super_mark} (validated: {validated})"
        )
        lines.append(f"    {r['current_state']}")
    return "\n".join(lines)


def _tool_get_knowledge(args: dict, ctx: AgentContext) -> str:
    """単一ナレッジレコードを ID 指定でフル展開する（rationale / 代替案 / sources / relations）。"""
    kid = (args.get("id") or "").strip()
    if not kid:
        return "id を指定してください（例: KN-0042）"

    conn = _open_knowledge_db(ctx)
    if conn is None:
        return "（knowledge.db が未構築です）"
    try:
        rec = conn.execute(
            "SELECT * FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()
        if not rec:
            return f"（{kid} が見つかりません）"
        ctx.cited_knowledge_ids.add(kid)

        sources = conn.execute(
            "SELECT source_type, source_ref, weight, excerpt"
            " FROM knowledge_sources WHERE knowledge_id = ? ORDER BY added_at",
            (kid,),
        ).fetchall()
        relations = conn.execute(
            "SELECT 'from' AS dir, relation, to_id AS other, note"
            " FROM knowledge_relations WHERE from_id = ?"
            " UNION ALL"
            " SELECT 'to' AS dir, relation, from_id AS other, note"
            " FROM knowledge_relations WHERE to_id = ?",
            (kid, kid),
        ).fetchall()
    finally:
        conn.close()

    lines = [f"# {kid} [{rec['kind']}/{rec['confidence']}]"]
    lines.append(f"- topic: {rec['topic']}")
    lines.append(f"- current_state: {rec['current_state']}")
    if rec["rationale"]:
        lines.append(f"- rationale: {rec['rationale']}")
    if rec["alternatives_rejected"]:
        lines.append(f"- alternatives_rejected: {rec['alternatives_rejected']}")
    if rec["constraints_invariants"]:
        lines.append(f"- constraints_invariants: {rec['constraints_invariants']}")
    if rec["owners"]:
        lines.append(f"- owners: {rec['owners']}")
    if rec["tags"]:
        lines.append(f"- tags: {rec['tags']}")
    if rec["decided_at"]:
        lines.append(f"- decided_at: {rec['decided_at']}")
    if rec["last_validated_at"]:
        lines.append(f"- last_validated_at: {rec['last_validated_at']}")
    if rec["superseded_by"]:
        lines.append(f"- **superseded_by**: {rec['superseded_by']}")
    if rec["deleted"]:
        lines.append("- **deleted: 1（無効）**")

    if sources:
        lines.append("\n## sources")
        for s in sources:
            excerpt = ((s["excerpt"] or "")[:120].replace("\n", " "))
            lines.append(f"- [{s['weight']}] {s['source_type']}/{s['source_ref']}"
                         + (f" — {excerpt}" if excerpt else ""))
    if relations:
        lines.append("\n## relations")
        for rel in relations:
            arrow = "→" if rel["dir"] == "from" else "←"
            note = f" ({rel['note']})" if rel["note"] else ""
            lines.append(f"- {arrow} {rel['relation']}: {rel['other']}{note}")
    return "\n".join(lines)


def _tool_get_slack_messages(args: dict, ctx: AgentContext) -> str:
    from pm_argus import fetch_raw_messages
    channel_id = args.get("channel_id", "")
    ch_names = _load_channel_names()

    def _fmt_channels(ids: list[str]) -> str:
        return ", ".join(
            f"{cid}({ch_names[cid]})" if cid in ch_names else cid
            for cid in ids
        )

    # 表示名（または #表示名）で指定された場合は channel_id に解決を試みる
    if channel_id and not channel_id.startswith("C"):
        norm = channel_id.lstrip("#").strip()
        for cid, name in ch_names.items():
            if name == norm:
                channel_id = cid
                break

    if not channel_id:
        return f"channel_id が必要です。利用可能なチャンネル: {_fmt_channels(ctx.channels)}"
    if ctx.channels and channel_id not in ctx.channels:
        ch_label = f"{channel_id}({ch_names[channel_id]})" if channel_id in ch_names else channel_id
        return (
            f"チャンネル {ch_label} は現在のインデックスの対象外です。"
            f" 利用可能なチャンネル: {_fmt_channels(ctx.channels)}"
        )
    since = args.get("since", ctx.since)
    max_chars = int(args.get("max_chars", 10000))
    return fetch_raw_messages(
        channel_id, since, data_dir=ctx.data_dir, no_encrypt=ctx.no_encrypt,
        max_chars=max_chars,
    )


_CHANNEL_NAME_CACHE: dict[str, str] | None = None


def _load_channel_names() -> dict[str, str]:
    """argus_config.yaml の `channel_names:` セクションから channel_id→表示名を取得。
    pm_qa_server.load_qa_config() が先に呼ばれていればそのモジュール変数を再利用し、
    未ロードなら自前で yaml を読む。"""
    global _CHANNEL_NAME_CACHE
    if _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE
    # まず pm_qa_server に読み込み済みのものがあれば借用
    try:
        import pm_qa_server
        if pm_qa_server._channel_names:
            _CHANNEL_NAME_CACHE = dict(pm_qa_server._channel_names)
            return _CHANNEL_NAME_CACHE
    except Exception:
        pass
    # フォールバック: yaml を直接読む
    names: dict[str, str] = {}
    cfg_path = _ARGUS_CONFIG if _ARGUS_CONFIG.exists() else _QA_CONFIG_LEGACY
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            cn = cfg.get("channel_names") or {}
            if isinstance(cn, dict):
                names = {str(k): str(v) for k, v in cn.items()}
        except Exception as e:
            logger.warning("channel name load error: %s", e)
    _CHANNEL_NAME_CACHE = names
    return names


def _tool_search_mentions(args: dict, ctx: AgentContext) -> str:
    """指定ユーザーがメンション/名指しされた Slack メッセージを集計する。

    user_id（<@Uxxx>）と名前（text への部分一致）の両方で検索。
    since/until（YYYY-MM-DD）で期間絞り込み可。省略時は ctx.since 以降。
    """
    from db_utils import open_db

    user_id = (args.get("user_id") or "").strip()
    name = (args.get("name") or "").strip()
    since = args.get("since") or ctx.since
    until = args.get("until") or ctx.today
    limit = int(args.get("limit", 50))

    if not user_id and not name:
        return "user_id または name のどちらかを指定してください。"

    # LLM がカンマ区切りで複数候補を渡してくる場合（例: "西田, Takuhiro Nishida, 西田拓展"）がある。
    # 分割して user_id 解決に使う。name 本体は最初のトークン（多くは漢字の姓）を使う。
    name_candidates: list[str] = []
    if name:
        for tok in re.split(r"[、,，]", name):
            tok = tok.strip()
            if tok:
                name_candidates.append(tok)
        if name_candidates:
            name = name_candidates[0]

    # name が与えられて user_id が空なら、名前から user_id を解決して両方でOR検索する。
    # Slackメッセージ本文では user_id (Uxxxx) 文字列で呼びかけられることが多く、
    # 「西田さん」のような部分名では名簿由来の user_id でしか拾えないケースがある。
    resolved_uid = None
    resolved_display = None
    if name and not user_id:
        try:
            from argus.patrol.state import PatrolState
            from argus.patrol.users import UserResolver
            from slack_sdk import WebClient
            import os
            state = PatrolState(ctx.data_dir / "patrol_state.db")
            bot_token = os.environ.get("SLACK_BOT_TOKEN")
            slack = WebClient(token=bot_token) if bot_token else None
            resolver = UserResolver(state, slack, ctx.data_dir)
            # name_candidates を順に試す（カンマ区切り入力対策）
            for cand in name_candidates or [name]:
                resolved_uid = resolver.resolve(cand)
                if resolved_uid:
                    name = cand
                    break
            if resolved_uid:
                user_id = resolved_uid
                # 解決に使えた display_name を統合 Slack DB から引いて補助検索に使う
                from db_utils import open_db
                slack_db = ctx.data_dir / "slack.db"
                if slack_db.exists():
                    try:
                        c = open_db(slack_db, encrypt=not ctx.no_encrypt)
                        row = c.execute(
                            "SELECT user_name FROM messages WHERE user_id=?"
                            " AND user_name IS NOT NULL LIMIT 1",
                            (resolved_uid,),
                        ).fetchone()
                        c.close()
                        if row and row["user_name"]:
                            resolved_display = row["user_name"]
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[search_mentions] name→user_id 解決失敗: {e}")

    # 情報アクセス境界を argus_config.yaml のインデックス定義に合わせるため、
    # 現在のインデックスに紐づく channels に限定して検索する。
    channels = ctx.channels or []
    if not channels:
        return "検索対象チャンネルがありません。"

    where_parts = ["date(timestamp) >= ?", "date(timestamp) <= ?",
                   "text IS NOT NULL", "text != ''"]
    params: list = [since, until]
    like_parts = []
    if user_id:
        # Slack DB 内では text のメンションが <@Uxxx> 形式の場合と、
        # <@ と > が剥がされて素の Uxxx 文字列で保存される場合の両方がある。
        # どちらでもヒットさせるため user_id 自体で LIKE する。
        like_parts.append("text LIKE ?")
        params.append(f"%{user_id}%")
        # user_id が確定しているなら name マッチは曖昧になるため行わない
        # （「西田さん」と本文で言及されただけで、宛てではない投稿を拾ってしまう）
    elif name:
        like_parts.append("text LIKE ?")
        params.append(f"%{name}%")
    where_parts.append("(" + " OR ".join(like_parts) + ")")
    # 対象ユーザー本人の投稿は除外（宛て=受け取ったメッセージを集計するため）
    if user_id:
        where_parts.append("(user_id IS NULL OR user_id != ?)")
        params.append(user_id)
    where_clause = " AND ".join(where_parts)

    all_rows = []
    db_path = ctx.data_dir / "slack.db"
    if not db_path.exists():
        return f"（data/slack.db が見つかりません）"

    ph = ",".join("?" * len(channels))
    sql = (
        f"SELECT timestamp, user_name, text, permalink, channel_id, 0 AS is_reply "
        f"FROM messages WHERE channel_id IN ({ph}) AND {where_clause} "
        f"UNION ALL "
        f"SELECT timestamp, user_name, text, permalink, channel_id, 1 AS is_reply "
        f"FROM replies WHERE channel_id IN ({ph}) AND {where_clause} "
        f"ORDER BY timestamp DESC LIMIT ?"
    )
    try:
        conn = open_db(db_path, encrypt=not ctx.no_encrypt)
        rows = conn.execute(
            sql,
            channels + params + channels + params + [limit],
        ).fetchall()
        for r in rows:
            all_rows.append((r["timestamp"], r["user_name"], r["text"],
                             r["permalink"], r["is_reply"], r["channel_id"]))
        conn.close()
    except Exception as e:
        all_rows.append((None, None, f"（slack.db クエリエラー: {e}）", None, 0, ""))

    if not all_rows:
        q = f"user_id={user_id}" if user_id else f"name={name}"
        return f"該当メッセージなし ({q}, {since}〜{until})"

    # 新しい順に並べて上位 limit 件
    all_rows.sort(key=lambda x: x[0] or "", reverse=True)
    all_rows = all_rows[:limit]

    header_bits = [f"{len(all_rows)} 件", f"{since}〜{until}"]
    if resolved_uid:
        header_bits.append(f"name=\"{name}\" → user_id={resolved_uid}"
                           + (f" (display_name={resolved_display})" if resolved_display else ""))
    ch_names = _load_channel_names()
    lines = [f"# 検索結果: " + "、".join(header_bits)]
    lines.append("")
    lines.append(
        "（注: 以下は生メッセージ。要約や省略せず、そのままユーザーに提示すること。）"
    )
    for ts, user, text, permalink, is_reply, ch_id in all_rows:
        ts_short = (ts or "")[:16]
        ch_name = ch_names.get(ch_id, ch_id)
        tag = "返信" if is_reply else "投稿"
        body = text or ""
        link = f"\n  {permalink}" if permalink else ""
        lines.append("")
        lines.append(f"---")
        lines.append(f"[{ts_short}] #{ch_name} ({tag}) {user}:")
        lines.append(body)
        if link:
            lines.append(link.strip())
    return "\n".join(lines)


TOOLS: list[ToolDef] = [
    ToolDef(
        name="get_milestone_progress",
        description="マイルストーンの完了率・期限・残日数を一覧表示する",
        parameters={},
        fn=_tool_get_milestone_progress,
    ),
    ToolDef(
        name="get_overdue_items",
        description="期限超過のアクションアイテムを一覧表示する。担当者・マイルストーンでフィルタ可能",
        parameters={"assignee": "担当者名（部分一致）", "milestone": "マイルストーンID（例: M3）", "limit": "取得件数（デフォルト20）"},
        fn=_tool_get_overdue_items,
    ),
    ToolDef(
        name="get_assignee_workload",
        description="担当者別のオープンAI件数・期限超過件数を一覧表示する",
        parameters={},
        fn=_tool_get_assignee_workload,
    ),
    ToolDef(
        name="get_weekly_trends",
        description="週次のアクションアイテム作成数・完了数のトレンドを表示する",
        parameters={"weeks": "直近何週間分か（デフォルト4）"},
        fn=_tool_get_weekly_trends,
    ),
    ToolDef(
        name="get_unacknowledged_decisions",
        description="まだ確認されていない決定事項を一覧表示する",
        parameters={"since": "この日付以降（YYYY-MM-DD、省略時はデフォルト期間）"},
        fn=_tool_get_unacknowledged_decisions,
    ),
    ToolDef(
        name="search_action_items",
        description="アクションアイテムを条件検索する（担当者・状態・マイルストーン・キーワード）",
        parameters={"assignee": "担当者名", "status": "open または closed", "milestone": "マイルストーンID", "keyword": "内容のキーワード", "limit": "取得件数（デフォルト20）"},
        fn=_tool_search_action_items,
    ),
    ToolDef(
        name="search_decisions",
        description="決定事項をキーワードで検索する",
        parameters={"keyword": "検索キーワード", "limit": "取得件数（デフォルト20）"},
        fn=_tool_search_decisions,
    ),
    ToolDef(
        name="search_text",
        description="議事録・Slackメッセージを全文検索する（FTS5 + LLM re-ranking）",
        parameters={"query": "検索クエリ（自然言語可）"},
        fn=_tool_search_text,
    ),
    ToolDef(
        name="search_mentions",
        description=(
            "指定ユーザーがメンション（<@Uxxx>）または名指し（姓・氏名の文字列）"
            "された Slack メッセージを全チャンネルから集計する。"
            "「自分宛」「富岳太郎さん宛」系の質問はこのツールを使う。"
            " 推奨: user_id と name の両方を同時に指定する（OR検索）。"
            " 日本語/英語名での呼びかけ（例: 「Hikaru Inoue 5/19は...」）は"
            " user_id だけでは拾えないため name の指定が必須。"
        ),
        parameters={
            "user_id": "Slack user_id（例: U08ABC123）。メンション検索用",
            "name": "名前文字列（例: 西田、Takuhiro Nishida）。単一の名前のみ。複数候補のカンマ区切りは禁止。漢字の姓を推奨（内部で user_id に自動解決される）",
            "since": "この日付以降（YYYY-MM-DD、省略時はデフォルト期間）",
            "until": "この日付以前（YYYY-MM-DD、省略時は今日）",
            "limit": "取得件数（デフォルト50）",
        },
        fn=_tool_search_mentions,
    ),
    ToolDef(
        name="get_slack_messages",
        description="特定チャンネルの生Slackメッセージを取得する",
        parameters={"channel_id": "SlackチャンネルID（C で始まる文字列）", "since": "この日付以降（YYYY-MM-DD）", "max_chars": "最大文字数（デフォルト10000）"},
        fn=_tool_get_slack_messages,
    ),
    ToolDef(
        name="search_knowledge",
        description=(
            "蒸留ナレッジ knowledge.db を topic / current_state / tags / rationale で検索。"
            "プロジェクト全体共通の確定事項（意思決定 / 制約 / 立場 / 用語）から該当レコードを返す。"
            "current_state が短すぎて根拠が必要な場合は、続けて get_knowledge で展開する。"
        ),
        parameters={
            "query": "検索キーワード（自然言語可）。空文字なら全件",
            "limit": "取得件数（デフォルト10）",
            "include_superseded": "true なら superseded_by 立ちのレコードも対象（履歴閲覧用）",
        },
        fn=_tool_search_knowledge,
    ),
    ToolDef(
        name="get_knowledge",
        description=(
            "蒸留ナレッジレコードを id 指定でフル展開する。"
            "rationale / alternatives_rejected / constraints_invariants / sources / relations を含む。"
            "search_knowledge で見つけた KN-XXXX を深掘りする際に使う。"
        ),
        parameters={"id": "ナレッジ ID（例: KN-0042）"},
        fn=_tool_get_knowledge,
    ),
]

_TOOL_MAP: dict[str, ToolDef] = {t.name: t for t in TOOLS}


# =========================================================================== #
#  Tool Description Builder
# =========================================================================== #

def _build_tool_descriptions() -> str:
    lines = []
    for i, t in enumerate(TOOLS, 1):
        params_desc = "なし"
        if t.parameters:
            params_desc = ", ".join(f"`{k}`: {v}" for k, v in t.parameters.items())
        lines.append(f"{i}. **{t.name}** — {t.description}\n   引数: {params_desc}")
    return "\n".join(lines)


# =========================================================================== #
#  Protocol Parser
# =========================================================================== #

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FINAL_ANSWER_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)
# Kimi-K2-Thinking が稀に <answer>...</answer> タグでラップして返すケースの救済
# 中身が JSON なら tool_call として扱い、それ以外は final_answer として扱う
_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.DOTALL)
_JSON_TOOL_CALL_RE = re.compile(r"\{[^{}]*?\"name\"\s*:\s*\"[^\"]+\"[^{}]*?\"args\"\s*:\s*\{[^{}]*\}[^{}]*\}", re.DOTALL)


def parse_tool_calls(response: str) -> list[dict]:
    results = []
    for m in _TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            results.append({"error": f"JSONパースエラー: {m.group(1)[:100]}"})
    if results:
        return results
    # フォールバック: <answer>{json}</answer> 形式や、生 JSON の混入を検出
    for m in _ANSWER_TAG_RE.finditer(response):
        body = m.group(1).strip()
        for jm in _JSON_TOOL_CALL_RE.finditer(body):
            try:
                obj = json.loads(jm.group(0))
                name = obj.get("name", "")
                args = obj.get("args", {})
                if isinstance(args, dict) and isinstance(name, str) and name:
                    results.append({"name": name, "args": args})
            except json.JSONDecodeError:
                pass
    if results:
        return results
    # <answer> タグなしで生 JSON だけ返ってくるケース
    for jm in _JSON_TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(jm.group(0))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            pass
    return results


def parse_final_answer(response: str) -> str | None:
    m = _FINAL_ANSWER_RE.search(response)
    if m:
        return m.group(1).strip()
    # フォールバック: <answer> タグの中身が JSON でなければ最終回答とみなす
    a = _ANSWER_TAG_RE.search(response)
    if a:
        body = a.group(1).strip()
        # JSON tool_call っぽくない（"name":"..." を含まない）なら最終回答
        if "\"name\"" not in body or "\"args\"" not in body:
            return body
    return None


# =========================================================================== #
#  System Prompt
# =========================================================================== #

_AGENT_SYSTEM_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」の調査エージェントです。
ユーザーの質問に対し、ツールでデータを集めて根拠に基づき回答してください。

## ツール呼び出し形式

<tool_call>
{{"name": "ツール名", "args": {{"引数名": "値"}}}}
</tool_call>

1ステップで複数の <tool_call> を並列に並べてよい。タグ名は厳密に：
ツール呼び出しは `<tool_call>`、最終回答は `<final_answer>`（`<answer>` 等の代替タグは禁止）。

{tool_descriptions}

## 進め方

1. まず質問の主題で `search_text` を打って土台のチャンクを得る。
2. Step 1 結果に出てきた **数値・人名・日付・固有名詞** で深掘りする。同じ概念の言い換えだけで再検索しない（例: 「スケールアウトネットワーク 帯域幅」と「scale-out network bandwidth」は同義語であり情報量が増えない）。
3. 判断材料として **賛否・コスト・代替案・閾値・技術的影響** などの観点が抜けていれば追加検索する。reasoning で抜けに気付くこと。
4. 結果が薄い（同類クエリ 2 回でも合計 10 件未満）ときは諦めて `<final_answer>` でデータが乏しい旨を述べる。西暦の解釈違い・対象外チャンネル等の可能性に触れる。
5. 特定日付の議論は `search_text` での日付文字列検索ではなく `get_slack_messages`（チャンネル+期間）や `search_decisions`（since/until）を優先する。
6. **質問が意思決定 / 制約 / 用語に関する場合は `search_knowledge` を併用**する。蒸留済みの確定事項に該当があれば、回答中で `KN-XXXX` 形式で引用すること。`current_state` だけで根拠が薄ければ `get_knowledge` で `rationale` / `alternatives_rejected` まで展開して引用する。

## 早期終了の判断（最重要）

**ツール呼び出しは「不足が明確なときだけ」追加する。** 無駄なステップは回答品質を下げる:

- **既に質問に答えられる材料が揃っていれば、残りステップ数に関係なく即 `<final_answer>` を出す**。「念のためもう一回検索」は禁止。
- 直前のツール結果が **500 chars 未満 / ヒット 0 件** の場合、同種ツールでの追加検索は**1 回まで**。それでも薄ければ final_answer に進む。
- 3 ステップ目に入る前に必ず自問: 「次のツールが返す情報は、回答にどう使うか？」答えられないなら呼ばずに final_answer。
- 同じツール（例: `search_text`）を 3 回以上連続で呼ぶことは**禁止**。視点を変える（`search_knowledge` / `search_decisions` / `get_slack_messages` 等）か final_answer。

## スレッド追質問

「## スレッド内の過去のやり取り」が含まれる場合は会話の続きと解釈する。指示語は過去発言から解決し、ツールを呼ばずに答えられるなら直接答える。

## search_mentions の結果

`search_mentions` の結果は要約せず、全件・原文（タイムスタンプ・チャンネル名・投稿者・本文・URL）を最終回答にそのままコピーすること。

## 最終回答の形式

```
## 結論
（2〜3 行で質問への直接回答）

## 根拠
（数値・日付・人名・会議名を引用した事実、3〜6 項目）

## 影響と代替案
（技術的影響・閾値・代替オプション、1〜3 項目。該当なければ省略）

## 補足
（賛否対立・未解決事項・追加調査が必要な点。なければ省略）
```

1200 字以内。冗長なチェックリスト出力は不要。

## 制約

- 最大 {max_steps} 回までツールを使える。効率的に。
- アイテムID・担当者名・期限・マイルストーン名など具体的根拠を引用する。
- 推測ではなくツール結果に基づいて答える。
- 必ず `<final_answer>` タグで終わる。
"""


# =========================================================================== #
#  Seed Data (Lean Start)
# =========================================================================== #

def build_seed_data(ctx: AgentContext) -> str:
    all_stats = [fetch_summary_stats(c, since=ctx.since, today=ctx.today) for c in ctx.conns]
    stats: dict = {}
    for s in all_stats:
        for k, v in s.items():
            stats[k] = stats.get(k, 0) + v
    milestones = _query_all(ctx, fetch_milestone_progress)
    all_wl: list = []
    for c in ctx.conns:
        all_wl.extend(fetch_assignee_workload(c, ctx.today))
    wl_map: dict[str, dict] = {}
    for w in all_wl:
        name = w["assignee"]
        if name in wl_map:
            wl_map[name]["total_open"] += w["total_open"]
            wl_map[name]["overdue"] += w["overdue"]
            wl_map[name]["no_due_date"] += w.get("no_due_date", 0)
        else:
            wl_map[name] = {**w}
    workload = sorted(wl_map.values(), key=lambda x: (-x["overdue"], -x["total_open"]))

    parts = [
        "## プロジェクト概況\n",
        f"- オープンAI: {stats.get('total_open', 0)} 件",
        f"- クローズ済みAI: {stats.get('total_closed', 0)} 件",
        f"- 期限超過: {stats.get('overdue_count', 0)} 件",
        f"- 未確認決定事項: {stats.get('unacknowledged_decisions', 0)} 件",
        f"- 本日: {ctx.today}",
        "",
    ]

    if milestones:
        parts.append("## マイルストーン進捗\n")
        parts.append(format_milestone_table(milestones, ctx.today))
        parts.append("")

    if workload:
        parts.append("## 担当者別負荷\n")
        parts.append(format_assignee_table(workload))
        parts.append("")

    return "\n".join(parts)


# =========================================================================== #
#  Conversation Serializer
# =========================================================================== #

def _serialize_conversation(system: str, messages: list[dict]) -> str:
    parts = [f"[System]\n{system}"]
    for msg in messages:
        label = "[User]" if msg["role"] == "user" else "[Assistant]"
        parts.append(f"\n{label}\n{msg['content']}")
    return "\n".join(parts)


def _estimate_chars(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages)


def _compact_messages(messages: list[dict]) -> list[dict]:
    """古いツール結果を1行要約に圧縮し、直近2ターン分は維持する。"""
    if len(messages) <= 4:
        return messages
    compacted = []
    for msg in messages[:-4]:
        if msg["role"] == "user" and msg["content"].startswith("[Tool Result:"):
            first_line = msg["content"].split("\n", 1)[0]
            char_count = len(msg["content"])
            compacted.append({"role": "user", "content": f"{first_line} （{char_count}文字、圧縮済み）"})
        else:
            compacted.append(msg)
    compacted.extend(messages[-4:])
    return compacted


# =========================================================================== #
#  Progress Updater
# =========================================================================== #

def _make_progress_updater(respond: Callable | None, max_respond_calls: int = 2) -> Callable[[str], None]:
    steps: list[str] = []
    respond_count = 0

    def update(msg: str) -> None:
        nonlocal respond_count
        steps.append(msg)
        if respond is not None and respond_count < max_respond_calls:
            try:
                respond(
                    text=":mag: Argus 調査中...\n" + "\n".join(steps),
                    response_type="ephemeral",
                    replace_original=True,
                )
                respond_count += 1
            except Exception as e:
                logger.warning(f"進捗通知エラー: {e}")
        else:
            logger.info(f"[STEP] {msg}")

    return update


# =========================================================================== #
#  Agent Loop
# =========================================================================== #

def execute_tool(name: str, args: dict, ctx: AgentContext) -> str:
    tool = _TOOL_MAP.get(name)
    if tool is None:
        available = ", ".join(_TOOL_MAP.keys())
        return f"エラー: ツール「{name}」は存在しません。利用可能なツール: {available}"
    try:
        return tool.fn(args, ctx)
    except Exception as e:
        return f"エラー: {name} の実行に失敗しました — {e}"


_QUERY_REWRITE_PROMPT = """\
あなたは富岳NEXTプロジェクト（理研×富士通×NVIDIA、次世代スーパーコンピュータ開発）の AI アシスタントです。
ユーザーが Slack の `/argus-investigate` に投げた **短く曖昧な質問** を、社内ナレッジ検索エージェント向けに展開します。

質問:
{question}

このプロジェクトに登場する固有名詞の例（参考、すべて社内用語）:
- EEA (Early Evaluation Application): 評価対象アプリ群、EEA-1 / EEA-2 などのフェーズあり
- コデザイン: 富岳NEXT のハード・ソフト協調設計活動
- スケールアウトネットワーク: ノード間相互接続
- HBM, ノード構成, ベンチマーク, 性能予測, LQCD, GENESIS, NICAM, Petsy, PyTorch 等

以下を JSON で出力してください（コードブロック禁止、JSON のみ）:

{{
  "intent": "ユーザーが本当に知りたいこと（1〜2文、推測でよい）",
  "entities": ["質問に含まれる/想起される固有名詞や略語の正規形（最大6個）"],
  "search_queries": ["検索エンジンに投げる具体クエリ（2〜4個、日本語/英語混在可、固有名詞優先）"]
}}

注意:
- ユーザー語彙を一般用語に置き換えない（例: 「EEA」を「欧州経済領域」と展開してはいけない。これは社内略語）。
- タイプミス・省略形は元のまま entities に残し、正規形を**併記**する。
- 推測が不確実なら intent に「（推測）」を付ける。
"""


def _rewrite_query(question: str) -> dict | None:
    """ユーザー質問を意図 / 固有名詞 / 検索クエリに展開する。失敗時は None。"""
    prompt = _QUERY_REWRITE_PROMPT.format(question=question.strip())
    try:
        t0 = time.time()
        response = call_argus_llm(prompt, max_tokens=512, timeout=30, think=False)
        elapsed = time.time() - t0
        logger.info(f"[rewrite] LLM応答 {len(response)} chars, {elapsed:.1f}s")
        # JSON 抽出（前後の余計な文字を許容）
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if not m:
            logger.warning(f"[rewrite] JSON 抽出失敗: {response[:200]}")
            return None
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return None
        intent = str(data.get("intent", "")).strip()
        entities = [str(x) for x in data.get("entities", []) if x][:6]
        queries = [str(x) for x in data.get("search_queries", []) if x][:4]
        if not intent and not entities and not queries:
            return None
        return {"intent": intent, "entities": entities, "search_queries": queries}
    except Exception as e:
        logger.warning(f"[rewrite] 失敗: {e}")
        return None


def _format_rewrite_for_seed(rewrite: dict) -> str:
    """seed_data 冒頭に注入する形式に整形。"""
    parts = ["## 質問の解釈（自動展開）\n"]
    if rewrite.get("intent"):
        parts.append(f"- **意図**: {rewrite['intent']}")
    if rewrite.get("entities"):
        parts.append(f"- **関連語**: {', '.join(rewrite['entities'])}")
    if rewrite.get("search_queries"):
        parts.append(f"- **推奨検索クエリ**: {', '.join(rewrite['search_queries'])}")
    parts.append("")
    parts.append("（上記は LLM による自動展開。原質問の語を優先しつつ、固有名詞や言い換えで補完して検索すること）")
    parts.append("")
    return "\n".join(parts)


def run_agent(
    question: str,
    seed_data: str,
    respond: Callable | None,
    ctx: AgentContext,
    *,
    max_steps: int = _DEFAULT_MAX_STEPS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    tool_desc = _build_tool_descriptions()
    system_prompt = _AGENT_SYSTEM_PROMPT.format(
        tool_descriptions=tool_desc,
        max_steps=max_steps,
    )

    progress = _make_progress_updater(None)

    # 質問リライト（意図解釈 → 関連語 → 推奨クエリ）
    progress("質問の意図を解釈中...")
    rewrite = _rewrite_query(question)
    rewrite_block = ""
    if rewrite:
        rewrite_block = _format_rewrite_for_seed(rewrite)
        logger.info(
            f"[rewrite] intent={rewrite.get('intent', '')[:80]!r}"
            f" entities={rewrite.get('entities')}"
            f" queries={rewrite.get('search_queries')}"
        )

    messages: list[dict] = [
        {"role": "user", "content": f"## 調査依頼\n\n{question}\n\n{rewrite_block}{seed_data}"},
    ]

    intent_header = ""
    if rewrite and rewrite.get("intent"):
        intent_header = f"> **ご質問の解釈**: {rewrite['intent']}\n\n"

    def _finalize(answer: str) -> str:
        return intent_header + _append_sources_section(answer, ctx)

    progress(f"シードデータ収集完了。調査開始（最大{max_steps}ステップ）")

    call_history: list[str] = []
    parse_error_count = 0
    start_time = time.monotonic()

    for step in range(1, max_steps + 1):
        elapsed = time.monotonic() - start_time
        if elapsed > timeout:
            logger.warning(f"タイムアウト ({timeout}s) に到達。ステップ {step} で中断")
            progress(f"タイムアウト（{int(elapsed)}秒経過）。現時点の分析結果を返します")
            break

        if _estimate_chars(messages) > _CONTEXT_CHAR_LIMIT:
            messages = _compact_messages(messages)
            logger.info(f"コンテキスト圧縮: {_estimate_chars(messages)} chars")

        prompt = _serialize_conversation(system_prompt, messages)
        logger.info(f"[investigate] Step {step}/{max_steps}: LLM呼び出し ({len(prompt)} chars)")

        llm_t0 = time.time()
        try:
            response = call_argus_llm(
                prompt,
                max_tokens=32768,
                timeout=max(30, int(timeout - elapsed)),
                think=True,
            )
        except Exception as e:
            logger.exception(f"[investigate] LLM呼び出しエラー: {e}")
            progress(f"LLMエラー: {e}")
            break
        llm_elapsed = time.time() - llm_t0
        logger.info(
            f"[investigate] Step {step}/{max_steps}: LLM応答 "
            f"{len(response)} chars, {llm_elapsed:.1f}s"
        )
        logger.info(
            f"[investigate] Step {step}/{max_steps} 生成内容:\n"
            f"----8<---- raw response ----8<----\n{response}\n----8<---- end ----8<----"
        )

        final = parse_final_answer(response)
        if final:
            logger.info(f"[investigate] <final_answer> 検出 (Step {step})")
            return _finalize(final)

        tool_calls = parse_tool_calls(response)

        if not tool_calls:
            parse_error_count += 1
            if parse_error_count >= 2:
                logger.warning("[investigate] 2回連続でツール呼び出し/最終回答なし。生テキストを返却")
                clean = re.sub(r"<[^>]+>", "", response).strip()
                return _finalize(clean if clean else response)
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": (
                "[System] ツール呼び出しか最終回答が検出できませんでした。\n"
                "ツールを使う場合は <tool_call>{...}</tool_call> 形式で、\n"
                "回答が完了したら <final_answer>...</final_answer> 形式で出力してください。"
            )})
            continue

        parse_error_count = 0
        messages.append({"role": "assistant", "content": response})

        result_parts = []
        for tc in tool_calls:
            if "error" in tc:
                result_parts.append(f"[Tool Error]\n{tc['error']}")
                continue

            call_key = json.dumps(tc, sort_keys=True, ensure_ascii=False)
            if call_key in call_history:
                result_parts.append(
                    f"[Tool Result: {tc['name']}]\n"
                    f"（同一引数での再呼び出し。前回と同じ結果です。別の引数を試すか <final_answer> で回答してください）"
                )
                continue
            # 同一ツール名を3回以上呼んでいれば打ち切り（引数違いでも）
            same_name_count = sum(1 for k in call_history if f'"name": "{tc["name"]}"' in k)
            if same_name_count >= 2:
                result_parts.append(
                    f"[Tool Result: {tc['name']}]\n"
                    f"（同一ツール {tc['name']} が既に{same_name_count}回呼ばれています。"
                    f"**別のツール**を試すか、これまでの結果で <final_answer> を出力してください。"
                    f"特に `search_text` はまだ試していなければ最優先で使うこと。）"
                )
                call_history.append(call_key)
                continue
            call_history.append(call_key)

            tool_name = tc["name"]
            tool_args = tc["args"]
            args_desc = ", ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
            progress(f"Step {step}/{max_steps}: {tool_name}({args_desc})")

            tool_t0 = time.time()
            result = execute_tool(tool_name, tool_args, ctx)
            tool_elapsed = time.time() - tool_t0
            logger.info(
                f"[investigate] Step {step}/{max_steps} tool={tool_name}"
                f" args={tool_args} result_len={len(str(result))} chars, {tool_elapsed:.1f}s"
            )
            result_parts.append(f"[Tool Result: {tool_name}]\n{result}")

        messages.append({"role": "user", "content": "\n\n".join(result_parts)})

    # ステップ上限到達: ここまでのツール結果で最終回答を強制合成する
    progress("ステップ上限到達。これまでの結果で最終回答を合成します")
    messages.append({"role": "user", "content": (
        "[System] 調査ステップの上限に到達しました。これ以上ツールは呼べません。\n"
        "**ここまでの全ツール結果を根拠**に、必ず `<final_answer>...</final_answer>` で回答を出力してください。\n"
        "情報が乏しい場合でも、得られた事実と不足している点を率直にまとめること。"
    )})
    if _estimate_chars(messages) > _CONTEXT_CHAR_LIMIT:
        messages = _compact_messages(messages)
    final_prompt = _serialize_conversation(system_prompt, messages)
    logger.info(f"[investigate] 強制合成: LLM呼び出し ({len(final_prompt)} chars)")
    try:
        elapsed = time.monotonic() - start_time
        synth_response = call_argus_llm(
            final_prompt,
            max_tokens=32768,
            timeout=max(30, int(timeout - elapsed)) if elapsed < timeout else 60,
            think=True,
        )
        logger.info(
            f"[investigate] 強制合成 応答 {len(synth_response)} chars\n"
            f"----8<---- raw response ----8<----\n{synth_response}\n----8<---- end ----8<----"
        )
        final = parse_final_answer(synth_response)
        if final:
            return _finalize(final)
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", synth_response, flags=re.DOTALL)
        clean = re.sub(r"<[^>]+>", "", clean).strip()
        if clean:
            return _finalize(clean)
    except Exception as e:
        logger.exception(f"[investigate] 強制合成エラー: {e}")

    # 強制合成も失敗した場合のフォールバック: 最後のアシスタント応答を使う
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            clean = re.sub(r"<tool_call>.*?</tool_call>", "", msg["content"], flags=re.DOTALL).strip()
            if clean:
                return _finalize(clean)
    return "調査が完了しませんでした。より具体的な質問で再度お試しください。"


_SLACK_REF_CACHE: dict[str, list[str]] = {}


def _fetch_slack_references_for_box(box_file_id: str, limit: int = 2) -> list[str]:
    """box_file_id に紐づく Slack 共有パーマリンクを最大 limit 件返す（新しい順）。"""
    if not box_file_id:
        return []
    if box_file_id in _SLACK_REF_CACHE:
        return _SLACK_REF_CACHE[box_file_id]
    try:
        from db_utils import open_db
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent.parent / "data" / "box_docs.db"
        if not path.exists():
            _SLACK_REF_CACHE[box_file_id] = []
            return []
        conn = open_db(path, encrypt=True)
        rows = conn.execute(
            "SELECT slack_permalink, shared_at, shared_by FROM slack_references"
            " WHERE box_file_id=? AND slack_permalink IS NOT NULL"
            " ORDER BY shared_at DESC LIMIT ?",
            (box_file_id, limit),
        ).fetchall()
        conn.close()
    except Exception:
        _SLACK_REF_CACHE[box_file_id] = []
        return []
    out = []
    for r in rows:
        link = r["slack_permalink"]
        date = (r["shared_at"] or "")[:10]
        by = r["shared_by"] or ""
        label_bits = " ".join(b for b in [date, by] if b)
        out.append(f"<{link}|{label_bits or 'Slack'}>")
    _SLACK_REF_CACHE[box_file_id] = out
    return out


_KN_ID_RE = re.compile(r"\bKN-\d{4}\b")


def _append_sources_section(answer: str, ctx: AgentContext) -> str:
    """ctx.cited_chunks にあるチャンクから「## 出典」セクションを生成して回答末尾に付与する。
    また、ctx.cited_knowledge_ids または回答本文中の KN-XXXX を検出した場合は、
    「## 引用したナレッジ」セクションと修正導線を付与する。
    """
    from pm_qa_server import _format_source_label

    # 回答本文に出現した KN-XXXX を cited_knowledge_ids にマージする
    for m in _KN_ID_RE.findall(answer or ""):
        ctx.cited_knowledge_ids.add(m)

    extra_sections: list[str] = []

    # ナレッジ引用の追記
    if ctx.cited_knowledge_ids:
        kn_lines = ["", "## 引用したナレッジ"]
        # 詳細を knowledge.db から引いてくる（topic 表示用）
        topics: dict[str, str] = {}
        from db_utils import open_knowledge_db
        db_path = ctx.data_dir / "knowledge.db"
        if db_path.exists():
            try:
                conn = open_knowledge_db(db_path, no_encrypt=ctx.no_encrypt)
                placeholders = ",".join("?" * len(ctx.cited_knowledge_ids))
                rows = conn.execute(
                    f"SELECT id, topic, last_validated_at, deleted, superseded_by"
                    f" FROM knowledge WHERE id IN ({placeholders})",
                    list(ctx.cited_knowledge_ids),
                ).fetchall()
                conn.close()
                for r in rows:
                    label = r["topic"] or ""
                    suffix = ""
                    if r["deleted"]:
                        suffix = " ⚠️ 無効"
                    elif r["superseded_by"]:
                        suffix = f" ⚠️ {r['superseded_by']} に上書き済み"
                    elif r["last_validated_at"]:
                        suffix = f" (validated: {r['last_validated_at']})"
                    topics[r["id"]] = f"{label}{suffix}"
            except Exception:
                pass
        for kid in sorted(ctx.cited_knowledge_ids):
            t = topics.get(kid, "（詳細取得失敗）")
            kn_lines.append(f"- **{kid}** {t}")
        kn_lines.append("")
        kn_lines.append(
            "_修正が必要な場合: `/argus-knowledge invalidate KN-XXXX`"
            " または `/argus-knowledge supersede KN-XXXX KN-YYYY` で更新できます。_"
        )
        extra_sections.append("\n".join(kn_lines))

    # 出典セクション
    if ctx.cited_chunks:
        seen: set[tuple] = set()
        items: list[tuple[int, str]] = []
        for i, chunk in enumerate(ctx.cited_chunks, 1):
            key = (chunk.get("source_type"), chunk.get("source_ref"), chunk.get("held_at"))
            if key in seen:
                continue
            seen.add(key)
            label = _format_source_label(chunk)
            ref = chunk.get("source_ref") or ""
            source_type = chunk.get("source_type", "")
            if source_type == "slack_raw" and ref:
                link = f"<{ref}|スレッドを開く>"
            elif source_type == "web" and ref:
                link = f"<{ref}|リンク>"
            elif source_type == "box_document" and ref:
                link = f"<{ref}|Boxで開く>"
                slack_links = _fetch_slack_references_for_box(chunk.get("record_id") or "")
                if slack_links:
                    link = link + " / Slack共有: " + ", ".join(slack_links)
            elif source_type == "minutes_content" and ref:
                held_at = chunk.get("held_at") or ""
                link = f"{held_at} {ref}".strip()
            else:
                link = ref
            items.append((i, f"- [{i}] {label}" + (f" — {link}" if link else "")))
        if items:
            extra_sections.append("\n".join(["", "## 出典"] + [s for _, s in items]))

    if not extra_sections:
        return answer
    return answer.rstrip() + "\n" + "\n\n".join(extra_sections)


# =========================================================================== #
#  Slack Handler
# =========================================================================== #

_ID_REF_RE = re.compile(
    r"(?P<full>"
    r"(?P<kind>a|d|AI|決定|ID)"      # 種別
    r"\s*[:： ]\s*"
    r"(?P<id>\d{1,6})"
    r")"
)


def _expand_id_references(text: str, conns: list) -> str:
    """出力中の `a:670` / `AI:670` / `決定:42` / `ID:670` 等を content[:60] で展開する。

    参照先が action_items なら `a:670 "xxxxx..."`、decisions なら `d:42 "xxxxx..."`。
    pm.db に見つからないIDは元のまま残す。
    """
    cache: dict[tuple[str, int], str | None] = {}

    def _lookup(table: str, item_id: int) -> str | None:
        key = (table, item_id)
        if key in cache:
            return cache[key]
        snippet: str | None = None
        for conn in conns:
            try:
                row = conn.execute(
                    f"SELECT content FROM {table} WHERE id = ? AND COALESCE(deleted,0)=0",
                    (item_id,),
                ).fetchone()
            except Exception:
                continue
            if row and row["content"]:
                s = row["content"].replace("\n", " ").strip()
                snippet = s[:60] + ("…" if len(s) > 60 else "")
                break
        cache[key] = snippet
        return snippet

    def _replace(m: re.Match) -> str:
        kind = m.group("kind")
        item_id = int(m.group("id"))
        # 種別から対象テーブルを推定。a/AI → action_items、d/決定 → decisions、
        # ID は両方試す（action_items 優先）
        if kind in ("a", "AI"):
            tables = ["action_items"]
            norm = f"a:{item_id}"
        elif kind in ("d", "決定"):
            tables = ["decisions"]
            norm = f"d:{item_id}"
        else:  # ID
            tables = ["action_items", "decisions"]
            norm = f"ID:{item_id}"

        for t in tables:
            snippet = _lookup(t, item_id)
            if snippet:
                prefix = "a" if t == "action_items" else "d"
                if kind == "ID":
                    return f"{prefix}:{item_id} “{snippet}”"
                return f"{norm} “{snippet}”"
        return m.group("full")

    return _ID_REF_RE.sub(_replace, text)


def _run_investigate(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-investigate のバックグラウンド処理"""
    try:
        from pm_argus import _parse_command_args
        cmd_text = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()

        index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels(channel_id)
        conns = [open_pm_db(p, no_encrypt=no_encrypt) for p in pm_db_paths]

        ctx = AgentContext(
            conns=conns,
            today=today,
            since=since_date,
            no_encrypt=no_encrypt,
            data_dir=_DATA_DIR,
            minutes_dir=_MINUTES_DIR,
            index_db=index_db,
            index_name=index_name,
            channels=channels,
        )

        seed_data = build_seed_data(ctx)
        result = run_agent(
            question=cmd_text,
            seed_data=seed_data,
            respond=respond,
            ctx=ctx,
        )

        # ID 参照 (a:670 / d:42 / AI:670 / 決定:42 / ID:670) を content[:60] で展開
        result = _expand_id_references(result, conns)

        for c in conns:
            c.close()

        # Slack ephemeral は約 3000 文字が実用上限
        _SLACK_MAX_CHARS = 2900
        header = f"*Argus 調査結果* ({today})\n\n"
        if len(header) + len(result) > _SLACK_MAX_CHARS:
            result = result[:_SLACK_MAX_CHARS - len(header) - 20] + "\n\n（...以下省略）"

        # GitHub Flavored Markdown を Slack mrkdwn に変換（他 Argus コマンドと揃える）
        from pm_argus import _to_slack_mrkdwn
        body = _to_slack_mrkdwn(header + result)

        try:
            respond(
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": body},
                    }
                ],
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception as e:
            logger.error(f"[investigate] 最終結果の Slack 送信エラー: {e}")
            logger.info(f"[investigate] 結果テキスト:\n{result[:500]}")
        logger.info("[investigate] 完了")

    except Exception as e:
        logger.exception("[investigate] エラー")
        try:
            respond(
                text=f":warning: Argus 調査エラー: {e}",
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception:
            pass


# =========================================================================== #
#  CLI Mode
# =========================================================================== #

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Argus Investigation Agent")
    parser.add_argument("--investigate", required=True, help="調査内容")
    parser.add_argument("--max-steps", type=int, default=_DEFAULT_MAX_STEPS, help="最大ステップ数")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT, help="タイムアウト（秒）")
    parser.add_argument("--days", type=int, default=_DEFAULT_SINCE_DAYS, help="直近何日分を対象にするか")
    parser.add_argument("--db", default=str(_PM_DB), help="pm.db のパス")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--dry-run", action="store_true", help="LLM呼び出しなし（シードデータ確認用）")
    args = parser.parse_args()

    today = date.today().isoformat()
    since_date = (date.today() - timedelta(days=args.days)).isoformat()

    index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels()
    if args.db != str(_PM_DB):
        pm_db_paths = [Path(args.db)]
    conns = [open_pm_db(p, no_encrypt=args.no_encrypt) for p in pm_db_paths]

    ctx = AgentContext(
        conns=conns,
        today=today,
        since=since_date,
        no_encrypt=args.no_encrypt,
        data_dir=_DATA_DIR,
        minutes_dir=_MINUTES_DIR,
        index_db=index_db,
        index_name=index_name,
        channels=channels,
    )

    seed_data = build_seed_data(ctx)

    if args.dry_run:
        print("=== シードデータ ===")
        print(seed_data)
        print(f"\n=== 調査質問 ===\n{args.investigate}")
        print(f"\n=== ツール一覧 ===\n{_build_tool_descriptions()}")
        for c in conns:
            c.close()
        return

    result = run_agent(
        question=args.investigate,
        seed_data=seed_data,
        respond=None,
        ctx=ctx,
        max_steps=args.max_steps,
        timeout=args.timeout,
    )

    for c in conns:
        c.close()

    print("\n=== Argus 調査結果 ===\n")
    print(result)


if __name__ == "__main__":
    main()
