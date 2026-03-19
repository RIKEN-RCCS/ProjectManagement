#!/usr/bin/env python3
"""
Slack要約パイプライン（Phase 1: DB差分処理版）

処理フロー:
  1. Slack MCPサーバーからチャンネル履歴を取得
     - DBに存在しない新規スレッドのみ全取得
     - DBに存在するが返信が増えたスレッドのみ再取得
     - 変化のないスレッドはスキップ（API呼び出しなし）
  2. 新規・更新スレッドのみ Claude CLI で要約しDBに蓄積
     - 変化のないスレッドはDBの要約をそのまま利用（LLM呼び出しなし）
  3. DB内の全要約（--since フィルタ適用）を統合して全体要約を生成
     - --canvas-id 指定時: Canvas に投稿
     - --output 指定時: ファイルに保存
"""

import asyncio
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import add_no_encrypt_arg, add_output_arg, make_logger

from slack_bolt import App
from slack_sdk.errors import SlackApiError

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ------------------------------------------------------------------ 定数
JST = timezone(timedelta(hours=9))
DEFAULT_CHANNEL = "C0A9KG036CS"
DEFAULT_DB = None  # デフォルトは {channel_id}.db

# ------------------------------------------------------------------ メモリキャッシュ
user_cache: dict = {}
channel_cache: dict = {}
permalink_cache: dict = {}
workspace_domain: str | None = None


# ==================================================================
# 引数パース
# ==================================================================

