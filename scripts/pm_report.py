#!/usr/bin/env python3
"""
pm_report.py

pm.db から決定事項・アクションアイテム・会議情報を読み込み、
LLMで週次進捗レポートを生成して Slack Canvas に投稿する。

レポート構成:
  サマリー → 直近の決定事項 → 要注意事項 → 未完了アクションアイテム（表形式）

Usage:
    python3 scripts/pm_report.py
    python3 scripts/pm_report.py --since 2026-01-01
    python3 scripts/pm_report.py --dry-run --output report.md

Options:
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --canvas-id ID          投稿先 Canvas ID
    --since YYYY-MM-DD      この日付以降のデータのみ対象
    --skip-canvas           Canvas 投稿をスキップ
    --dry-run               結果を標準出力のみ（Canvas投稿なし）
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db

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
def open_pm_db(db_path: Path, no_encrypt: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


def fetch_open_action_items(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    query = """
        SELECT a.id, a.content, a.assignee, a.due_date, a.status,
               a.note, a.source, a.source_ref, a.extracted_at, a.meeting_id,
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

以下の3セクションのみMarkdownで出力してください。アクションアイテムの表は別途追加するため出力不要です。

# 富岳NEXT プロジェクト進捗レポート（{today}）

## サマリー
（3〜5行で全体状況を要約）

## 直近の決定事項
（箇条書き、ソース参照付き）

