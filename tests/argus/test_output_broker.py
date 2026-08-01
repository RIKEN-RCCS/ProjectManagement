"""出力ブローカー（docs/security-architecture.md §4.2、層3）のテスト。"""
from __future__ import annotations

import sqlite3

import pytest
from argus import output_broker as ob
from argus.output_broker import EgressBlocked, EgressPendingApproval, post, scan_payload


@pytest.fixture
def targets(tmp_path, monkeypatch):
    p = tmp_path / "egress_targets.yaml"
    p.write_text(
        """
targets:
  ok_canvas:
    type: canvas
    config_ref: report.canvas_id
    visibility: internal
    free_text_allowed: true
    requires_human_approval: false
  fixed_only:
    type: canvas
    config_ref: report.canvas_id
    visibility: internal
    free_text_allowed: false
    requires_human_approval: false
  external:
    type: slack
    config_ref: slack.channel_id
    visibility: external_visible
    free_text_allowed: true
    requires_human_approval: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(ob, "_targets_cache", None)
    monkeypatch.setattr(ob, "_TARGETS_PATH", p)
    return p


@pytest.fixture
def conn(pm_db_path):
    from db_utils import ensure_canary_table, ensure_tool_calls_table

    c = sqlite3.connect(str(pm_db_path)); c.row_factory = sqlite3.Row
    ensure_tool_calls_table(c); ensure_canary_table(c)
    yield c
    c.close()


class TestScanPayload:
    def test_clean_text_passes(self):
        assert scan_payload("今週の進捗をまとめました") == []

    def test_canary_token_is_detected(self):
        r = scan_payload("参考: docs-abc123.internal-check.invalid",
                         ["docs-abc123.internal-check.invalid"])
        assert len(r) == 1 and "canary" in r[0]

    def test_zero_width_characters_are_detected(self):
        assert any("ゼロ幅" in x for x in scan_payload("正常な文​章"))


class TestTargetAllowList:
    def test_unknown_target_is_blocked(self, targets):
        with pytest.raises(EgressBlocked, match="egress_targets.yaml にありません"):
            post("どこかのチャンネル", "本文")

    def test_free_text_denied_target_blocks_text(self, targets):
        with pytest.raises(EgressBlocked, match="自由文を許可していません"):
            post("fixed_only", "自由文です")

    def test_external_visible_requires_approval(self, targets):
        with pytest.raises(EgressPendingApproval, match="承認が必要"):
            post("external", "外部から見える宛先への投稿")

    def test_dry_run_passes_checks_without_sending(self, targets):
        r = post("ok_canvas", "本文", dry_run=True)
        assert r["outcome"] == "dry_run"


class TestCanaryBlocking:
    def test_canary_in_payload_blocks_send(self, targets, conn):
        from db_utils import plant_canary

        row = plant_canary(conn, kind="hostname", planted_in="box_docs")
        with pytest.raises(EgressBlocked, match="canary"):
            post("ok_canvas", f"資料: {row['token']} を参照", conn=conn, dry_run=True)

    def test_revoked_canary_does_not_block(self, targets, conn):
        from db_utils import plant_canary, revoke_canary

        row = plant_canary(conn, kind="hostname", planted_in="box_docs")
        revoke_canary(conn, row["token"])
        assert post("ok_canvas", f"{row['token']}", conn=conn, dry_run=True)["outcome"] == "dry_run"


class TestRecording:
    def test_blocked_send_is_recorded(self, targets, conn):
        with pytest.raises(EgressBlocked):
            post("fixed_only", "自由文", conn=conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM tool_calls")]
        assert len(rows) == 1
        assert rows[0]["plane"] == "egress"
        assert rows[0]["outcome"] == "blocked"
        assert rows[0]["tool_name"] == "broker:fixed_only"

    def test_ok_send_is_recorded(self, targets, conn):
        post("ok_canvas", "本文", conn=conn, dry_run=True)
        r = conn.execute("SELECT outcome, tool_name FROM tool_calls").fetchone()
        assert r["outcome"] == "ok" and r["tool_name"] == "broker:ok_canvas"

    def test_payload_body_is_not_stored(self, targets, conn):
        """本文そのものは台帳に残さない（機微データを増やさない）。"""
        post("ok_canvas", "極秘の本文テキスト", conn=conn, dry_run=True)
        r = conn.execute("SELECT args_json, result_sha256 FROM tool_calls").fetchone()
        assert "極秘の本文テキスト" not in r["args_json"]


class TestSlackNotMigrated:
    def test_slack_dispatch_is_explicitly_unavailable(self, targets, conn, monkeypatch):
        """Slack は SDK 直叩き 25 箇所の移送が未完了。黙って成功させない。"""
        monkeypatch.setattr(ob, "_resolve_config_ref", lambda ref: "C0XXXXXXX")
        monkeypatch.setitem(ob.load_targets(), "external",
                            {**ob.load_targets()["external"], "requires_human_approval": False})
        with pytest.raises(EgressBlocked, match="移送していません"):
            post("external", "本文", conn=conn)
