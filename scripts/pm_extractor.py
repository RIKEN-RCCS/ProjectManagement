#!/usr/bin/env python3
"""
pm_extractor.py

{channel_id}.db のスレッド要約を読み込み、LLMで決定事項・アクションアイテムを抽出して
pm.db に保存する。

Usage:
    python3 scripts/pm_extractor.py
    python3 scripts/pm_extractor.py -c C08SXA4M7JT
    python3 scripts/pm_extractor.py -c C08SXA4M7JT --since 2026-01-01
    python3 scripts/pm_extractor.py --force-reextract
    python3 scripts/pm_extractor.py --dry-run

Options:
    -c CHANNEL_ID       対象チャンネルID（デフォルト: C0A9KG036CS）
    --db-slack PATH     {channel_id}.db のパス（省略時は data/{channel_id}.db）
    --db-pm PATH        pm.db のパス（デフォルト: data/pm.db）
    --since YYYY-MM-DD  この日付以降の要約のみ対象
    --force-reextract   既に抽出済みのスレッドも再抽出
    --dry-run           DB保存なし・結果を標準出力のみ
    --output PATH       標準出力の内容をファイルにも保存
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEFAULT_CHANNEL = "C0A9KG036CS"
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"


# --------------------------------------------------------------------------- #
# pm.db スキーマ（meeting_parser.py と共通）
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   TEXT PRIMARY KEY,
    held_at      TEXT,
    kind         TEXT,
    file_path    TEXT,
    summary      TEXT,
    parsed_at    TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    assignee     TEXT,
    due_date     TEXT,
    status       TEXT DEFAULT 'open',
    note         TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    decided_at   TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS slack_extractions (
    thread_ts    TEXT,
    channel_id   TEXT,
    extracted_at TEXT,
    PRIMARY KEY (thread_ts, channel_id)
);
"""


def init_pm_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 既存DBへのマイグレーション: note列がなければ追加
    cols = [r[1] for r in conn.execute("PRAGMA table_info(action_items)").fetchall()]
    if "note" not in cols:
        conn.execute("ALTER TABLE action_items ADD COLUMN note TEXT")
    conn.commit()
    return conn


