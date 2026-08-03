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


class TestPendingApprovalRecording:
    """承認フローの受け皿（§4.2）: EgressPendingApproval を投げる前に pending_egress
    へ記録し、例外メッセージに保留 id を含めること。"""

    def test_pending_approval_records_and_includes_id_in_message(self, targets, conn):
        from db_utils import list_pending_egress

        with pytest.raises(EgressPendingApproval) as exc_info:
            post("external", "外部から見える宛先への投稿", conn=conn)

        rows = list_pending_egress(conn)
        assert len(rows) == 1
        assert rows[0]["target"] == "external"
        assert rows[0]["status"] == "pending"
        assert f"id={rows[0]['id']}" in str(exc_info.value)
        assert "--approve" in str(exc_info.value)

    def test_pending_approval_without_conn_does_not_record(self, targets):
        with pytest.raises(EgressPendingApproval, match="承認が必要"):
            post("external", "外部から見える宛先への投稿")


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


class TestApprovalCLI:
    """CLI --list-pending/--approve/--reject（§4.2 の承認フローの受け皿）。

    実 Box/Canvas/Slack へは送信しない（`_dispatch` をモンキーパッチする）。
    実 argus_config.yaml も読まない（`_ARGUS_CONFIG` を一時ファイルへ差し替える）。
    """

    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch, pm_db_path):
        targets_path = tmp_path / "egress_targets.yaml"
        targets_path.write_text(
            """
targets:
  external:
    type: canvas
    config_ref: report.canvas_id
    visibility: external_visible
    free_text_allowed: true
    requires_human_approval: true
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(ob, "_targets_cache", None)
        monkeypatch.setattr(ob, "_TARGETS_PATH", targets_path)

        argus_config_path = tmp_path / "argus_config.yaml"
        argus_config_path.write_text("report:\n  canvas_id: F0XXXXXXX\n", encoding="utf-8")
        monkeypatch.setattr(ob, "_ARGUS_CONFIG", argus_config_path)
        return pm_db_path

    def _seed_pending(self, db_path, *, content="本文", plant_canary_token=False):
        import sqlite3

        from db_utils import ensure_tool_calls_table, record_pending_egress

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        ensure_tool_calls_table(conn)
        token = None
        if plant_canary_token:
            from db_utils import ensure_canary_table, plant_canary
            ensure_canary_table(conn)
            token = plant_canary(conn, kind="hostname", planted_in="box_docs")["token"]
            content = f"参照: {token}"
        egress_id = record_pending_egress(
            conn, target="external", content=content, block_reason="人間の承認が必要な宛先です",
        )
        conn.close()
        return egress_id

    def _status(self, db_path, egress_id):
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM pending_egress WHERE id = ?", (egress_id,)
        ).fetchone()
        conn.close()
        return row["status"]

    def test_list_pending_shows_truncated_body(self, cli_env, capsys):
        self._seed_pending(cli_env, content="x" * 300)

        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--list-pending"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "target=external" in out
        assert "x" * 200 in out
        assert "x" * 201 not in out

    def test_no_flags_prints_help_and_fails(self, cli_env):
        assert ob.main(["--db", str(cli_env), "--no-encrypt"]) == 1

    def test_approve_dispatches_and_updates_status(self, cli_env, monkeypatch):
        egress_id = self._seed_pending(cli_env)
        dispatched = []
        monkeypatch.setattr(
            ob, "_dispatch",
            lambda kind, dest, content, **kw: dispatched.append((kind, dest, content)),
        )

        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--approve", str(egress_id)])

        assert rc == 0
        assert dispatched == [("canvas", "F0XXXXXXX", "本文")]
        assert self._status(cli_env, egress_id) == "approved"

    def test_reject_does_not_dispatch(self, cli_env, monkeypatch):
        egress_id = self._seed_pending(cli_env)
        dispatched = []
        monkeypatch.setattr(ob, "_dispatch", lambda *a, **kw: dispatched.append(a))

        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--reject", str(egress_id)])

        assert rc == 0
        assert dispatched == []
        assert self._status(cli_env, egress_id) == "rejected"

    def test_approve_blocks_when_canary_present(self, cli_env, monkeypatch):
        egress_id = self._seed_pending(cli_env, plant_canary_token=True)
        dispatched = []
        monkeypatch.setattr(ob, "_dispatch", lambda *a, **kw: dispatched.append(a))

        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--approve", str(egress_id)])

        assert rc == 1
        assert dispatched == []
        assert self._status(cli_env, egress_id) == "pending"

    def test_approve_unknown_id_fails_without_dispatch(self, cli_env, monkeypatch):
        dispatched = []
        monkeypatch.setattr(ob, "_dispatch", lambda *a, **kw: dispatched.append(a))

        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--approve", "999999"])

        assert rc == 1
        assert dispatched == []

    def test_reject_unknown_id_fails(self, cli_env):
        rc = ob.main(["--db", str(cli_env), "--no-encrypt", "--reject", "999999"])
        assert rc == 1
