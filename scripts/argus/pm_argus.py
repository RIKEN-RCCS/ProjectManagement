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
import logging
import os
import sys
import threading

import yaml
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("pm_argus")

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import (
    open_db, open_pm_db, open_knowledge_db,
    fetch_milestone_progress, fetch_assignee_workload,
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
_ARGUS_CONFIG_FILE = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_FILE_LEGACY = _DATA_DIR / "qa_config.yaml"


_DEFAULT_SINCE_DAYS = 30
_DRAFT_REPORT_SINCE_DAYS = 14

# --------------------------------------------------------------------------- #
# /argus-transcribe ジョブ排他制御
# --------------------------------------------------------------------------- #
_transcribe_jobs: dict[str, tuple[str, str]] = {}  # thread_ts → (filename, channel_id)
_transcribe_lock = threading.Lock()

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


def fetch_knowledge_summary(
    *,
    no_encrypt: bool = False,
    data_dir: Path | None = None,
    max_items: int = _KNOWLEDGE_MAX_ITEMS_DEFAULT,
) -> str:
    """data/knowledge.db から brief/risk プロンプト同梱用の短文サマリを生成する。

    抽出条件（docs/distill_policy.md 参照）:
    - deleted = 0
    - superseded_by IS NULL（現役のみ）
    - confidence IN ('high', 'medium')（low は書き込まれない想定だが念のためフィルタ）

    並び順: confidence high → medium、その中で last_validated_at 降順。
    出力は Markdown 箇条書きで、1 行に
      - **[KN-XXXX | KIND | conf]** topic — current_state（last_validated: YYYY-MM-DD）
    まで詰める。テキスト全体を _KNOWLEDGE_MAX_CHARS で打ち切る。
    """
    data_dir = data_dir or _DATA_DIR
    db_path = data_dir / "knowledge.db"
    if not db_path.exists():
        return ""

    try:
        conn = open_knowledge_db(db_path, no_encrypt=no_encrypt)
    except Exception as e:
        logger.warning(f"knowledge.db 接続失敗: {e}")
        return ""

    try:
        rows = conn.execute(
            """SELECT id, kind, topic, current_state, confidence,
                       last_validated_at, decided_at, owners, tags
                  FROM knowledge
                 WHERE COALESCE(deleted, 0) = 0
                   AND superseded_by IS NULL
                   AND confidence IN ('high', 'medium')
                 ORDER BY CASE confidence WHEN 'high' THEN 0 ELSE 1 END,
                          COALESCE(last_validated_at, decided_at, '') DESC
                 LIMIT ?""",
            (max_items,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"knowledge.db クエリ失敗: {e}")
        conn.close()
        return ""
    conn.close()

    if not rows:
        return ""

    lines = []
    for r in rows:
        kid = r["id"]
        kind = (r["kind"] or "")[:10]
        conf = r["confidence"] or ""
        topic = (r["topic"] or "").strip()
        cs = (r["current_state"] or "").strip()
        validated = r["last_validated_at"] or r["decided_at"] or ""
        validated_str = f" (validated: {validated})" if validated else ""
        line = f"- **[{kid} | {kind} | {conf}]** {topic} — {cs}{validated_str}"
        lines.append(line)

    body = "\n".join(lines)
    if len(body) > _KNOWLEDGE_MAX_CHARS:
        # 末尾を切り捨てて省略マーカーを付ける
        truncated = body[:_KNOWLEDGE_MAX_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        omitted = len(rows) - len(truncated.splitlines())
        body = truncated + f"\n_…他 {omitted} 件は省略_"
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

## 確定済みナレッジ（蒸留 / 上書き未済）

これらは富岳NEXTプロジェクト全体に渡って有効と判定されている確定事項です。
推奨アクションが既存の意思決定 / 制約と整合しているかを必ず照合し、参照したレコードは
回答中に `KN-XXXX` 形式で引用してください。空の場合は無視して構いません。

{knowledge_summary}

## 集計日: {today}（{period_desc}）

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

## 直近の Slack 生メッセージ（{period_desc}）

{messages}

## 直近の議事録（{period_desc}）

{minutes}

---

上記データを踏まえ、富岳NEXTプロジェクト全体として今日取るべきアクションを5件以内で提示してください。
特定の個人のタスク管理ではなく、プロジェクトのゴール達成・マイルストーン到達・リスク軽減に
直結する事項を優先してください。
データが示す具体的な懸案（マイルストーン名・期限超過のID・担当者名）を必ず引用してください。
"""


def _to_slack_mrkdwn(text: str) -> str:
    """GitHub Flavored Markdown を Slack mrkdwn に変換。

    - `**bold**` → `*bold*`
    - `## heading` / `### heading` → `*heading*`
    - `- item` はそのまま（Slackでも箇条書きとして表示される）
    """
    import re
    # ヘッダー (## ... / ### ...) を太字に変換
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*（ただし *...* と衝突しないよう一時置換）
    text = re.sub(r'\*\*([^*\n]+?)\*\*', r'*\1*', text)
    return text


# Slack section block の text は 3000 文字上限。超過するとブロック全体が無音で破棄される。
_SLACK_SECTION_LIMIT = 2900  # 安全マージン


def _split_mrkdwn_to_blocks(text: str) -> list[dict]:
    """長文 mrkdwn を Slack section block の上限内で分割する。

    改行優先で区切り、超過する単一行は文字数で強制切断する。
    """
    blocks: list[dict] = []
    buf = ""
    for line in text.split("\n"):
        # 単一行が上限を超える場合は強制分割
        while len(line) > _SLACK_SECTION_LIMIT:
            if buf:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
                buf = ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line[:_SLACK_SECTION_LIMIT]}})
            line = line[_SLACK_SECTION_LIMIT:]
        # 通常の改行単位で詰める
        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) > _SLACK_SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = line
        else:
            buf = candidate
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks


_DAILY_SUMMARY_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
以下のデータを分析し、**本日 {today} のプロジェクト活動記録**をサマライズしてください。

## プロジェクト文脈

{context}

## 確定済みナレッジ（蒸留 / 上書き未済）

本日の議論が既存の確定事項と矛盾している場合は明示してください。引用は `KN-XXXX` 形式。
空の場合は無視して構いません。

{knowledge_summary}

## 本日のSlackメッセージ

{messages}

## 本日の議事録

{minutes}

---

以下の観点で本日の活動をサマライズしてください:

### 1. 主な議論トピック
本日Slack・会議で議論された主要なテーマ（3〜5件）

### 2. 決定事項
誰が何を決定したか、決定の背景・理由

### 3. 新規アクションアイテム
誰が何を担当することになったか、期限が設定されているか

### 4. 重要な進捗・変更
プロジェクトに影響する技術的進展・方針変更

**データがない場合は「本日は活動記録がありません」と簡潔に記載してください。**
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

## 確定済みナレッジ（蒸留 / 上書き未済）

これらは確定事項です。Slack 上の発言・議事録の議論がこれらと矛盾している場合は、
それ自体が高優先度のリスクです（不整合・周知漏れ・古い前提に基づく作業）。
参照したレコードは `KN-XXXX` 形式で引用してください。空の場合は無視して構いません。

{knowledge_summary}

## 集計日: {today}（{period_desc}）

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

## 直近の Slack 生メッセージ（{period_desc}）

{messages}

## 直近の議事録（{period_desc}）

{minutes}

---

定量データと会話の文脈から、富岳NEXTプロジェクト全体に影響するリスクを分析してください。
特定の個人の作業遅延ではなく、マイルストーン達成・プロジェクトゴールへの影響度を軸に、
顕在化しているリスクと放置すると問題になりうる予兆の両方を列挙してください。
各リスクに優先度（高/中/低）と推奨対応を付けてください。
"""


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
    message_parts = []
    for ch_id in channel_ids:
        raw = fetch_raw_messages(ch_id, since_date, data_dir=data_dir, no_encrypt=no_encrypt)
        if raw:
            message_parts.append(f"## チャンネル: {ch_id}\n\n{raw}")
    messages = "\n\n---\n\n".join(message_parts)

    minutes = fetch_recent_minutes(
        since_date, minutes_dir=minutes_dir, no_encrypt=no_encrypt,
        minutes_names=minutes_names or None,
    )

    conns = []
    stats_list = []
    for p in pm_db_paths:
        try:
            conn = open_pm_db(p, no_encrypt=no_encrypt)
            conns.append(conn)
            stats_list.append(fetch_pm_stats(conn, today, since=since_date))
        except Exception as e:
            print(f"[WARN] pm.db 接続スキップ ({p}): {e}", file=sys.stderr)

    stats = merge_pm_stats(stats_list)
    knowledge_summary = fetch_knowledge_summary(
        no_encrypt=no_encrypt, data_dir=data_dir,
    )
    return messages, minutes, stats, conns, knowledge_summary


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
        index_name = resolve_index_name(command.get("channel_id") or None)
        focus_desc = "".join([
            f" days={days}",
            f" index={index_name}",
            f" requester={requester}",
            f" assignee={assignee}" if assignee else "",
            f" topic={topic}" if topic else "",
        ])
        logger.info(f"[argus-brief] since={since_date}{focus_desc}")

        context = load_claude_md_context()
        messages, minutes, stats, conns, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )
        for c in conns:
            c.close()

        prompt = build_brief_prompt(
            messages, minutes, stats, context, today, days,
            assignee=assignee, topic=topic, requester=requester,
            knowledge_summary=knowledge_summary,
        )
        logger.info("[argus-brief] LLM 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        header = f"*Argus ブリーフィング ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-brief] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)
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
        messages, minutes, stats, conns, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )

        prompt = build_draft_prompt(purpose, subject, messages, stats, context, conns=conns, today=today)
        for c in conns:
            c.close()

        logger.info("[argus-draft] RiVault 呼び出し中...")
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
    """Slack /argus-risk のバックグラウンド処理"""
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

        context = load_claude_md_context()
        messages, minutes, stats, conns, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )
        for c in conns:
            c.close()

        prompt = build_risk_prompt(
            messages, minutes, stats, context, today, days,
            assignee=assignee, topic=topic,
            knowledge_summary=knowledge_summary,
        )
        logger.info("[argus-risk] LLM 呼び出し中...")
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        header = f"*Argus リスク分析 ({today})*"
        if assignee:
            header += f"  担当者フォーカス: {assignee}"
        if topic:
            header += f"  話題フォーカス: {topic}"
        full_text = _to_slack_mrkdwn(f"{header}\n\n{result}")
        blocks = _split_mrkdwn_to_blocks(full_text)
        logger.info(f"[argus-risk] respond text={len(full_text)} chars, blocks={len(blocks)}")
        respond(blocks=blocks)
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


