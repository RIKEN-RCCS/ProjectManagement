"""data/voice_uploads.db — Argus が Slack にアップロードした音声 mp3 の履歴。

/argus-delete スラッシュコマンドが、削除対象スレッドの thread_ts をキーに
file_id を引いて files.delete API を呼ぶために使う。

スキーマ:
    CREATE TABLE voice_uploads (
        message_ts TEXT NOT NULL,    -- bot が投稿したメッセージの ts (= スレッド親)
        channel_id TEXT NOT NULL,    -- 投稿先 (= 実行者との DM チャンネル)
        file_id    TEXT NOT NULL,    -- Slack の file_id (F0123ABCD)
        user_id    TEXT NOT NULL,    -- 実行者
        kind       TEXT NOT NULL,    -- today / brief / risk など
        title      TEXT,
        uploaded_at TEXT NOT NULL,   -- ISO8601 (UTC)
        PRIMARY KEY (channel_id, message_ts, file_id)
    );

非暗号化（file_id・channel_id 等の Slack 識別子のみで機密内容なし）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "voice_uploads.db"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_uploads (
            message_ts TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            file_id    TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            kind       TEXT NOT NULL,
            title      TEXT,
            uploaded_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, message_ts, file_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_uploads_user ON voice_uploads(user_id, uploaded_at)"
    )
    conn.commit()
    return conn


def record_upload(
    *,
    message_ts: str,
    channel_id: str,
    file_id: str,
    user_id: str,
    kind: str,
    title: str | None = None,
) -> None:
    """アップロード履歴を記録する。同一キーでの重複はそのまま上書き。"""
    if not (message_ts and channel_id and file_id):
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO voice_uploads
                (message_ts, channel_id, file_id, user_id, kind, title, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (message_ts, channel_id, file_id, user_id, kind, title, now),
        )


def find_by_thread(
    *,
    channel_id: str,
    message_ts: str,
    user_id: str | None = None,
) -> list[dict]:
    """thread_ts (= bot 投稿時の ts) で file_id を逆引きする。"""
    with _connect() as conn:
        if user_id:
            cur = conn.execute(
                """
                SELECT message_ts, channel_id, file_id, user_id, kind, title, uploaded_at
                FROM voice_uploads
                WHERE channel_id = ? AND message_ts = ? AND user_id = ?
                """,
                (channel_id, message_ts, user_id),
            )
        else:
            cur = conn.execute(
                """
                SELECT message_ts, channel_id, file_id, user_id, kind, title, uploaded_at
                FROM voice_uploads
                WHERE channel_id = ? AND message_ts = ?
                """,
                (channel_id, message_ts),
            )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def delete_record(*, channel_id: str, message_ts: str, file_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM voice_uploads WHERE channel_id = ? AND message_ts = ? AND file_id = ?",
            (channel_id, message_ts, file_id),
        )
