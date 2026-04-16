#!/usr/bin/env python3
"""
Slack差分取得パイプライン

処理フロー:
  1. Slack SDK でチャンネル履歴を取得
     - DBに存在しない新規スレッドのみ全取得
     - DBに存在するが返信が増えたスレッドのみ再取得
     - 変化のないスレッドはスキップ（API呼び出しなし）
  2. 取得したメッセージを {channel_id}.db に保存
     - pm_extractor.py が生メッセージから決定事項・AIを直接抽出
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import add_no_encrypt_arg

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ------------------------------------------------------------------ 定数
JST = timezone(timedelta(hours=9))
DEFAULT_CHANNEL = "C0A9KG036CS"

# ------------------------------------------------------------------ メモリキャッシュ
user_cache: dict = {}
permalink_cache: dict = {}
workspace_domain: str | None = None

# subtype のうち activity メッセージとして除外するもの
_SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic",
    "channel_purpose", "channel_name",
    "channel_archive", "channel_unarchive",
    "pinned_item", "unpinned_item",
}


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
        description="Slack チャンネル履歴の差分取得",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  %(prog)s                              # 差分のみ取得
  %(prog)s --since 2026-01-01 -l 200   # 初回: 範囲を絞って全件取得
  %(prog)s --list                       # DB内のスレッド一覧を表示
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
    parser.add_argument("--list", action="store_true", default=False,
                        help="DB内のスレッド一覧を表示して終了（--since 併用可）")
    add_no_encrypt_arg(parser)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Slack API 取得のみ実行（DB書き込みなし）")
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


def db_get_max_reply_ts(conn: sqlite3.Connection, channel_id: str,
                        thread_ts: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(msg_ts) AS max_ts FROM replies WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row["max_ts"] if row else None


# ==================================================================
# Slack SDK ヘルパー
# ==================================================================

def _make_client() -> WebClient:
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("エラー: SLACK_USER_TOKEN 環境変数を設定してください", file=sys.stderr)
        sys.exit(1)
    return WebClient(token=token)


def ts_to_jst(ts: str) -> str:
    """Slack unix タイムスタンプ文字列を JST 日時文字列に変換する"""
    return (
        datetime.fromtimestamp(float(ts), tz=timezone.utc)
        .astimezone(JST)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def resolve_username(client: WebClient, user_id: str) -> str:
    """ユーザーID → 表示名（キャッシュ付き）"""
    if not user_id:
        return "不明"
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        resp = client.users_info(user=user_id)
        profile = resp["user"]["profile"]
        name = profile.get("display_name") or profile.get("real_name") or user_id
        user_cache[user_id] = name
    except SlackApiError:
        user_cache[user_id] = user_id
    return user_cache[user_id]


def expand_user_mentions(client: WebClient, text: str) -> str:
    """テキスト中の <@UXXX> を表示名に展開する"""
    import re
    def _replace(m):
        uid = m.group(1)
        return resolve_username(client, uid)
    return re.sub(r"<@([A-Z0-9]+)>", _replace, text)


def build_permalink_fallback(channel_id: str, message_ts: str,
                              thread_ts: str = None) -> str:
    ts_no_dot = message_ts.replace(".", "")
    domain = workspace_domain or "WORKSPACE"
    url = f"https://{domain}.slack.com/archives/{channel_id}/p{ts_no_dot}"
    if thread_ts and thread_ts != message_ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url


def get_permalink(client: WebClient, channel_id: str, message_ts: str,
                  thread_ts: str = None) -> str:
    global workspace_domain
    cache_key = (channel_id, message_ts)
    if cache_key in permalink_cache:
        return permalink_cache[cache_key]
    try:
        resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        permalink = resp["permalink"]
        if permalink and not workspace_domain:
            m = re.match(r"https://([^.]+)\.slack\.com/", permalink)
            if m:
                workspace_domain = m.group(1)
                print(f"  ワークスペースドメイン検出: {workspace_domain}", file=sys.stderr)
        permalink_cache[cache_key] = permalink
        return permalink
    except SlackApiError as e:
        print(f"  Permalink API エラー ({message_ts}): {e.response['error']}", file=sys.stderr)
        fallback = build_permalink_fallback(channel_id, message_ts, thread_ts)
        permalink_cache[cache_key] = fallback
        return fallback


def format_message(client: WebClient, msg: dict, channel_id: str,
                   is_reply: bool = False, fetch_permalink: bool = True,
                   parent_thread_ts: str = None) -> dict:
    """SDK メッセージ dict → DB 保存用 dict に変換する"""
    user_id = msg.get("user", "") or msg.get("bot_id", "")
    # ボットメッセージは username フィールドを優先
    user_name = msg.get("username") or resolve_username(client, user_id) if user_id else "不明"
    text = expand_user_mentions(client, msg.get("text", ""))
    ts = msg.get("ts", "")
    thread_ts = msg.get("thread_ts", ts)

    formatted_time = ts_to_jst(ts) if ts else ""

    permalink = ""
    if fetch_permalink and ts:
        effective_thread_ts = parent_thread_ts if is_reply else None
        permalink = get_permalink(client, channel_id, ts, effective_thread_ts)

    indent = "  " if is_reply else ""
    link_info = " 🔗" if permalink else ""
    print(f"{indent}{formatted_time}：{user_name}：{text}{link_info}")

    return {
        "timestamp_unix": ts,
        "timestamp": formatted_time,
        "is_reply": is_reply,
        "user_id": user_id,
        "user_name": user_name,
        "message": text,
        "type": "user_message",
        "reply_count": 0,
        "thread_ts": thread_ts,
        "permalink": permalink,
    }


def fetch_thread_replies(client: WebClient, channel_id: str,
                         thread_ts: str) -> list[dict]:
    """スレッド返信を取得する（親メッセージを除く）"""
    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=100)
        # 先頭は親メッセージなのでスキップ
        return resp.get("messages", [])[1:]
    except SlackApiError as e:
        print(f"スレッド取得エラー ({thread_ts}): {e.response['error']}", file=sys.stderr)
        return []


# ==================================================================
# 差分取得 & DB保存
# ==================================================================

def fetch_and_store(
    conn: sqlite3.Connection,
    channel_id: str,
    limit: int,
    since_date: datetime | None,
    fetch_permalink: bool,
) -> int:
    """
    Slack から履歴を取得してDBに保存する。取得したスレッド数を返す。

    差分ロジック:
    - DBに存在しない thread_ts → 新規: 返信を取得してDBに保存
    - DBに存在する thread_ts:
        - conversations_history の latest_reply > DB保存済み max(replies.msg_ts) → 更新あり
        - 変化なし → スキップ（API呼び出しなし）
    """
    client = _make_client()
    oldest_ts = str(since_date.timestamp()) if since_date else None

    # ページネーションで全件取得
    all_messages: list[dict] = []
    cursor = None
    page = 0
    print(
        f"チャンネル {channel_id} の履歴を取得中"
        + (f" (oldest={oldest_ts})" if oldest_ts else "") + "...",
        file=sys.stderr,
    )

    while True:
        page += 1
        kwargs: dict = {"channel": channel_id, "limit": limit}
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_history(**kwargs)
        except SlackApiError as e:
            print(f"チャンネル履歴取得エラー: {e.response['error']}", file=sys.stderr)
            sys.exit(1)

        messages = resp.get("messages", [])
        all_messages.extend(messages)
        print(f"  ページ{page}: {len(messages)}件取得 (累計: {len(all_messages)}件)",
              file=sys.stderr)

        next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not next_cursor:
            break
        cursor = next_cursor

    # 親メッセージのみ抽出（activity メッセージを除外）
    parent_messages = [
        m for m in all_messages
        if m.get("type") == "message"
        and m.get("subtype") not in _SKIP_SUBTYPES
        and (not m.get("thread_ts") or m["thread_ts"] == m["ts"])
    ]

    print(f"処理対象の親メッセージ: {len(parent_messages)}件", file=sys.stderr)

    stats = {"new": 0, "updated": 0, "skipped": 0}

    for msg in parent_messages:
        ts = msg["ts"]
        thread_ts = ts
        latest_reply = msg.get("latest_reply")  # SDK が返す最新返信 ts

        # 差分検出: messages テーブルの存在 + replies の最新 msg_ts で判定
        existing = conn.execute(
            "SELECT 1 FROM messages WHERE thread_ts=? AND channel_id=?",
            (thread_ts, channel_id),
        ).fetchone()

        is_new = existing is None
        is_updated = False
        if not is_new and latest_reply:
            db_max = db_get_max_reply_ts(conn, channel_id, thread_ts)
            is_updated = latest_reply > (db_max or "0")

        if not is_new and not is_updated:
            stats["skipped"] += 1
            continue

        # 親メッセージを整形してDBに保存
        fmt_parent = format_message(
            client, msg, channel_id,
            is_reply=False, fetch_permalink=fetch_permalink,
        )
        db_upsert_message(conn, channel_id, fmt_parent)

        # 返信を取得してDBに保存
        reply_msgs = fetch_thread_replies(client, channel_id, thread_ts)
        for r_msg in reply_msgs:
            fmt_reply = format_message(
                client, r_msg, channel_id,
                is_reply=True, fetch_permalink=fetch_permalink,
                parent_thread_ts=thread_ts,
            )
            db_upsert_reply(conn, channel_id, thread_ts, fmt_reply)

        conn.commit()

        if is_new:
            stats["new"] += 1
            status = "新規"
        else:
            stats["updated"] += 1
            status = "更新"

        print(
            f"  [{status}] {thread_ts} "
            f"({fmt_parent.get('user_name', '?')}, 返信{len(reply_msgs)}件)",
            file=sys.stderr,
        )

    print(
        f"\n取得結果: 新規={stats['new']} 更新={stats['updated']} "
        f"スキップ={stats['skipped']}",
        file=sys.stderr,
    )

    return stats["new"] + stats["updated"]


# ==================================================================
# --list: DB内スレッド一覧
# ==================================================================

def cmd_list(conn: sqlite3.Connection, channel_id: str, since: datetime | None) -> None:
    query = """
        SELECT m.thread_ts, m.timestamp, m.user_name, m.permalink,
               (SELECT COUNT(*) FROM replies r
                WHERE r.thread_ts = m.thread_ts AND r.channel_id = m.channel_id) AS reply_count
        FROM messages m
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
    print("─" * 70)
    for i, row in enumerate(rows, 1):
        ts = (row["timestamp"] or "")[:16]
        user = (row["user_name"] or "")[:12]
        rc = row["reply_count"]
        replies_str = f"↩{rc}" if rc else " "
        text_head = ""
        # 親メッセージのテキスト先頭を表示
        text_row = conn.execute(
            "SELECT text FROM messages WHERE thread_ts=? AND channel_id=?",
            (row["thread_ts"], channel_id),
        ).fetchone()
        if text_row and text_row["text"]:
            text_head = text_row["text"].replace("\n", " ")[:50]
        print(f"[{i:4d}] {ts}  {user:<12}  {replies_str:<4}  {text_head}")
    print(f"\n合計: {len(rows)} 件")


# ==================================================================
# メイン
# ==================================================================

def main():
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

    # ---- 差分取得 & DB保存 ----
    if not args.skip_fetch:
        print(f"\n{'='*60}")
        print(f"差分取得 (チャンネル: {channel_id})")
        print(f"{'='*60}")
        fetched = fetch_and_store(
            conn=conn,
            channel_id=channel_id,
            limit=args.limit,
            since_date=args.since,
            fetch_permalink=not args.no_permalink,
        )
        print(f"\n取得・保存: {fetched} スレッド")
    else:
        print(f"\nスキップ（DB のみ使用）")

    total_threads = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)
    ).fetchone()[0]
    print(f"DB内スレッド総数: {total_threads}件")

    conn.close()
    print("\n✓ パイプライン完了")


if __name__ == "__main__":
    main()
