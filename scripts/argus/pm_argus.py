#!/usr/bin/env python3
"""
pm_argus.py — Argus AI Project Intelligence System

PM分析コア: データ収集・プロンプト構築・Slackハンドラ + --brief-to-canvas CLI モード。

Slack (/argus-brief, /argus-draft, /argus-risk, /argus-today, /argus-transcribe) コマンドの
バックグラウンド処理と、cron による毎朝の自動ブリーフィング生成 (--brief-to-canvas) を担う。

TTS/動画生成は argus.narrate に委譲する（依存方向: pm_argus → narrate）。

Usage:
    # ブリーフィング生成 → Canvas 投稿
    python3 scripts/pm_argus.py --brief-to-canvas --canvas-id <CANVAS_ID>

    # ブリーフィング生成 → 標準出力のみ（--dry-run）
    python3 scripts/pm_argus.py --brief-to-canvas --dry-run

    # リスク分析のみ
    python3 scripts/pm_argus.py --risk --dry-run

環境変数:
    RIVAULT_URL   — RiVault エンドポイント URL
    RIVAULT_TOKEN — RiVault API トークン
    SLACK_BOT_TOKEN — Canvas 投稿時に必要（slack_sdk 用）
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("pm_argus")

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

import yaml
from cli_utils import (
    call_argus_llm,
    load_claude_md_context,
    resolve_brief_canvas_id,
    resolve_risk_canvas_id,
)
from db_utils import (
    fetch_assignee_workload,
    fetch_milestone_progress,
    fetch_no_assignee_count,
    fetch_overdue_items,
    fetch_summary_stats,
    fetch_unacknowledged_decisions,
    fetch_unlinked_items_count,
    fetch_weekly_trends,
    open_db,
    open_pm_db,
)
from format_utils import (
    format_assignee_table,
    format_decisions_list,
    format_milestone_table,
    format_overdue_list,
)
from format_utils import (
    format_weekly_trends as format_trends_table,
)
from utils.slack_post import _split_mrkdwn_to_blocks, _to_slack_mrkdwn

from argus.prompts import (
    _DAILY_SUMMARY_PROMPT,
    _DRAFT_AGENDA_PROMPT,
    _DRAFT_REPORT_PROMPT,
    _DRAFT_REQUEST_PROMPT,
)
from argus.qa_engine import _query_action_items, _query_decisions

# --------------------------------------------------------------------------- #
# 設定・定数
# --------------------------------------------------------------------------- #
_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_ARGUS_CONFIG_FILE = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_FILE_LEGACY = _DATA_DIR / "qa_config.yaml"

_DEFAULT_SINCE_DAYS = 30
_DRAFT_REPORT_SINCE_DAYS = 14
_WORKER_MAX_CHARS = 8000  # Worker に渡す各セクションの最大文字数（ARGUS_DISABLE_FULLCTX 時のみ使用）
_KNOWLEDGE_MAX_ITEMS_DEFAULT = 30
_KNOWLEDGE_MAX_CHARS = 4000
_MAX_CHARS_PER_CHANNEL = 20000   # 1チャンネルあたりの最大文字数（最新を優先）

# 全文脈方式（検索なし・期間内全データ投入）の予算。ARGUS_FULLCTX_CHAR_BUDGET で上書き可。
# 350,000字 = glm-5.2 入力上限 200k tok を実測 1.96字/tok で換算した上での安全マージン込み既定値。
_FULLCTX_CHAR_BUDGET_DEFAULT = 350_000
_FULLCTX_BOX_CHAR_CAP = 100_000
# est_tokens 換算係数。glm-5.2 実測 1.96字/tok (2026-07-23)。
_FULLCTX_CHARS_PER_TOKEN = 1.96
# brief/risk の出力上限。出力2千字要件 + 退化暴走幅の抑制（2026-07-23、旧32768から縮小）。
# today/draft の fullctx LLM 呼び出しにも同じ値を明示する（2026-07-26 レビュー指摘反映）。
_BRIEF_RISK_MAX_TOKENS = 8192

# /argus-transcribe ジョブ排他制御
_transcribe_jobs: dict[str, tuple[str, str]] = {}  # thread_ts → (filename, channel_id)
_transcribe_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# 設定ローダー
# --------------------------------------------------------------------------- #

def _load_argus_config() -> dict:
    """argus_config.yaml をパースして返す（旧 qa_config.yaml にフォールバック）。"""
    for p in (_ARGUS_CONFIG_FILE, _QA_CONFIG_FILE_LEGACY):
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}


def _load_channel_ids(index_name: str | None = None) -> list[str]:
    """argus_config.yaml からチャンネルIDリストを読み込む。"""
    cfg = _load_argus_config()
    indices = cfg.get("indices") or {}
    target = index_name or cfg.get("default_index", "pm")
    return indices.get(target, {}).get("channels", [])


def _load_minutes_names(index_name: str | None = None) -> list[str]:
    """argus_config.yaml から議事録 kind 名リストを読み込む。"""
    cfg = _load_argus_config()
    indices = cfg.get("indices") or {}
    target = index_name or cfg.get("default_index", "pm")
    return indices.get(target, {}).get("minutes", [])


def load_pm_db_paths(index_name: str | None = None) -> list[Path]:
    """argus_config.yaml の pm_db パスリストを読み込む。"""
    cfg = _load_argus_config()
    indices = cfg.get("indices") or {}
    target = index_name or cfg.get("default_index", "pm")
    pm_db_list = indices.get(target, {}).get("pm_db", ["data/pm.db"])
    return [_REPO_ROOT / p for p in pm_db_list]


def resolve_index_name(channel_id: str | None) -> str:
    """コマンド実行チャンネルから index_name を解決する。
    channel_map にエントリがなければ default_index を返す。
    pm_argus_agent.py:_resolve_index_and_channels と同じ考え方。
    """
    cfg = _load_argus_config()
    default_index = cfg.get("default_index", "pm")
    channel_map = cfg.get("channel_map") or {}
    if not channel_id:
        return default_index
    return channel_map.get(channel_id, default_index)


# --------------------------------------------------------------------------- #
# データ収集
# --------------------------------------------------------------------------- #

def fetch_raw_messages(
    channel_id: str,
    since_date: str,
    *,
    data_dir: Path,
    no_encrypt: bool = False,
    max_chars: int = _MAX_CHARS_PER_CHANNEL,
) -> str:
    """
    Slack 統合 DB (data/slack.db) から指定チャンネルの messages + replies を取得し、
    "[YYYY-MM-DD HH:MM] user_name: text" 形式で整形して返す。
    max_chars を超える場合は最古のメッセージから切り捨てる（最新を優先）。
    """
    db_path = data_dir / "slack.db"
    if not db_path.exists():
        return "（data/slack.db が見つかりません）"

    try:
        conn = open_db(db_path, encrypt=not no_encrypt)
    except Exception as e:
        return f"（{db_path.name} の接続に失敗: {e}）"

    lines = []
    try:
        rows = conn.execute(
            """SELECT timestamp, user_name, text, 0 AS is_reply
                 FROM messages
                 WHERE channel_id = ? AND date(timestamp) >= ? AND text IS NOT NULL AND text != ''
                 UNION ALL
                 SELECT timestamp, user_name, text, 1 AS is_reply
                 FROM replies
                 WHERE channel_id = ? AND date(timestamp) >= ? AND text IS NOT NULL AND text != ''
                 ORDER BY timestamp ASC""",
            (channel_id, since_date, channel_id, since_date),
        ).fetchall()

        formatted = []
        for r in rows:
            ts = (r["timestamp"] or "")[:16]  # "YYYY-MM-DD HH:MM"
            user = r["user_name"] or "unknown"
            text = (r["text"] or "").replace("\n", " ")
            indent = "  " if r["is_reply"] else ""
            formatted.append(f"[{ts}] {indent}{user}: {text}")

        # max_chars を超える場合は末尾（最新）を優先して古いものを切り捨てる
        result = "\n".join(formatted)
        if len(result) > max_chars:
            # 末尾 max_chars 文字を使い、最初の不完全な行は除く
            truncated = result[-max_chars:]
            first_newline = truncated.find("\n")
            if first_newline > 0:
                truncated = truncated[first_newline + 1:]
            total = len(formatted)
            kept = len(truncated.splitlines())
            lines.append(f"（古い {total - kept} 件は省略）")
            lines.append(truncated)
        else:
            lines.append(result)

    except Exception as e:
        lines.append(f"（クエリエラー: {e}）")
    finally:
        conn.close()

    return "\n".join(lines)


def fetch_recent_minutes(
    since_date: str,
    *,
    minutes_dir: Path,
    no_encrypt: bool = False,
    minutes_names: list[str] | None = None,
) -> str:
    """
    data/minutes/{kind}.db の instances + minutes_content テーブルから
    held_at >= since_date の議事録本文を取得して返す。

    minutes_names: 指定された kind（DB ファイルの stem）のみを対象にする。
                   None または空リストの場合は全 kind を対象にする（後方互換）。
    """
    if not minutes_dir.exists():
        return "（議事録ディレクトリが見つかりません）"

    db_files = sorted(minutes_dir.glob("*.db"))
    if minutes_names:
        wanted = set(minutes_names)
        db_files = [p for p in db_files if p.stem in wanted]
    if not db_files:
        return "（議事録DBが見つかりません）"

    sections = []
    for db_file in db_files:
        kind = db_file.stem
        try:
            conn = open_db(db_file, encrypt=not no_encrypt)
        except Exception as e:
            sections.append(f"### {kind}\n（接続に失敗: {e}）")
            continue

        try:
            rows = conn.execute(
                """SELECT i.meeting_id, i.held_at, mc.content
                   FROM instances i
                   JOIN minutes_content mc ON mc.meeting_id = i.meeting_id
                   WHERE i.held_at >= ?
                   ORDER BY i.held_at DESC""",
                (since_date,),
            ).fetchall()
            for r in rows:
                sections.append(
                    f"### {kind} ({r['held_at']})\n\n{r['content']}"
                )
        except Exception as e:
            sections.append(f"### {kind}\n（クエリエラー: {e}）")
        finally:
            conn.close()

    return "\n\n---\n\n".join(sections) if sections else "（対象期間の議事録なし）"


def fetch_background_knowledge(
    *,
    pm_db_paths: list[Path],
    no_encrypt: bool = False,
    max_items: int = _KNOWLEDGE_MAX_ITEMS_DEFAULT,
    max_chars: int = _KNOWLEDGE_MAX_CHARS,
) -> str:
    """brief/risk プロンプト同梱用『背景知識』を pm.db.decisions から構築する。

    旧 fetch_knowledge_summary (knowledge.db 由来) の置き換え。
    pm.db.decisions のうち rationale が入っている現役エントリを
    決定日降順で取り出し、Markdown 箇条書きで返す。

    BOX 由来の制約・方針は investigate の search_text で取得する想定。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for db_path in pm_db_paths:
        try:
            conn = open_pm_db(db_path, no_encrypt=no_encrypt)
        except Exception as e:
            logger.warning(f"pm.db 接続失敗 ({db_path}): {e}")
            continue
        try:
            rows = conn.execute(
                """SELECT id, content, rationale, decided_at, decided_by
                     FROM decisions
                    WHERE COALESCE(deleted, 0) = 0
                      AND rationale IS NOT NULL
                      AND TRIM(rationale) != ''
                    ORDER BY COALESCE(decided_at, '') DESC, id DESC
                    LIMIT ?""",
                (max_items,),
            ).fetchall()
        except Exception as e:
            logger.warning(f"decisions クエリ失敗 ({db_path}): {e}")
            rows = []
        finally:
            conn.close()
        for r in rows:
            key = f"D-{r['id']}"
            if key in seen:
                continue
            seen.add(key)
            content = (r["content"] or "").strip()
            rationale = (r["rationale"] or "").strip()
            decided_at = r["decided_at"] or ""
            decided_by = r["decided_by"] or ""
            who = f" by {decided_by}" if decided_by else ""
            line = f"- **[{key}]** {content} — 根拠: {rationale}（決定: {decided_at}{who}）"
            lines.append(line)

    body = "\n".join(lines[:max_items])
    if len(body) > max_chars:
        truncated = body[:max_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        omitted = max(0, len(lines) - len(truncated.splitlines()))
        if omitted > 0:
            body = truncated + f"\n_…他 {omitted} 件は省略_"
        else:
            body = truncated
    return body


def fetch_recent_web_articles(
    qa_index_path: Path,
    *,
    index_name: str = "pm",
    max_chars: int = 4000,
) -> str:
    """brief プロンプト同梱用: qa_index.db から source_type='web' の直近記事を取得する。"""
    import sqlite3
    if not qa_index_path.exists():
        return ""
    conn = sqlite3.connect(str(qa_index_path))
    try:
        rows = conn.execute(
            "SELECT c.content, c.source_ref, c.held_at "
            "FROM chunks c "
            "JOIN chunk_indexes ci ON c.id = ci.chunk_id "
            "WHERE ci.index_name = ? AND c.source_type = 'web' "
            "ORDER BY c.held_at DESC LIMIT 20",
            (index_name,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    NL = chr(10)
    lines = ["## 最近の外部記事" + NL]
    for content, url, held_at in rows:
        title = ((content or "").split(NL)[0]) or "(無題)"
        snippet = (content or "")[:200].replace(NL, " ")
        date_str = held_at or ""
        lines.append(f"- [{title}]({url}) ({date_str})")
        lines.append(f"  {snippet}")
    body = NL.join(lines)
    if len(body) > max_chars:
        body = body[:max_chars]
    return body


def fetch_pm_stats(
    conn,
    today: str,
    since: str | None = None,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> dict:
    """pm.db から統計データを収集する。
    channel_ids/minutes_names を指定すると、該当チャンネル・議事録に由来する
    アクションアイテム・決定事項のみに絞り込んで集計する（省略時は全体集計）。
    """
    return {
        "milestones": fetch_milestone_progress(conn),
        "overdue_items": fetch_overdue_items(conn, today, since, channel_ids=channel_ids, minutes_names=minutes_names),
        "assignee_workload": fetch_assignee_workload(conn, today, channel_ids=channel_ids, minutes_names=minutes_names),
        "unlinked_count": fetch_unlinked_items_count(conn, since, channel_ids=channel_ids, minutes_names=minutes_names),
        "no_assignee_count": fetch_no_assignee_count(conn, since, channel_ids=channel_ids, minutes_names=minutes_names),
        "weekly_trends": fetch_weekly_trends(conn, channel_ids=channel_ids, minutes_names=minutes_names),
        "unacknowledged_decisions": fetch_unacknowledged_decisions(conn, since, channel_ids=channel_ids, minutes_names=minutes_names),
        "stats": fetch_summary_stats(conn, since, today, channel_ids=channel_ids, minutes_names=minutes_names),
    }


def merge_pm_stats(stats_list: list[dict]) -> dict:
    """複数 pm.db の統計を 1 つにマージする。"""
    if len(stats_list) == 1:
        return stats_list[0]
    if not stats_list:
        return {"milestones": [], "overdue_items": [], "assignee_workload": [],
                "unlinked_count": 0, "no_assignee_count": 0, "weekly_trends": [],
                "unacknowledged_decisions": [], "stats": {}}

    merged: dict = {
        "milestones": [],
        "overdue_items": [],
        "unacknowledged_decisions": [],
        "unlinked_count": 0,
        "no_assignee_count": 0,
    }
    for s in stats_list:
        merged["milestones"].extend(s.get("milestones", []))
        merged["overdue_items"].extend(s.get("overdue_items", []))
        merged["unacknowledged_decisions"].extend(s.get("unacknowledged_decisions", []))
        merged["unlinked_count"] += s.get("unlinked_count", 0)
        merged["no_assignee_count"] += s.get("no_assignee_count", 0)

    wl_map: dict[str, dict] = {}
    for s in stats_list:
        for w in s.get("assignee_workload", []):
            name = w["assignee"]
            if name in wl_map:
                wl_map[name]["total_open"] += w["total_open"]
                wl_map[name]["overdue"] += w["overdue"]
                wl_map[name]["no_due_date"] += w.get("no_due_date", 0)
            else:
                wl_map[name] = {**w}
    merged["assignee_workload"] = sorted(
        wl_map.values(), key=lambda x: (-x["overdue"], -x["total_open"]))

    trend_map: dict[str, dict] = {}
    for s in stats_list:
        for t in s.get("weekly_trends", []):
            k = t["week_start"]
            if k in trend_map:
                trend_map[k]["created"] += t["created"]
                trend_map[k]["closed"] += t["closed"]
            else:
                trend_map[k] = {**t}
    merged["weekly_trends"] = sorted(trend_map.values(), key=lambda x: x["week_start"])

    stat_keys = ["total_open", "total_closed", "overdue_count",
                 "total_decisions", "unacknowledged_decisions"]
    merged["stats"] = {
        k: sum(s.get("stats", {}).get(k, 0) for s in stats_list)
        for k in stat_keys
    }
    return merged


# --------------------------------------------------------------------------- #
# 全文脈ビルダー（検索なし・期間内全データ投入方式）
# --------------------------------------------------------------------------- #

def _format_full_decisions(rows: list[dict]) -> str:
    if not rows:
        return "（decisions なし）"
    lines = []
    for r in rows:
        who = f" by {r.get('decided_by')}" if r.get("decided_by") else ""
        lines.append(f"- **[D-{r['id']}]** ({r.get('decided_at') or ''}) {r.get('content') or ''}{who}")
        if r.get("rationale"):
            lines.append(f"  根拠: {r['rationale']}")
    return "\n".join(lines)


def _format_full_action_items(rows: list[dict]) -> str:
    if not rows:
        return "（action_items なし）"
    lines = []
    for r in rows:
        lines.append(
            f"- **[AI-{r['id']}]** [{r.get('status') or ''}] {r.get('content') or ''} "
            f"(担当: {r.get('assignee') or '未定'}, 期限: {r.get('due_date') or '未定'})"
        )
    return "\n".join(lines)


def _fetch_box_documents_full(
    box_docs_path: Path, *, since_date: str, index_name: str, no_encrypt: bool, char_cap: int,
) -> tuple[str, dict]:
    """box_docs.db から期間内更新 + relevance 採用分を新しい順に char_cap まで取得する。
    参照: scripts/data-pipeline/pm_embed.py:630-634（relevance フィルタ）/
          scripts/data-pipeline/pm_box_crawl.py:118-146（スキーマ）
    """
    if not box_docs_path.exists() or char_cap <= 0:
        return "", {"doc_count": 0, "used_count": 0, "truncated": char_cap <= 0}
    try:
        conn = open_db(box_docs_path, encrypt=not no_encrypt)
    except Exception as e:
        logger.warning(f"box_docs.db 接続失敗: {e}")
        return "", {"doc_count": 0, "used_count": 0, "truncated": False}
    try:
        rows = conn.execute(
            "SELECT dc.box_file_id, dc.content_md, bf.name, bf.folder_path, bf.modified_at, bf.index_name "
            "FROM doc_content dc JOIN box_files bf ON dc.box_file_id = bf.box_file_id "
            "WHERE COALESCE(bf.relevance, '') != 'noise' AND bf.modified_at >= ? "
            "ORDER BY bf.modified_at DESC",
            (since_date,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"box_docs.db クエリ失敗: {e}")
        rows = []
    finally:
        conn.close()

    doc_count = 0
    parts: list[str] = []
    used_chars = 0
    truncated = False
    for row in rows:
        idx_raw = row["index_name"] or ""
        try:
            targets = json.loads(idx_raw)
        except Exception:
            targets = [idx_raw] if idx_raw else []
        if index_name not in targets:
            continue
        doc_count += 1
        content = (row["content_md"] or "").strip()
        if not content:
            continue
        heading = row["name"] or ""
        if row["folder_path"]:
            heading = f"{row['folder_path']}/{heading}"
        piece = f"### {heading} ({row['modified_at'] or ''})\n\n{content}"
        if used_chars + len(piece) > char_cap:
            truncated = True
            break
        parts.append(piece)
        used_chars += len(piece)
    return "\n\n---\n\n".join(parts), {"doc_count": doc_count, "used_count": len(parts), "truncated": truncated}


def build_full_context_sections(
    since_date: str,
    today: str,
    *,
    index_name: str | None = None,
    no_encrypt: bool = False,
    char_budget: int | None = None,
    include_box: bool = True,
    box_char_cap: int = _FULLCTX_BOX_CHAR_CAP,
    pm_db_paths: list[Path] | None = None,
) -> tuple[dict[str, str], dict]:
    """期間内の全データ（検索なし）を収集し、セクション別 Markdown dict と meta を返す。

    brief/risk の全文脈方式（generate_brief_report/generate_risk_report）が使う。
    優先度（超過時は下位から切り詰め、構造化データ=優先1は切り詰めない）:
      1. pm.db 統計 + decisions/action_items 全件 + milestones
      2. 議事録全文（fetch_recent_minutes は元々無切り詰め）
      3. Slack 全対象チャンネル全ログ（予算超過時は実サイズ比例で按分）
      4. Box 資料（include_box=False で除外可、box_char_cap まで）

    char_budget は未指定時 ARGUS_FULLCTX_CHAR_BUDGET 環境変数 → 既定 350,000字。

    Returns: (sections, meta)
      sections: {"pm_stats": str, "minutes": str, "slack": str, "box": str}
      meta: 期間・index・セクション別 chars/est_tokens/truncated 等
    """
    if char_budget is None:
        char_budget = int(os.environ.get("ARGUS_FULLCTX_CHAR_BUDGET", _FULLCTX_CHAR_BUDGET_DEFAULT))

    if pm_db_paths is None:
        pm_db_paths = load_pm_db_paths(index_name)
    channel_ids = _load_channel_ids(index_name)
    minutes_names = _load_minutes_names(index_name)
    channel_names = _build_channel_name_map()
    resolved_index = index_name or "pm"

    meta: dict = {
        "since_date": since_date, "today": today, "index_name": index_name or "(default)",
        "char_budget": char_budget, "sections": {},
    }
    remaining = char_budget

    # --- 優先1: pm.db 統計 + decisions/action_items 全件 + milestones（切り詰めなし） ---
    stats_list = []
    decisions_all: list[dict] = []
    actions_all: list[dict] = []
    milestones: list[dict] = []
    for p in pm_db_paths:
        try:
            conn = open_pm_db(p, no_encrypt=no_encrypt)
        except Exception as e:
            logger.warning(f"pm.db 接続失敗 ({p}): {e}")
            continue
        try:
            stats_list.append(fetch_pm_stats(
                conn, today, since=since_date, channel_ids=channel_ids, minutes_names=minutes_names,
            ))
            decisions_all.extend(_query_decisions(conn, since=since_date, limit=1_000_000))
            actions_all.extend(_query_action_items(conn, since=since_date, limit=1_000_000))
            milestones.extend(fetch_milestone_progress(conn))
        except Exception as e:
            logger.warning(f"pm.db 統計取得失敗 ({p}): {e}")
        finally:
            conn.close()
    stats = merge_pm_stats(stats_list) if stats_list else {
        "milestones": [], "overdue_items": [], "assignee_workload": [], "unlinked_count": 0,
        "no_assignee_count": 0, "weekly_trends": [], "unacknowledged_decisions": [], "stats": {},
    }
    if milestones:
        stats["milestones"] = milestones
    s = stats.get("stats", {})
    stats_section = (
        _build_stats_section(stats, s, today)
        + f"\n\n## 全決定事項一覧（{len(decisions_all)}件、切り詰めなし）\n\n"
        + _format_full_decisions(decisions_all)
        + f"\n\n## 全アクションアイテム一覧（{len(actions_all)}件、切り詰めなし）\n\n"
        + _format_full_action_items(actions_all)
    )
    stats_header = f"## 構造化データ（期間: {since_date}〜{today}, 切り詰めなし）\n\n"
    stats_section = stats_header + stats_section
    meta["sections"]["pm_stats"] = {
        "chars": len(stats_section), "est_tokens": len(stats_section) / _FULLCTX_CHARS_PER_TOKEN, "truncated": False,
        "decisions": len(decisions_all), "action_items": len(actions_all),
    }
    remaining -= len(stats_section)

    # --- 優先2: 議事録全文 ---
    minutes_text = fetch_recent_minutes(
        since_date, minutes_dir=_MINUTES_DIR, no_encrypt=no_encrypt, minutes_names=minutes_names or None,
    )
    minutes_truncated = False
    if remaining <= 0:
        minutes_text = "（予算超過のため省略）"
        minutes_truncated = True
    elif len(minutes_text) > remaining:
        # fetch_recent_minutes は kind ごとに held_at DESC で連結されるため、末尾を
        # 切り詰めるのは厳密な「最古から削除」ではなく近似（現実的な妥協）
        minutes_text = minutes_text[:remaining]
        minutes_truncated = True
    minutes_header = f"## 議事録（期間: {since_date}〜{today}, 切り詰め{'あり' if minutes_truncated else 'なし'}）\n\n"
    minutes_section = minutes_header + minutes_text
    meta["sections"]["minutes"] = {
        "chars": len(minutes_section), "est_tokens": len(minutes_section) / _FULLCTX_CHARS_PER_TOKEN, "truncated": minutes_truncated,
    }
    remaining -= len(minutes_section)

    # --- 優先3: Slack 全対象チャンネル全ログ ---
    channel_raw_full: dict[str, str] = {}
    total_slack_chars = 0
    for ch_id in channel_ids:
        raw = fetch_raw_messages(
            ch_id, since_date, data_dir=_DATA_DIR, no_encrypt=no_encrypt, max_chars=10**9,
        )
        channel_raw_full[ch_id] = raw
        total_slack_chars += len(raw)

    slack_truncated = False
    if remaining <= 0:
        channel_raw = dict.fromkeys(channel_ids, "（予算超過のため省略）")
        slack_truncated = True
    elif total_slack_chars > remaining:
        # 実サイズに比例して按分する（均等割りだと閑散チャンネルの余りが多忙チャンネルに
        # 再配分されず予算の使い残しが発生し、下位優先度セクションへ意図せず流れるため）
        scale = remaining / total_slack_chars if total_slack_chars > 0 else 0
        channel_raw = {}
        for ch_id in channel_ids:
            actual = channel_raw_full[ch_id]
            cap = max(500, int(len(actual) * scale))
            if len(actual) <= cap:
                channel_raw[ch_id] = actual  # 既に取得済みで按分後も収まる場合は再取得しない
            else:
                channel_raw[ch_id] = fetch_raw_messages(
                    ch_id, since_date, data_dir=_DATA_DIR, no_encrypt=no_encrypt, max_chars=cap,
                )
        slack_truncated = True
    else:
        channel_raw = channel_raw_full

    slack_parts = []
    for ch_id in channel_ids:
        raw = channel_raw.get(ch_id, "")
        if not raw:
            continue
        label = f"{ch_id} (#{channel_names[ch_id]})" if ch_id in channel_names else ch_id
        slack_parts.append(f"## チャンネル: {label}\n\n{raw}")
    slack_text = "\n\n---\n\n".join(slack_parts)
    slack_header = (
        f"## Slack 会話（期間: {since_date}〜{today}, {len(channel_ids)}チャンネル全対象, "
        f"切り詰め{'あり' if slack_truncated else 'なし'}）\n\n"
    )
    slack_section = slack_header + (slack_text or "（データなし）")
    meta["sections"]["slack"] = {
        "chars": len(slack_section), "est_tokens": len(slack_section) / _FULLCTX_CHARS_PER_TOKEN, "truncated": slack_truncated,
        "channels": len(channel_ids),
    }
    remaining -= len(slack_section)

    # --- 優先4: Box 資料（オプション） ---
    if include_box:
        box_char_cap_eff = min(box_char_cap, max(remaining, 0))
        box_text, box_meta = _fetch_box_documents_full(
            _DATA_DIR / "box_docs.db", since_date=since_date, index_name=resolved_index,
            no_encrypt=no_encrypt, char_cap=box_char_cap_eff,
        )
        box_header = (
            f"## Box 資料（期間: {since_date}〜{today}以降更新, {box_meta['doc_count']}件中"
            f"{box_meta['used_count']}件採用, 切り詰め{'あり' if box_meta['truncated'] else 'なし'}）\n\n"
        )
        box_section = box_header + (box_text or "（対象文書なし）")
        meta["sections"]["box"] = {
            "chars": len(box_section), "est_tokens": len(box_section) / _FULLCTX_CHARS_PER_TOKEN,
            "truncated": box_meta["truncated"], "doc_count": box_meta["doc_count"],
            "used_count": box_meta["used_count"],
        }
    else:
        box_section = "## Box 資料\n\n（除外設定により省略）"
        meta["sections"]["box"] = {"chars": len(box_section), "est_tokens": len(box_section) / _FULLCTX_CHARS_PER_TOKEN,
                                    "truncated": False, "skipped": True}

    meta["total_chars"] = sum(sec["chars"] for sec in meta["sections"].values())
    meta["total_est_tokens"] = int(meta["total_chars"] / _FULLCTX_CHARS_PER_TOKEN)

    sections = {
        "pm_stats": stats_section,
        "minutes": minutes_section,
        "slack": slack_section,
        "box": box_section,
    }
    return sections, meta


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #

def _fmt_closed_items(conns, since_date: str, limit: int = 20) -> str:
    if not isinstance(conns, list):
        conns = [conns]
    all_rows: list[dict] = []
    for conn in conns:
        try:
            rows = conn.execute(
                """SELECT id, content, assignee, due_date
                   FROM action_items
                   WHERE status='closed' AND COALESCE(deleted,0)=0
                   AND extracted_at >= ?
                   ORDER BY extracted_at DESC LIMIT ?""",
                (since_date, limit),
            ).fetchall()
            all_rows.extend(dict(r) for r in rows)
        except Exception:
            continue
    if not all_rows:
        return "（なし）"
    return "\n".join(
        f"- [ID:{r['id']}][担当:{r['assignee'] or '未定'}] {r['content'][:80]}"
        for r in all_rows[:limit]
    )


def _parse_command_args(text: str) -> tuple[int | None, str | None, str | None]:
    """
    Slack コマンドの引数テキストをパースする。

    書式例:
        /argus-brief 60            → days=60, assignee=None, topic=None
        /argus-brief @西澤          → days=None, assignee="西澤", topic=None
        /argus-brief Benchpark     → days=None, assignee=None, topic="Benchpark"
        /argus-brief 60 @西澤      → days=60, assignee="西澤", topic=None
        /argus-brief 60 Benchpark  → days=60, assignee=None, topic="Benchpark"
        /argus-brief 60 @西澤 GPU性能 → days=60, assignee="西澤", topic="GPU性能"

    Returns: (days, assignee, topic)
    """
    days: int | None = None
    assignee: str | None = None
    topic_parts: list[str] = []

    for token in text.split():
        if re.fullmatch(r"\d+", token):
            days = int(token)
        elif token.startswith("@"):
            assignee = token[1:]  # "@西澤" → "西澤"
        else:
            topic_parts.append(token)

    topic = " ".join(topic_parts) if topic_parts else None
    return days, assignee, topic


def build_draft_prompt(
    purpose: str,
    subject: str,
    messages: str,
    stats: dict,
    context: str,
    conns=None,
    today: str = "",
) -> str:
    today = today or date.today().isoformat()
    if purpose == "agenda":
        return _DRAFT_AGENDA_PROMPT.format(
            subject=subject,
            context=context,
            decisions_list=format_decisions_list(stats["unacknowledged_decisions"]),
            overdue_list=format_overdue_list(stats["overdue_items"]),
            messages=messages or "（データなし）",
            today=today,
        )
    elif purpose == "report":
        since_14 = (date.fromisoformat(today) - timedelta(days=_DRAFT_REPORT_SINCE_DAYS)).isoformat()
        closed_items = _fmt_closed_items(conns, since_14) if conns else "（取得不可）"
        return _DRAFT_REPORT_PROMPT.format(
            subject=subject,
            context=context,
            milestone_table=format_milestone_table(stats["milestones"], today),
            closed_items=closed_items,
            overdue_list=format_overdue_list(stats["overdue_items"]),
            assignee_table=format_assignee_table(stats["assignee_workload"]),
            today=today,
        )
    else:  # request
        return _DRAFT_REQUEST_PROMPT.format(
            subject=subject,
            context=context,
            assignee_table=format_assignee_table(stats["assignee_workload"]),
            overdue_list=format_overdue_list(stats["overdue_items"]),
            messages=messages or "（データなし）",
            today=today,
        )


# --------------------------------------------------------------------------- #
# 並列データ収集
# --------------------------------------------------------------------------- #

def _fetch_single_pm_stats(
    p: Path, today: str, since_date: str, no_encrypt: bool,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> dict:
    """単一 pm.db から stats を取得（ThreadPoolExecutor 用）。
    コネクションはスレッド内で閉じて結果だけ返す。"""
    conn = open_pm_db(p, no_encrypt=no_encrypt)
    try:
        stats = fetch_pm_stats(conn, today, since=since_date,
                               channel_ids=channel_ids, minutes_names=minutes_names)
    finally:
        conn.close()
    return stats


def _collect_all_data(
    today: str,
    since_date: str,
    *,
    no_encrypt: bool = False,
    data_dir: Path | None = None,
    minutes_dir: Path | None = None,
    pm_db_path: Path | None = None,
    pm_db_paths: list[Path] | None = None,
    index_name: str | None = None,
    qa_index_path: Path | None = None,
) -> tuple[str, str, dict, str, str]:
    """messages/minutes/stats/knowledge を一括収集し
    (messages, minutes, stats, knowledge_summary, web_articles) を返す。
    knowledge_summary は pm.db.decisions の rationale 付きから取得した背景知識
    （プロジェクト全体共通、index_name の影響を受けない）。

    index_name: argus_config.yaml の indices.{name} を選択する。指定すると
                その index の channels / minutes / pm_db を絞り込み対象にする。
                None の場合は default_index に従う（後方互換）。
    """
    data_dir = data_dir or _DATA_DIR
    minutes_dir = minutes_dir or _MINUTES_DIR
    if pm_db_paths is None:
        pm_db_paths = [pm_db_path] if pm_db_path else load_pm_db_paths(index_name)

    channel_ids = _load_channel_ids(index_name)
    minutes_names = _load_minutes_names(index_name)
    channel_names = _build_channel_name_map()

    # 並列データ収集
    message_parts = []
    minutes = ""
    stats = {}
    knowledge_summary = ""
    web_articles = ""
    if qa_index_path is None:
        qa_index_path = data_dir / "qa_index.db"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        # Slack messages: チャンネルごと並列 fetch
        msg_futs = {}
        for ch_id in channel_ids:
            f = pool.submit(fetch_raw_messages, ch_id, since_date,
                            data_dir=data_dir, no_encrypt=no_encrypt)
            msg_futs[f] = ch_id

        # Minutes: 全 kind を並列 fetch
        min_fut = pool.submit(fetch_recent_minutes, since_date,
                              minutes_dir=minutes_dir, no_encrypt=no_encrypt,
                              minutes_names=minutes_names or None)

        # pm.db stats: 全 pm.db を並列 fetch（channel_ids/minutes_names でフィルタ）
        pm_futs = []
        for p in pm_db_paths:
            f = pool.submit(_fetch_single_pm_stats, p, today, since_date, no_encrypt,  # type: ignore[arg-type]
                            channel_ids=channel_ids, minutes_names=minutes_names)
            pm_futs.append(f)

        # qa_index.db から Web 記事を取得
        web_fut = pool.submit(fetch_recent_web_articles, qa_index_path, index_name=index_name)  # type: ignore[arg-type]

        # background knowledge: pm.db.decisions の rationale 付きから取得
        kn_fut = pool.submit(fetch_background_knowledge,
                             pm_db_paths=pm_db_paths, no_encrypt=no_encrypt)

        # Slack 結果を集約
        for f in concurrent.futures.as_completed(msg_futs):
            ch_id = msg_futs[f]
            try:
                raw = f.result()
                if raw:
                    label = f"{ch_id} (#{channel_names[ch_id]})" if ch_id in channel_names else ch_id
                    message_parts.append(f"## チャンネル: {label}\n\n{raw}")
            except Exception:
                pass
        messages = "\n\n---\n\n".join(message_parts)

        # Minutes 結果
        try:
            minutes = min_fut.result()
        except Exception:
            minutes = ""

        # pm.db stats 結果
        stats_list = []
        for f in pm_futs:
            try:
                stats_list.append(f.result())
            except Exception:
                pass
        stats = merge_pm_stats(stats_list)  # type: ignore[arg-type]

        # knowledge 結果
        try:
            knowledge_summary = kn_fut.result()
        except Exception:
            knowledge_summary = ""

        # Web 記事結果
        try:
            web_articles = web_fut.result()
        except Exception:
            web_articles = ""

    return messages, minutes, stats, knowledge_summary, web_articles


# --------------------------------------------------------------------------- #
# 共有生成ロジック（brief/risk — Slack ハンドラと CLI --brief-to-canvas/--risk で共有）
# --------------------------------------------------------------------------- #

def _brief_prompt_truncated(context, focus, stats_section, conversation_section, minutes_section,
                             knowledge_summary, web_articles) -> str:
    """従来（切り詰め）方式の brief プロンプト。ARGUS_DISABLE_FULLCTX=1 またはフォールバック時に使用。"""
    return (
        f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
        f"以下のプロジェクトデータを分析し、ブリーフィングを生成してください。\n"
        f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
        f"必要に応じて使い、多角的な視点から分析してください。\n\n"
        f"## プロジェクト文脈\n\n{context}\n\n"
        f"## フォーカス指定\n\n{focus}\n\n"
        f"## pm.db 統計\n\n{stats_section}\n\n"
        f"## Slack 会話\n\n{conversation_section}\n\n"
        f"## 議事録\n\n{minutes_section}\n\n"
        f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
        f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
        f"## 指示\n\n"
        f"- 上記のデータを統合し、優先順位を付けたブリーフィングを生成してください\n"
        f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
        f"- 全体で2,000字以内に収めること。画面1枚で読み切れる分量が厳守事項です。"
        f"最重要の3〜5項目に絞り優先度順に、見出し+簡潔な箇条書きで記載してください。"
        f"詳細の列挙ではなく、PMが次に取るべき行動につながる要点のみを書いてください\n"
        f"- `<final_answer>` タグで回答を囲んでください\n"
    )


def _brief_prompt_fullctx(context, focus, sections, knowledge_summary, web_articles) -> str:
    """全文脈（検索なし・期間内全データ投入）方式の brief プロンプト。
    指示部は _brief_prompt_truncated と1字も変えず、データセクションのみ全文版に差し替え
    + 構造化全件（sections['pm_stats']内）/ Box を追加。既定の brief 生成経路。
    """
    return (
        f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
        f"以下のプロジェクトデータを分析し、ブリーフィングを生成してください。\n"
        f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
        f"必要に応じて使い、多角的な視点から分析してください。\n\n"
        f"## プロジェクト文脈\n\n{context}\n\n"
        f"## フォーカス指定\n\n{focus}\n\n"
        f"{sections['pm_stats']}\n\n"
        f"{sections['slack']}\n\n"
        f"{sections['minutes']}\n\n"
        f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
        f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
        f"{sections['box']}\n\n"
        f"## 指示\n\n"
        f"- 上記のデータを統合し、優先順位を付けたブリーフィングを生成してください\n"
        f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
        f"- 全体で2,000字以内に収めること。画面1枚で読み切れる分量が厳守事項です。"
        f"最重要の3〜5項目に絞り優先度順に、見出し+簡潔な箇条書きで記載してください。"
        f"詳細の列挙ではなく、PMが次に取るべき行動につながる要点のみを書いてください\n"
        f"- `<final_answer>` タグで回答を囲んでください\n"
    )


def _risk_prompt_truncated(context, focus, stats_section, conversation_section, minutes_section,
                            knowledge_summary, web_articles) -> str:
    """従来（切り詰め）方式の risk プロンプト。ARGUS_DISABLE_FULLCTX=1 またはフォールバック時に使用。"""
    return (
        f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
        f"以下のプロジェクトデータを分析し、リスク分析レポートを生成してください。\n"
        f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
        f"必要に応じて使い、多角的な視点からリスクを洗い出してください。\n\n"
        f"## プロジェクト文脈\n\n{context}\n\n"
        f"## フォーカス指定\n\n{focus}\n\n"
        f"## pm.db 統計\n\n{stats_section}\n\n"
        f"## Slack 会話\n\n{conversation_section}\n\n"
        f"## 議事録\n\n{minutes_section}\n\n"
        f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
        f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
        f"## 指示\n\n"
        f"- 上記のデータからリスク・懸念・予兆を洗い出し、優先度付きで報告してください\n"
        f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
        f"- リスクは「顕在化しているリスク」と「放置すると問題になりうる予兆」に分けて記載\n"
        f"- 全体で2,000字以内に収めること。画面1枚で読み切れる分量が厳守事項です。"
        f"リスクは影響度の高い上位3〜5件に絞り、各リスクは2〜3行（状況・根拠・推奨対応）で"
        f"記載してください\n"
        f"- `<final_answer>` タグで回答を囲んでください\n"
    )


def _risk_prompt_fullctx(context, focus, sections, knowledge_summary, web_articles) -> str:
    """全文脈（検索なし・期間内全データ投入）方式の risk プロンプト。
    指示部は _risk_prompt_truncated と1字も変えず、データセクションのみ全文版に差し替え
    + 構造化全件（sections['pm_stats']内）/ Box を追加。既定の risk 生成経路。
    """
    return (
        f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
        f"以下のプロジェクトデータを分析し、リスク分析レポートを生成してください。\n"
        f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
        f"必要に応じて使い、多角的な視点からリスクを洗い出してください。\n\n"
        f"## プロジェクト文脈\n\n{context}\n\n"
        f"## フォーカス指定\n\n{focus}\n\n"
        f"{sections['pm_stats']}\n\n"
        f"{sections['slack']}\n\n"
        f"{sections['minutes']}\n\n"
        f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
        f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
        f"{sections['box']}\n\n"
        f"## 指示\n\n"
        f"- 上記のデータからリスク・懸念・予兆を洗い出し、優先度付きで報告してください\n"
        f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
        f"- リスクは「顕在化しているリスク」と「放置すると問題になりうる予兆」に分けて記載\n"
        f"- 全体で2,000字以内に収めること。画面1枚で読み切れる分量が厳守事項です。"
        f"リスクは影響度の高い上位3〜5件に絞り、各リスクは2〜3行（状況・根拠・推奨対応）で"
        f"記載してください\n"
        f"- `<final_answer>` タグで回答を囲んでください\n"
    )


def _load_context_with_glossary() -> str:
    """プロジェクト文脈（docs/project.md）+ 動的用語辞書 + glossary を組み立てる。"""
    context = load_claude_md_context()
    try:
        from utils.terminology import build_terminology_reference
        dyn_terms = build_terminology_reference()
        if dyn_terms:
            context = context + dyn_terms
    except Exception:
        pass
    try:
        from utils.glossary import build_reference as build_glossary_ref
        glossary_ref = build_glossary_ref()
        if glossary_ref:
            context = context + glossary_ref
    except Exception:
        pass
    return context


def _extract_final_answer(result: str) -> str:
    """<final_answer> タグがあれば抽出、なければタグを除去して返す。"""
    final = re.search(r"<final_answer>(.*?)</final_answer>", result, re.DOTALL)
    if final:
        return final.group(1).strip()
    return re.sub(r"<[^>]+>", "", result).strip()


def _fetch_knowledge_and_web_articles(
    pm_db_paths: list[Path], no_encrypt: bool, index_name: str | None,
) -> tuple[str, str]:
    """確定済みナレッジ・外部記事を個別に取得する（全文脈方式・従来方式で共通）。

    どちらか一方が失敗しても、それだけで全文脈方式全体をフォールバックさせない
    （build_full_context_sections + call_argus_llm のみをフォールバック対象にするため）。
    失敗時は空文字に縮退する。
    """
    try:
        knowledge_summary = fetch_background_knowledge(pm_db_paths=pm_db_paths, no_encrypt=no_encrypt)
    except Exception as e:
        logger.warning(f"fetch_background_knowledge 失敗（{type(e).__name__}: {e}）。空文字で継続")
        knowledge_summary = ""
    try:
        web_articles = fetch_recent_web_articles(_DATA_DIR / "qa_index.db", index_name=index_name)
    except Exception as e:
        logger.warning(f"fetch_recent_web_articles 失敗（{type(e).__name__}: {e}）。空文字で継続")
        web_articles = ""
    return knowledge_summary, web_articles


def _log_fullctx_meta(tag: str, ctx_meta: dict) -> None:
    """build_full_context_sections の meta を1行に要約してログ出力する
    （明朝の無人 cron 実行の事後診断用）。"""
    section_summary = " ".join(
        f"{name}={info.get('chars', 0)}chars/trunc={info.get('truncated', False)}"
        for name, info in ctx_meta.get("sections", {}).items()
    )
    logger.info(
        f"{tag} fullctx meta: total_chars={ctx_meta.get('total_chars')} "
        f"est_tokens={ctx_meta.get('total_est_tokens')} {section_summary}"
    )


# 同一文字の100連続以上（「!」を32,768トークン埋め尽くす反復退化の検知用）
_DEGENERATE_RUN_RE = re.compile(r"(.)\1{99,}")


def _is_degenerate_output(text: str) -> bool:
    """LLM 出力が退化（同一文字反復・タグ未クローズ・記号だらけ）していないか判定する。

    call_argus_llm は例外にならず正常終了として退化出力（例: 32,768トークン全部が「!」）を
    返すことが実測で確認されている（温度0.8でも確率的に発生）ため、例外系フォールバックとは
    別に出力そのものの品質ゲートが必要。
    """
    if not text:
        return True
    # (a) 同一文字の100連続以上
    if _DEGENERATE_RUN_RE.search(text):
        return True
    # (b) <final_answer> が開いているのに閉じていない（タグ除去前の生テキストで判定）
    if "<final_answer>" in text and "</final_answer>" not in text:
        return True
    # (c) 有効文字（英数字・かな漢字等）が空白除去後の50%未満 = 記号・反復だらけ
    non_ws = [c for c in text if not c.isspace()]
    if non_ws:
        meaningful = sum(1 for c in non_ws if c.isalnum())
        if meaningful / len(non_ws) < 0.5:
            return True
    return False


def _sanitize_degenerate_output(text: str) -> str:
    """退化出力から同一文字の長い連続 run 以降を切り落として返す。
    切り落とし後に実質空ならエラーメッセージ文字列を返す（無限リトライはしない）。
    """
    m = _DEGENERATE_RUN_RE.search(text)
    if m:
        text = text[: m.start(1)]
    text = text.strip()
    if len(text) < 20:
        return "（Argus: 出力生成に失敗しました。しばらく待ってから再度お試しください）"
    return text


def _ensure_not_degenerate(result: str, tag: str) -> str:
    """フォールバック（従来切り詰め方式）の出力も退化していないか最終確認する。
    退化していた場合は logger.error の上、退化部分を切り落として返す（無限リトライはしない）。
    """
    if _is_degenerate_output(result):
        logger.error(f"{tag} 従来方式の出力も退化と判定されました: {result[:100]!r}")
        return _sanitize_degenerate_output(result)
    return result


def generate_brief_report(
    today: str,
    since_date: str,
    *,
    index_name: str | None = None,
    no_encrypt: bool = False,
    assignee: str | None = None,
    topic: str | None = None,
    pm_db_paths: list[Path] | None = None,
) -> str:
    """ブリーフィング本文を生成する（/argus-brief と --brief-to-canvas の共有ロジック）。

    既定で全文脈方式（検索なし・期間内全データ投入）を使う。2026-07-23 の盲検 A/B（judge:
    RiVault Kimi-K2-Thinking）では risk は fullctx が明確勝ち、brief は僅差（4対5）だった。
    期間サマリー型タスク（brief/risk）への適用として PM 判断で全文脈方式を既定化した
    （ARGUS_DISABLE_FULLCTX=1 で従来の切り詰め single-shot に戻せる）。
    全文脈方式の構築・生成（build_full_context_sections + call_argus_llm）が例外になった
    場合は logger.warning の上、従来方式で1回だけ再試行する（フォールバック。
    系統Bの旧worker/orchestratorには戻さない）。

    pm_db_paths: CLI --db 等で pm.db パスを明示指定したい場合に渡す。省略時は
    index_name から load_pm_db_paths() で解決する。
    """
    context = _load_context_with_glossary()
    focus_lines = []
    if assignee:
        focus_lines.append(f"担当者フォーカス: {assignee}")
    if topic:
        focus_lines.append(f"話題フォーカス: {topic}")
    focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

    resolved_pm_db_paths = pm_db_paths if pm_db_paths is not None else load_pm_db_paths(index_name)

    def _run_truncated() -> str:
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, pm_db_paths=resolved_pm_db_paths,
            index_name=index_name,
        )
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        prompt = _brief_prompt_truncated(context, focus_section_str, stats_section,
                                          conversation_section, minutes_section,
                                          knowledge_summary, web_articles)
        return call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                               max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)

    if os.environ.get("ARGUS_DISABLE_FULLCTX") == "1":
        result = _ensure_not_degenerate(_run_truncated(), "[argus-brief]")
    else:
        knowledge_summary, web_articles = _fetch_knowledge_and_web_articles(
            resolved_pm_db_paths, no_encrypt, index_name,
        )
        try:
            sections, ctx_meta = build_full_context_sections(
                since_date, today, index_name=index_name, no_encrypt=no_encrypt,
                pm_db_paths=resolved_pm_db_paths,
            )
            _log_fullctx_meta("[argus-brief]", ctx_meta)
            prompt = _brief_prompt_fullctx(context, focus_section_str, sections,
                                            knowledge_summary, web_articles)
            result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                                     max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)
            if _is_degenerate_output(result):
                logger.warning(f"[argus-brief] fullctx 出力が退化、truncated 方式へフォールバック: "
                                f"{result[:100]!r}")
                result = _ensure_not_degenerate(_run_truncated(), "[argus-brief]")
        except Exception as e:
            logger.warning(f"[argus-brief] 全文脈方式が失敗（{type(e).__name__}: {e}）。"
                            f"従来の切り詰めプロンプトで1回だけ再試行します")
            result = _ensure_not_degenerate(_run_truncated(), "[argus-brief]")

    return _extract_final_answer(result)


def generate_risk_report(
    today: str,
    since_date: str,
    *,
    index_name: str | None = None,
    no_encrypt: bool = False,
    assignee: str | None = None,
    topic: str | None = None,
    pm_db_paths: list[Path] | None = None,
) -> str:
    """リスク分析レポート本文を生成する（/argus-risk と --risk の共有ロジック）。
    挙動は generate_brief_report と対称（全文脈既定 / ARGUS_DISABLE_FULLCTX / フォールバック /
    pm_db_paths 明示指定）。2026-07-23 の盲検 A/B では risk は fullctx が明確勝ちだった。
    """
    context = _load_context_with_glossary()
    focus_lines = []
    if assignee:
        focus_lines.append(f"担当者フォーカス: {assignee}")
    if topic:
        focus_lines.append(f"話題フォーカス: {topic}")
    focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

    resolved_pm_db_paths = pm_db_paths if pm_db_paths is not None else load_pm_db_paths(index_name)

    def _run_truncated() -> str:
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, pm_db_paths=resolved_pm_db_paths,
            index_name=index_name,
        )
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        prompt = _risk_prompt_truncated(context, focus_section_str, stats_section,
                                         conversation_section, minutes_section,
                                         knowledge_summary, web_articles)
        return call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                               max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)

    if os.environ.get("ARGUS_DISABLE_FULLCTX") == "1":
        result = _ensure_not_degenerate(_run_truncated(), "[argus-risk]")
    else:
        knowledge_summary, web_articles = _fetch_knowledge_and_web_articles(
            resolved_pm_db_paths, no_encrypt, index_name,
        )
        try:
            sections, ctx_meta = build_full_context_sections(
                since_date, today, index_name=index_name, no_encrypt=no_encrypt,
                pm_db_paths=resolved_pm_db_paths,
            )
            _log_fullctx_meta("[argus-risk]", ctx_meta)
            prompt = _risk_prompt_fullctx(context, focus_section_str, sections,
                                           knowledge_summary, web_articles)
            result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                                     max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)
            if _is_degenerate_output(result):
                logger.warning(f"[argus-risk] fullctx 出力が退化、truncated 方式へフォールバック: "
                                f"{result[:100]!r}")
                result = _ensure_not_degenerate(_run_truncated(), "[argus-risk]")
        except Exception as e:
            logger.warning(f"[argus-risk] 全文脈方式が失敗（{type(e).__name__}: {e}）。"
                            f"従来の切り詰めプロンプトで1回だけ再試行します")
            result = _ensure_not_degenerate(_run_truncated(), "[argus-risk]")

    return _extract_final_answer(result)


