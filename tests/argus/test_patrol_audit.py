"""Patrol Agent の LLM 判定・自動クローズの監査記録（§4.4）テスト。

`patrol/audit.py` の record_call / record_reasoning と、その呼び出し元
（patrol/detect.py の LLM 判定3箇所、patrol/actions.py の close_action_item）を検証する。
本文（ai_content・証拠テキスト）が tool_calls.args_json に漏れないことが本設計の要。

Patrol は単一スレッドのため、record_call / record_reasoning は `ctx.conn` が
あればそれに相乗りし、別接続は開かない（別接続方式では ctx.conn の未コミットの
書き込みとロック競合し、監査行が恒久的に記録されないことが実測で確認されている）。
`ctx.conn` に相乗りする以上、監査行は呼び出し側が commit するまで永続化されない
（対象の変更と監査行は同じコミット境界で一緒に確定する）。
`TestRecordCallSharesCallerConnection` はロック衝突の再発検出と、
コミット境界が対象の変更と揃っていることの両方を確認する。
"""
from __future__ import annotations

from types import SimpleNamespace

from argus.patrol import actions, audit, detect
from db_utils import init_pm_db, open_pm_db


def _ctx(db_path, **kw):
    return SimpleNamespace(
        session_id=kw.pop("session_id", "patrol-test"),
        audit_db=kw.pop("audit_db", db_path),
        data_dir=kw.pop("data_dir", db_path.parent),
        tool_seq=kw.pop("tool_seq", 0),
        model=kw.pop("model", "test-model"),
        model_revision=kw.pop("model_revision", ""),
        **kw,
    )


def _tool_call_rows(db_path):
    conn = open_pm_db(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tool_calls ORDER BY seq")]
    finally:
        conn.close()


class TestRecordCallNoSession:
    def test_record_call_noop_when_session_empty(self, tmp_path):
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        ctx = _ctx(db_path, session_id="")
        audit.record_call(ctx, "patrol_judge_completion", {"a": 1}, "ok")
        assert _tool_call_rows(db_path) == []

    def test_record_reasoning_noop_when_session_empty(self, tmp_path):
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        ctx = _ctx(db_path, session_id="")
        assert audit.record_reasoning(ctx, 1, "何らかの思考トレース") is None


class TestRecordCallWritesRow:
    def test_appends_row_and_seq_increments(self, tmp_path):
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        ctx = _ctx(db_path)
        audit.record_call(ctx, "patrol_judge_completion", {"a": 1}, "ok")
        audit.record_call(ctx, "patrol_judge_obsolete", {"b": 2}, "error")
        rows = _tool_call_rows(db_path)
        assert [r["seq"] for r in rows] == [1, 2]
        assert ctx.tool_seq == 2
        assert rows[0]["tool_name"] == "patrol_judge_completion"
        assert rows[0]["outcome"] == "ok"
        assert rows[1]["outcome"] == "error"


class TestRecordCallFailOpen:
    def test_recording_failure_does_not_raise(self, tmp_path):
        """audit_db の親ディレクトリが存在しない等、記録自体が失敗しても
        呼び出し元へ例外を伝播しない（fail-open）。"""
        db_path = tmp_path / "存在しないディレクトリ" / "pm.db"
        ctx = _ctx(db_path)
        audit.record_call(ctx, "patrol_judge_completion", {"a": 1}, "ok")
        assert audit.record_reasoning(ctx, 1, "トレース") is None


class TestRecordCallSharesCallerConnection:
    def test_uncommitted_write_then_record_call_then_commit(self, tmp_path):
        """ctx.conn に未コミットの書き込みがある状態で record_call を呼んでも
        例外にならないこと（別接続方式で再現していたロック衝突の再発検出）。

        record_call は ctx.conn に相乗りするため、呼び出し側が commit() する
        までは対象の変更（action_items）も監査行（tool_calls）も別接続からは
        見えず、commit() した時点で両方が同じコミット境界で一緒に見えること。
        """
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        writer = open_pm_db(db_path)
        writer.execute(
            "INSERT INTO action_items (id, content, assignee, status, source)"
            " VALUES (1, 'x', 'someone', 'open', 'slack')"
        )
        assert writer.in_transaction is True

        ctx = _ctx(db_path)
        ctx.conn = writer
        # 別接続を開かないため、writer 側の未コミットの書き込みとロック競合しない
        # （例外が飛ばないこと自体がこのテストの主眼）。
        audit.record_call(ctx, "patrol_judge_completion", {"a": 1}, "ok")

        # commit() 前は、対象の変更（action_items）も監査行（tool_calls）も
        # 別接続からは一切見えない。
        reader = open_pm_db(db_path)
        try:
            assert reader.execute(
                "SELECT COUNT(*) FROM action_items"
            ).fetchone()[0] == 0
            has_tool_calls_table = reader.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
            ).fetchone()
            assert (
                has_tool_calls_table is None
                or reader.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
            )
        finally:
            reader.close()

        writer.commit()
        writer.close()

        # commit() 後は両方が同じコミット境界で一緒に見える。
        reader = open_pm_db(db_path)
        try:
            assert reader.execute(
                "SELECT COUNT(*) FROM action_items"
            ).fetchone()[0] == 1
            rows = [dict(r) for r in reader.execute("SELECT * FROM tool_calls ORDER BY seq")]
        finally:
            reader.close()
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "patrol_judge_completion"

    def test_uses_ctx_conn_directly_without_opening_new_connection(self, tmp_path, monkeypatch):
        """audit_db を壊れたパスにしても ctx.conn があればそちらが使われる
        （別接続を一切開かないことの確認）。"""
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        writer = open_pm_db(db_path)

        ctx = _ctx(db_path, audit_db=tmp_path / "存在しないディレクトリ" / "pm.db")
        ctx.conn = writer
        audit.record_call(ctx, "patrol_judge_completion", {"a": 1}, "ok")
        writer.commit()
        writer.close()

        assert len(_tool_call_rows(db_path)) == 1


