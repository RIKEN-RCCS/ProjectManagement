"""Slack 投稿ファネル（docs/security-architecture.md §4.2）のテスト。"""
from __future__ import annotations

import sqlite3

import pytest
from utils.slack_post import (
    SlackEgressBlocked,
    post_ephemeral,
    post_message,
    update_message,
    upload_file,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def _rec(self, name, kwargs):
        self.calls.append((name, kwargs))
        return {"ts": "1.0"}

    def chat_postMessage(self, **kw):
        return self._rec("chat_postMessage", kw)

    def chat_postEphemeral(self, **kw):
        return self._rec("chat_postEphemeral", kw)

    def chat_update(self, **kw):
        return self._rec("chat_update", kw)

    def files_upload_v2(self, **kw):
        return self._rec("files_upload_v2", kw)


@pytest.fixture
def conn(pm_db_path):
    from db_utils import ensure_canary_table, ensure_tool_calls_table

    c = sqlite3.connect(str(pm_db_path)); c.row_factory = sqlite3.Row
    ensure_tool_calls_table(c); ensure_canary_table(c)
    yield c
    c.close()


@pytest.fixture
def patch_audit_conn(pm_db_path, monkeypatch):
    """`_open_audit_conn()` を差し替え、conn 未指定でも `pm_db_path` を自前で
    開くようにする（本番の data/pm.db には決して触れない）。
    """
    from utils import slack_post

    def _open():
        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(slack_post, "_open_audit_conn", _open)
    return pm_db_path


class TestPassThrough:
    def test_kwargs_are_forwarded_unchanged(self):
        """移送を機械的にするため kwargs はそのまま透過する。"""
        c = FakeClient()
        post_message(c, channel="C0XXXXXXX", text="本文", thread_ts="1.0")
        assert c.calls == [("chat_postMessage",
                            {"channel": "C0XXXXXXX", "text": "本文", "thread_ts": "1.0"})]

    def test_all_four_methods_are_funneled(self):
        c = FakeClient()
        post_message(c, channel="C0XXXXXXX", text="a")
        post_ephemeral(c, channel="C0XXXXXXX", user="U0XXXXXXX", text="b")
        update_message(c, channel="C0XXXXXXX", ts="1.0", text="c")
        upload_file(c, channel="C0XXXXXXX", file="/tmp/x")
        assert [n for n, _ in c.calls] == [
            "chat_postMessage", "chat_postEphemeral", "chat_update", "files_upload_v2"]


class TestGuard:
    def test_zero_width_character_blocks_send(self):
        c = FakeClient()
        with pytest.raises(SlackEgressBlocked, match="ゼロ幅"):
            post_message(c, channel="C0XXXXXXX", text="正常な文​章")
        assert c.calls == []  # 送信していない

    def test_canary_in_blocks_is_detected(self, conn):
        from db_utils import plant_canary

        row = plant_canary(conn, kind="hostname", planted_in="box_docs")
        c = FakeClient()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": row["token"]}}]
        with pytest.raises(SlackEgressBlocked, match="canary"):
            post_message(c, channel="C0XXXXXXX", blocks=blocks, text="要約", conn=conn)
        assert c.calls == []

    def test_clean_text_passes(self, conn):
        c = FakeClient()
        post_message(c, channel="C0XXXXXXX", text="今週の進捗です", conn=conn)
        assert len(c.calls) == 1


class TestRecording:
    def test_ok_send_is_recorded_as_egress(self, conn):
        post_message(FakeClient(), channel="C0XXXXXXX", text="本文", conn=conn)
        r = conn.execute("SELECT plane, tool_name, outcome FROM tool_calls").fetchone()
        assert (r["plane"], r["tool_name"], r["outcome"]) == (
            "egress", "slack:chat_postMessage", "ok")

    def test_blocked_send_is_recorded(self, conn):
        with pytest.raises(SlackEgressBlocked):
            post_message(FakeClient(), channel="C0XXXXXXX", text="文​字", conn=conn)
        r = conn.execute("SELECT outcome, block_reason FROM tool_calls").fetchone()
        assert r["outcome"] == "blocked" and "ゼロ幅" in r["block_reason"]

    def test_message_body_is_not_stored(self, conn):
        post_message(FakeClient(), channel="C0XXXXXXX", text="極秘の本文", conn=conn)
        r = conn.execute("SELECT args_json FROM tool_calls").fetchone()
        assert "極秘の本文" not in r["args_json"]

    def test_without_conn_send_proceeds_when_audit_conn_unavailable(self, monkeypatch):
        """自前で開く pm.db 接続すら得られない場合でも fail-open で送信は続ける。"""
        from utils import slack_post

        monkeypatch.setattr(slack_post, "_open_audit_conn", lambda: None)
        c = FakeClient()
        post_message(c, channel="C0XXXXXXX", text="本文")
        assert len(c.calls) == 1


class TestGuardWithoutConnArgument:
    """欠陥1（conn 未指定だと canary 検査・egress 記録がまるごと素通りする）の回帰テスト。"""

    def test_canary_is_detected_without_passing_conn(self, conn, patch_audit_conn):
        from db_utils import plant_canary

        row = plant_canary(conn, kind="text", planted_in="registry_only")
        c = FakeClient()
        with pytest.raises(SlackEgressBlocked, match="canary"):
            post_message(c, channel="C0XXXXXXX", text=f"本文に {row['token']} を含む")
        assert c.calls == []

    def test_egress_is_recorded_without_passing_conn(self, conn, patch_audit_conn):
        post_message(FakeClient(), channel="C0XXXXXXX", text="通常の本文")
        rows = conn.execute(
            "SELECT tool_name, plane, outcome FROM tool_calls"
            " WHERE tool_name='slack:chat_postMessage' AND plane='egress' AND outcome='ok'"
        ).fetchall()
        assert len(rows) == 1