## 要注意事項
（リスク・懸念・遅延の可能性があるもの。なければ「特になし」と記載）
"""


def format_action_items(items: list[dict]) -> str:
    """Canvas に貼るアクションアイテム表（ID・対応状況列付き）"""
    if not items:
        return "（なし）"
    header = "| ID | 担当者 | 内容 | 期限 | ソース | 対応状況 |"
    sep    = "|----|--------|------|------|--------|----------|"
    rows = [header, sep]
    for a in items:
        ai_id    = a.get("id", "")
        assignee = a.get("assignee") or "未定"
        content  = a.get("content", "").replace("|", "｜")
        due      = a.get("due_date") or ""
        source   = a.get("source") or ""
        note     = a.get("note") or ""
        rows.append(f"| {ai_id} | {assignee} | {content} | {due} | {source} | {note} |")
    return "\n".join(rows)


def format_action_items_text(items: list[dict]) -> str:
    """LLMプロンプト用テキスト形式（表ではなく箇条書き）"""
    if not items:
        return "（なし）"
    lines = []
    for a in items:
        assignee = a.get("assignee") or "未定"
        due = f" 期限:{a['due_date']}" if a.get("due_date") else ""
        lines.append(f"- [ID:{a.get('id','')}][{assignee}]{due} {a['content']}")
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
        action_items=format_action_items_text(action_items),
        d_count=len(decisions),
        decisions=format_decisions(decisions),
        m_count=len(meetings),
        meetings=format_meetings(meetings),
        risk_count=len(risk_items),
        risk_items=format_action_items_text(risk_items),
    )
    llm_output = call_claude(prompt, timeout=300)
    # LLMが生成したMarkdownテーブルを箇条書きに変換（Canvasに2つのテーブルが混在するのを防ぐ）
    llm_output = _table_to_list(llm_output)
    # アクションアイテム表をLLM出力の末尾に追記
    table = format_action_items(action_items)
    return llm_output.rstrip() + f"\n\n## 未完了アクションアイテム\n\n{table}"


def _table_to_list(text: str) -> str:
    """LLM出力内のMarkdownテーブルを箇条書きに変換する"""
    lines = text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # テーブルヘッダー行を検出
        if re.match(r"^\s*\|.+\|", line):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 1
            # セパレーター行をスキップ
            if i < len(lines) and re.match(r"^\s*\|[-| :]+\|", lines[i]):
                i += 1
            # データ行を箇条書きに変換
            while i < len(lines) and re.match(r"^\s*\|.+\|", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                parts = [f"{h}: {c}" for h, c in zip(headers, cells) if c]
                result.append("- " + ", ".join(parts))
                i += 1
        else:
            result.append(line)
            i += 1
    return "\n".join(result)




# --------------------------------------------------------------------------- #
# Slack Canvas 投稿
# --------------------------------------------------------------------------- #
def sanitize_for_canvas(text: str) -> str:
    # 記号・特殊文字を標準的な文字に置換
    replacements = {
        # ダッシュ・ハイフン類
        "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u2212": "-", "\u2011": "-", "\u2010": "-",
        # 波ダッシュ・チルダ類
        "\uff5e": "-", "\u301c": "-",
        # 全角括弧
        "\uff08": "(", "\uff09": ")",
        # 全角記号
        "\uff0c": ",", "\uff0e": ".", "\uff01": "!",
        "\uff1a": ":", "\uff1b": ";", "\uff1f": "?",
        # 引用符類
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
        # 矢印類
        "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
        "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>",
        "\u25b6": ">", "\u25c0": "<",
        # 点・中黒
        "\u30fb": ".", "\u2022": "-", "\u2023": "-",
        "\u25cf": "-", "\u25cb": "-", "\u2027": ".",
        # スペース類
        "\u3000": " ", "\u00a0": " ",
        # その他よく出る記号
        "\u2026": "...", "\u22ef": "...",
        "\u00d7": "x", "\u00f7": "/",
        "\u2605": "*", "\u2606": "*",
        "\u2713": "OK", "\u2714": "OK", "\u2715": "NG", "\u2716": "NG",
        "\u25a0": "-", "\u25a1": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # h4以下の見出しはh3に統一（Canvasで未サポート）
    text = re.sub(r"^#{4,6}\s+", "### ", text, flags=re.MULTILINE)
    # インデントされた番号リストをリストに変換
    text = re.sub(r"^(\s+)\d+\.\s+", r"\1- ", text, flags=re.MULTILINE)
    # ブロッククオート内のリスト項目からブロッククオートを除去
    # (Slack Canvas は blockquote 内の List ブロックをサポートしない)
    text = re.sub(r"^> (-|\*|\d+\.)\s+", r"\1 ", text, flags=re.MULTILINE)

    # 上記で対処できなかった非ASCII・非日本語の特殊記号を除去
    # 日本語(CJK)・英数字・基本記号・改行・スペースは保持
    def keep_char(c: str) -> str:
        cp = ord(c)
        # ASCII printable
        if 0x20 <= cp <= 0x7E:
            return c
        # 改行・タブ
        if c in ("\n", "\t"):
            return c
        # 日本語: ひらがな・カタカナ・漢字・半角カタカナ・記号
        if 0x3000 <= cp <= 0x9FFF:
            return c
        if 0xF900 <= cp <= 0xFAFF:
            return c
        if 0xFF00 <= cp <= 0xFFEF:
            return c
        # latin拡張（アクセント付き文字など）
        if 0x00C0 <= cp <= 0x024F:
            return c
        # それ以外の特殊記号は除去
        return ""

    text = "".join(keep_char(c) for c in text)

    # 連続する空行を1行に圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def post_to_canvas(canvas_id: str, content: str) -> None:
    token = os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN または SLACK_MCP_XOXB_TOKEN を設定してください",
              file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Canvas投稿コンテンツ: {len(content)} 文字")
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
        print(f"レスポンス詳細: {e.response}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="pm.db → 進捗レポート・アジェンダ生成・Canvas投稿")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="投稿先 Canvas ID")
    parser.add_argument("--since", default=None, help="この日付以降のデータのみ対象 (YYYY-MM-DD)")
    parser.add_argument("--skip-canvas", action="store_true", help="Canvas 投稿をスキップ")
    parser.add_argument("--dry-run", action="store_true", help="Canvas 投稿なし・結果を標準出力のみ")
    parser.add_argument("--output", default=None, help="出力をファイルにも保存")
    parser.add_argument("--no-encrypt", action="store_true", help="DBを暗号化しない（平文モード）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
    today = date.today().isoformat()

    output_file = open(args.output, "w", encoding="utf-8") if args.output else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    log(f"[INFO] pm.db     : {db_path}")
    log(f"[INFO] since     : {args.since or '全期間'}")
    log(f"[INFO] 生成日    : {today}")

    conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    context = load_context()

    action_items = fetch_open_action_items(conn, args.since)
    decisions = fetch_recent_decisions(conn, args.since)
    meetings = fetch_recent_meetings(conn, args.since)
    risk_items = detect_risk_items(action_items)
    conn.close()

    log(f"[INFO] アクションアイテム: {len(action_items)}件 (うちリスク: {len(risk_items)}件)")
    log(f"[INFO] 決定事項          : {len(decisions)}件")
    log(f"[INFO] 会議              : {len(meetings)}件")

    log("\n[INFO] 進捗レポートを生成中...")
    report = generate_report(action_items, decisions, meetings, risk_items, context, today)
    report = sanitize_for_canvas(report)
    log("\n" + "=" * 60)
    log(report)
    log("=" * 60)

    if args.dry_run or args.skip_canvas:
        log("[INFO] Canvas 投稿をスキップしました")
        if output_file:
            output_file.close()
        return

    post_to_canvas(args.canvas_id, report)

    if output_file:
        output_file.close()


if __name__ == "__main__":
    main()
