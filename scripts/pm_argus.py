#!/usr/bin/env python3
"""
pm_argus.py — Argus AI Project Intelligence System

データ収集・プロンプト構築ロジック + --brief-to-canvas CLI モード。

Slack (/argus-brief, /argus-draft, /argus-risk) コマンドのバックグラウンド処理と、
cron による毎朝の自動ブリーフィング生成 (--brief-to-canvas) を担う。

Usage:
    # ブリーフィング生成 → Canvas 投稿
    python3 scripts/pm_argus.py --brief-to-canvas --canvas-id F0XXXXXXXX

    # ブリーフィング生成 → 標準出力のみ（--dry-run）
    python3 scripts/pm_argus.py --brief-to-canvas --dry-run

    # リスク分析のみ
    python3 scripts/pm_argus.py --risk --dry-run

環境変数:
    RIVAULT_URL   — RiVault エンドポイント URL
    RIVAULT_TOKEN — RiVault API トークン
    SLACK_BOT_TOKEN — Canvas 投稿時に必要（slack_sdk 用）
"""

import argparse
import os
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import (
    open_db, open_pm_db, fetch_milestone_progress, fetch_assignee_workload,
    fetch_overdue_items, fetch_unacknowledged_decisions,
    fetch_unlinked_items_count, fetch_no_assignee_count,
    fetch_weekly_trends, fetch_summary_stats,
)
from cli_utils import call_argus_llm, load_claude_md_context
from format_utils import (
    format_milestone_table, format_overdue_list, format_assignee_table,
    format_weekly_trends as format_trends_table, format_decisions_list,
)

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_SECRETARY_CHANNELS_FILE = _DATA_DIR / "secretary_channels.txt"
_SECRETARY_CANVAS_ID_FILE = _DATA_DIR / "secretary_canvas_id.txt"

_DEFAULT_SINCE_DAYS = 30
_DRAFT_REPORT_SINCE_DAYS = 14

# --------------------------------------------------------------------------- #
# /argus-transcribe ジョブ排他制御
# --------------------------------------------------------------------------- #
_transcribe_jobs: dict[str, tuple[str, str]] = {}  # thread_ts → (filename, channel_id)
_transcribe_lock = threading.Lock()

# Minutes リポジトリのパス（同一ホスト上の兄弟ディレクトリ）
_MINUTES_REPO = _REPO_ROOT.parent / "Minutes"
_MINUTES_PIPELINE = _MINUTES_REPO / "slack_bot" / "pipeline.py"


# --------------------------------------------------------------------------- #
# データ収集
# --------------------------------------------------------------------------- #

