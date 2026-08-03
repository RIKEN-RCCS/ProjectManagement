"""Slack 投稿ファネル（docs/security-architecture.md §4.2）のテスト。"""
from __future__ import annotations

import os
import sqlite3

import pytest
from utils.slack_post import (
    SlackEgressBlocked,
    post_ephemeral,
    post_message,
    update_message,
    upload_file,
)

# 収集（import）時点のバインディング。conftest.py の autouse フィクスチャ
# （テスト実行フェーズで初めて発動する）より前に確定するため、モンキーパッチ前の
# 本物の関数を指す（下の TestAuditConnProductionIsolation で使う）。
from utils.slack_post import _open_audit_conn as _real_open_audit_conn


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


class TestAuditConnProductionIsolation:
    """本番 data/pm.db をテストが汚染しないことの回帰テスト
    （実測: コミット b6b95e5 で `_open_audit_conn()` が conn 未指定時に自前で
    本番 pm.db を開くようになり、テストの `post_message` 呼び出しだけで
    本番 tool_calls にテスト由来の行が書き込まれていた）。
    """

    def test_real_open_audit_conn_refuses_production_path_under_pytest(self):
        """副対策: PYTEST_CURRENT_TEST が立っている間、本物の `_open_audit_conn()` は
        fail-closed で RuntimeError を送出する（本番実行時はこの環境変数自体が
        存在しないため、この分岐は一切効かない）。
        """
        assert os.environ.get("PYTEST_CURRENT_TEST") is not None
        with pytest.raises(RuntimeError, match="本番 pm.db"):
            _real_open_audit_conn()

    def test_post_message_leaves_production_tool_calls_row_count_unchanged(self):
        """主対策: conftest.py の autouse フィクスチャが `_open_audit_conn()` を
        一時 DB へ差し替えていること、および `post_message` を呼ぶと一時 DB
        側の `tool_calls` に行が入ることを確認する。本番 pm.db には一切
        アクセスしない（読み取りであっても「テストは本番 DB に触らない」
        規約に反するため）。
        """
        from db_utils import ensure_tool_calls_table
        from utils import slack_post

        # autouse フィクスチャによって差し替わっていること（本物の関数ではない）
        assert slack_post._open_audit_conn is not _real_open_audit_conn

        conn = slack_post._open_audit_conn()
        try:
            ensure_tool_calls_table(conn)
            before = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        finally:
            conn.close()

        post_message(FakeClient(), channel="C0XXXXXXX", text="監査分離の回帰テスト")

        conn = slack_post._open_audit_conn()
        try:
            after = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        finally:
            conn.close()

        assert after == before + 1


# --------------------------------------------------------------------------- #
# 層3（宛先粒度）の照合（docs/security-architecture.md §4.7）
#
# `tests/conftest.py` の autouse フィクスチャ（`_isolate_dest_config`）が
# `_argus_config_path()` を既定で存在しないパスへ差し替え、`_dest_cache` も
# 毎テストでリセットしている。ここではさらに特定の宛先集合を持つ一時 yaml へ
# 差し替える（本番 data/argus_config.yaml は一切読まない）。
#
# チャンネル/ユーザー/Canvas の識別子はプレースホルダ表記（pre-commit の
# no-slack-id-literals が許容する `[CUFG]0[XY]+` 形式）のみを使い、区別は
# X/Y の個数（＝文字列長）で付ける。
# --------------------------------------------------------------------------- #

_LEADER_CHANNEL = "C0XXXXXXX"          # 9文字
_PM_CHANNEL = "C0XXXXXXXX"             # 10文字（leader とは別の宛先）
_UNKNOWN_CHANNEL = "C0XXXXXXXXX"       # 11文字（config に含めない）
_REDIRECT_USER = "U0XXXXXXX"
_REPORT_CANVAS = "F0YYYYYYY"           # 9文字
_CATALOG_CANVAS = "F0YYYYYYYY"         # 10文字（report とは別の宛先）


@pytest.fixture
def dest_config_path(tmp_path, monkeypatch):
    """テスト用の一時 argus_config.yaml。本番 data/argus_config.yaml は一切読まない。"""
    p = tmp_path / "argus_config.yaml"
    p.write_text(
        f"""
patrol:
  leader_channel: "{_LEADER_CHANNEL}"
  dm_redirect_user: "{_REDIRECT_USER}"
indices:
  pm:
    channels: ["{_PM_CHANNEL}"]
report:
  canvas_id: "{_REPORT_CANVAS}"
  box_folder_id: "111111"
meetings:
  Leader_Meeting:
    box_folder_id: "222222"
    catalog_canvas_id: "{_CATALOG_CANVAS}"
""",
        encoding="utf-8",
    )
    from utils import slack_post
    monkeypatch.setattr(slack_post, "_argus_config_path", lambda: p)
    monkeypatch.setattr(slack_post, "_dest_cache", None)
    return p


