"""execute_tool の tool_calls 記録（§4.4）— 実行経路側のテスト。"""
from __future__ import annotations

import sqlite3

from argus.agent_tools import AgentContext, plane_of
from argus.pm_argus_agent import execute_tool
from db_utils import init_pm_db, verify_tool_call_chain


def _ctx(tmp_path, **kw):
    init_pm_db(tmp_path / "pm.db", no_encrypt=True).close()
    return AgentContext(
        conns=[], today="2026-08-01", since="2026-07-01", no_encrypt=True,
        data_dir=tmp_path, session_id=kw.pop("session_id", "sess-1"),
        audit_db=tmp_path / "pm.db", model="glm-5.2", model_revision="unverified", **kw,
    )


def _rows(tmp_path):
    c = sqlite3.connect(str(tmp_path / "pm.db")); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM tool_calls ORDER BY seq")]
    finally:
        c.close()


class TestPlaneOf:
    def test_egress_read_and_unknown(self):
        assert plane_of("slack_post_message") == "egress"
        assert plane_of("search_text") == "read"
        assert plane_of("未知のツール") == "mutate"


class TestExecuteToolRecording:
    def test_egress_attempt_is_recorded_as_blocked(self, tmp_path):
        ctx = _ctx(tmp_path)
        out = execute_tool("slack_post_message", {"channel": "C0XXXXXXX"}, ctx)
        assert "使用できません" in out
        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "blocked"
        assert rows[0]["plane"] == "egress"
        assert rows[0]["block_reason"] == "egress_not_in_allowlist"

    def test_unknown_tool_is_recorded(self, tmp_path):
        ctx = _ctx(tmp_path)
        execute_tool("no_such_tool", {}, ctx)
        assert _rows(tmp_path)[0]["block_reason"] == "not_in_allowlist"

    def test_seq_increments_and_chain_stays_valid(self, tmp_path):
        ctx = _ctx(tmp_path)
        execute_tool("slack_post_message", {}, ctx)
        execute_tool("no_such_tool", {}, ctx)
        rows = _rows(tmp_path)
        assert [r["seq"] for r in rows] == [1, 2]
        c = sqlite3.connect(str(tmp_path / "pm.db")); c.row_factory = sqlite3.Row
        assert verify_tool_call_chain(c) == []
        c.close()

    def test_no_session_id_disables_recording(self, tmp_path):
        """session_id が空なら記録しない（dry-run / テスト用の退避路）。"""
        ctx = _ctx(tmp_path, session_id="")
        execute_tool("slack_post_message", {}, ctx)
        assert _rows(tmp_path) == []

    def test_recording_failure_does_not_break_execution(self, tmp_path):
        """監査ログの失敗はツール実行を止めない（fail-open）。"""
        ctx = _ctx(tmp_path)
        ctx.audit_db = tmp_path / "存在しないディレクトリ" / "pm.db"
        out = execute_tool("slack_post_message", {}, ctx)
        assert "使用できません" in out  # 例外を出さずに通常の応答を返す