def _load_channel_ids() -> list[str]:
    """data/secretary_channels.txt からチャンネルIDリストを読み込む"""
    if not _SECRETARY_CHANNELS_FILE.exists():
        return []
    lines = _SECRETARY_CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


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
    Slack DB ({channel_id}.db) から messages + replies を取得し、
    "[YYYY-MM-DD HH:MM] user_name: text" 形式で整形して返す。
    max_chars を超える場合は最古のメッセージから切り捨てる（最新を優先）。
    """
    db_path = data_dir / f"{channel_id}.db"
    if not db_path.exists():
        return f"（{channel_id}.db が見つかりません）"

    try:
        conn = open_db(db_path, encrypt=not no_encrypt)
    except Exception as e:
        return f"（{channel_id}.db の接続に失敗: {e}）"

    lines = []
    try:
        # 親メッセージ + 返信を timestamp 昇順で取得
        rows = conn.execute(
            """SELECT timestamp, user_name, text, 0 AS is_reply
               FROM messages
               WHERE date(timestamp) >= ? AND text IS NOT NULL AND text != ''
               UNION ALL
               SELECT timestamp, user_name, text, 1 AS is_reply
               FROM replies
               WHERE date(timestamp) >= ? AND text IS NOT NULL AND text != ''
               ORDER BY timestamp ASC""",
            (since_date, since_date),
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
) -> str:
    """
    data/minutes/{kind}.db の instances + minutes_content テーブルから
    held_at >= since_date の議事録本文を取得して返す。
    """
    if not minutes_dir.exists():
        return "（議事録ディレクトリが見つかりません）"

    db_files = sorted(minutes_dir.glob("*.db"))
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


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #

_BRIEF_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを分析し、**プロジェクト全体として今日 {today} 中に優先的に対応すべきこと**を
最大5件、優先度順にリストアップしてください。
個人の担当タスク管理ではなく、プロジェクトのゴール達成・マイルストーン進捗・
リスク軽減の観点からプロジェクト全体を俯瞰してください。

各項目の形式:
- **[優先度: 高/中/低]** タイトル
  - 状況: （プロジェクト全体での現状の簡潔な説明）
  - 次の一手: （誰が何を確認/決定/依頼すべきかを含む具体的なアクション）
  - 根拠: （データ上の根拠: マイルストーン名・アイテムID・担当者名・期限など）

## プロジェクト文脈

{context}

## 集計日: {today}（過去{days}日間のデータ）

## pm.db 統計サマリー

- オープンAI: {total_open}件 / 完了AI: {total_closed}件
- 期限超過（open）: {overdue_count}件
- 未確認決定事項: {unacknowledged_decisions}件
- マイルストーン未紐づけ: {unlinked_count}件 / 担当者なし: {no_assignee_count}件

## マイルストーン進捗

{milestone_table}

## 期限超過アクションアイテム（上位10件）

{overdue_list}

## 担当者別負荷

{assignee_table}

## 未確認決定事項

{decisions_list}

## 週次トレンド（直近4週）

{weekly_trends}

## 直近 {days} 日間の Slack 生メッセージ

{messages}

## 直近 {days} 日間の議事録

{minutes}

---

上記データを踏まえ、富岳NEXTプロジェクト全体として今日取るべきアクションを5件以内で提示してください。
特定の個人のタスク管理ではなく、プロジェクトのゴール達成・マイルストーン到達・リスク軽減に
直結する事項を優先してください。
データが示す具体的な懸案（マイルストーン名・期限超過のID・担当者名）を必ず引用してください。
"""


_DRAFT_AGENDA_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを基に、**次回「{subject}」のための会議アジェンダ草案**を生成してください。

## プロジェクト文脈

{context}

## 未確認決定事項（要フォローアップ）

{decisions_list}

## 直近の未完了アクションアイテム（期限超過含む）

{overdue_list}

## 直近 14 日間の Slack 生メッセージ（議論の流れ）

{messages}

---

アジェンダ草案を以下の形式で生成してください:

# 会議アジェンダ: {subject}
日時: （調整中）
参加者: （調整中）

## 1. 前回決定事項の確認（10分）
（前回未確認の決定事項をリストアップ）

## 2. アクションアイテム進捗確認（15分）
（期限超過・要注意アイテムを担当者別にリスト）

## 3. 主要議題（30分）
（Slackの議論と統計データから浮上している課題を3〜5件）

## 4. 次回アクションアイテムの確定（5分）

---
_Argus 生成: {today}_
"""

_DRAFT_REPORT_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを基に、**進捗報告「{subject}」の草案**を生成してください。

## プロジェクト文脈

{context}

## マイルストーン進捗

{milestone_table}

## 直近 2 週間の完了アクションアイテム

{closed_items}

## 期限超過アクションアイテム

{overdue_list}

## 担当者別負荷

{assignee_table}

---

進捗報告草案を以下の形式で生成してください:

# 進捗報告: {subject}
報告日: {today}

## 全体状況
（マイルストーン進捗サマリー・完了率・ヘルス評価）

## マイルストーン別進捗
（各マイルストーンの状況・完了数・懸念事項）

## 直近2週間の主な成果
（完了AIのリスト）

## 課題・リスク
（期限超過・担当者過負荷・要注意事項）

## 次期アクション
（優先対応事項3件以内）

---
_Argus 生成: {today}_
"""

_DRAFT_REQUEST_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを基に、**確認依頼「{subject}」のメッセージ草案**を生成してください。

## プロジェクト文脈

{context}

## 担当者別負荷（期限超過が多い担当者に優先して確認）

{assignee_table}

## 期限超過アクションアイテム

{overdue_list}

## 関連する直近 14 日間の Slack メッセージ

{messages}

---

確認依頼メッセージ草案を生成してください。
対象者が複数いる場合は担当者ごとにメッセージを分けてください。
丁寧かつ具体的に（アイテムID・期限・内容を明示）してください。