def generate_daily_summary_report(
    today: str,
    *,
    index_name: str | None = None,
    no_encrypt: bool = False,
    pm_db_paths: list[Path] | None = None,
) -> tuple[str, str]:
    """日次サマリー本文を生成する（/argus-today の共有ロジック）。

    既定で全文脈方式（検索なし・当日全データ投入）を使う。brief/risk と同じ
    ARGUS_DISABLE_FULLCTX=1 で従来の切り詰め（チャンネルあたり20,000字）に戻せる。
    プロンプト本文（_DAILY_SUMMARY_PROMPT）は変更せず、{messages}/{minutes} に渡す
    入力データの取得方法のみを切り替える。全文脈方式の構築・生成が例外/退化になった場合は
    logger.warning の上、従来方式で1回だけ再試行する（brief/risk と対称のフォールバック）。

    Returns: (result, messages) — messages はメンション抽出（_filter_mentions_for_user）に
    そのまま再利用する生 Slack テキスト。実際に使用した方式の raw text を返す
    （fullctx 時は build_full_context_sections の slack セクション）。
    """
    context = load_claude_md_context()
    resolved_pm_db_paths = pm_db_paths if pm_db_paths is not None else load_pm_db_paths(index_name)
    since_date = today  # /argus-today は常に当日のみ

    def _run_truncated() -> tuple[str, str]:
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, pm_db_paths=resolved_pm_db_paths,
            index_name=index_name,
        )
        prompt = _DAILY_SUMMARY_PROMPT.format(
            today=today,
            context=context,
            knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
            messages=messages or "（本日のメッセージはありません）",
            minutes=minutes or "（本日の議事録はありません）",
        )
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        return result, messages

    if os.environ.get("ARGUS_DISABLE_FULLCTX") == "1":
        result, messages = _run_truncated()
        result = _ensure_not_degenerate(result, "[argus-today]")
    else:
        # today は knowledge_summary のみ使用し web_articles は使わないため、
        # _fetch_knowledge_and_web_articles（両方取得）は使わず knowledge のみ取得する
        # （取得して捨てる無駄を解消。2026-07-26 レビュー指摘）
        try:
            knowledge_summary = fetch_background_knowledge(
                pm_db_paths=resolved_pm_db_paths, no_encrypt=no_encrypt,
            )
        except Exception as e:
            logger.warning(f"[argus-today] fetch_background_knowledge 失敗"
                            f"（{type(e).__name__}: {e}）。空文字で継続")
            knowledge_summary = ""
        try:
            sections, ctx_meta = build_full_context_sections(
                since_date, today, index_name=index_name, no_encrypt=no_encrypt,
                # today は当日・直近の会話が主材料であり Box 本文は過大なため除外
                pm_db_paths=resolved_pm_db_paths, include_box=False,
            )
            _log_fullctx_meta("[argus-today]", ctx_meta)
            prompt = _DAILY_SUMMARY_PROMPT.format(
                today=today,
                context=context,
                knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
                messages=sections["slack"],
                minutes=sections["minutes"],
            )
            result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                                     max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)
            if _is_degenerate_output(result):
                logger.warning(f"[argus-today] fullctx 出力が退化、truncated 方式へフォールバック: "
                                f"{result[:100]!r}")
                result, messages = _run_truncated()
                result = _ensure_not_degenerate(result, "[argus-today]")
            else:
                messages = sections["slack"]
        except Exception as e:
            logger.warning(f"[argus-today] 全文脈方式が失敗（{type(e).__name__}: {e}）。"
                            f"従来の切り詰めプロンプトで1回だけ再試行します")
            result, messages = _run_truncated()
            result = _ensure_not_degenerate(result, "[argus-today]")

    return result, messages


