#!/usr/bin/env python3
"""
pm_report.py

pm.db から決定事項・アクションアイテム・会議情報を読み込み、
LLMで週次/月次進捗レポートと次回会議アジェンダ草案を生成して
Slack Canvas に投稿する。

Usage:
    python3 scripts/pm_report.py
    python3 scripts/pm_report.py --since 2026-01-01
    python3 scripts/pm_report.py --mode agenda --meeting-name Leader_Meeting
    python3 scripts/pm_report.py --dry-run --output report.md

Options:
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --mode MODE             生成モード: report（デフォルト）/ agenda / both
    --meeting-name NAME     次回会議種別（agenda モード時に使用）
    --canvas-id ID          投稿先 Canvas ID
    --since YYYY-MM-DD      この日付以降のデータのみ対象
    --skip-canvas           Canvas 投稿をスキップ
    --dry-run               DB保存なし・結果を標準出力のみ
    --output PATH           標準出力の内容をファイルにも保存
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

from slack_bolt import App
from slack_sdk.errors import SlackApiError

# --------------------------------------------------------------------------- #
# 定数・パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_CANVAS_ID = "F0AAD2494VB"  # 20_1_リーダ会議メンバ Canvas

RISK_KEYWORDS = [
    "問題", "障害", "遅延", "困難", "難しい", "間に合わない",
    "ブロック", "懸念", "リスク", "未解決", "未定", "不明",
    "issue", "blocker", "delay", "risk", "concern",
]


# --------------------------------------------------------------------------- #
# CLAUDE.md 読み込み（コンテキスト用）
# --------------------------------------------------------------------------- #
def load_context() -> str:
    if not CLAUDE_MD.exists():
        return ""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    sections = []
    capture = False
    for line in text.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)", line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else text[:3000]


# --------------------------------------------------------------------------- #
# pm.db 読み込み
# --------------------------------------------------------------------------- #
def open_pm_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_open_action_items(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    query = """
        SELECT a.id, a.content, a.assignee, a.due_date, a.status,
               a.source, a.source_ref, a.extracted_at, a.meeting_id,
               m.kind as meeting_kind, m.held_at as meeting_held_at
        FROM action_items a
        LEFT JOIN meetings m ON a.meeting_id = m.meeting_id
        WHERE a.status = 'open'
    """
    params: list = []
    if since:
        query += " AND a.extracted_at >= ?"
        params.append(since)
    query += " ORDER BY a.due_date ASC NULLS LAST, a.extracted_at ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_recent_decisions(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    query = """
        SELECT d.id, d.content, d.decided_at, d.source, d.source_ref,
               d.meeting_id, m.kind as meeting_kind
        FROM decisions d
        LEFT JOIN meetings m ON d.meeting_id = m.meeting_id
        WHERE 1=1
    """
    params: list = []
    if since:
        query += " AND d.decided_at >= ?"
        params.append(since)
    query += " ORDER BY d.decided_at DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_recent_meetings(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    query = "SELECT * FROM meetings WHERE 1=1"
    params: list = []
    if since:
        query += " AND held_at >= ?"
        params.append(since)
    query += " ORDER BY held_at DESC LIMIT 10"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def detect_risk_items(action_items: list[dict]) -> list[dict]:
    """リスクキーワードを含むアクションアイテムを抽出"""
    risk_items = []
    for item in action_items:
        content = item.get("content", "").lower()
        if any(kw.lower() in content for kw in RISK_KEYWORDS):
            risk_items.append(item)
    return risk_items


# --------------------------------------------------------------------------- #
# claude CLI 呼び出し
# --------------------------------------------------------------------------- #
def call_claude(prompt: str, timeout: int = 300) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    # <thinking>タグを除去
    output = re.sub(r"<thinking>[\s\S]*?</thinking>", "", result.stdout).strip()
    return output


# --------------------------------------------------------------------------- #
# レポート生成
# --------------------------------------------------------------------------- #
REPORT_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のプロジェクト情報をもとに、週次進捗レポートを生成してください。

## 指示

1. **簡潔かつ実用的に**: マネージャーが5分で読めるレポートにすること
2. **事実のみ記載**: 推測や補完は含めないこと
3. **リスク・懸念事項を強調**: 「要注意」として目立つように記載すること
4. **Markdown形式で出力**: Slack Canvas での表示を想定

## プロジェクト文脈

{context}

## データ（生成日: {today}）

### 未完了アクションアイテム（{ai_count}件）

{action_items}

### 直近の決定事項（{d_count}件）

{decisions}

### 直近の会議（{m_count}件）

{meetings}

### リスク検知アイテム（{risk_count}件）

{risk_items}

## 出力形式

以下のセクション構成でMarkdownレポートを出力してください:

# 富岳NEXT プロジェクト進捗レポート（{today}）

## サマリー
（3〜5行で全体状況を要約）

## 未完了アクションアイテム
（担当者・期限付きの表形式または箇条書き）

## 直近の決定事項
（箇条書き、ソース参照付き）

## 要注意事項
（リスク・懸念・遅延の可能性があるもの）

## 次のステップ
（次回会議までに対応すべき事項）
"""