---
_Argus 生成: {today}_
"""

_RISK_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを分析し、**顕在化しているリスクと放置すると問題になりうる予兆**を列挙してください。

各リスクの形式:
- **[高/中/低]** リスクタイトル
  - 状況: （現状の説明）
  - 根拠: （具体的なデータの引用）
  - 推奨対応: （今すぐやるべき対応）

## プロジェクト文脈

{context}

## 集計日: {today}（過去{days}日間のデータ）

## pm.db 統計サマリー

- オープンAI: {total_open}件 / 完了AI: {total_closed}件
- 期限超過（open）: {overdue_count}件
- 未確認決定事項: {unacknowledged_decisions}件
- マイルストーン未紐づけ: {unlinked_count}件 / 担当者なし: {no_assignee_count}件

## マイルストーン進捗

{milestone_table}

## 期限超過アクションアイテム（上位15件）

{overdue_list}

## 担当者別負荷

{assignee_table}

## 週次トレンド（直近4週）

{weekly_trends}

## 未確認決定事項

{decisions_list}

## 直近 {days} 日間の Slack 生メッセージ

{messages}

## 直近 {days} 日間の議事録

{minutes}

---

定量データと会話の文脈から、富岳NEXTプロジェクト全体に影響するリスクを分析してください。
特定の個人の作業遅延ではなく、マイルストーン達成・プロジェクトゴールへの影響度を軸に、
顕在化しているリスクと放置すると問題になりうる予兆の両方を列挙してください。
各リスクに優先度（高/中/低）と推奨対応を付けてください。
"""