class TestConfiguredSlackDestinations:
    def test_collects_destinations_from_tmp_yaml(self, dest_config_path):
        from utils.slack_post import configured_slack_destinations

        dests = configured_slack_destinations()
        assert dests == {
            _LEADER_CHANNEL, _REDIRECT_USER, _PM_CHANNEL,
            _REPORT_CANVAS, "111111", "222222", _CATALOG_CANVAS,
        }

    def test_missing_file_returns_empty_set_and_warns(self, tmp_path, monkeypatch, caplog):
        from utils import slack_post

        monkeypatch.setattr(slack_post, "_argus_config_path", lambda: tmp_path / "no_such.yaml")
        monkeypatch.setattr(slack_post, "_dest_cache", None)
        with caplog.at_level("WARNING"):
            dests = slack_post.configured_slack_destinations()
        assert dests == set()
        assert "見つかりません" in caplog.text


class TestDestinationMatch:
    def test_known_destination_is_recorded_as_dest_known_true(self, conn, dest_config_path, monkeypatch):
        import json

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "warn")
        post_message(FakeClient(), channel=_LEADER_CHANNEL, text="本文", conn=conn)
        r = conn.execute("SELECT args_json FROM tool_calls").fetchone()
        assert json.loads(r["args_json"])["dest_known"] is True

    def test_unknown_destination_warns_and_passes_in_warn_mode(
        self, conn, dest_config_path, monkeypatch, caplog,
    ):
        import json

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "warn")
        with caplog.at_level("WARNING"):
            post_message(FakeClient(), channel=_UNKNOWN_CHANNEL, text="本文", conn=conn)
        r = conn.execute("SELECT args_json, outcome FROM tool_calls").fetchone()
        assert json.loads(r["args_json"])["dest_known"] is False
        assert r["outcome"] == "ok"
        assert "EGRESS-L3" in caplog.text

    def test_unknown_destination_blocks_in_enforce_mode(self, conn, dest_config_path, monkeypatch):
        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "enforce")
        with pytest.raises(SlackEgressBlocked, match="宛先"):
            post_message(FakeClient(), channel=_UNKNOWN_CHANNEL, text="本文", conn=conn)
        r = conn.execute("SELECT outcome, block_reason FROM tool_calls").fetchone()
        assert r["outcome"] == "blocked"
        assert "宛先" in r["block_reason"]

    def test_ephemeral_never_blocks_even_in_enforce_mode(self, conn, dest_config_path, monkeypatch):
        """ephemeral はコマンド実行チャンネルへ正当に返るため、enforce でも遮断しない
        （分布観測のため記録は行う）。"""
        import json

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "enforce")
        post_ephemeral(
            FakeClient(), channel=_UNKNOWN_CHANNEL, user=_REDIRECT_USER, text="本文", conn=conn,
        )
        r = conn.execute("SELECT args_json, outcome FROM tool_calls").fetchone()
        assert json.loads(r["args_json"])["dest_known"] is False
        assert r["outcome"] == "ok"

    def test_off_mode_skips_matching_entirely(self, conn, dest_config_path, monkeypatch):
        import json

        from utils import slack_post

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "off")

        def _boom(conn=None):
            raise AssertionError("mode=off では照合自体が行われないはず")

        monkeypatch.setattr(slack_post, "configured_slack_destinations", _boom)
        post_message(FakeClient(), channel=_UNKNOWN_CHANNEL, text="本文", conn=conn)
        r = conn.execute("SELECT args_json FROM tool_calls").fetchone()
        assert "dest_known" not in json.loads(r["args_json"])

    def test_guard_outbound_text_matches_canvas_destination(self, conn, dest_config_path, monkeypatch):
        import json

        from utils.slack_post import guard_outbound_text

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "warn")
        guard_outbound_text("本文", transport="canvas", dest=_REPORT_CANVAS, conn=conn)
        r = conn.execute("SELECT args_json FROM tool_calls").fetchone()
        assert json.loads(r["args_json"])["dest_known"] is True

    def test_guard_outbound_text_flags_unknown_box_folder(self, conn, dest_config_path, monkeypatch):
        import json

        from utils.slack_post import guard_outbound_text

        monkeypatch.setenv("ARGUS_EGRESS_TARGETS", "warn")
        guard_outbound_text("本文", transport="box", dest="999999999", conn=conn)
        r = conn.execute("SELECT args_json FROM tool_calls").fetchone()
        assert json.loads(r["args_json"])["dest_known"] is False