class TestLlmJudgmentRecordingOmitsBody:
    def test_completion_judgment_does_not_leak_body_text(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        ctx = _ctx(db_path)

        def fake_call(prompt, **kw):
            return "YES|HIGH: 極秘の根拠テキストそのもの"

        monkeypatch.setattr("cli_utils.call_argus_llm", fake_call)

        ai_content = "極秘プロジェクトXの内部資料を提出する"
        evidence = [
            {"source_type": "meeting", "held_at": "2026-06-15",
             "source_ref": "ref", "content": "極秘の証拠テキストそのもの"},
        ]

        result = detect._llm_judge_completion(
            ai_content, evidence, "2026-06-10T09:00:00", ctx=ctx, ai_id=42,
        )
        assert result[0] is True
        assert result[1] == "HIGH"

        rows = _tool_call_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["tool_name"] == "patrol_judge_completion"
        assert row["plane"] == "read"
        assert row["outcome"] == "ok"
        assert row["result_sha256"]
        # 本文そのものは args_json に現れない（sha256/文字数のみ）
        assert ai_content not in row["args_json"]
        assert "極秘" not in row["args_json"]
        assert "content_sha256" in row["args_json"]
        assert "evidence_sha256" in row["args_json"]
        assert '"ai_id": 42' in row["args_json"]
        # 生応答は reasoning_traces 側にのみ保存される
        assert row["reasoning_sha256"]

        conn = open_pm_db(db_path)
        try:
            trace_rows = [
                dict(r) for r in conn.execute("SELECT * FROM reasoning_traces")
            ]
        finally:
            conn.close()
        assert len(trace_rows) == 1
        assert "極秘の根拠テキストそのもの" in trace_rows[0]["trace"]

    def test_no_recording_when_ctx_is_none(self, monkeypatch):
        """既存の呼び出し形（ctx 省略）は壊れない。"""
        def fake_call(prompt, **kw):
            return "NO"

        monkeypatch.setattr("cli_utils.call_argus_llm", fake_call)
        evidence = [{"source_type": "meeting", "held_at": "2026-06-15",
                     "source_ref": "ref", "content": "何らかの証拠"}]
        result = detect._llm_judge_completion("資料を提出する", evidence, "2026-06-10T09:00:00")
        assert result == (False, None, "")


class TestCloseActionItemRecordsMutatePlane:
    def test_close_action_item_records_mutate_ok(self, tmp_path):
        """close_action_item は ctx.conn.commit() を自分では呼ばない
        （actions.py 側の設計。§4.4 の docstring 参照）。record_call が
        ctx.conn に相乗りするため、呼び出し側が commit() するまではクローズ
        （action_items）も監査行（tool_calls）も別接続からは見えず、
        commit() した時点で両方が同じコミット境界で一緒に見えることを確認する。
        """
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        writer = open_pm_db(db_path)
        writer.execute(
            "INSERT INTO action_items (id, content, assignee, status, source)"
            " VALUES (1, 'x', 'someone', 'open', 'slack')"
        )
        writer.commit()

        ctx = _ctx(db_path, dry_run=False, today="2026-08-01")
        ctx.conn = writer
        ok = actions.close_action_item(ctx, 1, "argus_auto", note="完了の根拠")
        assert ok is True

        # close_action_item はここで commit() を呼んでいないため、
        # クローズも監査行もまだ別接続からは見えない。
        reader = open_pm_db(db_path)
        try:
            status = reader.execute(
                "SELECT status FROM action_items WHERE id = 1"
            ).fetchone()["status"]
            has_tool_calls_table = reader.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
            ).fetchone()
        finally:
            reader.close()
        assert status == "open"
        assert (
            has_tool_calls_table is None
            or _tool_call_rows(db_path) == []
        )

        # 呼び出し側（本来は detect.py）が commit() した時点で、
        # クローズと監査行が同じコミット境界で一緒に見えるようになる。
        writer.commit()
        writer.close()

        reader = open_pm_db(db_path)
        try:
            status = reader.execute(
                "SELECT status FROM action_items WHERE id = 1"
            ).fetchone()["status"]
            rows = [dict(r) for r in reader.execute("SELECT * FROM tool_calls ORDER BY seq")]
        finally:
            reader.close()

        assert status == "closed"
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "patrol_close_action_item"
        assert rows[0]["plane"] == "mutate"
        assert rows[0]["outcome"] == "ok"
        assert '"ai_id": 1' in rows[0]["args_json"]

    def test_close_action_item_not_found_records_error(self, tmp_path):
        db_path = tmp_path / "pm.db"
        init_pm_db(db_path).close()
        conn = open_pm_db(db_path)

        ctx = _ctx(db_path, dry_run=False, today="2026-08-01")
        ctx.conn = conn
        ok = actions.close_action_item(ctx, 999, "argus_auto")
        assert ok is False
        conn.close()

        rows = _tool_call_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "error"
