"""agent_tools.py — Argus Agent ツール定義・実装レジストリ

pm_argus_agent.py から分離。ツール追加・変更はこのファイルのみ編集すればよい。
AgentContext / ToolDef / TOOLS / _TOOL_MAP / _build_tool_descriptions を提供する。
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import (
    fetch_milestone_progress,
    fetch_assignee_workload,
    fetch_overdue_items,
    fetch_weekly_trends,
    fetch_unacknowledged_decisions,
)
from format_utils import (
    format_milestone_table,
    format_overdue_list,
    format_assignee_table,
    format_weekly_trends as format_trends_table,
    format_decisions_list,
)

logger = logging.getLogger("pm_argus_agent")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"

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