AGENDA_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のプロジェクト情報をもとに、次回{meeting_name}の会議アジェンダ草案を生成してください。

## 指示

1. **優先度順に整理**: 重要・緊急度の高い議題を先に
2. **時間配分の目安を付ける**: 各アジェンダ項目に目安時間を記載
3. **未解決事項を反映**: 前回持ち越しのアクションアイテムを確認事項に含める
4. **Markdown形式で出力**: Slack Canvas での表示を想定

## プロジェクト文脈

{context}

## データ（生成日: {today}）

### 未完了アクションアイテム（{ai_count}件）

{action_items}

### 直近の決定事項（{d_count}件）

{decisions}

### リスク・懸念事項（{risk_count}件）

{risk_items}

## 出力形式

以下の構成でMarkdownアジェンダを出力してください:

# 次回{meeting_name} アジェンダ草案（{today}版）

## 前回からの持ち越し確認
（未完了アクションアイテムの進捗確認）

## 主要議題
（優先度順、各項目に目安時間）

## その他・情報共有

## 次回アクションアイテム確認
（この会議で決まりそなアクションの確認）
"""


def format_action_items(items: list[dict]) -> str:
    if not items:
        return "（なし）"
    lines = []
    for a in items:
        assignee = a.get("assignee") or "未定"
        due = f" 期限:{a['due_date']}" if a.get("due_date") else ""
        source = f" [{a.get('source', '')}]" if a.get("source") else ""
        ref = f" {a.get('source_ref', '')}" if a.get("source_ref") else ""
        lines.append(f"- [{assignee}]{due} {a['content']}{source}{ref}")
    return "\n".join(lines)


def format_decisions(items: list[dict]) -> str:
    if not items:
        return "（なし）"
    lines = []
    for d in items:
        date_str = f" ({d['decided_at']})" if d.get("decided_at") else ""
        ref = f" {d.get('source_ref', '')}" if d.get("source_ref") else ""
        lines.append(f"- {d['content']}{date_str}{ref}")
    return "\n".join(lines)


def format_meetings(items: list[dict]) -> str:
    if not items:
        return "（なし）"
    lines = []
    for m in items:
        lines.append(f"- {m.get('held_at', '?')} {m.get('kind', '?')}: {m.get('summary', '')[:100]}")
    return "\n".join(lines)


def generate_report(
    action_items: list[dict],
    decisions: list[dict],
    meetings: list[dict],
    risk_items: list[dict],
    context: str,
    today: str,
) -> str:
    prompt = REPORT_PROMPT.format(
        context=context,
        today=today,
        ai_count=len(action_items),
        action_items=format_action_items(action_items),
        d_count=len(decisions),
        decisions=format_decisions(decisions),
        m_count=len(meetings),
        meetings=format_meetings(meetings),
        risk_count=len(risk_items),
        risk_items=format_action_items(risk_items),
    )
    return call_claude(prompt, timeout=300)


def generate_agenda(
    action_items: list[dict],
    decisions: list[dict],
    risk_items: list[dict],
    context: str,
    today: str,
    meeting_name: str,
) -> str:
    prompt = AGENDA_PROMPT.format(
        context=context,
        today=today,
        meeting_name=meeting_name,
        ai_count=len(action_items),
        action_items=format_action_items(action_items),
        d_count=len(decisions),
        decisions=format_decisions(decisions),
        risk_count=len(risk_items),
        risk_items=format_action_items(risk_items),
    )
    return call_claude(prompt, timeout=300)


# --------------------------------------------------------------------------- #
# Slack Canvas 投稿
# --------------------------------------------------------------------------- #
def sanitize_for_canvas(text: str) -> str:
    for old, new in {
        "～": " - ", "〜": " - ", "–": " - ", "—": " - ",
        "−": " - ", "‑": "-", "（": "(", "）": ")",
    }.items():
        text = text.replace(old, new)
    text = re.sub(r"^#{4,6}\s+", "### ", text, flags=re.MULTILINE)
    url_pattern = r"(?<!\[)(?<!\()(?<!\<)https?://[^\s\)>]+"
    text = re.sub(url_pattern,
                  lambda m: f"<{m.group(0).rstrip('.,;:!?、。')}>", text)
    return text


def post_to_canvas(canvas_id: str, content: str) -> None:
    token = os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN または SLACK_MCP_XOXB_TOKEN を設定してください",
              file=sys.stderr)
        sys.exit(1)
    app = App(token=token)
    try:
        app.client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "replace",
                "document_content": {"type": "markdown", "markdown": content},
            }],
        )
        print(f"✓ Canvas 更新成功: {canvas_id}")
    except SlackApiError as e:
        print(f"Slack API エラー: {e.response['error']}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="pm.db → 進捗レポート・アジェンダ生成・Canvas投稿")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--mode", choices=["report", "agenda", "both"], default="report",
                        help="生成モード (デフォルト: report)")
    parser.add_argument("--meeting-name", default="Leader_Meeting",
                        help="次回会議種別名 (agenda モード時に使用)")
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="投稿先 Canvas ID")
    parser.add_argument("--since", default=None, help="この日付以降のデータのみ対象 (YYYY-MM-DD)")
    parser.add_argument("--skip-canvas", action="store_true", help="Canvas 投稿をスキップ")
    parser.add_argument("--dry-run", action="store_true", help="Canvas 投稿なし・結果を標準出力のみ")
    parser.add_argument("--output", default=None, help="出力をファイルにも保存")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
    today = date.today().isoformat()

    output_file = open(args.output, "w", encoding="utf-8") if args.output else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    log(f"[INFO] pm.db     : {db_path}")
    log(f"[INFO] mode      : {args.mode}")
    log(f"[INFO] since     : {args.since or '全期間'}")
    log(f"[INFO] 生成日    : {today}")

    conn = open_pm_db(db_path)
    context = load_context()

    action_items = fetch_open_action_items(conn, args.since)
    decisions = fetch_recent_decisions(conn, args.since)
    meetings = fetch_recent_meetings(conn, args.since)
    risk_items = detect_risk_items(action_items)
    conn.close()

    log(f"[INFO] アクションアイテム: {len(action_items)}件 (うちリスク: {len(risk_items)}件)")
    log(f"[INFO] 決定事項          : {len(decisions)}件")
    log(f"[INFO] 会議              : {len(meetings)}件")

    results: list[tuple[str, str]] = []  # (mode_label, content)

    if args.mode in ("report", "both"):
        log("\n[INFO] 進捗レポートを生成中...")
        report = generate_report(action_items, decisions, meetings, risk_items, context, today)
        report = sanitize_for_canvas(report)
        log("\n" + "=" * 60)
        log(report)
        log("=" * 60)
        results.append(("report", report))

    if args.mode in ("agenda", "both"):
        log(f"\n[INFO] {args.meeting_name} アジェンダ草案を生成中...")
        agenda = generate_agenda(action_items, decisions, risk_items, context, today, args.meeting_name)
        agenda = sanitize_for_canvas(agenda)
        log("\n" + "=" * 60)
        log(agenda)
        log("=" * 60)
        results.append(("agenda", agenda))

    if args.output and output_file:
        output_file.close()

    if args.dry_run or args.skip_canvas:
        log("[INFO] Canvas 投稿をスキップしました")
        return

    # Canvas 投稿: both の場合はレポート + アジェンダを連結
    if results:
        content = "\n\n---\n\n".join(c for _, c in results)
        post_to_canvas(args.canvas_id, content)


if __name__ == "__main__":
    main()