def open_slack_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: Slack DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# CLAUDE.md 読み込み（コンテキスト用）
# --------------------------------------------------------------------------- #
def load_context_from_claude_md() -> str:
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
# Slack DB からスレッド要約を取得
# --------------------------------------------------------------------------- #
def fetch_summaries(
    slack_conn: sqlite3.Connection,
    channel_id: str,
    since: str | None,
) -> list[dict]:
    query = """
        SELECT s.thread_ts, s.summary, s.summarized_at,
               m.timestamp, m.permalink, m.user_name
        FROM summaries s
        JOIN messages m ON s.thread_ts = m.thread_ts AND s.channel_id = m.channel_id
        WHERE s.channel_id = ?
    """
    params: list = [channel_id]
    if since:
        query += " AND m.timestamp >= ?"
        params.append(since)
    query += " ORDER BY m.timestamp ASC"
    rows = slack_conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def is_already_extracted(pm_conn: sqlite3.Connection, thread_ts: str, channel_id: str) -> bool:
    row = pm_conn.execute(
        "SELECT 1 FROM slack_extractions WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row is not None


def mark_extracted(pm_conn: sqlite3.Connection, thread_ts: str, channel_id: str) -> None:
    pm_conn.execute(
        "INSERT OR REPLACE INTO slack_extractions (thread_ts, channel_id, extracted_at) VALUES (?,?,?)",
        (thread_ts, channel_id, datetime.now().isoformat()),
    )


# --------------------------------------------------------------------------- #
# claude CLI 呼び出し
# --------------------------------------------------------------------------- #
def call_claude(prompt: str, timeout: int = 120) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# プロンプト
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のSlackスレッド要約を読み、決定事項とアクションアイテムを抽出してください。

## 指示

1. **明示されたものだけ抽出**: 要約に明示されていない内容を推測・補完しないこと
2. **出力形式**: 必ず以下のJSON形式のみ出力すること（前後の説明テキスト不要）
3. 決定事項・アクションアイテムがない場合は空配列 `[]` を返すこと

## プロジェクト文脈

{context}

## Slackスレッド要約

投稿日時: {timestamp}
投稿者: {user_name}
{summary}

## 出力JSON形式

```json
{{
  "decisions": [
    {{
      "content": "決定事項の内容",
      "decided_at": "YYYY-MM-DD または null"
    }}
  ],
  "action_items": [
    {{
      "content": "アクションアイテムの内容",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null"
    }}
  ]
}}
```
"""


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


def extract_from_summary(row: dict, context: str) -> dict:
    prompt = EXTRACT_PROMPT.format(
        context=context,
        timestamp=row.get("timestamp", "不明"),
        user_name=row.get("user_name", "不明"),
        summary=row["summary"],
    )
    raw = call_claude(prompt)
    return extract_json(raw)


# --------------------------------------------------------------------------- #
# pm.db へ保存
# --------------------------------------------------------------------------- #
def save_slack_items(
    pm_conn: sqlite3.Connection,
    thread_ts: str,
    channel_id: str,
    permalink: str | None,
    timestamp: str,
    extracted: dict,
) -> tuple[int, int]:
    now = datetime.now().isoformat()
    # decided_at の推定（要約投稿日をフォールバック）
    date_fallback = timestamp[:10] if timestamp else None
    source_ref = permalink or f"slack://{channel_id}/{thread_ts}"

    d_count = 0
    for d in extracted.get("decisions", []):
        decided_at = d.get("decided_at") or date_fallback
        pm_conn.execute(
            """
            INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref, extracted_at)
            VALUES (?, ?, ?, 'slack', ?, ?)
            """,
            (None, d["content"], decided_at, source_ref, now),
        )
        d_count += 1

    a_count = 0
    for a in extracted.get("action_items", []):
        pm_conn.execute(
            """
            INSERT INTO action_items
                (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at)
            VALUES (?, ?, ?, ?, 'open', 'slack', ?, ?)
            """,
            (None, a["content"], a.get("assignee"), a.get("due_date"), source_ref, now),
        )
        a_count += 1

    return d_count, a_count


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Slack要約 → pm.db への決定事項・アクションアイテム抽出")
    parser.add_argument("-c", "--channel", default=DEFAULT_CHANNEL, help="対象チャンネルID")
    parser.add_argument("--db-slack", default=None, help="{channel_id}.db のパス")
    parser.add_argument("--db-pm", default=None, help="pm.db のパス")
    parser.add_argument("--since", default=None, help="この日付以降の要約のみ対象 (YYYY-MM-DD)")
    parser.add_argument("--force-reextract", action="store_true", help="抽出済みスレッドも再処理")
    parser.add_argument("--dry-run", action="store_true", help="DB保存なし・結果を標準出力のみ")
    parser.add_argument("--output", default=None, help="標準出力の内容をファイルにも保存")
    args = parser.parse_args()

    channel_id = args.channel
    slack_db_path = Path(args.db_slack) if args.db_slack else REPO_ROOT / "data" / f"{channel_id}.db"
    pm_db_path = Path(args.db_pm) if args.db_pm else DEFAULT_PM_DB

    output_file = open(args.output, "w", encoding="utf-8") if args.output else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    log(f"[INFO] チャンネル  : {channel_id}")
    log(f"[INFO] Slack DB    : {slack_db_path}")
    log(f"[INFO] PM DB       : {pm_db_path}")
    if args.since:
        log(f"[INFO] since       : {args.since}")

    slack_conn = open_slack_db(slack_db_path)
    pm_conn = init_pm_db(pm_db_path)
    context = load_context_from_claude_md()

    summaries = fetch_summaries(slack_conn, channel_id, args.since)
    log(f"[INFO] 対象スレッド: {len(summaries)} 件")

    total_d = total_a = skipped = 0

    for i, row in enumerate(summaries, 1):
        ts = row["thread_ts"]
        if not args.force_reextract and is_already_extracted(pm_conn, ts, channel_id):
            skipped += 1
            continue

        log(f"\n[{i}/{len(summaries)}] {row.get('user_name')} ({row.get('timestamp', '')[:16]})")

        try:
            extracted = extract_from_summary(row, context)
        except Exception as e:
            log(f"  [WARN] 抽出失敗: {e}")
            continue

        d_count = len(extracted.get("decisions", []))
        a_count = len(extracted.get("action_items", []))

        if d_count == 0 and a_count == 0:
            log("  → 決定事項・アクションアイテムなし")
        else:
            for d in extracted.get("decisions", []):
                log(f"  [決定] {d['content']}")
            for a in extracted.get("action_items", []):
                assignee = a.get("assignee") or "未定"
                due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
                log(f"  [AI  ] [{assignee}] {a['content']}{due}")

        if not args.dry_run:
            nd, na = save_slack_items(
                pm_conn, ts, channel_id,
                row.get("permalink"), row.get("timestamp", ""), extracted,
            )
            mark_extracted(pm_conn, ts, channel_id)
            pm_conn.commit()
            total_d += nd
            total_a += na
        else:
            total_d += d_count
            total_a += a_count

    slack_conn.close()
    pm_conn.close()
    if output_file:
        output_file.close()

    log("\n" + "=" * 60)
    log(f"完了: decisions={total_d}件, action_items={total_a}件, スキップ={skipped}件")
    if args.dry_run:
        log("[INFO] --dry-run のため DB保存をスキップしました")


if __name__ == "__main__":
    main()
