"""narrate.py — Argus TTS/ナレーション動画生成層

pm_argus.py から分離。PPTX/PDF スライドに音声ナレーションを付けた mp4 を生成し
Slack に投稿する機能群。brief/risk Orchestrator とは完全に独立した関心事。

公開 API:
    _NarrateSession  — セッション状態 dataclass
    _narrate_sessions, _narrate_lock  — セッション管理グローバル
    _run_narrate, _run_narrate_build, _run_narrate_cancel  — Slack ハンドラ
    _post_argus_voice, _post_argus_video  — Slack 投稿ヘルパ
    _post_today_voice  — today コマンド用音声投稿
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

import concurrent.futures
import argparse
import yaml
from dataclasses import dataclass, field  # already imported above, kept for clarity
from datetime import date, timedelta
from typing import Any

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import (
    open_db, open_pm_db,
    fetch_milestone_progress, fetch_assignee_workload,
    fetch_overdue_items, fetch_unacknowledged_decisions,
    fetch_unlinked_items_count, fetch_no_assignee_count,
    fetch_weekly_trends, fetch_summary_stats,
)
from format_utils import (
    format_milestone_table, format_overdue_list, format_assignee_table,
    format_weekly_trends as format_trends_table, format_decisions_list,
)
from utils.slack_post import _to_slack_mrkdwn, _split_mrkdwn_to_blocks, _SLACK_SECTION_LIMIT
from argus.prompts import (
    _BRIEF_PROMPT, _DAILY_SUMMARY_PROMPT,
    _DRAFT_AGENDA_PROMPT, _DRAFT_REPORT_PROMPT, _DRAFT_REQUEST_PROMPT,
    _RISK_PROMPT,
    _RISK_WORKER_PM_PROMPT, _RISK_WORKER_CONVERSATION_PROMPT,
    _RISK_WORKER_MINUTES_PROMPT, _RISK_WORKER_KNOWLEDGE_PROMPT,
    _RISK_ORCHESTRATOR_PROMPT,
    _BRIEF_WORKER_PM_PROMPT, _BRIEF_WORKER_CONVERSATION_PROMPT,
    _BRIEF_WORKER_MINUTES_PROMPT, _BRIEF_ORCHESTRATOR_PROMPT,
)

logger = logging.getLogger("pm_argus")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_ARGUS_CONFIG_FILE = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_FILE_LEGACY = _DATA_DIR / "qa_config.yaml"

_DEFAULT_SINCE_DAYS = 30
_DRAFT_REPORT_SINCE_DAYS = 14
_WORKER_MAX_CHARS = 8000

# /argus-transcribe ジョブ排他制御
_transcribe_jobs: dict[str, tuple[str, str]] = {}  # thread_ts → (filename, channel_id)
_transcribe_lock = threading.Lock()

class _NarrateSession:
    thread_ts: str
    channel_id: str
    filename: str
    work_dir: Path
    slides: list = field(default_factory=list)        # list[build_slide_video.Slide]
    narrations: list[str] = field(default_factory=list)  # 現在採用中の narration (各 build 後に更新)
    lang: str = "ja"
    command: dict = field(default_factory=dict)        # _post_argus_video に渡す元 command
    phase: str = "draft"                                # "draft" | "rendering"
    iteration: int = 0                                  # 何回目の build か (build 成功時に +1)


_narrate_sessions: dict[str, _NarrateSession] = {}     # thread_ts → session
_narrate_lock = threading.Lock()

from recording.transcribe_pipeline import run_pipeline as _run_transcribe_pipeline


# --------------------------------------------------------------------------- #
# データ収集
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



_MAX_CHARS_PER_CHANNEL = 20000   # 1チャンネルあたりの最大文字数（最新を優先）


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


# プロンプトに同梱する蒸留ナレッジの上限
_KNOWLEDGE_MAX_ITEMS_DEFAULT = 30
_KNOWLEDGE_MAX_CHARS = 4000


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


def fetch_pm_stats(conn, today: str, since: str | None = None) -> dict:
    """pm.db から統計データを収集する"""
    return {
        "milestones": fetch_milestone_progress(conn),
        "overdue_items": fetch_overdue_items(conn, today, since),
        "assignee_workload": fetch_assignee_workload(conn, today),
        "unlinked_count": fetch_unlinked_items_count(conn, since),
        "no_assignee_count": fetch_no_assignee_count(conn, since),
        "weekly_trends": fetch_weekly_trends(conn),
        "unacknowledged_decisions": fetch_unacknowledged_decisions(conn, since),
        "stats": fetch_summary_stats(conn, since, today),
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
    import re
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
# Slack コマンドのバックグラウンド処理
# --------------------------------------------------------------------------- #

def _fetch_single_pm_stats(p: Path, today: str, since_date: str, no_encrypt: bool) -> dict:
    """単一 pm.db から stats を取得（ThreadPoolExecutor 用）。
    コネクションはスレッド内で閉じて結果だけ返す。"""
    conn = open_pm_db(p, no_encrypt=no_encrypt)
    try:
        stats = fetch_pm_stats(conn, today, since=since_date)
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
) -> tuple[str, str, dict, list, str]:
    """messages/minutes/stats/knowledge を一括収集し
    (messages, minutes, stats, conns, knowledge_summary) を返す。
    conns は呼び出し元で全てクローズすること。
    knowledge_summary は data/knowledge.db から取得した蒸留サマリ（プロジェクト全体共通、
    index_name の影響を受けない）。

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

        # pm.db stats: 全 pm.db を並列 fetch
        pm_futs = []
        for p in pm_db_paths:
            f = pool.submit(_fetch_single_pm_stats, p, today, since_date, no_encrypt)
            pm_futs.append(f)

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
        stats = merge_pm_stats(stats_list)

        # knowledge 結果
        try:
            knowledge_summary = kn_fut.result()
        except Exception:
            knowledge_summary = ""

    return messages, minutes, stats, knowledge_summary