def generate_draft_report(
    purpose: str,
    subject: str,
    today: str,
    since_date: str,
    *,
    index_name: str | None = None,
    no_encrypt: bool = False,
    pm_db_paths: list[Path] | None = None,
) -> str:
    """草案本文を生成する（/argus-draft の共有ロジック）。

    purpose in ("agenda", "request") は {messages}（Slack 会話）を使うため、
    brief/risk/today と同じ全文脈方式（ARGUS_DISABLE_FULLCTX でopt-out）を適用する。
    purpose == "report" は milestone_table/closed_items/overdue_list/assignee_table のみで
    Slack 会話（{messages}）を使わず、元々チャンネルあたり20,000字切り詰めの影響を受けない
    ため常に同一ロジックを使う（フォールバック分岐そのものが不要）。
    プロンプト本文（_DRAFT_AGENDA_PROMPT / _DRAFT_REQUEST_PROMPT 等）は変更せず、
    {messages} に渡す入力データの取得方法のみを切り替える。

    fullctx 経路（既定）では stats のみ _fetch_single_pm_stats + merge_pm_stats で
    軽量取得し、Slack/議事録/知識/Web を含む重い _collect_all_data は
    実際に truncated 方式（ARGUS_DISABLE_FULLCTX=1・report purpose・フォールバック時）を
    使う場合のみ呼ぶ（brief/risk と同型。二重収集の解消。2026-07-26 レビュー指摘）。
    """
    context = load_claude_md_context()
    resolved_pm_db_paths = pm_db_paths if pm_db_paths is not None else load_pm_db_paths(index_name)
    uses_messages = purpose in ("agenda", "request")

    # report 用途では build_draft_prompt が pm.db への接続を必要とする（完了アイテム取得）。
    # 他の用途では None で良い。
    conns = None
    if purpose == "report":
        conns = [open_pm_db(p, no_encrypt=no_encrypt) for p in resolved_pm_db_paths]

    def _run_truncated() -> str:
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, pm_db_paths=resolved_pm_db_paths,
            index_name=index_name,
        )
        prompt = build_draft_prompt(purpose, subject, messages, stats, context,
                                    conns=conns, today=today)
        return call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")

    try:
        if not uses_messages or os.environ.get("ARGUS_DISABLE_FULLCTX") == "1":
            result = _run_truncated()
            result = _ensure_not_degenerate(result, f"[argus-draft:{purpose}]")
        else:
            try:
                channel_ids = _load_channel_ids(index_name)
                minutes_names = _load_minutes_names(index_name)
                stats_list = [
                    _fetch_single_pm_stats(p, today, since_date, no_encrypt,
                                           channel_ids=channel_ids, minutes_names=minutes_names)
                    for p in resolved_pm_db_paths
                ]
                stats = merge_pm_stats(stats_list)
                sections, ctx_meta = build_full_context_sections(
                    since_date, today, index_name=index_name, no_encrypt=no_encrypt,
                    # draft は直近の会話が主材料であり Box 本文は過大なため除外
                    pm_db_paths=resolved_pm_db_paths, include_box=False,
                )
                _log_fullctx_meta(f"[argus-draft:{purpose}]", ctx_meta)
                prompt = build_draft_prompt(purpose, subject, sections["slack"], stats, context,
                                            conns=conns, today=today)
                result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。",
                                         max_tokens=_BRIEF_RISK_MAX_TOKENS, timeout=600)
                if _is_degenerate_output(result):
                    logger.warning(f"[argus-draft:{purpose}] fullctx 出力が退化、"
                                    f"truncated 方式へフォールバック: {result[:100]!r}")
                    result = _ensure_not_degenerate(_run_truncated(), f"[argus-draft:{purpose}]")
            except Exception as e:
                logger.warning(f"[argus-draft:{purpose}] 全文脈方式が失敗（{type(e).__name__}: {e}）。"
                                f"従来の切り詰めプロンプトで1回だけ再試行します")
                result = _ensure_not_degenerate(_run_truncated(), f"[argus-draft:{purpose}]")
    finally:
        if conns:
            for c in conns:
                c.close()

    return result


