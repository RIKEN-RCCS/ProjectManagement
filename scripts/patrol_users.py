#!/usr/bin/env python3
"""
patrol_users.py — 担当者名（日本語表示名）→ Slack user_id 解決

pm.db の assignee は "西澤" のような日本語表示名だが、
Slack DM には user_id ("U0XXXXXX") が必要。

解決の優先順序:
  1. patrol_state.db の user_cache（24時間キャッシュ）
  2. Slack DB マイニング（{channel_id}.db の messages/replies テーブル）
  3. Slack API フォールバック（users.list で全メンバーを取得）
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slack_sdk import WebClient
    from patrol_state import PatrolState

logger = logging.getLogger(__name__)


class UserResolver:
    """担当者の日本語表示名を Slack user_id に解決する。"""

    def __init__(
        self,
        state: PatrolState,
        slack: WebClient | None,
        data_dir: Path,
    ):
        self._state = state
        self._slack = slack
        self._data_dir = data_dir
        self._api_members: list[dict] | None = None

    def resolve(self, display_name: str) -> str | None:
        """
        担当者名 → Slack user_id。解決不能なら None。

        名前が空・None の場合は即座に None を返す。
        """
        if not display_name or not display_name.strip():
            return None

        name = display_name.strip()

        cached = self._state.get_cached_user(name)
        if cached:
            return cached

        uid = self._mine_slack_dbs(name)
        if uid:
            self._state.cache_user(name, uid)
            return uid

        uid = self._search_api(name)
        if uid:
            self._state.cache_user(name, uid)
            return uid

        logger.warning("user_id 解決失敗: %s", name)
        return None

    def _mine_slack_dbs(self, name: str) -> str | None:
        """Slack DB ({channel_id}.db) から user_name で user_id を検索。"""
        for db_file in self._data_dir.glob("C*.db"):
            try:
                conn = sqlite3.connect(str(db_file))
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT user_id FROM messages"
                    " WHERE user_name LIKE ? AND user_id IS NOT NULL AND user_id != ''"
                    " LIMIT 1",
                    (f"%{name}%",),
                ).fetchone()
                conn.close()
                if row:
                    logger.info(
                        "Slack DB マイニング: %s → %s (%s)",
                        name,
                        row["user_id"],
                        db_file.name,
                    )
                    return row["user_id"]
            except Exception:
                continue
        return None

    def _search_api(self, name: str) -> str | None:
        """Slack users.list API で全メンバーを取得し、名前でマッチ。"""
        if not self._slack:
            return None

        if self._api_members is None:
            self._api_members = self._fetch_all_members()

        name_lower = name.lower()
        for m in self._api_members:
            profile = m.get("profile", {})
            display = (profile.get("display_name") or "").lower()
            real = (profile.get("real_name") or "").lower()
            if name_lower in display or name_lower in real:
                uid = m.get("id", "")
                if uid:
                    logger.info("Slack API: %s → %s", name, uid)
                    return uid
        return None

    def _fetch_all_members(self) -> list[dict]:
        """users.list API でワークスペースの全メンバーを取得。"""
        if not self._slack:
            return []
        members: list[dict] = []
        try:
            cursor = None
            while True:
                resp = self._slack.users_list(cursor=cursor, limit=200)
                members.extend(resp.get("members", []))
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            logger.info("Slack users.list: %d メンバー取得", len(members))
        except Exception as e:
            logger.error("users.list API エラー: %s", e)
        return members