def parse_date_arg(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    except ValueError:
        raise ValueError(f"日付は YYYY-MM-DD 形式で指定してください: {date_str}")


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        description="Slack チャンネル履歴の差分取得・要約を一括実行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  %(prog)s                              # 差分のみ取得・要約
  %(prog)s --since 2026-01-01 -l 200   # 初回: 範囲を絞って全件取得
  %(prog)s --skip-fetch                 # DBの既存データから要約のみ
  %(prog)s --force-resummary            # 全スレッドを強制的に再要約
  %(prog)s --skip-llm                   # 取得・DB保存のみ（LLM呼び出しなし）
        """,
    )
    parser.add_argument("-c", "--channel", default=DEFAULT_CHANNEL,
                        help=f"チャンネルID (デフォルト: {DEFAULT_CHANNEL})")
    parser.add_argument("-l", "--limit", type=int, default=100,
                        help="取得するメッセージ数の上限 (デフォルト: 100)")
    parser.add_argument("--since", type=parse_date_arg,
                        help="この日付以降のメッセージのみ対象 (YYYY-MM-DD, JST)")
    parser.add_argument("--db", default=None,
                        help="SQLite DB ファイルパス (デフォルト: {channel_id}.db)")
    parser.add_argument("--no-permalink", action="store_true", default=False,
                        help="パーマリンク取得を無効化")
    parser.add_argument("--skip-fetch", action="store_true", default=False,
                        help="Slack API 取得をスキップ（DB のみ使用）")
    parser.add_argument("--force-resummary", action="store_true", default=False,
                        help="全スレッドを強制的に再要約（差分無視）")
    parser.add_argument("--skip-llm", action="store_true", default=False,
                        help="LLM呼び出し（スレッド要約・全体要約）をスキップ")
    parser.add_argument("--list", action="store_true", default=False,
                        help="DB内のスレッド要約一覧を表示して終了（--since 併用可）")
    parser.add_argument("--canvas-id", default=None,
                        help="Canvas ID（指定時のみ Canvas に全体要約を投稿）")
    parser.add_argument("--skip-canvas", action="store_true", default=False,
                        help="Canvas 投稿をスキップ（全体要約は生成する）")
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="LLM呼び出しをスキップ（Slack API・DB書き込みは実行される）")
    return parser.parse_args()


# ==================================================================
# DB レイヤー
# ==================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    thread_ts   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    user_id     TEXT,
    user_name   TEXT,
    text        TEXT,
    timestamp   TEXT,
    permalink   TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (thread_ts, channel_id)
);

CREATE TABLE IF NOT EXISTS replies (
    msg_ts      TEXT NOT NULL,
    thread_ts   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    user_id     TEXT,
    user_name   TEXT,
    text        TEXT,
    timestamp   TEXT,
    permalink   TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (msg_ts, channel_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    thread_ts       TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    summarized_at   TEXT NOT NULL,
    last_reply_ts   TEXT,
    PRIMARY KEY (thread_ts, channel_id)
);
"""


def init_db(db_path: str, no_encrypt: bool = False) -> sqlite3.Connection:
    return open_db(Path(db_path), encrypt=not no_encrypt, schema=SCHEMA)


def db_upsert_message(conn: sqlite3.Connection, channel_id: str, msg: dict) -> None:
    conn.execute(
        """INSERT INTO messages (thread_ts, channel_id, user_id, user_name, text,
                                 timestamp, permalink, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(thread_ts, channel_id) DO UPDATE SET
               user_id=excluded.user_id, user_name=excluded.user_name,
               text=excluded.text, timestamp=excluded.timestamp,
               permalink=excluded.permalink, fetched_at=excluded.fetched_at""",
        (
            msg["timestamp_unix"], channel_id,
            msg.get("user_id"), msg.get("user_name"), msg.get("message"),
            msg.get("timestamp"), msg.get("permalink"),
            datetime.now().isoformat(),
        ),
    )


def db_upsert_reply(conn: sqlite3.Connection, channel_id: str,
                    thread_ts: str, reply: dict) -> None:
    conn.execute(
        """INSERT INTO replies (msg_ts, thread_ts, channel_id, user_id, user_name,
                                text, timestamp, permalink, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(msg_ts, channel_id) DO UPDATE SET
               user_id=excluded.user_id, user_name=excluded.user_name,
               text=excluded.text, timestamp=excluded.timestamp,
               permalink=excluded.permalink, fetched_at=excluded.fetched_at""",
        (
            reply["timestamp_unix"], thread_ts, channel_id,
            reply.get("user_id"), reply.get("user_name"), reply.get("message"),
            reply.get("timestamp"), reply.get("permalink"),
            datetime.now().isoformat(),
        ),
    )


def db_upsert_summary(conn: sqlite3.Connection, channel_id: str,
                      thread_ts: str, summary: str, last_reply_ts: str | None) -> None:
    conn.execute(
        """INSERT INTO summaries (thread_ts, channel_id, summary, summarized_at, last_reply_ts)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(thread_ts, channel_id) DO UPDATE SET
               summary=excluded.summary, summarized_at=excluded.summarized_at,
               last_reply_ts=excluded.last_reply_ts""",
        (thread_ts, channel_id, summary, datetime.now().isoformat(), last_reply_ts),
    )


def db_get_summary(conn: sqlite3.Connection, channel_id: str,
                   thread_ts: str) -> dict | None:
    row = conn.execute(
        "SELECT summary, last_reply_ts FROM summaries WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return dict(row) if row else None


def db_get_thread(conn: sqlite3.Connection, channel_id: str, thread_ts: str) -> dict:
    """DBからスレッド（親＋返信）を取得して要約用のchunk形式で返す"""
    parent_row = conn.execute(
        "SELECT * FROM messages WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    if not parent_row:
        return {}

    parent = dict(parent_row)
    reply_rows = conn.execute(
        "SELECT * FROM replies WHERE thread_ts=? AND channel_id=? ORDER BY msg_ts ASC",
        (thread_ts, channel_id),
    ).fetchall()

    # chunk形式に変換（summarization関数が期待する形式）
    def row_to_msg(row: dict, is_reply: bool) -> dict:
        return {
            "timestamp_unix": row.get("msg_ts") or row.get("thread_ts"),
            "timestamp": row["timestamp"] or "",
            "user_id": row["user_id"] or "",
            "user_name": row["user_name"] or "不明",
            "message": row["text"] or "",
            "permalink": row["permalink"] or "",
            "is_reply": is_reply,
            "thread_ts": thread_ts,
        }

    replies = [row_to_msg(dict(r), True) for r in reply_rows]
    return {
        "parent": row_to_msg(parent, False),
        "replies": replies,
        "type": "thread" if replies else "single",
    }


def db_get_max_reply_ts(conn: sqlite3.Connection, channel_id: str,
                        thread_ts: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(msg_ts) AS max_ts FROM replies WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row["max_ts"] if row else None


# ==================================================================
# Permalink ヘルパー（変更なし）
# ==================================================================

def build_permalink_fallback(channel_id: str, message_ts: str,
                              thread_ts: str = None) -> str:
    ts_no_dot = message_ts.replace(".", "")
    domain = workspace_domain or "WORKSPACE"
    url = f"https://{domain}.slack.com/archives/{channel_id}/p{ts_no_dot}"
    if thread_ts and thread_ts != message_ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url


async def get_permalink_via_api(channel_id: str, message_ts: str) -> str | None:
    global workspace_domain
    token = os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        return None
    params = urllib.parse.urlencode({"channel": channel_id, "message_ts": message_ts})
    req = urllib.request.Request(f"https://slack.com/api/chat.getPermalink?{params}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                permalink = data.get("permalink", "")
                if permalink and not workspace_domain:
                    m = re.match(r"https://([^.]+)\.slack\.com/", permalink)
                    if m:
                        workspace_domain = m.group(1)
                        print(f"  ワークスペースドメイン検出: {workspace_domain}",
                              file=sys.stderr)
                return permalink
            print(f"  Permalink API エラー ({message_ts}): {data.get('error')}",
                  file=sys.stderr)
    except Exception as e:
        print(f"  Permalink API 通信エラー ({message_ts}): {e}", file=sys.stderr)
    return None


async def get_permalink_via_mcp(session, channel_id: str,
                                message_ts: str) -> str | None:
    try:
        result = await session.call_tool(
            "chat_getPermalink",
            arguments={"channel": channel_id, "message_ts": message_ts},
        )
        for content in result.content:
            if hasattr(content, "text"):
                try:
                    data = json.loads(content.text)
                    if data.get("ok"):
                        return data.get("permalink")
                except json.JSONDecodeError:
                    m = re.search(
                        r"https://[^\s]+slack\.com/archives/[^\s]+", content.text
                    )
                    if m:
                        return m.group(0)
    except Exception:
        pass
    return None


async def get_permalink(session, channel_id: str, message_ts: str,
                        thread_ts: str = None) -> str:
    cache_key = (channel_id, message_ts)
    if cache_key in permalink_cache:
        return permalink_cache[cache_key]
    permalink = await get_permalink_via_mcp(session, channel_id, message_ts)
    if not permalink:
        permalink = await get_permalink_via_api(channel_id, message_ts)
    if not permalink:
        permalink = build_permalink_fallback(channel_id, message_ts, thread_ts)
        print(f"  ⚠ フォールバックURL使用: {message_ts}", file=sys.stderr)
    permalink_cache[cache_key] = permalink
    return permalink


# ==================================================================
# メッセージ整形ヘルパー（変更なし）
# ==================================================================

def format_timestamp(time_str: str) -> str:
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time_str


def parse_message_time(time_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except Exception:
        return None


def is_message_after_date(msg: dict, since_date: datetime | None) -> bool:
    if since_date is None:
        return True
    t = parse_message_time(msg.get("Time", ""))
    return t is None or t >= since_date


def thread_has_recent_replies(replies: list, since_date: datetime | None) -> bool:
    if since_date is None:
        return True
    return any(is_message_after_date(r, since_date) for r in replies)


async def get_thread_replies(session, channel_id: str, thread_ts: str) -> list:
    try:
        result = await session.call_tool(
            "conversations_replies",
            arguments={"channel_id": channel_id, "thread_ts": thread_ts, "limit": "100"},
        )
        for content in result.content:
            if hasattr(content, "text"):
                reader = csv.DictReader(StringIO(content.text))
                return [row for row in reader if row.get("MsgID") != thread_ts]
    except Exception as e:
        print(f"スレッド取得エラー ({thread_ts}): {e}", file=sys.stderr)
    return []


async def format_message_from_csv(session, msg: dict, channel_id: str,
                                   is_reply: bool = False,
                                   fetch_permalink: bool = True,
                                   parent_thread_ts: str = None) -> dict:
    user_id = msg.get("UserID", "不明")
    user_name = msg.get("UserName", user_id)
    text = msg.get("Text", "")
    time_str = msg.get("Time", "")
    msg_id = msg.get("MsgID", "")
    thread_ts = msg.get("ThreadTs", "")

    formatted_time = format_timestamp(time_str)
    if user_id and user_name:
        user_cache[user_id] = user_name

    permalink = ""
    if fetch_permalink and msg_id:
        effective_thread_ts = parent_thread_ts if is_reply else None
        permalink = await get_permalink(session, channel_id, msg_id, effective_thread_ts)

    indent = "  " if is_reply else ""
    link_info = " 🔗" if permalink else ""
    print(f"{indent}{formatted_time}：{user_name}：{text}{link_info}")

    return {
        "timestamp_unix": msg_id,
        "timestamp": formatted_time,
        "is_reply": is_reply,
        "user_id": user_id,
        "user_name": user_name,
        "message": text,
        "type": "user_message",
        "reply_count": 0,
        "thread_ts": thread_ts if thread_ts else msg_id,
        "permalink": permalink,
    }


# ==================================================================
# ステップ1: 差分取得 & DB保存
# ==================================================================

def _find_mcp_binary() -> str:
    path = shutil.which("slack-mcp-server")
    if not path:
        for p in [
            os.path.expanduser("~/bin/slack-mcp-server"),
            os.path.expanduser("~/slack-mcp-server/slack-mcp-server"),
            os.path.expanduser("~/.local/bin/slack-mcp-server"),
            "/usr/local/bin/slack-mcp-server",
        ]:
            if os.path.exists(p):
                path = p
                break
    if not path:
        print("エラー: slack-mcp-server が見つかりません", file=sys.stderr)
        sys.exit(1)
    return path


async def fetch_and_store(
    conn: sqlite3.Connection,
    channel_id: str,
    limit: int,
    since_date: datetime | None,
    fetch_permalink: bool,
    force_resummary: bool,
) -> list[str]:
    """
    Slack から履歴を取得してDBに保存し、要約が必要なスレッドの thread_ts リストを返す。

    差分ロジック:
    - DBに存在しない thread_ts → 新規: 返信を取得してDBに保存、要約対象に追加
    - DBに存在する thread_ts:
        - history レスポンスに含まれる最新返信ts > DB保存済みの last_reply_ts → 更新あり
        - 変化なし → スキップ（API・LLM呼び出しなし）
    - force_resummary=True → 全スレッドを要約対象に追加（取得は差分のみ）
    """
    slack_token = os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not slack_token:
        print("エラー: SLACK_MCP_XOXB_TOKEN 環境変数を設定してください", file=sys.stderr)
        sys.exit(1)

    binary_path = _find_mcp_binary()
    print(f"MCPサーバーバイナリ: {binary_path}", file=sys.stderr)

    server_params = StdioServerParameters(
        command=binary_path, args=[], env={"SLACK_MCP_XOXB_TOKEN": slack_token}
    )

    needs_summarize: list[str] = []

    print("Slack MCPサーバーを起動中...", file=sys.stderr)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Slack MCPサーバーに接続しました", file=sys.stderr)

            # since_date を oldest (unix timestamp文字列) に変換
            oldest_ts = str(since_date.timestamp()) if since_date else ""

            # ページネーションで全件取得
            all_rows: list[dict] = []
            cursor = ""
            page = 0
            print(f"チャンネル {channel_id} の履歴を取得中"
                  + (f" (oldest={oldest_ts})" if oldest_ts else "") + "...", file=sys.stderr)
            while True:
                page += 1
                if cursor:
                    args = {
                        "channel_id": channel_id,
                        "limit": "",          # cursor使用時はlimit不要
                        "cursor": cursor,
                        "include_activity_messages": False,
                    }
                else:
                    args = {
                        "channel_id": channel_id,
                        "limit": str(limit),
                        "include_activity_messages": False,
                    }
                    if oldest_ts:
                        args["oldest"] = oldest_ts

                result = await session.call_tool("conversations_history", arguments=args)

                page_rows: list[dict] = []
                next_cursor = ""
                for content in result.content:
                    if not hasattr(content, "text"):
                        continue
                    rows = list(csv.DictReader(StringIO(content.text)))
                    if not rows:
                        break
                    # 最終行の最終カラムが next_cursor
                    last_row = rows[-1]
                    last_val = list(last_row.values())[-1] if last_row else ""
                    # next_cursor 行かどうかはカラム名で判定
                    last_key = list(last_row.keys())[-1] if last_row else ""
                    if last_key.lower() in ("cursor", "next_cursor") and last_val:
                        next_cursor = last_val
                        rows = rows[:-1]  # cursor行はデータから除外
                    else:
                        # MCPサーバーはCursorフィールドを最終メッセージに埋め込む場合もある
                        cursor_val = last_row.get("Cursor", "")
                        if cursor_val:
                            next_cursor = cursor_val
                    page_rows.extend(rows)

                all_rows.extend(page_rows)
                print(f"  ページ{page}: {len(page_rows)}件取得 (累計: {len(all_rows)}件)",
                      file=sys.stderr)

                if not next_cursor:
                    break
                cursor = next_cursor

            # ユーザーキャッシュ構築
            for row in all_rows:
                uid, uname = row.get("UserID", ""), row.get("UserName", "")
                if uid and uname:
                    user_cache[uid] = uname

            # history レスポンス内で各スレッドの最新返信tsを収集
            # （conversations_history は親メッセージと返信を混在して返す）
            latest_reply_in_history: dict[str, str] = {}
            for row in all_rows:
                t_ts = row.get("ThreadTs", "")
                msg_id = row.get("MsgID", "")
                if t_ts and t_ts != msg_id:
                    # 返信行: thread_ts に対する最新 msg_id を記録
                    if t_ts not in latest_reply_in_history or \
                            msg_id > latest_reply_in_history[t_ts]:
                        latest_reply_in_history[t_ts] = msg_id

            # 親メッセージのみ処理（スレッド返信は後でまとめて取得）
            parent_rows = []
            for row in all_rows:
                t_ts = row.get("ThreadTs", "")
                msg_id = row.get("MsgID", "")
                # 返信行はスキップ
                if t_ts and t_ts != msg_id:
                    continue
                parent_rows.append(row)

            print(f"処理対象の親メッセージ: {len(parent_rows)}件", file=sys.stderr)

            stats = {"new": 0, "updated": 0, "skipped": 0}

            for row in parent_rows:
                msg_id = row.get("MsgID", "")
                t_ts = row.get("ThreadTs", "") or msg_id
                # 親メッセージの thread_ts は MsgID と同じ（またはThreadTsが空の場合もMsgID）

                existing_summary = db_get_summary(conn, channel_id, t_ts)
                history_latest_reply = latest_reply_in_history.get(t_ts)

                # ---- 差分判定 ----
                is_new = existing_summary is None
                is_updated = (
                    not is_new
                    and history_latest_reply is not None
                    and history_latest_reply > (existing_summary.get("last_reply_ts") or "0")
                )

                if not is_new and not is_updated and not force_resummary:
                    stats["skipped"] += 1
                    continue

                # ---- 親メッセージを整形してDBに保存 ----
                fmt_parent = await format_message_from_csv(
                    session, row, channel_id,
                    is_reply=False, fetch_permalink=fetch_permalink,
                )
                db_upsert_message(conn, channel_id, fmt_parent)

                # ---- 返信を取得してDBに保存 ----
                reply_rows = await get_thread_replies(session, channel_id, t_ts)
                for r_row in reply_rows:
                    fmt_reply = await format_message_from_csv(
                        session, r_row, channel_id,
                        is_reply=True, fetch_permalink=fetch_permalink,
                        parent_thread_ts=t_ts,
                    )
                    db_upsert_reply(conn, channel_id, t_ts, fmt_reply)

                conn.commit()

                if is_new:
                    stats["new"] += 1
                    status = "新規"
                else:
                    stats["updated"] += 1
                    status = "更新"

                print(f"  [{status}] {t_ts} "
                      f"({row.get('UserName', '?')}, 返信{len(reply_rows)}件)",
                      file=sys.stderr)
                needs_summarize.append(t_ts)

            print(
                f"\n取得結果: 新規={stats['new']} 更新={stats['updated']} "
                f"スキップ={stats['skipped']}",
                file=sys.stderr,
            )

    return needs_summarize


# ==================================================================
# ステップ2: 差分要約
# ==================================================================

def extract_urls_from_message(text: str) -> list:
    return re.findall(r"https?://[^\s<>）」\]]+[^\s<>）」\].,;:!?、。]", text)


def format_chunk_for_summary(chunk: dict) -> str:
    parent = chunk["parent"]
    replies = chunk.get("replies", [])
    all_urls = list(dict.fromkeys(
        extract_urls_from_message(parent["message"])
        + [u for r in replies for u in extract_urls_from_message(r["message"])]
    ))

    text = "=== メッセージ ===\n"
    text += f"投稿者: {parent['user_name']}\n"
    text += f"日時: {parent['timestamp']}\n"
    text += f"内容: {parent['message']}\n"
    pl = parent.get("permalink", "")
    if pl:
        text += f"permalink: {pl}\n"
    if replies:
        text += f"\n--- 返信 ({len(replies)}件) ---\n"
        for i, r in enumerate(replies, 1):
            text += (f"\n[返信 {i}]\n投稿者: {r['user_name']}\n"
                     f"日時: {r['timestamp']}\n内容: {r['message']}\n")
    if all_urls:
        text += "\n【重要: 以下のURLを要約に含める場合は完全な形で一字一句そのまま記載すること】\n"
        for url in all_urls:
            text += f"- {url}\n"
    return text


def remove_thinking_tags(text: str) -> str:
    if "<think>" in text and "</think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    elif "</think>" in text:
        text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?answer>", "", text)
    text = re.sub(r"\n\n+", "\n\n", text)
    return text.strip()


def call_claude(prompt: str, timeout: int) -> str:
    # CLAUDECODE 環境変数が設定されているとネストセッション判定でエラーになるため除外する
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return remove_thinking_tags(result.stdout.strip())


def summarize_chunk(chunk_text: str) -> str:
    system_prompt = (
        "Slackのメッセージとスレッドを冗長な表現を控え端的に要約してください。"
        "推測を含めず、書かれていることを忠実に要約してください。"
        "要点、決定事項、アクションアイテムを抽出してください。"
        "メッセージにpermalinkが含まれている場合は、要約の末尾にpermalinkのURLのみを"
        "出力してください。「permalink:」等のラベルは付けないでください。"
        "スレッドの場合は親メッセージのpermalinkのみ出力してください。"
        "メッセージにURLが含まれる場合は、完全なURL（https://から最後まで）を"
        "一字一句正確に省略せずそのまま記載してください。"
        "URLの途中で改行したり、括弧を挿入したりしないでください。"
        "URLを出力する際にはその前後に必ず半角の空白を追加してください。"
        "決定事項・アクションアイテムがない場合は言及不要です。言語は日本語としてください。"
    )
    try:
        summary = call_claude(f"{system_prompt}\n\n{chunk_text}", timeout=300)
        print(summary)
        return summary
    except subprocess.TimeoutExpired:
        print("  タイムアウト")
        return "[要約失敗: タイムアウト]"
    except Exception as e:
        print(f"  エラー: {e}")
        return f"[要約失敗: {e}]"


def summarize_updated_threads(
    conn: sqlite3.Connection,
    channel_id: str,
    thread_ts_list: list[str],
) -> int:
    """
    指定スレッドのみ要約してDBに保存する。
    返値: 実際に要約したスレッド数
    """
    if not thread_ts_list:
        print("要約対象スレッドなし（全て最新）", file=sys.stderr)
        return 0

    print(f"要約対象: {len(thread_ts_list)}スレッド", file=sys.stderr)
    count = 0
    for i, thread_ts in enumerate(thread_ts_list, 1):
        chunk = db_get_thread(conn, channel_id, thread_ts)
        if not chunk:
            print(f"  警告: thread_ts={thread_ts} がDBに見つかりません", file=sys.stderr)
            continue

        parent = chunk["parent"]
        print(f"\n[{i}/{len(thread_ts_list)}] {parent['user_name']} "
              f"({parent['timestamp']})")

        summary = summarize_chunk(format_chunk_for_summary(chunk))

        # last_reply_ts: 最新返信の msg_ts（返信なしなら None）
        last_reply_ts = db_get_max_reply_ts(conn, channel_id, thread_ts)
        db_upsert_summary(conn, channel_id, thread_ts, summary, last_reply_ts)
        conn.commit()
        count += 1

        if i % 10 == 0:
            print(f"  → {i}/{len(thread_ts_list)} 完了")

    print(f"\n✓ {count}スレッドの要約をDBに保存しました")
    return count


# ==================================================================
# --list: DB内スレッド要約一覧
# ==================================================================

def cmd_list(conn: sqlite3.Connection, channel_id: str, since: datetime | None) -> None:
    query = """
        SELECT m.thread_ts, m.timestamp, m.user_name, m.permalink,
               s.summarized_at, s.last_reply_ts, s.summary
        FROM messages m
        LEFT JOIN summaries s ON m.thread_ts = s.thread_ts AND s.channel_id = m.channel_id
        WHERE m.channel_id = ?
    """
    params: list = [channel_id]
    if since:
        query += " AND m.timestamp >= ?"
        params.append(since.strftime("%Y-%m-%d"))
    query += " ORDER BY m.timestamp ASC"

    rows = conn.execute(query, params).fetchall()

    print(f"スレッド一覧（チャンネル: {channel_id}）")
    if since:
        print(f"（{since.strftime('%Y-%m-%d')} 以降）")
    print("─" * 80)
    summarized = unsummarized = 0
    for i, row in enumerate(rows, 1):
        ts = (row["timestamp"] or "")[:16]
        user = (row["user_name"] or "")[:12]
        if row["summarized_at"]:
            summarized += 1
            summary_head = (row["summary"] or "").replace("\n", " ")[:60]
            has_replies = "↩" if row["last_reply_ts"] else " "
            print(f"[{i:4d}] {ts}  {user:<12}  {has_replies}  要約:{row['summarized_at'][:10]}  {summary_head}…")
        else:
            unsummarized += 1
            print(f"[{i:4d}] {ts}  {user:<12}     （未要約）")
    print(f"\n合計: {len(rows)} 件（要約済み: {summarized}, 未要約: {unsummarized}）")


# ==================================================================
# ステップ3: 全体要約 & Canvas投稿
# ==================================================================

def fetch_summaries_for_overall(
    conn: sqlite3.Connection,
    channel_id: str,
    since_date: datetime | None,
) -> list[dict]:
    """DB から全スレッド要約と親メッセージ permalink を取得（全体要約用）"""
    query = """
        SELECT m.timestamp, m.user_name, m.permalink, s.summary
        FROM messages m
        JOIN summaries s ON m.thread_ts = s.thread_ts AND s.channel_id = m.channel_id
        WHERE m.channel_id = ?
    """
    params: list = [channel_id]
    if since_date:
        query += " AND m.timestamp >= ?"
        params.append(since_date.strftime("%Y-%m-%d"))
    query += " ORDER BY m.timestamp ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def summarize_overall(entries: list[dict]) -> str:
    """全スレッド要約を統合してチャンネル全体の総合要約を生成する（LLM使用）"""
    if not entries:
        return "（要約対象なし）"

    items = []
    for e in entries:
        item = f"- {e['summary']}"
        if e.get("permalink"):
            item += f"\n  (元投稿: {e['permalink']})"
        items.append(item)

    system_prompt = (
        "以下はSlackチャンネルの個別メッセージ・スレッドごとの要約です。"
        "これらを統合して、チャンネル全体の活動を俯瞰できる総合要約を作成してください。"
        "主要なトピック、共有されたリソース（URL含む）を整理して記載してください。"
        "URLは共有されたリソースにまとめてください。"
        "URLが含まれる場合は完全な形で記載してください。"
        "極力、表形式は使わずに文章で表現してください。"
        "推測を含めず、要約に基づいた内容のみを記載してください。"
        "\n\n"
        "【重要】各要約には元のSlack投稿のpermalinkが付記されています。"
        "全体要約の各トピックや要点の末尾に、参照元の投稿permalinkのURLのみを記載してください。「permalink:」等のラベルは付けないでください。"
        "言語は日本語としてください。"
    )
    print("\n全体要約を生成中...", file=sys.stderr)
    try:
        return call_claude(f"{system_prompt}\n\n" + "\n\n".join(items), timeout=600)
    except subprocess.TimeoutExpired:
        return "[全体要約失敗: タイムアウト]"
    except Exception as e:
        return f"[全体要約失敗: {e}]"


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
    text = re.sub(r"^> (-|\*|\d+\.)\s+", r"\1 ", text, flags=re.MULTILINE)

    # 上記で対処できなかった非ASCII・非日本語の特殊記号を除去
    def keep_char(c: str) -> str:
        cp = ord(c)
        if 0x20 <= cp <= 0x7E:
            return c
        if c in ("\n", "\t"):
            return c
        if 0x3000 <= cp <= 0x9FFF:
            return c
        if 0xF900 <= cp <= 0xFAFF:
            return c
        if 0xFF00 <= cp <= 0xFFEF:
            return c
        if 0x00C0 <= cp <= 0x024F:
            return c
        return ""

    text = "".join(keep_char(c) for c in text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _collect_section_ids(app: App, canvas_id: str) -> list[str]:
    """Canvas の全セクション ID を収集する（複数クエリで網羅）"""
    seen: set[str] = set()
    ids: list[str] = []
    for text in ["|", "##", "- ", "【", "project", "アクション"]:
        try:
            resp = app.client.canvases_sections_lookup(
                canvas_id=canvas_id,
                criteria={"contains_text": text},
            )
            for sec in resp.get("sections", []):
                sid = sec.get("id")
                if sid and sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
        except SlackApiError:
            pass
    return ids


def post_to_canvas(canvas_id: str, content: str) -> None:
    token = os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("ERROR: SLACK_MCP_XOXB_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Canvas投稿コンテンツ: {len(content)} 文字")
    app = App(token=token)

    try:
        section_ids = _collect_section_ids(app, canvas_id)
        if section_ids:
            print(f"[INFO] 既存セクション {len(section_ids)} 件を削除中...")
            app.client.canvases_edit(
                canvas_id=canvas_id,
                changes=[{"operation": "delete", "section_id": sid} for sid in section_ids],
            )

        app.client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "insert_at_start",
                "document_content": {"type": "markdown", "markdown": content},
            }],
        )
        print(f"✓ Canvas 更新成功: {canvas_id}")
    except SlackApiError as e:
        print(f"Slack API エラー: {e.response['error']}", file=sys.stderr)
        print(f"レスポンス詳細: {e.response}", file=sys.stderr)
        sys.exit(1)


# ==================================================================
# メイン
# ==================================================================

async def main():
    args = parse_args()
    channel_id = args.channel
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = args.db or os.path.join(repo_root, "data", f"{channel_id}.db")

    print(f"DB: {db_path}")
    conn = init_db(db_path, no_encrypt=args.no_encrypt)

    # ---- --list モード ----
    if args.list:
        cmd_list(conn, channel_id, args.since)
        conn.close()
        return

    # ---- ステップ1: 差分取得 & DB保存 ----
    if not args.skip_fetch:
        print(f"\n{'='*60}")
        print(f"ステップ1: 差分取得 (チャンネル: {channel_id})")
        print(f"{'='*60}")
        needs_summarize = await fetch_and_store(
            conn=conn,
            channel_id=channel_id,
            limit=args.limit,
            since_date=args.since,
            fetch_permalink=not args.no_permalink,
            force_resummary=args.force_resummary,
        )
    else:
        print(f"\nステップ1: スキップ（DB のみ使用）")
        if args.force_resummary:
            # DB内の全スレッドを再要約対象にする（--since フィルタを適用）
            query = "SELECT thread_ts FROM messages WHERE channel_id=?"
            params: list = [channel_id]
            if args.since:
                query += " AND timestamp >= ?"
                params.append(args.since.strftime("%Y-%m-%d"))
            query += " ORDER BY thread_ts ASC"
            rows = conn.execute(query, params).fetchall()
            needs_summarize = [r["thread_ts"] for r in rows]
            print(f"  → force-resummary: {len(needs_summarize)}スレッドを対象")
        else:
            needs_summarize = []

    # ---- ステップ2: 差分要約 & DB保存 ----
    print(f"\n{'='*60}")
    print("ステップ2: 差分要約")
    print(f"{'='*60}")
    if args.dry_run or args.skip_llm:
        reason = "--dry-run" if args.dry_run else "--skip-llm"
        print(f"[INFO] {reason} のため LLM呼び出し・DB保存をスキップしました（対象: {len(needs_summarize)}スレッド）")
    else:
        summarize_updated_threads(conn, channel_id, needs_summarize)

    total_summaries = conn.execute(
        "SELECT COUNT(*) FROM summaries WHERE channel_id=?", (channel_id,)
    ).fetchone()[0]
    print(f"\nDB内サマリー総数: {total_summaries}スレッド")

    # ---- ステップ3: 全体要約 & Canvas投稿 ----
    print(f"\n{'='*60}")
    print("ステップ3: 全体要約")
    print(f"{'='*60}")

    overall_summary = None
    if args.dry_run or args.skip_llm:
        reason = "--dry-run" if args.dry_run else "--skip-llm"
        print(f"[INFO] {reason} のため全体要約をスキップしました")
    else:
        entries = fetch_summaries_for_overall(conn, channel_id, args.since)
        print(f"全体要約対象: {len(entries)}スレッド", file=sys.stderr)
        if entries:
            overall_summary = summarize_overall(entries)
            overall_summary = sanitize_for_canvas(overall_summary)
            print(overall_summary)

    conn.close()

    if overall_summary and args.output:
        Path(args.output).write_text(overall_summary, encoding="utf-8")
        print(f"✓ 全体要約を {args.output} に保存しました")

    if overall_summary and args.canvas_id and not args.skip_canvas:
        post_to_canvas(args.canvas_id, overall_summary)
    elif args.canvas_id:
        print("[INFO] Canvas 投稿をスキップしました")

    print("\n✓ パイプライン完了")


if __name__ == "__main__":
    asyncio.run(main())