# --------------------------------------------------------------------------- #
# Slack コマンドのバックグラウンド処理
# --------------------------------------------------------------------------- #

def _run_brief(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-brief のバックグラウンド処理 — 全文脈方式（ARGUS_DISABLE_FULLCTX でopt-out）"""
    import logging
    logger = logging.getLogger("pm_argus")
    try:
        cmd_text = (command.get("text") or "").strip()
        arg_days, assignee, topic = _parse_command_args(cmd_text)
        days = arg_days if arg_days is not None else _DEFAULT_SINCE_DAYS
        requester = command.get("user_name") or "プロジェクトメンバー"

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=days)).isoformat()
        index_name = resolve_index_name(command.get("channel_id") or None)
        focus_desc = "".join([
            f" days={days}",
            f" index={index_name}",
            f" requester={requester}",
            f" assignee={assignee}" if assignee else "",
            f" topic={topic}" if topic else "",
        ])
        logger.info(f"[argus-brief] since={since_date}{focus_desc}")

        result = generate_brief_report(
            today, since_date, index_name=index_name, no_encrypt=no_encrypt,
            assignee=assignee, topic=topic,
        )

        header = f"*Argus ブリーフィング ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-brief] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

        from argus.narrate import _post_argus_voice
        _post_argus_voice(
            command,
            kind="brief",
            today=today,
            result_md=result,
            summarize_mode="priority",
            title=f"Argus ブリーフィング (音声版) {today}",
            enable_env="ARGUS_BRIEF_VOICE",
        )

        logger.info("[argus-brief] 完了")
    except Exception as e:
        logger.exception("[argus-brief] エラー")
        respond(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Argus ブリーフィング生成エラー: {e}",
                    },
                }
            ],
        )


def _build_stats_section(stats: dict, s: dict, today: str) -> str:
    """pm.db stats を Markdown セクションに整形する（risk/brief 共通）。"""
    return (
        f"## pm.db 統計サマリー\n\n"
        f"- オープンAI: {s.get('total_open', 0)}件 / 完了AI: {s.get('total_closed', 0)}件\n"
        f"- 期限超過（open）: {s.get('overdue_count', 0)}件\n"
        f"- 未確認決定事項: {s.get('unacknowledged_decisions', 0)}件\n\n"
        f"## マイルストーン進捗\n\n"
        f"{format_milestone_table(stats.get('milestones', []), today)}\n\n"
        f"## 期限超過アクションアイテム\n\n"
        f"{format_overdue_list(stats.get('overdue_items', []))}\n\n"
        f"## 担当者別負荷\n\n"
        f"{format_assignee_table(stats.get('assignee_workload', []))}\n\n"
        f"## 週次トレンド（直近4週）\n\n"
        f"{format_trends_table(stats.get('weekly_trends', []))}\n\n"
        f"## 未確認決定事項\n\n"
        f"{format_decisions_list(stats.get('unacknowledged_decisions', []))}"
    )


def _run_draft(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-draft のバックグラウンド処理 — agenda/request は全文脈方式
    （ARGUS_DISABLE_FULLCTX でopt-out）、report は元々切り詰めの影響を受けないため従来のまま"""
    import logging
    logger = logging.getLogger("pm_argus")
    try:
        text = (command.get("text") or "").strip()
        parts = text.split(None, 1)
        purpose = parts[0].lower() if parts else ""
        subject = parts[1] if len(parts) > 1 else ""

        if purpose not in ("agenda", "report", "request"):
            respond(
                text=(
                    "用途を指定してください。\n"
                    "例: `/argus-draft agenda 次回リーダー会議`\n"
                    "用途: `agenda`(会議アジェンダ), `report`(進捗報告), `request`(確認依頼)"
                ),
                response_type="ephemeral",
                replace_original=True,
            )
            return

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=_DRAFT_REPORT_SINCE_DAYS)).isoformat()
        index_name = resolve_index_name(command.get("channel_id") or None)
        logger.info(f"[argus-draft] purpose={purpose} subject={subject} index={index_name}")

        result = generate_draft_report(
            purpose, subject, today, since_date, index_name=index_name, no_encrypt=no_encrypt,
        )
        full_text = _to_slack_mrkdwn(f"*Argus 草案 ({purpose}: {subject})*\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-draft] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)
        logger.info("[argus-draft] 完了")
    except Exception as e:
        logger.exception("[argus-draft] エラー")
        respond(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Argus 草案生成エラー: {e}",
                    },
                }
            ],
        )


