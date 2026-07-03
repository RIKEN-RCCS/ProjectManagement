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
from cli_utils import call_argus_llm, load_claude_md_context
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

from argus.prompts import (  # noqa: F401 — 後方互換のため全プロンプト定数を再 export
    _BRIEF_ORCHESTRATOR_PROMPT,
    _BRIEF_PROMPT,
    _BRIEF_WORKER_CONVERSATION_PROMPT,
    _BRIEF_WORKER_MINUTES_PROMPT,
    _BRIEF_WORKER_PM_PROMPT,
    _DAILY_SUMMARY_PROMPT,
    _DRAFT_AGENDA_PROMPT,
    _DRAFT_REPORT_PROMPT,
    _DRAFT_REQUEST_PROMPT,
    _RISK_ORCHESTRATOR_PROMPT,
    _RISK_PROMPT,
    _RISK_WORKER_CONVERSATION_PROMPT,
    _RISK_WORKER_KNOWLEDGE_PROMPT,
    _RISK_WORKER_MINUTES_PROMPT,
    _RISK_WORKER_PM_PROMPT,
)

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
_WORKER_MAX_CHARS = 8000  # Worker に渡す各セクションの最大文字数
_KNOWLEDGE_MAX_ITEMS_DEFAULT = 30
_KNOWLEDGE_MAX_CHARS = 4000
_MAX_CHARS_PER_CHANNEL = 20000   # 1チャンネルあたりの最大文字数（最新を優先）

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


def _format_period_description(days: int) -> str:
    """日数に応じた期間表示文字列を返す。"""
    if days == 0:
        return "本日のデータ"
    else:
        return f"過去{days}日間のデータ"


def build_brief_prompt(
    messages: str,
    minutes: str,
    stats: dict,
    context: str,
    today: str,
    days: int = _DEFAULT_SINCE_DAYS,
    assignee: str | None = None,
    topic: str | None = None,
    requester: str = "プロジェクトメンバー",
    knowledge_summary: str = "",
) -> str:
    # days == 0 の場合は日次活動サマリープロンプトを使用
    if days == 0:
        return _DAILY_SUMMARY_PROMPT.format(
            today=today,
            context=context,
            knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
            messages=messages or "（本日のメッセージはありません）",
            minutes=minutes or "（本日の議事録はありません）",
        )

    # 既存のロジック（days > 0）
    s = stats["stats"]
    focus_lines = []
    if assignee:
        focus_lines.append(
            f"**担当者フォーカス**: 「{assignee}」に関する事項を特に重点的に分析してください。"
        )
    if topic:
        focus_lines.append(
            f"**話題フォーカス**: 「{topic}」に関連する情報を特に重点的に分析してください。"
        )
    focus_section = ("\n\n## フォーカス指定\n\n" + "\n".join(focus_lines)) if focus_lines else ""

    period_desc = _format_period_description(days)

    prompt = _BRIEF_PROMPT.format(
        today=today,
        period_desc=period_desc,
        context=context,
        knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
        total_open=s["total_open"],
        total_closed=s["total_closed"],
        overdue_count=s["overdue_count"],
        unacknowledged_decisions=s["unacknowledged_decisions"],
        unlinked_count=stats["unlinked_count"],
        no_assignee_count=stats["no_assignee_count"],
        milestone_table=format_milestone_table(stats["milestones"], today),
        overdue_list=format_overdue_list(stats["overdue_items"]),
        assignee_table=format_assignee_table(stats["assignee_workload"]),
        decisions_list=format_decisions_list(stats["unacknowledged_decisions"]),
        weekly_trends=format_trends_table(stats["weekly_trends"]),
        messages=messages or "（データなし）",
        minutes=minutes or "（データなし）",
    )
    if focus_section:
        # 末尾の「上記データを踏まえ...」の前にフォーカスセクションを挿入
        prompt = prompt.replace(
            "\n---\n\n上記データを踏まえ、",
            f"{focus_section}\n\n---\n\n上記データを踏まえ、",
        )
    return prompt


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