def _run_brief(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-brief のバックグラウンド処理 — マルチWorker + Orchestrator 版"""
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

        # Phase 1: 並列データ収集
        context = load_claude_md_context()
        messages, minutes, stats, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        # Phase 2: 多視点 Worker の並列 LLM 呼び出し
        logger.info("[argus-brief] Worker LLM 呼び出し（並列）")
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
                    logger.warning(f"[argus-brief] Worker {name} 失敗: {e}")

        # Phase 3: Orchestrator 統合
        logger.info("[argus-brief] Orchestrator 統合")
        orch_prompt = _BRIEF_ORCHESTRATOR_PROMPT.format(
            context=context,
            knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
        )
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")

        header = f"*Argus ブリーフィング ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-brief] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

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
        messages, minutes, stats, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        prompt = build_draft_prompt(purpose, subject, messages, stats, context, conns=conns, today=today)
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
    """Slack /argus-risk のバックグラウンド処理 — マルチWorker + Orchestrator 版"""
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

        # Phase 1: 並列データ収集
        logger.info("[argus-risk] データ収集（並列）")
        context = load_claude_md_context()
        messages, minutes, stats, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        # Phase 2: 多視点 Worker の並列 LLM 呼び出し
        logger.info("[argus-risk] Worker LLM 呼び出し（並列）")

        # 各 Worker の入力データを構築
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        knowledge_section = knowledge_summary or "（蒸留ナレッジなし）"

        # フォーカス指定
        focus_lines = []
        if assignee:
            focus_lines.append(f"担当者フォーカス: {assignee}")
        if topic:
            focus_lines.append(f"話題フォーカス: {topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        # Worker を並列実行
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
                    logger.warning(f"[argus-risk] Worker {name} 失敗: {e}")

        # Phase 3: Orchestrator 統合
        logger.info("[argus-risk] Orchestrator 統合")
        orch_prompt = _RISK_ORCHESTRATOR_PROMPT.format(
            context=context,
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
            worker_knowledge=worker_results.get("knowledge", "（エラー）"),
        )
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")

        header = f"*Argus リスク分析 ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-risk] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)

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
            from argus.pm_qa_server import _CHANNEL_NAMES
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


def _post_argus_voice(
    command: dict,
    *,
    kind: str,
    today: str,
    result_md: str,
    summarize_mode: str = "auto",
    title: str,
    enable_env: str = "",
) -> None:
    """argus-* コマンドの本文を音声合成し、コマンドを叩いたチャンネルにアップロードする。

    DM (conversations_open) に投稿すると Slack 上は "App" セクションに隔離され
    視認性が悪いため、コマンドを実行したチャンネル (command.channel_id) に
    chat_postMessage / files_upload_v2 でそのまま投稿する。
    テキスト本文は ephemeral だが、音声 mp3 はチャンネル全員に見える点に注意。

    失敗してもコマンド全体を落とさないよう例外は内側で握りつぶす。
    """
    import logging
    logger = logging.getLogger("pm_argus")

    if enable_env and os.environ.get(enable_env, "1") == "0":
        logger.info(f"[argus-{kind}] voice: {enable_env}=0 によりスキップ")
        return

    user_id = command.get("user_id") or ""
    channel_id = command.get("channel_id") or ""
    if not channel_id:
        logger.warning(f"[argus-{kind}] voice: channel_id 不明、スキップ")
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning(f"[argus-{kind}] voice: SLACK_BOT_TOKEN 未設定、スキップ")
        return

    try:
        import pm_tts
        from slack_sdk import WebClient
    except ImportError as exc:
        logger.warning(f"[argus-{kind}] voice: import 失敗 ({exc})、スキップ")
        return

    mp3_path = Path(f"/tmp/argus_{kind}_{today}_{user_id or 'anon'}.mp3")
    speaker_id = pm_tts.DEFAULT_SPEAKER
    try:
        logger.info(f"[argus-{kind}] voice: 要約・合成を開始 mode={summarize_mode}")
        pm_tts.synthesize_markdown(
            result_md,
            mp3_path,
            speaker=speaker_id,
            summarize=True,
            summarize_mode=summarize_mode,
            quiet=True,
        )
        logger.info(f"[argus-{kind}] voice: mp3 生成完了 size={mp3_path.stat().st_size} bytes")

        client = WebClient(token=bot_token)
        credit = pm_tts.credit_line(speaker_id)
        initial_comment = (
            ":sound: 音声版（要約・短縮）です。\n"
            f"_{credit}_\n"
            "削除する場合はこのメッセージに :wastebasket: リアクションを付けてください。"
        )

        upload_resp = client.files_upload_v2(
            channel=channel_id,
            file=str(mp3_path),
            filename=mp3_path.name,
            title=title,
            initial_comment=initial_comment,
        )

        # アップロード履歴を記録（reaction_added の対象判定に使う）
        try:
            import voice_uploads
            file_id, message_ts = _extract_share_ts(upload_resp, channel_id)
            if file_id and not message_ts:
                import time as _time
                for delay in (0.5, 1.0, 2.0):
                    _time.sleep(delay)
                    try:
                        info = client.files_info(file=file_id)
                    except Exception as exc:
                        logger.debug(f"[argus-{kind}] voice: files_info 失敗 {exc}")
                        continue
                    file_obj = info.get("file") or {}
                    _, message_ts = _extract_share_ts({"files": [file_obj]}, channel_id)
                    if message_ts:
                        break
            if file_id and message_ts:
                voice_uploads.record_upload(
                    message_ts=message_ts,
                    channel_id=channel_id,
                    file_id=file_id,
                    user_id=user_id,
                    kind=kind,
                    title=title,
                )
                logger.info(f"[argus-{kind}] voice: 履歴記録 file_id={file_id} ts={message_ts}")
            else:
                logger.warning(
                    f"[argus-{kind}] voice: file_id / message_ts を取得できず履歴未記録 "
                    f"(file_id={file_id!r} message_ts={message_ts!r})"
                )
        except Exception as exc:
            logger.warning(f"[argus-{kind}] voice: 履歴記録失敗 {exc}")

        logger.info(f"[argus-{kind}] voice: チャンネルアップロード完了 ch={channel_id}")
    except Exception as exc:
        logger.exception(f"[argus-{kind}] voice: 失敗 {exc}")
    finally:
        try:
            if mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass


def _post_argus_video(
    command: dict,
    *,
    kind: str,
    mp4_path: Path,
    title: str,
    initial_comment: str,
) -> None:
    """argus-* コマンドが生成した mp4 をコマンド実行チャンネルに投稿する。

    _post_argus_voice の動画版。voice_uploads.db に kind を記録し、
    :wastebasket: リアクションや /argus-delete スレッド一括削除の対象にする。
    """
    user_id = command.get("user_id") or ""
    channel_id = command.get("channel_id") or ""
    if not channel_id:
        logger.warning(f"[argus-{kind}] video: channel_id 不明、スキップ")
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning(f"[argus-{kind}] video: SLACK_BOT_TOKEN 未設定、スキップ")
        return

    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        logger.warning(f"[argus-{kind}] video: import 失敗 ({exc})、スキップ")
        return

    try:
        client = WebClient(token=bot_token)
        upload_resp = client.files_upload_v2(
            channel=channel_id,
            file=str(mp4_path),
            filename=mp4_path.name,
            title=title,
            initial_comment=initial_comment,
        )

        try:
            import voice_uploads
            file_id, message_ts = _extract_share_ts(upload_resp, channel_id)
            # files_upload_v2 のレスポンスに shares が入らないことがあるため、
            # file_id だけ取れて message_ts が空なら files_info でフォールバック取得。
            # それでも取れない場合 (shares 反映が遅延) は再試行してから諦める。
            if file_id and not message_ts:
                import time as _time
                for delay in (0.5, 1.0, 2.0):
                    _time.sleep(delay)
                    try:
                        info = client.files_info(file=file_id)
                    except Exception as exc:
                        logger.debug(f"[argus-{kind}] video: files_info 失敗 {exc}")
                        continue
                    file_obj = info.get("file") or {}
                    _, message_ts = _extract_share_ts({"files": [file_obj]}, channel_id)
                    if message_ts:
                        break
            if file_id and message_ts:
                voice_uploads.record_upload(
                    message_ts=message_ts,
                    channel_id=channel_id,
                    file_id=file_id,
                    user_id=user_id,
                    kind=kind,
                    title=title,
                )
                logger.info(f"[argus-{kind}] video: 履歴記録 file_id={file_id} ts={message_ts}")
            else:
                logger.warning(
                    f"[argus-{kind}] video: file_id / message_ts を取得できず履歴未記録 "
                    f"(file_id={file_id!r} message_ts={message_ts!r})"
                )
        except Exception as exc:
            logger.warning(f"[argus-{kind}] video: 履歴記録失敗 {exc}")

        logger.info(f"[argus-{kind}] video: チャンネルアップロード完了 ch={channel_id}")
    except Exception as exc:
        logger.exception(f"[argus-{kind}] video: 失敗 {exc}")


def _extract_share_ts(upload_resp: dict, channel_id: str) -> tuple[str, str]:
    """files_upload_v2 / files_info のレスポンスから (file_id, message_ts) を取り出す。

    取れない場合は空文字を返す。message_ts は files.shares.{public,private}.{channel_id}
    にあるため、レスポンスの形が違っても両方を試す。
    """
    file_id = ""
    message_ts = ""
    files_field = upload_resp.get("files") or []
    if not files_field:
        return file_id, message_ts
    f0 = files_field[0]
    file_id = f0.get("id") or f0.get("file", {}).get("id", "")
    shares = f0.get("shares") or {}
    for visibility in ("public", "private"):
        visible = shares.get(visibility) or {}
        if channel_id in visible:
            ts_list = visible[channel_id]
            if ts_list:
                message_ts = ts_list[0].get("ts", "")
                if message_ts:
                    return file_id, message_ts
    return file_id, message_ts


def _post_today_voice(command: dict, today: str, result_md: str) -> None:
    """argus-today 用の薄いラッパ（後方互換）。"""
    _post_argus_voice(
        command,
        kind="today",
        today=today,
        result_md=result_md,
        summarize_mode="auto",
        title=f"Argus 今日の活動サマリー (音声版) {today}",
        enable_env="ARGUS_TODAY_VOICE",
    )


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
        messages, minutes, stats, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )
                # 3. ユーザーIDマップを構築（テキスト内のID展開用）
        # 優先順位: argus_config.yaml の user_names: > slack.db の messages.user_name
        import re
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
# /argus-narrate: PPTX/PDF → 要約読み上げ付き mp4
# Phase1: 原稿生成 → スレッドに投稿（ユーザーが修正できるようにする）
# Phase2: 「動画を生成」ボタン押下 → 最新原稿で TTS + mp4 化
# --------------------------------------------------------------------------- #

# ハイフン (-), em-dash (—), en-dash (–) いずれも許容。
# 「スライド」「Slide」「slide」など日英どちらの表記でも拾う。
_NARRATE_SLIDE_HEADER_RE = re.compile(
    r"^\s*[-—–]{2,}\s*(?:スライド|slide)\s*(\d+)\s*[-—–]{2,}\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _format_narration_message(narrations: list[str], lang: str = "ja") -> str:
    """narration 一覧を「--- スライド N ---」または「--- Slide N ---」見出し付きに整形。"""
    label = "Slide" if lang == "en" else "スライド"
    parts: list[str] = []
    for i, narration in enumerate(narrations, 1):
        parts.append(f"--- {label} {i} ---\n{narration.strip()}")
    return "\n\n".join(parts)


def _parse_narration_message(
    text: str, expected_count: int, originals: list[str],
) -> tuple[list[str], int] | None:
    """ユーザー返信から修正済み narration を抽出して originals にマージする。

    - 見出しは日英・ダッシュ種別を許容（_NARRATE_SLIDE_HEADER_RE 参照）
    - 一部スライドだけの編集も許容（残りは originals のまま）
    - 有効な編集が 1 件以上あれば (merged_narrations, edited_count) を返す
    - 1 件もマッチしなければ None
    """
    if not text:
        return None
    matches = list(_NARRATE_SLIDE_HEADER_RE.finditer(text))
    if not matches:
        return None
    found: dict[int, str] = {}
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        if idx < 1 or idx > expected_count:
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            found[idx] = body
    if not found:
        return None
    merged = list(originals)
    for idx, body in found.items():
        merged[idx - 1] = body
    return merged, len(found)


def _narrate_action_blocks(thread_ts: str) -> list[dict]:
    """「動画を生成」「キャンセル」ボタンの Block Kit ペイロード。"""
    return [
        {
            "type": "actions",
            "block_id": f"argus_narrate_actions_{thread_ts}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "argus_narrate_build",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": ":movie_camera: 動画を生成"},
                    "value": thread_ts,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "動画を生成しますか?"},
                        "text": {"type": "mrkdwn", "text": "現在の原稿（修正があればスレッド最新返信）で TTS + mp4 を作ります。"},
                        "confirm": {"type": "plain_text", "text": "生成する"},
                        "deny": {"type": "plain_text", "text": "戻る"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "argus_narrate_cancel",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "終了"},
                    "value": thread_ts,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "編集セッションを終了しますか?"},
                        "text": {"type": "mrkdwn", "text": "セッションを閉じ、中間ファイル (work_dir) を削除します。投稿済みの動画はそのまま残ります。"},
                        "confirm": {"type": "plain_text", "text": "終了する"},
                        "deny": {"type": "plain_text", "text": "戻る"},
                    },
                },
            ],
        },
    ]


def _run_narrate(respond, command):
    """Slack /argus-narrate の Phase1 (原稿生成 → スレッド投稿)。

    アップロード済み PPTX/PDF を取得して LLM で各スライドの narration を生成し、
    その原稿をコマンド実行チャンネルのスレッドに投稿する。ユーザーが原稿を
    確認・必要なら同形式でスレッド返信して修正したのち、「動画を生成」ボタンを
    押すと _run_narrate_build (Phase2) に進む。
    """
    text = (command.get("text") or "").strip()
    text = text.strip("*_`~'\"「」​‌‍﻿")

    lang = "ja"
    lang_m = re.search(r"--lang\s+(ja|en)\b", text)
    if lang_m:
        lang = lang_m.group(1)
        text = re.sub(r"--lang\s+(ja|en)\b", "", text).strip()

    filename = text
    if filename and not Path(filename).suffix:
        filename += ".pptx"
    channel_id = command.get("channel_id", "")

    if not filename:
        respond(
            text=(
                "ファイル名を指定してください。\n"
                "例: `/argus-narrate slides.pptx` / `/argus-narrate handout.pdf`\n"
                "英語ナレーションにする場合は `--lang en` を付けてください。\n"
                "例: `/argus-narrate slides.pptx --lang en`"
            ),
            response_type="ephemeral",
            replace_original=True,
        )
        return

    suffix = Path(filename).suffix.lower()
    if suffix not in (".pptx", ".pdf"):
        respond(
            text=f":warning: 対応形式は .pptx / .pdf です（指定: `{filename}`）",
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
        listing = bot_client.files_list(channel=channel_id, types="all")
        files = listing.get("files", [])
    except Exception as exc:
        respond(
            text=f":warning: files_list 失敗: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    matched = [f for f in files if f.get("name") == filename]
    if not matched:
        respond(
            text=f":warning: `{filename}` がこのチャンネルに見つかりません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    url = matched[0].get("url_private_download")
    if not url:
        respond(
            text=f":warning: `{filename}` のダウンロードURLが取得できません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # スレッド親メッセージは「原稿レビュー用ヘッダ」1 行のみ。
    # 進捗は chat_update で上書きしてチャンネルへの追加投稿を増やさない。
    try:
        progress = bot_client.chat_postMessage(
            channel=channel_id,
            text=f":scroll: `{filename}` のナレーション原稿（生成中）",
        )
        thread_ts = progress["ts"]
    except Exception as exc:
        respond(
            text=f":warning: Slack 投稿失敗: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # 原稿生成中はセッション登録だけ先行 (Phase2 と排他)
    work_dir = Path(tempfile.mkdtemp(prefix="argus_narrate_"))
    session = _NarrateSession(
        thread_ts=thread_ts, channel_id=channel_id, filename=filename,
        work_dir=work_dir, lang=lang, command=dict(command), phase="draft",
    )
    with _narrate_lock:
        _narrate_sessions[thread_ts] = session

    try:
        import requests as _req
        src_path = work_dir / filename
        dl = _req.get(
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            stream=True, timeout=300,
        )
        dl.raise_for_status()
        with open(src_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)

        try:
            from build_slide_video import prepare_slides, summarize_all_slides
        except ImportError as exc:
            raise RuntimeError(f"build_slide_video の import 失敗: {exc}")

        logger.info(f"[argus-narrate] Phase1 開始: {filename}")
        slides = prepare_slides(src_path, work_dir)
        narrations = summarize_all_slides(slides, lang=lang, quiet=True)
        logger.info(f"[argus-narrate] 原稿生成完了: {len(narrations)} 枚")

        session.slides = slides
        session.narrations = narrations

        narration_text = _format_narration_message(narrations, lang=lang)
        # 1) スレッド: 原稿本文（修正は同形式で全件返信、ガイド文は親へ）
        bot_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=narration_text,
        )
        # 2) スレッド: 動画生成 / キャンセル ボタンのみ（テキストは fallback）
        bot_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text="動画を生成しますか?",
            blocks=_narrate_action_blocks(thread_ts),
        )
        # 3) チャンネル親メッセージを更新（追加投稿はしない）
        try:
            bot_client.chat_update(
                channel=channel_id, ts=thread_ts,
                text=(
                    f":scroll: `{filename}` のナレーション原稿（{len(narrations)} 枚）— "
                    "スレッドの原稿を確認し、修正したいスライドだけ同形式で返信、"
                    "「動画を生成」で何度でも作り直せます（編集を終えたら「終了」）"
                ),
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[argus-narrate] Phase1 エラー")
        try:
            bot_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f":warning: 原稿生成に失敗しました: {exc}",
            )
        except Exception:
            pass
        respond(
            text=f":warning: 原稿生成エラー: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        # 失敗時はセッションごと破棄
        with _narrate_lock:
            _narrate_sessions.pop(thread_ts, None)
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_narrate_build(thread_ts: str, user_id: str) -> None:
    """Phase2: スレッドの最新返信を読み込み、修正済み narration で動画を生成。

    ボタンハンドラ (pm_qa_server.py) から呼ばれる。Slack への通知も内部で完結する。
    """
    with _narrate_lock:
        session = _narrate_sessions.get(thread_ts)
        if session is None:
            return
        if session.phase != "draft":
            return  # 多重押下を無視
        session.phase = "rendering"

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning("[argus-narrate] Phase2: SLACK_BOT_TOKEN 未設定")
        return
    try:
        from slack_sdk import WebClient
        bot_client = WebClient(token=bot_token)
    except ImportError:
        logger.warning("[argus-narrate] Phase2: slack_sdk なし")
        return

    channel_id = session.channel_id
    filename = session.filename
    expected = len(session.slides)

    # スレッドから最新の修正済み narration を拾う
    final_narrations = list(session.narrations)
    edited = False
    edited_count = 0
    try:
        replies = bot_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
        bot_user_id = None
        try:
            auth = bot_client.auth_test()
            bot_user_id = auth.get("user_id")
        except Exception:
            pass
        candidate_msgs: list[dict] = []
        for msg in reversed(replies.get("messages", []) or []):
            if msg.get("ts") == thread_ts:
                continue
            if bot_user_id and msg.get("user") == bot_user_id:
                continue
            if msg.get("bot_id"):
                continue
            candidate_msgs.append(msg)
        for msg in candidate_msgs:
            parsed = _parse_narration_message(msg.get("text") or "", expected, session.narrations)
            if parsed is not None:
                final_narrations, edited_count = parsed
                edited = True
                logger.info(f"[argus-narrate] Phase2: ユーザー修正 {edited_count}/{expected} 枚を採用 (ts={msg.get('ts')})")
                break
        if not edited and candidate_msgs:
            latest = candidate_msgs[0]
            preview = (latest.get("text") or "")[:240].replace("\n", " | ")
            logger.warning(
                f"[argus-narrate] Phase2: ユーザー返信が見出し形式と合致せず元原稿を使用 "
                f"(返信件数={len(candidate_msgs)}, latest_ts={latest.get('ts')}, preview={preview!r})"
            )
    except Exception as exc:
        logger.warning(f"[argus-narrate] Phase2: スレッド取得失敗 {exc}")

    work_dir = session.work_dir
    try:
        from build_slide_video import render_video

        iteration = session.iteration + 1
        # 同名上書きにすると Slack 側で「同じ mp4」と扱われる可能性があるので回ごとに名前を分ける
        mp4_path = work_dir / f"{Path(filename).stem}.narrate.r{iteration}.mp4"
        logger.info(f"[argus-narrate] Phase2 開始 r{iteration}: {filename} (edited={edited})")
        render_video(
            session.slides, final_narrations, mp4_path, work_dir,
            lang=session.lang, quiet=True,
        )
        logger.info(
            f"[argus-narrate] mp4 生成完了 r{iteration} size={mp4_path.stat().st_size} bytes"
        )

        try:
            import pm_tts
            credit = pm_tts.credit_line(pm_tts.DEFAULT_SPEAKER)
        except Exception:
            credit = ""
        round_note = f" (Round {iteration})"
        initial_comment = (
            f":movie_camera: スライド要約動画{round_note}です。\n"
            + (f"_{credit}_\n" if credit else "")
            + "原稿を直したい場合はこのスレッドに修正版を返信し、再度「動画を生成」を押してください。\n"
            + "完了したらスレッドの「終了」ボタンで編集セッションを閉じてください。"
        )
        _post_argus_video(
            session.command,
            kind="narrate",
            mp4_path=mp4_path,
            title=f"{Path(filename).stem} 要約動画 r{iteration}",
            initial_comment=initial_comment,
        )
        # セッションを次のラウンドに繰り越す: narrations を「今回採用した版」で更新し、
        # 次回 reply は前回採用版に対して差分マージされる。
        with _narrate_lock:
            session.narrations = final_narrations
            session.iteration = iteration
            session.phase = "draft"
        try:
            if edited:
                note = f"（直近 {edited_count}/{expected} 枚を修正）"
            else:
                note = ""
            bot_client.chat_update(
                channel=channel_id, ts=thread_ts,
                text=(
                    f":white_check_mark: `{filename}` Round {iteration} 完了{note} — "
                    "原稿を再修正してもう一度「動画を生成」、または「終了」で締めてください"
                ),
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[argus-narrate] Phase2 エラー")
        try:
            bot_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f":warning: 動画生成に失敗しました: {exc}",
            )
        except Exception:
            pass
        # 失敗時はリトライできるよう draft に戻すだけでセッションは残す
        with _narrate_lock:
            sess = _narrate_sessions.get(thread_ts)
            if sess is not None:
                sess.phase = "draft"


def _run_narrate_cancel(thread_ts: str, user_id: str) -> None:
    """編集セッションを終了し、work_dir を破棄して親メッセージを更新する。"""
    with _narrate_lock:
        session = _narrate_sessions.pop(thread_ts, None)
    if session is None:
        return
    shutil.rmtree(session.work_dir, ignore_errors=True)
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        return
    try:
        from slack_sdk import WebClient
        rounds = session.iteration
        suffix = f"（{rounds} 回生成）" if rounds else "（動画は生成されませんでした）"
        WebClient(token=bot_token).chat_update(
            channel=session.channel_id, ts=thread_ts,
            text=f":checkered_flag: `{session.filename}` のナレーション編集を終了しました{suffix}",
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