def _run_risk(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-risk のバックグラウンド処理 — 全文脈方式（ARGUS_DISABLE_FULLCTX でopt-out）"""
    import logging
    logger = logging.getLogger("pm_argus")
    try:
        cmd_text = (command.get("text") or "").strip()
        arg_days, assignee, topic = _parse_command_args(cmd_text)
        days = arg_days if arg_days is not None else _DEFAULT_SINCE_DAYS

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=days)).isoformat()
        index_name = resolve_index_name(command.get("channel_id") or None)
        focus_desc = "".join([
            f" days={days}",
            f" index={index_name}",
            f" assignee={assignee}" if assignee else "",
            f" topic={topic}" if topic else "",
        ])
        logger.info(f"[argus-risk] since={since_date}{focus_desc}")

        result = generate_risk_report(
            today, since_date, index_name=index_name, no_encrypt=no_encrypt,
            assignee=assignee, topic=topic,
        )

        header = f"*Argus リスク分析 ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-risk] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

        from argus.narrate import _post_argus_voice
        _post_argus_voice(
            command,
            kind="risk",
            today=today,
            result_md=result,
            summarize_mode="priority",
            title=f"Argus リスク分析 (音声版) {today}",
            enable_env="ARGUS_RISK_VOICE",
        )
        logger.info("[argus-risk] 完了")
    except Exception as e:
        logger.exception("[argus-risk] エラー")
        respond(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Argus リスク分析エラー: {e}",
                    },
                }
            ],
        )


def _run_direction(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-direction のバックグラウンド処理 — 機能2: 決定クラスタ集約・方向Δ

    brief/risk と異なり Slack 会話・議事録データは使わない。pm.db の
    ledger_edges/ledger_goals/decisions のみを参照する台帳グラフ計算
    （集合化・投入量集計・Δ照合はLLM不使用）+ クラスタ命名のみLLMを使う
    （設計書§6：LLMの裁量を命名に限定し、存在しない一貫性の付与を防ぐ）。
    """
    import logging
    import os
    logger = logging.getLogger("pm_argus")
    try:
        from argus.direction import build_direction_report

        index_name = resolve_index_name(command.get("channel_id") or None)
        pm_db_paths = load_pm_db_paths(index_name)
        pm_conn = open_pm_db(pm_db_paths[0], no_encrypt=no_encrypt)

        logger.info("[argus-direction] レポート生成中")
        result, graph_path = build_direction_report(pm_conn, use_llm_naming=True, include_graph=True)

        today = date.today().isoformat()
        header = f"*Argus 方向Δレポート ({today})*"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-direction] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)
        logger.info("[argus-direction] 完了")

        if graph_path:
            bot_token = os.environ.get("SLACK_BOT_TOKEN")
            channel_id = command.get("channel_id")
            if bot_token and channel_id:
                try:
                    from slack_sdk import WebClient
                    WebClient(token=bot_token).files_upload_v2(
                        channel=channel_id,
                        file=str(graph_path),
                        filename="argus_direction_graph.png",
                        title="Argus 方向Δ 台帳グラフ",
                        initial_comment=(
                            ":bar_chart: 目標→決定クラスタの構造図です。"
                            "テキストレポートと異なりチャンネル全員に表示されます。"
                        ),
                    )
                    logger.info("[argus-direction] グラフ画像アップロード完了")
                except Exception:
                    logger.exception(
                        "[argus-direction] グラフ画像アップロード失敗（レポート本体は送信済み）"
                    )
            else:
                logger.warning(
                    "[argus-direction] SLACK_BOT_TOKEN/channel_id 不明のためグラフ画像アップロードをスキップ"
                )
            try:
                graph_path.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        logger.exception("[argus-direction] エラー")
        respond(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Argus 方向Δ分析エラー: {e}",
                    },
                }
            ],
        )


