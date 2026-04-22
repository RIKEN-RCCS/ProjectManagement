#!/usr/bin/env python3
"""
ingest_slack.py

Slack {channel_id}.db → pm.db へ決定事項・アクションアイテムを抽出するプラグイン。
元ロジックは pm_extractor.py から移植。pm_extractor.py は後方互換 CLI ラッパーとして残す。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db, normalize_assignee
from cli_utils import load_claude_md, call_claude
from ingest_plugin import IngestContext


# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEFAULT_CHANNEL = "C0A9KG036CS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_extractions (
    thread_ts    TEXT,
    channel_id   TEXT,
    extracted_at TEXT,
    PRIMARY KEY (thread_ts, channel_id)
);
"""


# --------------------------------------------------------------------------- #
# Slack DB 接続
# --------------------------------------------------------------------------- #
def open_slack_db(db_path: Path, no_encrypt: bool = False):
    if not db_path.exists():
        print(f"ERROR: Slack DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


# --------------------------------------------------------------------------- #
# pm.db 初期化（slack_extractions テーブル追加）
# --------------------------------------------------------------------------- #
def ensure_slack_extractions(pm_conn) -> None:
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                pm_conn.execute(stmt)
            except Exception:
                pass
    pm_conn.commit()


# --------------------------------------------------------------------------- #
# コンテキスト読み込み
# --------------------------------------------------------------------------- #
def load_context_from_claude_md() -> str:
    text = load_claude_md(CLAUDE_MD)
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
# マイルストーン取得
# --------------------------------------------------------------------------- #
def fetch_milestones(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT milestone_id, name, due_date, area FROM milestones WHERE status='active' ORDER BY due_date"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def format_milestones_for_prompt(milestones: list[dict]) -> str:
    if not milestones:
        return "（マイルストーン未登録）"
    lines = ["| ID | マイルストーン名 | 期限 | エリア |",
             "|----|----------------|------|--------|"]
    for m in milestones:
        lines.append(f"| {m['milestone_id']} | {m['name']} | {m.get('due_date') or '未定'} | {m.get('area') or ''} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# スレッド取得
# --------------------------------------------------------------------------- #
def fetch_threads(slack_conn, channel_id: str, since: str | None) -> list[dict]:
    query = """
        SELECT m.thread_ts, m.timestamp, m.permalink, m.user_name, m.text
        FROM messages m
        WHERE m.channel_id = ?
    """
    params: list = [channel_id]
    if since:
        query += " AND m.timestamp >= ?"
        params.append(since)
    query += " ORDER BY m.timestamp ASC"
    parents = slack_conn.execute(query, params).fetchall()

    results = []
    for p in parents:
        thread_ts = p["thread_ts"]
        lines = [f"[{(p['timestamp'] or '')[:16]}] {p['user_name'] or '不明'}: {p['text'] or ''}"]
        replies = slack_conn.execute(
            "SELECT timestamp, user_name, text FROM replies"
            " WHERE thread_ts=? AND channel_id=? ORDER BY msg_ts ASC",
            (thread_ts, channel_id),
        ).fetchall()
        for r in replies:
            lines.append(f"  [{(r['timestamp'] or '')[:16]}] {r['user_name'] or '不明'}: {r['text'] or ''}")
        results.append({
            "thread_ts": thread_ts,
            "thread_text": "\n".join(lines),
            "timestamp": p["timestamp"],
            "permalink": p["permalink"],
            "user_name": p["user_name"],
        })
    return results


# --------------------------------------------------------------------------- #
# 重複管理
# --------------------------------------------------------------------------- #
def is_already_extracted(pm_conn, thread_ts: str, channel_id: str) -> bool:
    row = pm_conn.execute(
        "SELECT 1 FROM slack_extractions WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row is not None


def mark_extracted(pm_conn, thread_ts: str, channel_id: str) -> None:
    pm_conn.execute(
        "INSERT OR REPLACE INTO slack_extractions (thread_ts, channel_id, extracted_at) VALUES (?,?,?)",
        (thread_ts, channel_id, datetime.now().isoformat()),
    )


# --------------------------------------------------------------------------- #
# LLM 抽出
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のSlackスレッドのメッセージを読み、決定事項とアクションアイテムを抽出してください。

## 指示

1. **明示されたものだけ抽出**: メッセージに明示されていない内容を推測・補完しないこと
2. **出力形式**: 必ず以下のJSON形式のみ出力すること（前後の説明テキスト不要）
3. 決定事項・アクションアイテムがない場合は空配列 `[]` を返すこと
4. **マイルストーン紐づけ**: 各アクションアイテムについて、下記「マイルストーン一覧」の
   いずれかに明らかに関連する場合は milestone_id を記入すること。判断できない場合は null。

## マイルストーン一覧

{milestones}

## プロジェクト文脈

{context}

## Slackスレッド

投稿日時: {timestamp}
投稿者: {user_name}
{thread_text}

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
      "due_date": "YYYY-MM-DD または null",
      "milestone_id": "マイルストーンID（M1等）または null"
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


def extract_from_thread(row: dict, context: str, milestones: list[dict]) -> dict:
    prompt = EXTRACT_PROMPT.format(
        context=context,
        timestamp=row.get("timestamp", "不明"),
        user_name=row.get("user_name", "不明"),
        thread_text=row["thread_text"],
        milestones=format_milestones_for_prompt(milestones),
    )
    raw = call_claude(prompt)
    return extract_json(raw)


# --------------------------------------------------------------------------- #
# pm.db 書き込み
# --------------------------------------------------------------------------- #
def save_slack_items(
    pm_conn,
    thread_ts: str,
    channel_id: str,
    permalink: str | None,
    timestamp: str,
    extracted: dict,
) -> tuple[int, int]:
    post_date = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
    source_ref = permalink or f"slack://{channel_id}/{thread_ts}"

    d_count = 0
    for d in extracted.get("decisions", []):
        if not d.get("content"):
            continue
        decided_at = d.get("decided_at") or post_date
        pm_conn.execute(
            "INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref, extracted_at)"
            " VALUES (?, ?, ?, 'slack', ?, ?)",
            (None, d["content"], decided_at, source_ref, post_date),
        )
        d_count += 1

    a_count = 0
    for a in extracted.get("action_items", []):
        if not a.get("content"):
            continue
        pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at, milestone_id)"
            " VALUES (?, ?, ?, ?, 'open', 'slack', ?, ?, ?)",
            (None, a["content"], normalize_assignee(a.get("assignee")), a.get("due_date"),
             source_ref, post_date, a.get("milestone_id")),
        )
        a_count += 1

    return d_count, a_count


# --------------------------------------------------------------------------- #
# 抽出済み一覧表示
# --------------------------------------------------------------------------- #
def cmd_list_extractions(slack_conn, pm_conn, channel_id: str, since: str | None, log=print) -> None:
    se_query = "SELECT thread_ts, extracted_at FROM slack_extractions WHERE channel_id = ?"
    se_params: list = [channel_id]
    if since:
        se_query += " AND extracted_at >= ?"
        se_params.append(since)

    se_rows = pm_conn.execute(se_query, se_params).fetchall()

    ts_map: dict[str, str] = {}
    if se_rows:
        placeholders = ",".join("?" * len(se_rows))
        ts_rows = slack_conn.execute(
            f"SELECT thread_ts, timestamp FROM messages WHERE channel_id = ? AND thread_ts IN ({placeholders})",
            [channel_id] + [r["thread_ts"] for r in se_rows],
        ).fetchall()
        ts_map = {r["thread_ts"]: r["timestamp"] for r in ts_rows}

    sorted_rows = sorted(se_rows, key=lambda r: ts_map.get(r["thread_ts"], r["extracted_at"]))

    log(f"抽出済みスレッド一覧（チャンネル: {channel_id}）")
    log("─" * 50)
    for i, row in enumerate(sorted_rows, 1):
        ts = (ts_map.get(row["thread_ts"]) or "")[:19]
        extracted = (row["extracted_at"] or "")[:19]
        log(f"[{i:3d}] {ts}  抽出: {extracted}")
    log(f"合計: {len(sorted_rows)} 件")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class SlackIngestPlugin:
    source_name = "slack"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--slack-channel", default=DEFAULT_CHANNEL,
            metavar="CHANNEL_ID",
            help="対象チャンネルID（slack ソース用、デフォルト: C0A9KG036CS）",
        )
        parser.add_argument(
            "--slack-db", default=None,
            metavar="PATH",
            help="{channel_id}.db のパス（slack ソース用、省略時は data/{channel_id}.db）",
        )
        parser.add_argument(
            "--slack-force-reextract", action="store_true",
            help="抽出済みスレッドも再処理（slack ソース用）",
        )
        parser.add_argument(
            "--slack-list", action="store_true",
            help="抽出済みスレッドの一覧を表示して終了（slack ソース用）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        channel_id = args.slack_channel
        slack_db_path = (
            Path(args.slack_db) if args.slack_db
            else ctx.repo_root / "data" / f"{channel_id}.db"
        )

        slack_conn = open_slack_db(slack_db_path, no_encrypt=ctx.no_encrypt)
        ensure_slack_extractions(ctx.pm_conn)

        if getattr(args, "slack_list", False):
            cmd_list_extractions(slack_conn, ctx.pm_conn, channel_id, ctx.since, log=ctx.log)
            slack_conn.close()
            return

        ctx.log(f"[INFO] チャンネル  : {channel_id}")
        ctx.log(f"[INFO] Slack DB    : {slack_db_path}")
        if ctx.since:
            ctx.log(f"[INFO] since       : {ctx.since}")

        context = load_context_from_claude_md()
        milestones = fetch_milestones(ctx.pm_conn)
        ctx.log(f"[INFO] マイルストーン: {len(milestones)} 件")

        threads = fetch_threads(slack_conn, channel_id, ctx.since)
        ctx.log(f"[INFO] 対象スレッド: {len(threads)} 件")

        total_d = total_a = skipped = 0
        force_reextract = getattr(args, "slack_force_reextract", False)

        for i, row in enumerate(threads, 1):
            ts = row["thread_ts"]
            if not force_reextract and is_already_extracted(ctx.pm_conn, ts, channel_id):
                skipped += 1
                continue

            ctx.log(f"\n[{i}/{len(threads)}] {row.get('user_name')} ({row.get('timestamp', '')[:16]})")

            if ctx.dry_run:
                ctx.log("  [INFO] --dry-run のため LLM呼び出し・DB保存をスキップしました")
                skipped += 1
                continue

            try:
                extracted = extract_from_thread(row, context, milestones)
            except Exception as e:
                ctx.log(f"  [WARN] 抽出失敗: {e}")
                continue

            d_count = len(extracted.get("decisions", []))
            a_count = len(extracted.get("action_items", []))

            if d_count == 0 and a_count == 0:
                ctx.log("  → 決定事項・アクションアイテムなし")
            else:
                for d in extracted.get("decisions", []):
                    ctx.log(f"  [決定] {d['content']}")
                for a in extracted.get("action_items", []):
                    assignee = a.get("assignee") or "未定"
                    due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
                    ctx.log(f"  [AI  ] [{assignee}] {a['content']}{due}")

            nd, na = save_slack_items(
                ctx.pm_conn, ts, channel_id,
                row.get("permalink"), row.get("timestamp", ""), extracted,
            )
            mark_extracted(ctx.pm_conn, ts, channel_id)
            ctx.pm_conn.commit()
            total_d += nd
            total_a += na

        slack_conn.close()

        ctx.log("\n" + "=" * 60)
        ctx.log(f"完了: decisions={total_d}件, action_items={total_a}件, スキップ={skipped}件")