def _build_channel_name_map() -> dict[str, str]:
    """argus_config.yaml からチャンネルID -> 名前のマッピングを生成"""
    channel_map = {}
    config_path = Path(_REPO_ROOT) / "data" / "argus_config.yaml"

    try:
        with open(config_path) as f:
            for line in f:
                # コメント行から抽出: # C0XXXXXXX チャンネル名
                if line.startswith("#") and line.lstrip("#").startswith(" C0"):
                    parts = line.lstrip("# ").strip().split(None, 1)
                    if len(parts) == 2:
                        ch_id, ch_name = parts
                        if ch_id not in channel_map:  # 重複は先出を優先
                            channel_map[ch_id] = ch_name
    except Exception:
        pass

    # pm_qa_server.py の _CHANNEL_NAMES をフォールバック
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

        # チャンネルID取得 (先頭行)
        lines = ch_section.strip().split("\n")
        ch_id = lines[0].strip()
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
        messages, minutes, stats, conns, knowledge_summary = _collect_all_data(
            today, since_date, no_encrypt=no_encrypt, index_name=index_name,
        )
        for c in conns:
            c.close()

        # 3. ユーザーIDマップを構築（テキスト内のID展開用）
        import re
        user_id_map = {}
        try:
            from db_utils import open_db

            # 統合 DB (data/slack.db) からユーザーID展開用マップを構築
            unified_db = _REPO_ROOT / "data" / "slack.db"
            uid_pattern = re.compile(r'(U0[A-Z0-9]{9})')
            text_uids = set()

            try:
                conn = open_db(unified_db, encrypt=not no_encrypt)
                # テキスト内のユーザーIDパターン (U0XXXXXXXXX = 10文字) を抽出
                for row in conn.execute("SELECT text FROM messages WHERE text IS NOT NULL").fetchall():
                    if row[0]:
                        text_uids.update(uid_pattern.findall(row[0]))
                # テキストに出現した user_id の display_name を引く
                for uid in text_uids:
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
    filename = (command.get("text") or "").strip()
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
        _run_transcribe_pipeline(bot_client, channel_id, filename, thread_ts)
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
    parser.add_argument("--requester", default=None, metavar="NAME",
                        help="実行者名（CLI モード用。省略時はシステムユーザー名）")
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
    requester = args.requester or os.environ.get("USER") or "プロジェクトメンバー"

    context = load_claude_md_context()
    print(f"[INFO] since: {since_date} / today: {today} / "
          f"index: {args.index_name or '(default)'}", file=sys.stderr)

    messages, minutes, stats, conns, knowledge_summary = _collect_all_data(
        today, since_date,
        no_encrypt=args.no_encrypt,
        pm_db_paths=pm_db_paths_cli,
        index_name=args.index_name,
    )

    def _close_conns():
        for c in conns:
            c.close()

    if args.brief_to_canvas:
        _close_conns()
        prompt = build_brief_prompt(
            messages, minutes, stats, context, today, days,
            assignee=args.assignee, topic=args.topic, requester=requester,
            knowledge_summary=knowledge_summary,
        )
        print("[INFO] LLM に問い合わせ中（ブリーフィング）...", file=sys.stderr)
        result = call_argus_llm(prompt, system="あなたはAIインテリジェンスシステムArgusです。")

        # タイトルを days == 0 の場合に変更
        if days == 0:
            canvas_content = f"# Argus 日次活動サマリー ({today})\n\n{result}\n\n_生成: {today} JST_"
        else:
            canvas_content = f"# Argus ブリーフィング ({today})\n\n{result}\n\n_生成: {today} JST_"

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
        _close_conns()
        prompt = build_risk_prompt(
            messages, minutes, stats, context, today, days,
            assignee=args.assignee, topic=args.topic,
            knowledge_summary=knowledge_summary,
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
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    else:
        _close_conns()
        parser.print_help()


if __name__ == "__main__":
    main()