def _build_channel_name_map() -> dict[str, str]:
    """argus_config.yaml の `channel_names:` セクションから channel_id → 表示名を取得。
    yaml に無い場合は pm_qa_server._CHANNEL_NAMES をフォールバック。"""
    from cli_utils import resolve_channel_names
    channel_map = dict(resolve_channel_names())
    if not channel_map:
        try:
            from argus.pm_qa_server import _channel_names as _CHANNEL_NAMES
            channel_map.update(_CHANNEL_NAMES)
        except ImportError:
            pass
    return channel_map


def _filter_mentions_for_user(
    messages: str,
    user_name: str,
    user_id: str,
    channel_names: dict[str, str],
    user_id_map: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    生メッセージから実行者へのメンションを抽出し、
    (全体メッセージ, メンション専用セクション) を返す。
    実行者が投稿したメッセージは除外する。

    Args:
        messages: fetch_raw_messages() の出力 (チャンネル単位で整形済み)
        user_name: 実行者の表示名 (例: "Hikaru Inoue (RIKEN)" または "hikaru.inoue")
        user_id: 実行者の Slack user_id (例: "U0XXXXXXX")
        channel_names: チャンネルID -> 表示名のマッピング
        user_id_map: user_id -> user_name のマッピング（テキスト内のユーザーID展開用）

    Returns:
        (全体メッセージ, メンション専用セクション or "")
        メンションがゼロ件の場合は ("全体", "")
    """
    if user_id_map is None:
        user_id_map = {}
    mention_lines = []

    # 検索パターン: user_id、姓、user_name の全パターンを試す
    search_patterns = [user_id]  # 最優先: user_id (最も正確)

    # 姓を取得 (例: "Hikaru Inoue (RIKEN)" -> "Inoue")
    parts = user_name.split()
    if len(parts) >= 2:
        search_patterns.append(parts[1])  # 姓

    # user_name 全体も追加 (例: "Hikaru Inoue" または "hikaru.inoue")
    search_patterns.append(user_name)

    # チャンネルごとに分割 (## チャンネル: で区切られている)
    for ch_section in messages.split("## チャンネル: "):
        if not ch_section.strip():
            continue

        # チャンネルID取得 (先頭行は "Cxxx" または "Cxxx (#name)" 形式)
        lines = ch_section.strip().split("\n")
        header = lines[0].strip()
        m_ch = re.match(r"^(C[A-Z0-9]+)", header)
        ch_id = m_ch.group(1) if m_ch else header
        ch_name = channel_names.get(ch_id, ch_id)

        # メッセージ行を走査
        for line in lines[1:]:
            # [YYYY-MM-DD HH:MM] user: text 形式
            if "] " not in line:
                continue

            # 投稿者名と本文を分離
            bracket_part = line.split("] ", 1)
            if len(bracket_part) < 2:
                continue

            poster_and_text = bracket_part[1]
            # "  user: text" または "user: text" 形式
            colon_idx = poster_and_text.find(": ")
            if colon_idx == -1:
                continue

            poster = poster_and_text[:colon_idx].strip()
            text_part = poster_and_text[colon_idx + 2:]

            # ★ ここで投稿者が実行者と異なるか確認（自分宛のメンションのみ）
            if poster == user_name or poster == user_id or any(p in poster for p in search_patterns):
                # 自分が投稿したメッセージなので除外
                continue

            # text 部分に任意のパターンが含まれるか確認
            if any(pattern in text_part for pattern in search_patterns):
                # テキスト内のユーザーID (U0XXXXXXX) を展開
                expanded_line = line
                for uid, uname in user_id_map.items():
                    expanded_line = expanded_line.replace(uid, uname)
                # テキスト内のチャンネルID (C0XXXXXXX、<#C..>、<#C..|name>) を展開
                for cid, cname in channel_names.items():
                    expanded_line = re.sub(
                        rf"<#{re.escape(cid)}(?:\|[^>]*)?>",
                        f"#{cname}",
                        expanded_line,
                    )
                    expanded_line = expanded_line.replace(cid, f"#{cname}")

                # チャンネル名付きで記録
                mention_lines.append(f"{ch_name} {expanded_line}")

    if not mention_lines:
        return messages, ""

    mention_section = (
        "## あなた宛のメンション\n\n"
        + "\n".join(mention_lines)
        + "\n"
    )

    return messages, mention_section


def _run_today_only(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-today のバックグラウンド処理。
    本日のデータのみ収集し、実行者宛メンションを別トピック化。
    """
    import logging
    logger = logging.getLogger("pm_argus")

    try:
        # 1. 実行者情報取得
        user_name = command.get("user_name") or "プロジェクトメンバー"
        user_id = command.get("user_id") or ""
        requester = user_name

        # 2. 今日のデータを収集・LLM呼び出し（全文脈方式。ARGUS_DISABLE_FULLCTX でopt-out）
        today = date.today().isoformat()

        index_name = resolve_index_name(command.get("channel_id") or None)
        logger.info(f"[argus-today] requester={requester} user_id={user_id} index={index_name}")

        logger.info("[argus-today] LLM 呼び出し中...")
        result, messages = generate_daily_summary_report(
            today, index_name=index_name, no_encrypt=no_encrypt,
        )

        # 3. ユーザーIDマップを構築（テキスト内のID展開用）
        # 優先順位: argus_config.yaml の user_names: > slack.db の messages.user_name

        from cli_utils import resolve_user_names
        user_id_map: dict[str, str] = dict(resolve_user_names())
        try:
            from db_utils import open_db

            unified_db = _REPO_ROOT / "data" / "slack.db"
            uid_pattern = re.compile(r'(U0[A-Z0-9]{9})')
            text_uids: set[str] = set()

            try:
                conn = open_db(unified_db, encrypt=not no_encrypt)
                for row in conn.execute("SELECT text FROM messages WHERE text IS NOT NULL").fetchall():
                    if row[0]:
                        text_uids.update(uid_pattern.findall(row[0]))
                # yaml で未解決の user_id だけ slack.db から引く
                for uid in text_uids - user_id_map.keys():
                    row = conn.execute(
                        "SELECT user_name FROM messages WHERE user_id = ?"
                        " AND user_name IS NOT NULL AND user_name != ? AND user_name NOT LIKE 'U0%' LIMIT 1",
                        (uid, uid),
                    ).fetchone()
                    if row and row[0]:
                        user_id_map[uid] = row[0]
                conn.close()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[argus-today] ユーザーIDマップ構築失敗: {e}")

        # 4. メンション抽出 (argus_config.yaml + _CHANNEL_NAMES から取得)
        channel_names = _build_channel_name_map()
        _, mention_section = _filter_mentions_for_user(messages, user_name, user_id, channel_names, user_id_map)

        # 5. メンションセクションを追加
        if mention_section:
            result += f"\n\n---\n\n{mention_section}"

        # 6. ephemeral 応答 (Block Kit で mrkdwn 有効化)
        header = f":memo: *Argus 今日の活動サマリー ({today})*"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-today] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

        # 7. 音声版 (mp3) を生成して実行者の DM にアップロード
        from argus.narrate import _post_today_voice
        _post_today_voice(command, today, result)

        logger.info("[argus-today] 完了")

    except Exception as e:
        logger.exception("[argus-today] エラー")
        respond(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Argus 日次サマリー生成エラー: {e}",
                    },
                }
            ],
        )