def build_risk_prompt(
    messages: str,
    minutes: str,
    stats: dict,
    context: str,
    today: str,
    days: int = _DEFAULT_SINCE_DAYS,
    assignee: str | None = None,
    topic: str | None = None,
    knowledge_summary: str = "",
) -> str:
    s = stats["stats"]
    focus_lines = []
    if assignee:
        focus_lines.append(
            f"**担当者フォーカス**: 「{assignee}」に関するリスクを特に重点的に分析してください。"
        )
    if topic:
        focus_lines.append(
            f"**話題フォーカス**: 「{topic}」に関連するリスクを特に重点的に分析してください。"
        )
    focus_section = ("\n\n## フォーカス指定\n\n" + "\n".join(focus_lines)) if focus_lines else ""

    period_desc = _format_period_description(days)

    prompt = _RISK_PROMPT.format(
        today=today,
        period_desc=period_desc,
        context=context,
        knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
        total_open=s["total_open"],
        total_closed=s["total_closed"],
        overdue_count=s["overdue_count"],
        unacknowledged_decisions=s["unacknowledged_decisions"],
        unlinked_count=stats["unlinked_count"],
        no_assignee_count=stats["no_assignee_count"],
        milestone_table=format_milestone_table(stats["milestones"], today),
        overdue_list=format_overdue_list(stats["overdue_items"], limit=15),
        assignee_table=format_assignee_table(stats["assignee_workload"]),
        decisions_list=format_decisions_list(stats["unacknowledged_decisions"]),
        weekly_trends=format_trends_table(stats["weekly_trends"]),
        messages=messages or "（データなし）",
        minutes=minutes or "（データなし）",
    )
    if focus_section:
        prompt = prompt.replace(
            "\n---\n\n定量データと会話の文脈から、",
            f"{focus_section}\n\n---\n\n定量データと会話の文脈から、",
        )
    return prompt


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
# Slack コマンドのバックグラウンド処理
# --------------------------------------------------------------------------- #

