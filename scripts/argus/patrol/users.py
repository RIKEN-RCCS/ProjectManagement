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
    from .state import PatrolState

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

    def _roster_lookup_uid(self, name: str) -> str | None:
        """docs/project.md の行に埋め込まれた [Uxxx] を直接引く。
        行内のどこかに name の文字列が含まれていて、かつ [Uxxx] マーカーがあれば採用。"""
        try:
            repo_root = Path(__file__).resolve().parents[3]
            md = repo_root / "docs" / "project.md"
            if not md.exists():
                return None
            import re
            uid_re = re.compile(r"\[(U[A-Z0-9]{5,})\]")
            n = name.strip()
            for line in md.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue
                if n not in line:
                    continue
                m = uid_re.search(line)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.warning("roster uid lookup error: %s", e)
        return None

    def _roster_aliases(self, name: str) -> list[str]:
        """docs/project.md の名簿から、name（姓漢字・姓英語など）に対応する英語フルネーム候補を返す。"""
        try:
            repo_root = Path(__file__).resolve().parents[3]
            md = repo_root / "docs" / "project.md"
            if not md.exists():
                return []
            import re
            out = []
            n = name.strip()
            for line in md.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue
                body = line[2:]
                # email / ":" より前を取り出し、空白（タブ含む）で分割
                head = re.split(r"[:：]", body, 1)[0]
                parts = re.split(r"\s+", head.strip())
                if len(parts) < 3:
                    continue
                # parts は [姓漢字, 名漢字, 英名First, 英名Last, (email)] 形式が多い
                # name が parts 内のいずれか（部分一致）なら、英語名候補を抽出
                joined = " ".join(parts)
                if n in joined:
                    # ASCII トークンを英語名として抽出
                    ascii_tokens = [p for p in parts if re.fullmatch(r"[A-Za-z'\.\-]+", p)]
                    if len(ascii_tokens) >= 2:
                        out.append(" ".join(ascii_tokens[:2]))
                        out.append(" ".join(ascii_tokens[-2:]))
                        out.extend(ascii_tokens)
            # 重複除去、順序保持
            seen = set()
            result = []
            for a in out:
                if a and a not in seen:
                    seen.add(a)
                    result.append(a)
            return result
        except Exception as e:
            logger.warning("roster parse error: %s", e)
            return []

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

        # 名簿に [Uxxx] が埋め込まれていればそれを最優先で採用
        uid = self._roster_lookup_uid(name)
        if uid:
            self._state.cache_user(name, uid)
            return uid

        uid = self._mine_slack_dbs(name)
        if uid:
            self._state.cache_user(name, uid)
            return uid

        uid = self._search_api(name)
        if uid:
            self._state.cache_user(name, uid)
            return uid

        # docs/project.md の名簿で英語名エイリアスを引いて再試行
        for alias in self._roster_aliases(name):
            if alias == name:
                continue
            uid = self._mine_slack_dbs(alias) or self._search_api(alias)
            if uid:
                logger.info("roster alias: %s → %s → %s", name, alias, uid)
                self._state.cache_user(name, uid)
                return uid

        logger.warning("user_id 解決失敗: %s", name)
        return None

    def _mine_slack_dbs(self, name: str) -> str | None:
        """統合 Slack DB (data/slack.db) から user_name で user_id を検索。"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from db_utils import open_db
        except Exception:
            open_db = None

        db_file = self._data_dir / "slack.db"
        if not db_file.exists():
            return None

        try:
            if open_db is not None:
                conn = open_db(db_file, encrypt=True)
            else:
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
                logger.info("Slack DB マイニング: %s → %s", name, row["user_id"])
                return row["user_id"]
        except Exception:
            pass
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