def _fmt_closed_items(conn, since_date: str, limit: int = 20) -> str:
    try:
        rows = conn.execute(
            """SELECT id, content, assignee, due_date
               FROM action_items
               WHERE status='closed' AND COALESCE(deleted,0)=0
               AND extracted_at >= ?
               ORDER BY extracted_at DESC LIMIT ?""",
            (since_date, limit),
        ).fetchall()
        if not rows:
            return "（なし）"
        return "\n".join(
            f"- [ID:{r['id']}][担当:{r['assignee'] or '未定'}] {r['content'][:80]}"
            for r in rows
        )
    except Exception:
        return "（取得エラー）"


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
) -> str:
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

    prompt = _BRIEF_PROMPT.format(
        today=today,
        days=days,
        context=context,
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
    conn=None,
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
        closed_items = _fmt_closed_items(conn, since_14) if conn else "（取得不可）"
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

    prompt = _RISK_PROMPT.format(
        today=today,
        days=days,
        context=context,
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

def _collect_all_data(
    today: str,
    since_date: str,
    *,
    no_encrypt: bool = False,
    data_dir: Path | None = None,
    minutes_dir: Path | None = None,
    pm_db_path: Path | None = None,
) -> tuple[str, str, dict, object]:
    """messages/minutes/stats を一括収集し (messages, minutes, stats, conn) を返す。
    conn は呼び出し元でクローズすること。
    """
    data_dir = data_dir or _DATA_DIR
    minutes_dir = minutes_dir or _MINUTES_DIR
    pm_db_path = pm_db_path or _PM_DB

    channel_ids = _load_channel_ids()
    message_parts = []
    for ch_id in channel_ids:
        raw = fetch_raw_messages(ch_id, since_date, data_dir=data_dir, no_encrypt=no_encrypt)
        if raw:
            message_parts.append(f"## チャンネル: {ch_id}\n\n{raw}")
    messages = "\n\n---\n\n".join(message_parts)

    minutes = fetch_recent_minutes(since_date, minutes_dir=minutes_dir, no_encrypt=no_encrypt)

    conn = open_pm_db(pm_db_path, no_encrypt=no_encrypt)
    stats = fetch_pm_stats(conn, today, since=since_date)

    return messages, minutes, stats, conn


def _run_brief(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-brief のバックグラウンド処理"""
    import logging
    logger = logging.getLogger("pm_argus")
    try:
        cmd_text = (command.get("text") or "").strip()
        arg_days, assignee, topic = _parse_command_args(cmd_text)
        days = arg_days if arg_days is not None else _DEFAULT_SINCE_DAYS
        requester = command.get("user_name") or "プロジェクトメンバー"

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=days)).isoformat()
        focus_desc = "".join([
            f" days={days}",
            f" requester={requester}",
            f" assignee={assignee}" if assignee else "",
            f" topic={topic}" if topic else "",
        ])
        logger.info(f"[argus-brief] since={since_date}{focus_desc}")

        context = load_claude_md_context()
        messages, minutes, stats, conn = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt
        )
        conn.close()

        prompt = build_brief_prompt(
            messages, minutes, stats, context, today, days,
            assignee=assignee, topic=topic, requester=requester,
        )
        logger.info("[argus-brief] LLM 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        header = f"*Argus ブリーフィング ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        respond(
            text=f"{header}\n\n{result}",
            response_type="ephemeral",
            replace_original=True,
        )
        logger.info("[argus-brief] 完了")
    except Exception as e:
        logger.exception("[argus-brief] エラー")
        respond(
            text=f":warning: Argus ブリーフィング生成エラー: {e}",
            response_type="ephemeral",
            replace_original=True,
        )


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
        logger.info(f"[argus-draft] purpose={purpose} subject={subject}")

        context = load_claude_md_context()
        messages, minutes, stats, conn = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt
        )

        prompt = build_draft_prompt(purpose, subject, messages, stats, context, conn=conn, today=today)
        conn.close()

        logger.info("[argus-draft] RiVault 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        respond(
            text=f"*Argus 草案 ({purpose}: {subject})*\n\n{result}",
            response_type="ephemeral",
            replace_original=True,
        )
        logger.info("[argus-draft] 完了")
    except Exception as e:
        logger.exception("[argus-draft] エラー")
        respond(
            text=f":warning: Argus 草案生成エラー: {e}",
            response_type="ephemeral",
            replace_original=True,
        )


def _run_risk(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-risk のバックグラウンド処理"""
    import logging
    logger = logging.getLogger("pm_argus")
    try:
        cmd_text = (command.get("text") or "").strip()
        arg_days, assignee, topic = _parse_command_args(cmd_text)
        days = arg_days if arg_days is not None else _DEFAULT_SINCE_DAYS

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=days)).isoformat()
        focus_desc = "".join([
            f" days={days}",
            f" assignee={assignee}" if assignee else "",
            f" topic={topic}" if topic else "",
        ])
        logger.info(f"[argus-risk] since={since_date}{focus_desc}")

        context = load_claude_md_context()
        messages, minutes, stats, conn = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt
        )
        conn.close()

        prompt = build_risk_prompt(
            messages, minutes, stats, context, today, days,
            assignee=assignee, topic=topic,
        )
        logger.info("[argus-risk] LLM 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        header = f"*Argus リスク分析 ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        respond(
            text=f"{header}\n\n{result}",
            response_type="ephemeral",
            replace_original=True,
        )
        logger.info("[argus-risk] 完了")
    except Exception as e:
        logger.exception("[argus-risk] エラー")
        respond(
            text=f":warning: Argus リスク分析エラー: {e}",
            response_type="ephemeral",
            replace_original=True,
        )


def _run_transcribe(respond, command):
    """Slack /argus-transcribe のバックグラウンド処理。

    Minutes リポジトリの pipeline.py を使い、
    ダウンロード → Whisper文字起こし → LLM議事録生成 を実行する。
    進捗はスレッドへの chat_postMessage で可視投稿し、
    完了・エラー通知は respond() で ephemeral 返信する。
    """
    import logging
    logger = logging.getLogger("pm_argus")

    filename = (command.get("text") or "").strip()
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

    # Minutes pipeline モジュールが利用可能か確認
    if not _MINUTES_PIPELINE.exists():
        respond(
            text=f":warning: Minutes リポジトリが見つかりません: `{_MINUTES_PIPELINE}`",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # Slack WebClient（チャンネル投稿・ファイルダウンロード・アップロード用）
    # files:read / files:write スコープが必要なため User Token (xoxp-) を使用
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        respond(
            text=":warning: SLACK_USER_TOKEN が設定されていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    try:
        from slack_sdk import WebClient
        client = WebClient(token=user_token)
    except ImportError:
        respond(
            text=":warning: slack_sdk がインストールされていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # pipeline モジュールを動的 import
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pipeline", str(_MINUTES_PIPELINE))
        pipeline = importlib.util.module_from_spec(spec)
        # Minutes の config.py 参照のため sys.path を一時追加
        slack_bot_dir = str(_MINUTES_PIPELINE.parent)
        if slack_bot_dir not in sys.path:
            sys.path.insert(0, slack_bot_dir)
        spec.loader.exec_module(pipeline)
    except Exception as e:
        respond(
            text=f":warning: pipeline モジュールの読み込みに失敗しました: {e}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # スレッドを作成して進捗投稿先にする
    try:
        post = client.chat_postMessage(
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
        # pipeline.py 内部の requests.get が SLACK_BOT_TOKEN をヘッダーに使うため
        # User Token で一時上書きする
        os.environ["SLACK_BOT_TOKEN"] = user_token
        pipeline.run_pipeline(client, channel_id, filename, thread_ts)
        logger.info(f"[argus-transcribe] 完了: filename={filename}")
        respond(
            text=f":white_check_mark: `{filename}` の議事録生成が完了しました。スレッドをご確認ください。",
            response_type="ephemeral",
            replace_original=True,
        )
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
    parser.add_argument("--canvas-id", default=None, metavar="ID",
                        help="投稿先 Canvas ID（省略時は secretary_canvas_id.txt を参照）")
    parser.add_argument("--dry-run", action="store_true",
                        help="Canvas 投稿なし・標準出力のみ")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DB を暗号化しない（平文モード）")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="データ収集の開始日（デフォルト: 30日前）")
    parser.add_argument("--days", type=int, default=None, metavar="N",
                        help="直近何日分を対象にするか（デフォルト: 30日。--since と同時指定時は --since 優先）")
    parser.add_argument("--assignee", default=None, metavar="NAME",
                        help="担当者フォーカス（例: --assignee 西澤）")
    parser.add_argument("--topic", default=None, metavar="TEXT",
                        help="話題フォーカス（例: --topic Benchpark）")
    parser.add_argument("--requester", default=None, metavar="NAME",
                        help="実行者名（CLI モード用。省略時はシステムユーザー名）")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="pm.db のパス（デフォルト: data/pm.db）")
    args = parser.parse_args()

    today = date.today().isoformat()
    days = args.days if args.days is not None else _DEFAULT_SINCE_DAYS
    since_date = args.since or (date.today() - timedelta(days=days)).isoformat()
    pm_db_path = Path(args.db) if args.db else _PM_DB
    requester = args.requester or os.environ.get("USER") or "プロジェクトメンバー"

    context = load_claude_md_context()
    print(f"[INFO] since: {since_date} / today: {today}", file=sys.stderr)

    messages, minutes, stats, conn = _collect_all_data(
        today, since_date,
        no_encrypt=args.no_encrypt,
        pm_db_path=pm_db_path,
    )

    if args.brief_to_canvas:
        conn.close()
        prompt = build_brief_prompt(
            messages, minutes, stats, context, today, days,
            assignee=args.assignee, topic=args.topic, requester=requester,
        )
        print("[INFO] LLM に問い合わせ中（ブリーフィング）...", file=sys.stderr)
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        canvas_content = f"# Argus ブリーフィング ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id
        if not canvas_id and _SECRETARY_CANVAS_ID_FILE.exists():
            canvas_id = _SECRETARY_CANVAS_ID_FILE.read_text(encoding="utf-8").strip()

        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id か data/secretary_canvas_id.txt を設定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    elif args.risk:
        conn.close()
        prompt = build_risk_prompt(
            messages, minutes, stats, context, today, days,
            assignee=args.assignee, topic=args.topic,
        )
        print("[INFO] LLM に問い合わせ中（リスク分析）...", file=sys.stderr)
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        canvas_content = f"# Argus リスク分析 ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id
        if not canvas_id and _SECRETARY_CANVAS_ID_FILE.exists():
            canvas_id = _SECRETARY_CANVAS_ID_FILE.read_text(encoding="utf-8").strip()

        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id か data/secretary_canvas_id.txt を設定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    else:
        conn.close()
        parser.print_help()


if __name__ == "__main__":
    main()