def _run_brief(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-brief のバックグラウンド処理 — single-shot（pm-multi-agent 統合）"""
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

        # データ収集
        context = load_claude_md_context()
        # terminology 動的用語辞書を追記
        try:
            from utils.terminology import build_terminology_reference
            dyn_terms = build_terminology_reference()
            if dyn_terms:
                context = context + dyn_terms
        except Exception:
            pass
        # glossary 構造化テキストを追記
        try:
            from utils.glossary import build_reference as build_glossary_ref
            glossary_ref = build_glossary_ref()
            if glossary_ref:
                context = context + glossary_ref
        except Exception:
            pass
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]

        focus_lines = []
        if assignee:
            focus_lines.append(f"担当者フォーカス: {assignee}")
        if topic:
            focus_lines.append(f"話題フォーカス: {topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        # プロジェクト文脈と全データを1つのプロンプトにまとめて LLM に投げる
        prompt = (
            f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
            f"以下のプロジェクトデータを分析し、ブリーフィングを生成してください。\n"
            f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
            f"必要に応じて使い、多角的な視点から分析してください。\n\n"
            f"## プロジェクト文脈\n\n{context}\n\n"
            f"## フォーカス指定\n\n{focus_section_str}\n\n"
            f"## pm.db 統計\n\n{stats_section}\n\n"
            f"## Slack 会話\n\n{conversation_section}\n\n"
            f"## 議事録\n\n{minutes_section}\n\n"
            f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
            f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
            f"## 指示\n\n"
            f"- 上記のデータを統合し、優先順位を付けたブリーフィングを生成してください\n"
            f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
            f"- 回答の長さに制限はありません。必要なだけ詳しく説明してください\n"
            f"- `<final_answer>` タグで回答を囲んでください\n"
        )
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。", max_tokens=32768, timeout=600)

        # <final_answer> タグがあれば抽出
        final = re.search(r"<final_answer>(.*?)</final_answer>", result, re.DOTALL)
        if final:
            result = final.group(1).strip()
        else:
            result = re.sub(r"<[^>]+>", "", result).strip()

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


def _run_brief_worker(worker_type: str, data: str) -> str:
    """ブリーフィング Worker を実行する（ThreadPoolExecutor 用）。"""
    prompt_map = {
        "pm": _BRIEF_WORKER_PM_PROMPT,
        "conversation": _BRIEF_WORKER_CONVERSATION_PROMPT,
        "minutes": _BRIEF_WORKER_MINUTES_PROMPT,
    }
    tmpl = prompt_map.get(worker_type)
    if not tmpl:
        return f"（不明な Worker: {worker_type}）"
    prompt = tmpl.format(
        stats_section=data,
        conversation_section=data,
        minutes_section=data,
    )
    return call_argus_llm(prompt, system="あなたはAIエージェントです。与えられたデータからアクション候補を抽出してください。", max_tokens=4096)


def _run_draft(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-draft のバックグラウンド処理"""
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

        context = load_claude_md_context()
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        # report 用途では build_draft_prompt が pm.db への接続を必要とする
        # (完了アイテム取得)。他の用途では None で良い。
        conns = None
        if purpose == "report":
            conns = [open_pm_db(p, no_encrypt=no_encrypt)
                     for p in load_pm_db_paths(index_name)]
        try:
            prompt = build_draft_prompt(purpose, subject, messages, stats, context,
                                        conns=conns, today=today)
        finally:
            if conns:
                for c in conns:
                    c.close()
        logger.info("[argus-draft] LLM 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
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
    """Slack /argus-risk のバックグラウンド処理 — single-shot（pm-multi-agent 統合）"""
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

        # データ収集
        logger.info("[argus-risk] データ収集")
        context = load_claude_md_context()
        # terminology 動的用語辞書を追記
        try:
            from utils.terminology import build_terminology_reference
            dyn_terms = build_terminology_reference()
            if dyn_terms:
                context = context + dyn_terms
        except Exception:
            pass
        # glossary 構造化テキストを追記
        try:
            from utils.glossary import build_reference as build_glossary_ref
            glossary_ref = build_glossary_ref()
            if glossary_ref:
                context = context + glossary_ref
        except Exception:
            pass
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]

        focus_lines = []
        if assignee:
            focus_lines.append(f"担当者フォーカス: {assignee}")
        if topic:
            focus_lines.append(f"話題フォーカス: {topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        # プロジェクト文脈と全データを1つのプロンプトにまとめて LLM に投げる
        prompt = (
            f"あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。\n"
            f"以下のプロジェクトデータを分析し、リスク分析レポートを生成してください。\n"
            f"利用可能なツール（search_text / search_decisions / search_entity 等）を\n"
            f"必要に応じて使い、多角的な視点からリスクを洗い出してください。\n\n"
            f"## プロジェクト文脈\n\n{context}\n\n"
            f"## フォーカス指定\n\n{focus_section_str}\n\n"
            f"## pm.db 統計\n\n{stats_section}\n\n"
            f"## Slack 会話\n\n{conversation_section}\n\n"
            f"## 議事録\n\n{minutes_section}\n\n"
            f"## 確定済みナレッジ\n\n{knowledge_summary or '（なし）'}\n\n"
            f"## 外部記事\n\n{web_articles or '（なし）'}\n\n"
            f"## 指示\n\n"
            f"- 上記のデータからリスク・懸念・予兆を洗い出し、優先度付きで報告してください\n"
            f"- 数値・決定事項ID・担当者名など具体的根拠を引用すること\n"
            f"- リスクは「顕在化しているリスク」と「放置すると問題になりうる予兆」に分けて記載\n"
            f"- 回答の長さに制限はありません。必要なだけ詳しく説明してください\n"
            f"- `<final_answer>` タグで回答を囲んでください\n"
        )
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。", max_tokens=32768, timeout=600)

        # <final_answer> タグがあれば抽出
        final = re.search(r"<final_answer>(.*?)</final_answer>", result, re.DOTALL)
        if final:
            result = final.group(1).strip()
        else:
            result = re.sub(r"<[^>]+>", "", result).strip()

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
    logger = logging.getLogger("pm_argus")
    try:
        from argus.direction import build_direction_report

        index_name = resolve_index_name(command.get("channel_id") or None)
        pm_db_paths = load_pm_db_paths(index_name)
        pm_conn = open_pm_db(pm_db_paths[0], no_encrypt=no_encrypt)

        logger.info("[argus-direction] レポート生成中")
        result = build_direction_report(pm_conn, use_llm_naming=True)

        today = date.today().isoformat()
        header = f"*Argus 方向Δレポート ({today})*"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-direction] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)
        logger.info("[argus-direction] 完了")
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


