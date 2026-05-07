#!/usr/bin/env python3
"""
patrol_state.py — Patrol Agent の冪等性・スロットリング・承認待ち管理

patrol_state.db（平文 sqlite3）を管理する。機密情報は含まないため暗号化不要。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    channel_id TEXT,
    message_ts TEXT,
    UNIQUE(event_type, target_key)
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    target_id   INTEGER NOT NULL,
    proposed_by TEXT NOT NULL,
    evidence    TEXT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS user_cache (
    display_name TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);
"""

_PRUNE_DAYS = 90


class PatrolState:
    """patrol_state.db の管理クラス。"""

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        self._prune_old_records()

    # ------------------------------------------------------------------ #
    # 通知スロットリング
    # ------------------------------------------------------------------ #
    def already_notified(
        self, event_type: str, target_key: str, cooldown_days: int
    ) -> bool:
        """cooldown_days 以内に同一 event_type × target_key の通知があれば True。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=cooldown_days)
        ).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM notifications"
            " WHERE event_type = ? AND target_key = ? AND sent_at > ?",
            (event_type, target_key, cutoff),
        ).fetchone()
        return row is not None

    def record_notification(
        self,
        event_type: str,
        target_key: str,
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        """通知を記録する。同一 event_type × target_key は UPSERT。"""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO notifications (event_type, target_key, sent_at, channel_id, message_ts)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(event_type, target_key)"
            " DO UPDATE SET sent_at=excluded.sent_at, channel_id=excluded.channel_id, message_ts=excluded.message_ts",
            (event_type, target_key, now, channel_id, message_ts),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 承認待ち管理
    # ------------------------------------------------------------------ #
    def create_pending(
        self, action_type: str, target_id: int, evidence: str
    ) -> int:
        """承認待ちエントリを作成し、ID を返す。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO pending_confirmations"
            " (action_type, target_id, proposed_by, evidence, created_at, status)"
            " VALUES (?, ?, 'patrol', ?, ?, 'pending')",
            (action_type, target_id, evidence, now),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def resolve_pending(
        self, pending_id: int, status: str, resolved_by: str
    ) -> bool:
        """承認待ちを approved/rejected に更新。対象が見つかれば True。"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_confirmations"
            " SET status = ?, resolved_at = ?, resolved_by = ?"
            " WHERE id = ? AND status = 'pending'",
            (status, now, resolved_by, pending_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_pending(self, pending_id: int) -> dict | None:
        """pending_id のエントリを取得。"""
        row = self._conn.execute(
            "SELECT * FROM pending_confirmations WHERE id = ?",
            (pending_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_pending(self) -> list[dict]:
        """status='pending' の全エントリを返す。"""
        rows = self._conn.execute(
            "SELECT * FROM pending_confirmations WHERE status = 'pending'"
            " ORDER BY created_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # ユーザーキャッシュ
    # ------------------------------------------------------------------ #
    def get_cached_user(self, display_name: str) -> str | None:
        """24時間以内のキャッシュがあれば user_id を返す。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        row = self._conn.execute(
            "SELECT user_id FROM user_cache"
            " WHERE display_name = ? AND cached_at > ?",
            (display_name, cutoff),
        ).fetchone()
        return row["user_id"] if row else None

    def cache_user(self, display_name: str, user_id: str) -> None:
        """ユーザーキャッシュを更新。"""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO user_cache (display_name, user_id, cached_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(display_name)"
            " DO UPDATE SET user_id=excluded.user_id, cached_at=excluded.cached_at",
            (display_name, user_id, now),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # メンテナンス
    # ------------------------------------------------------------------ #
    def _prune_old_records(self) -> None:
        """_PRUNE_DAYS 以上前のレコードを自動削除。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_PRUNE_DAYS)
        ).isoformat()
        self._conn.execute(
            "DELETE FROM notifications WHERE sent_at < ?", (cutoff,)
        )
        self._conn.execute(
            "DELETE FROM pending_confirmations WHERE created_at < ?",
            (cutoff,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