def _run_transcribe(respond, command):
    """Slack /argus-transcribe・/transcribe のバックグラウンド処理。

    transcribe_pipeline.run_pipeline() を使い、
    ダウンロード → Whisper文字起こし → LLM議事録生成 を実行する。
    進捗はスレッドへの chat_postMessage で可視投稿し、
    完了・エラー通知は respond() で ephemeral 返信する。
    """
    from recording.transcribe_pipeline import run_pipeline as _run_transcribe_pipeline

    text = (command.get("text") or "").strip()

    # `consensus=N` を空白区切りトークンとして抽出（位置不問）。残りをファイル名扱い。
    # 2026-07-26 A/B（盲検2/2で同等以上・コスト1/7）により既定変更。旧構成は consensus=3 で再現可
    consensus_n = 1
    consensus_match = re.search(r"(?:^|\s)consensus=(\d+)(?:\s|$)", text)
    if consensus_match:
        try:
            consensus_n = max(1, int(consensus_match.group(1)))
        except ValueError:
            consensus_n = 1
        text = (text[: consensus_match.start()] + " " + text[consensus_match.end():]).strip()

    filename = text
    # Slack の装飾記法（*bold*, _italic_, `code`, ~strike~）や貼り付け時のゼロ幅/引用符を剥がす
    if filename:
        # 前後の装飾マーカー・引用符を剥がす
        filename = filename.strip("*_`~'\"「」​‌‍﻿")
        # <@U...|name> 形式や <http://...> Slack リンク記法は対象外なのでそのまま
    if filename and not Path(filename).suffix:
        filename += ".m4a"
    channel_id = command.get("channel_id", "")
    thread_ts = None

    if not filename:
        respond(
            text=(
                "ファイル名を指定してください。\n"
                "例: `/argus-transcribe GMT20260302-032528_Recording.mp4`"
            ),
            response_type="ephemeral",
            replace_original=True,
        )
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        respond(
            text=":warning: SLACK_BOT_TOKEN が設定されていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    try:
        from slack_sdk import WebClient
        bot_client = WebClient(token=bot_token)
    except ImportError:
        respond(
            text=":warning: slack_sdk がインストールされていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    try:
        post = bot_client.chat_postMessage(
            channel=channel_id,
            text=f":hourglass_flowing_sand: `{filename}` の処理を開始します...",
        )
        thread_ts = post["ts"]
    except Exception as e:
        respond(
            text=f":warning: Slack メッセージ投稿に失敗しました: {e}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # ジョブ登録
    with _transcribe_lock:
        _transcribe_jobs[thread_ts] = (filename, channel_id)

    try:
        logger.info(f"[argus-transcribe] 開始: filename={filename} channel={channel_id}")
        _run_transcribe_pipeline(bot_client, channel_id, filename, thread_ts, consensus_n=consensus_n)
        logger.info(f"[argus-transcribe] 完了: filename={filename}")
    except Exception as e:
        logger.exception("[argus-transcribe] エラー")
        respond(
            text=f":warning: 議事録生成エラー: {e}",
            response_type="ephemeral",
            replace_original=True,
        )
    finally:
        with _transcribe_lock:
            _transcribe_jobs.pop(thread_ts, None)


# --------------------------------------------------------------------------- #
# CLI モード（--brief-to-canvas / --risk / --dry-run）
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Argus — AI Project Intelligence System CLI"
    )
    parser.add_argument("--brief-to-canvas", action="store_true",
                        help="ブリーフィングを生成して Canvas に投稿")
    parser.add_argument("--risk", action="store_true",
                        help="リスク分析を生成して Canvas に投稿（--dry-run で投稿なし）")
    parser.add_argument("--direction", action="store_true",
                        help="Argus 垂直軸 機能2: 決定クラスタ集約・方向Δレポートを生成して"
                             " Canvas に投稿（--dry-run で投稿なし）")
    parser.add_argument("--canvas-id", default=None, metavar="ID",
                        help="投稿先 Canvas ID（必須）")
    parser.add_argument("--dry-run", action="store_true",
                        help="Canvas 投稿なし・標準出力のみ")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DB を暗号化しない（平文モード）")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="データ収集の開始日（デフォルト: 30日前）")
    parser.add_argument("--days", type=int, default=None, metavar="N",
                        help="直近何日分を対象にするか（デフォルト: 30日。--since と同時指定時は --since 優先）")
    parser.add_argument("--today-only", action="store_true",
                        help="今日のデータのみ収集（--days と --since を無視）")
    parser.add_argument("--assignee", default=None, metavar="NAME",
                        help="担当者フォーカス（例: --assignee 西澤）")
    parser.add_argument("--topic", default=None, metavar="TEXT",
                        help="話題フォーカス（例: --topic Benchpark）")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="pm.db のパス（デフォルト: data/pm.db）")
    parser.add_argument("--index-name", default=None, metavar="NAME",
                        help="argus_config.yaml の indices.{name} を選択して "
                             "channels / minutes / pm_db を絞り込む（例: pm-hpc）。"
                             "省略時は default_index。")
    args = parser.parse_args()

    today = date.today().isoformat()

    if args.today_only:
        # 今日のデータのみ
        days = 0
        since_date = today
    else:
        # 既存のロジック
        days = args.days if args.days is not None else _DEFAULT_SINCE_DAYS
        since_date = args.since or (date.today() - timedelta(days=days)).isoformat()
    pm_db_paths_cli = [Path(args.db)] if args.db else load_pm_db_paths(args.index_name)

    if args.direction:
        # brief/risk と異なり Slack/議事録データは不要（pm.dbのledger構造のみ参照）。
        # _collect_all_data() の重い並列収集をスキップして直接処理する。
        from argus.direction import build_direction_report

        pm_conn = open_pm_db(pm_db_paths_cli[0], no_encrypt=args.no_encrypt)
        print("[INFO] 決定クラスタ集約・方向Δ計算中...", file=sys.stderr)
        result, graph_path = build_direction_report(pm_conn, use_llm_naming=True, include_graph=True)
        canvas_content = f"# Argus 方向Δレポート ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)
        if graph_path:
            print(f"[INFO] グラフ画像: {graph_path}", file=sys.stderr)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)
        return

    print(f"[INFO] since: {since_date} / today: {today} / "
          f"index: {args.index_name or '(default)'}", file=sys.stderr)

    if args.brief_to_canvas:
        if args.today_only:
            # /argus-today と同じ generate_daily_summary_report を使う（外形=Canvasタイトル等は
            # 不変のまま、中身を実際の日次サマリープロンプトへ揃える。2026-07-26 レビュー指摘 C1）。
            # today は assignee/topic フォーカスに未対応（Slack /argus-today と同様）。
            print("[INFO] 日次活動サマリー生成中（全文脈方式。ARGUS_DISABLE_FULLCTX=1で従来方式）...",
                  file=sys.stderr)
            result, _messages = generate_daily_summary_report(
                today, index_name=args.index_name, no_encrypt=args.no_encrypt,
                pm_db_paths=pm_db_paths_cli,
            )
        else:
            print("[INFO] ブリーフィング生成中（全文脈方式。ARGUS_DISABLE_FULLCTX=1で従来方式）...",
                  file=sys.stderr)
            result = generate_brief_report(
                today, since_date, index_name=args.index_name, no_encrypt=args.no_encrypt,
                assignee=args.assignee, topic=args.topic, pm_db_paths=pm_db_paths_cli,
            )

        title = "Argus 日次活動サマリー" if days == 0 else "Argus ブリーフィング"
        canvas_content = f"# {title} ({today})\n\n{result}\n\n_生成: {today} JST_"

        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id or resolve_brief_canvas_id()
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定するか、"
                  "argus_config.yaml の argus_daily.brief_canvas_id を設定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    elif args.risk:
        print("[INFO] リスク分析生成中（全文脈方式。ARGUS_DISABLE_FULLCTX=1で従来方式）...",
              file=sys.stderr)
        result = generate_risk_report(
            today, since_date, index_name=args.index_name, no_encrypt=args.no_encrypt,
            assignee=args.assignee, topic=args.topic, pm_db_paths=pm_db_paths_cli,
        )
        canvas_content = f"# Argus リスク分析 ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id or resolve_risk_canvas_id()
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定するか、"
                  "argus_config.yaml の argus_daily.risk_canvas_id を設定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