def _run_risk_worker(worker_type: str, data: str) -> str:
    """リスク Worker を実行する（ThreadPoolExecutor 用）。
    worker_type: 'pm' / 'conversation' / 'minutes' / 'knowledge'
    """
    prompt_map = {
        "pm": _RISK_WORKER_PM_PROMPT,
        "conversation": _RISK_WORKER_CONVERSATION_PROMPT,
        "minutes": _RISK_WORKER_MINUTES_PROMPT,
        "knowledge": _RISK_WORKER_KNOWLEDGE_PROMPT,
    }
    tmpl = prompt_map.get(worker_type)
    if not tmpl:
        return f"（不明な Worker: {worker_type}）"
    prompt = tmpl.format(
        stats_section=data,
        conversation_section=data,
        minutes_section=data,
        knowledge_section=data,
    )
    return call_argus_llm(prompt, system="あなたはAIエージェントです。与えられたデータからリスクを分析してください。", max_tokens=4096)


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
        user_id: 実行者の Slack user_id (例: "U08MWC731GR")
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

        # 2. 今日のデータを収集
        today = date.today().isoformat()
        since_date = today  # --today-only 相当
        days = 0

        index_name = resolve_index_name(command.get("channel_id") or None)
        logger.info(f"[argus-today] requester={requester} user_id={user_id} index={index_name}")

        context = load_claude_md_context()
        messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
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
                    result = conn.execute(
                        "SELECT user_name FROM messages WHERE user_id = ?"
                        " AND user_name IS NOT NULL AND user_name != ? AND user_name NOT LIKE 'U0%' LIMIT 1",
                        (uid, uid),
                    ).fetchone()
                    if result and result[0]:
                        user_id_map[uid] = result[0]
                conn.close()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[argus-today] ユーザーIDマップ構築失敗: {e}")

        # 4. メンション抽出 (argus_config.yaml + _CHANNEL_NAMES から取得)
        channel_names = _build_channel_name_map()
        _, mention_section = _filter_mentions_for_user(messages, user_name, user_id, channel_names, user_id_map)

        # 5. プロンプト構築
        prompt = build_brief_prompt(
            messages, minutes, stats, context, today, days,
            assignee=None, topic=None, requester=requester,
            knowledge_summary=knowledge_summary,
        )

        # 6. LLM呼び出し (日次サマリープロンプト使用)
        logger.info("[argus-today] LLM 呼び出し中...")
        result = call_argus_llm(
            prompt,
            system="あなたはAIインテリジェンスシステムArgusです。",
        )

        # 7. メンションセクションを追加
        if mention_section:
            result += f"\n\n---\n\n{mention_section}"

        # 8. ephemeral 応答 (Block Kit で mrkdwn 有効化)
        header = f":memo: *Argus 今日の活動サマリー ({today})*"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-today] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

        # 9. 音声版 (mp3) を生成して実行者の DM にアップロード
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
    consensus_n = 3
    consensus_match = re.search(r"(?:^|\s)consensus=(\d+)(?:\s|$)", text)
    if consensus_match:
        try:
            consensus_n = max(1, int(consensus_match.group(1)))
        except ValueError:
            consensus_n = 3
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
        result = build_direction_report(pm_conn, use_llm_naming=True)
        canvas_content = f"# Argus 方向Δレポート ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

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

    context = load_claude_md_context()
    # terminology 動的用語辞書を追記
    try:
        from utils.terminology import build_terminology_reference
        dyn_terms = build_terminology_reference()
        if dyn_terms:
            context = context + dyn_terms
    except Exception:
        pass
    # glossary 構造化テキストを追記
    try:
        from utils.glossary import build_reference as build_glossary_ref
        glossary_ref = build_glossary_ref()
        if glossary_ref:
            context = context + glossary_ref
    except Exception:
        pass
    print(f"[INFO] since: {since_date} / today: {today} / "
          f"index: {args.index_name or '(default)'}", file=sys.stderr)

    messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
        today, since_date,
        no_encrypt=args.no_encrypt,
        pm_db_paths=pm_db_paths_cli,
        index_name=args.index_name,
    )

    if args.brief_to_canvas:
        # マルチWorker + Orchestrator で生成
        print("[INFO] 多視点 Worker でブリーフィング生成中...", file=sys.stderr)
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        focus_lines = []
        if args.assignee:
            focus_lines.append(f"担当者フォーカス: {args.assignee}")
        if args.topic:
            focus_lines.append(f"話題フォーカス: {args.topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        worker_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            wfuts = {
                pool.submit(_run_brief_worker, "pm", stats_section): "pm",
                pool.submit(_run_brief_worker, "conversation", conversation_section): "conversation",
                pool.submit(_run_brief_worker, "minutes", minutes_section): "minutes",
            }
            for f in concurrent.futures.as_completed(wfuts):
                name = wfuts[f]
                try:
                    worker_results[name] = f.result()
                except Exception as e:
                    worker_results[name] = f"（{name} Worker エラー: {e}）"
                    print(f"[WARN] Worker {name} 失敗: {e}", file=sys.stderr)

        orch_prompt = _BRIEF_ORCHESTRATOR_PROMPT.format(
            context=context,
            knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
        )
        print("[INFO] Orchestrator 統合中...", file=sys.stderr)
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")

        title = "Argus 日次活動サマリー" if days == 0 else "Argus ブリーフィング"
        canvas_content = f"# {title} ({today})\n\n{result}\n\n_生成: {today} JST_"

        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

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

    elif args.risk:
        # マルチWorker + Orchestrator で生成
        print("[INFO] 多視点 Worker でリスク分析生成中...", file=sys.stderr)
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        knowledge_section = knowledge_summary or "（蒸留ナレッジなし）"
        focus_lines = []
        if args.assignee:
            focus_lines.append(f"担当者フォーカス: {args.assignee}")
        if args.topic:
            focus_lines.append(f"話題フォーカス: {args.topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        worker_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            wfuts = {
                pool.submit(_run_risk_worker, "pm", stats_section): "pm",
                pool.submit(_run_risk_worker, "conversation", conversation_section): "conversation",
                pool.submit(_run_risk_worker, "minutes", minutes_section): "minutes",
                pool.submit(_run_risk_worker, "knowledge", knowledge_section): "knowledge",
            }
            for f in concurrent.futures.as_completed(wfuts):
                name = wfuts[f]
                try:
                    worker_results[name] = f.result()
                except Exception as e:
                    worker_results[name] = f"（{name} Worker エラー: {e}）"
                    print(f"[WARN] Worker {name} 失敗: {e}", file=sys.stderr)

        orch_prompt = _RISK_ORCHESTRATOR_PROMPT.format(
            context=context,
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
            worker_knowledge=worker_results.get("knowledge", "（エラー）"),
        )
        print("[INFO] Orchestrator 統合中...", file=sys.stderr)
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        canvas_content = f"# Argus リスク分析 ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

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

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
