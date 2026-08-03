"""tool_calls 台帳（docs/security-architecture.md §4.4）のテスト。

ハッシュ連鎖・追記専用・plane 判定・記録の fail-open を検証する。
"""
from __future__ import annotations

import logging
import sqlite3

import pytest
from db_utils import (
    GENESIS_HASH,
    ensure_tool_calls_table,
    record_tool_call,
    shannon_entropy,
    verify_tool_call_chain,
)


@pytest.fixture
def conn(pm_db_path):
    c = sqlite3.connect(str(pm_db_path))
    c.row_factory = sqlite3.Row
    ensure_tool_calls_table(c)
    yield c
    c.close()


def _rec(conn, name="search_text", outcome="ok", **kw):
    kw.setdefault("session_id", "s1")
    kw.setdefault("seq", conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0] + 1)
    kw.setdefault("plane", "read")
    kw.setdefault("args", {"query": "テスト"})
    return record_tool_call(conn, tool_name=name, outcome=outcome, **kw)


class TestHashChain:
    def test_first_entry_links_to_genesis(self, conn):
        row = _rec(conn)
        assert row["prev_hash"] == GENESIS_HASH
        assert len(row["entry_hash"]) == 64

    def test_chain_links_successive_entries(self, conn):
        a = _rec(conn)
        b = _rec(conn, name="search_decisions")
        assert b["prev_hash"] == a["entry_hash"]
        assert verify_tool_call_chain(conn) == []

    def test_verify_detects_tampered_content(self, conn):
        _rec(conn)
        _rec(conn, name="search_decisions")
        # トリガを外して直接書き換える（事故による破損の再現）
        conn.execute("DROP TRIGGER tool_calls_no_update")
        conn.execute("UPDATE tool_calls SET args_json='{\"query\":\"改竄\"}' WHERE seq=1")
        conn.commit()
        broken = verify_tool_call_chain(conn)
        assert len(broken) >= 1
        assert "entry_hash" in broken[0]["reason"]

    def test_verify_on_missing_table_is_empty(self, pm_db_path):
        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        assert verify_tool_call_chain(c) == []
        c.close()


class TestAppendOnly:
    def test_update_is_rejected(self, conn):
        _rec(conn)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE tool_calls SET outcome='blocked'")

    def test_delete_is_rejected(self, conn):
        _rec(conn)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM tool_calls")


class TestFields:
    def test_blocked_call_is_recorded_with_reason(self, conn):
        _rec(conn, name="slack_post_message", outcome="blocked", plane="egress",
             block_reason="egress_not_in_allowlist")
        r = conn.execute("SELECT * FROM tool_calls").fetchone()
        assert r["plane"] == "egress"
        assert r["outcome"] == "blocked"
        assert r["block_reason"] == "egress_not_in_allowlist"

    def test_result_hash_and_size_recorded(self, conn):
        _rec(conn, result="あいうえお")
        r = conn.execute("SELECT result_bytes, result_sha256 FROM tool_calls").fetchone()
        assert r["result_bytes"] == len("あいうえお".encode())
        assert len(r["result_sha256"]) == 64

    def test_result_absent_leaves_nulls(self, conn):
        _rec(conn, outcome="blocked", block_reason="x")
        r = conn.execute("SELECT result_bytes, result_sha256 FROM tool_calls").fetchone()
        assert r["result_bytes"] is None and r["result_sha256"] is None

    def test_invalid_plane_or_outcome_rejected(self, conn):
        with pytest.raises(ValueError, match="plane"):
            _rec(conn, plane="bogus")
        with pytest.raises(ValueError, match="outcome"):
            _rec(conn, outcome="bogus")

    def test_args_max_entropy_prefers_high_entropy_value(self, conn):
        _rec(conn, args={"short": "aaaa", "payload": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5"})
        r = conn.execute("SELECT args_max_entropy FROM tool_calls").fetchone()
        assert r["args_max_entropy"] > 3.0


class TestEntropy:
    def test_repeated_chars_are_low(self):
        assert shannon_entropy("aaaaaaaa") == 0.0

    def test_empty_is_zero(self):
        assert shannon_entropy("") == 0.0

    def test_base64ish_is_higher_than_natural_text(self):
        assert shannon_entropy("dGhpcyBpcyBhIHNlY3JldCBwYXlsb2Fk") > shannon_entropy("ああああいいいい")


# --------------------------------------------------------------------------- #
# reasoning_traces（§4.4）
# --------------------------------------------------------------------------- #
class TestReasoningTraces:
    def test_record_returns_sha_and_stores_body(self, conn):
        from db_utils import record_reasoning_trace

        sha = record_reasoning_trace(conn, session_id="s1", step=1,
                                     trace="まず決定事項を調べる", model="glm-5.2")
        assert len(sha) == 64
        r = conn.execute("SELECT * FROM reasoning_traces").fetchone()
        assert r["trace"] == "まず決定事項を調べる"
        assert r["char_count"] == len("まず決定事項を調べる")
        assert r["trace_sha256"] == sha

    def test_empty_trace_is_not_stored(self, conn):
        from db_utils import ensure_reasoning_traces_table, record_reasoning_trace

        ensure_reasoning_traces_table(conn)
        assert record_reasoning_trace(conn, session_id="s1", step=1, trace="") is None
        assert conn.execute("SELECT count(*) FROM reasoning_traces").fetchone()[0] == 0

    def test_purge_removes_only_expired(self, conn):
        from datetime import UTC, datetime, timedelta

        from db_utils import purge_reasoning_traces, record_reasoning_trace

        record_reasoning_trace(conn, session_id="old", step=1, trace="古い")
        record_reasoning_trace(conn, session_id="new", step=1, trace="新しい")
        stale = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        conn.execute("UPDATE reasoning_traces SET ts=? WHERE session_id='old'", (stale,))
        conn.commit()

        assert purge_reasoning_traces(conn, days=90) == 1
        left = [r["session_id"] for r in conn.execute("SELECT session_id FROM reasoning_traces")]
        assert left == ["new"]

    def test_purge_keeps_protected_sessions(self, conn):
        """canary 発火時の保全 — 期限切れでも keep_sessions は残す。"""
        from datetime import UTC, datetime, timedelta

        from db_utils import purge_reasoning_traces, record_reasoning_trace

        record_reasoning_trace(conn, session_id="incident", step=1, trace="保全対象")
        stale = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        conn.execute("UPDATE reasoning_traces SET ts=?", (stale,))
        conn.commit()

        assert purge_reasoning_traces(conn, days=90, keep_sessions=["incident"]) == 0
        assert conn.execute("SELECT count(*) FROM reasoning_traces").fetchone()[0] == 1

    def test_purge_on_missing_table_is_zero(self, pm_db_path):
        import sqlite3 as s3

        from db_utils import purge_reasoning_traces

        c = s3.connect(str(pm_db_path)); c.row_factory = s3.Row
        c.execute("DROP TABLE IF EXISTS reasoning_traces"); c.commit()
        assert purge_reasoning_traces(c) == 0
        c.close()


class TestConcurrentRecording:
    """欠陥2（prev_hash 読み取り〜INSERT が直列化されておらず並列書き込みで連鎖が壊れる）
    の回帰テスト。8スレッド x 10回、別接続から同一 DB に書き込んでも連鎖が壊れないこと。
    """

    def test_parallel_writes_from_separate_connections_keep_chain_valid(self, pm_db_path):
        import threading

        n_threads = 8
        n_per_thread = 10
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            c = sqlite3.connect(str(pm_db_path))
            c.row_factory = sqlite3.Row
            try:
                for i in range(n_per_thread):
                    record_tool_call(
                        c, session_id=f"s{n}", seq=i, plane="read",
                        tool_name="search_text", args={"thread": n, "i": i}, outcome="ok",
                    )
            except BaseException as exc:  # noqa: BLE001 - スレッド内例外を集めて後で assert する
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors

        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        try:
            assert c.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == (
                n_threads * n_per_thread
            )
            assert verify_tool_call_chain(c) == []
        finally:
            c.close()


class TestAnchor:
    def test_anchor_returns_latest_hash_and_count(self, conn):
        from db_utils import tool_call_anchor

        _rec(conn)
        last = _rec(conn, name="search_decisions")
        a = tool_call_anchor(conn)
        assert a["entry_hash"] == last["entry_hash"]
        assert a["count"] == 2

    def test_anchor_is_none_when_empty(self, conn):
        from db_utils import tool_call_anchor

        assert tool_call_anchor(conn) is None


# --------------------------------------------------------------------------- #
# pending_egress（承認フローの受け皿。docs/security-architecture.md §4.2）
# --------------------------------------------------------------------------- #
class TestPendingEgress:
    def test_record_returns_id_and_stores_body(self, conn):
        from db_utils import record_pending_egress

        egress_id = record_pending_egress(
            conn, target="collab_shared_slack", content="外部宛の本文",
            block_reason="人間の承認が必要な宛先です",
        )
        assert isinstance(egress_id, int)
        row = conn.execute("SELECT * FROM pending_egress WHERE id = ?", (egress_id,)).fetchone()
        assert row["target"] == "collab_shared_slack"
        assert row["content"] == "外部宛の本文"
        assert row["status"] == "pending"
        assert row["chars"] == len("外部宛の本文")

    def test_record_also_writes_tool_calls(self, conn):
        from db_utils import record_pending_egress

        record_pending_egress(conn, target="t", content="本文", block_reason="reason")
        r = conn.execute("SELECT plane, tool_name, outcome FROM tool_calls").fetchone()
        assert r["plane"] == "egress"
        assert r["tool_name"] == "broker:pending"
        assert r["outcome"] == "blocked"

    def test_list_returns_newest_first(self, conn):
        from db_utils import list_pending_egress, record_pending_egress

        record_pending_egress(conn, target="a", content="1件目", block_reason="r")
        record_pending_egress(conn, target="b", content="2件目", block_reason="r")
        rows = list_pending_egress(conn)
        assert [r["target"] for r in rows] == ["b", "a"]

    def test_list_on_missing_table_is_empty(self, conn):
        from db_utils import list_pending_egress

        assert list_pending_egress(conn) == []

    def test_decide_approve_updates_status(self, conn):
        from db_utils import decide_pending_egress, record_pending_egress

        egress_id = record_pending_egress(conn, target="a", content="本文", block_reason="r")
        row = decide_pending_egress(conn, egress_id, approve=True, decided_by="tester")
        assert row["status"] == "approved"
        assert row["decided_by"] == "tester"
        assert row["decided_at"] is not None
        stored = conn.execute(
            "SELECT status, decided_by FROM pending_egress WHERE id = ?", (egress_id,)
        ).fetchone()
        assert stored["status"] == "approved"
        assert stored["decided_by"] == "tester"

    def test_decide_reject_updates_status(self, conn):
        from db_utils import decide_pending_egress, record_pending_egress

        egress_id = record_pending_egress(conn, target="a", content="本文", block_reason="r")
        row = decide_pending_egress(conn, egress_id, approve=False, decided_by="tester")
        assert row["status"] == "rejected"

    def test_decide_unknown_id_raises(self, conn):
        from db_utils import decide_pending_egress

        with pytest.raises(ValueError, match="見つかりません"):
            decide_pending_egress(conn, 9999, approve=True, decided_by="tester")

    def test_decide_twice_raises(self, conn):
        from db_utils import decide_pending_egress, record_pending_egress

        egress_id = record_pending_egress(conn, target="a", content="本文", block_reason="r")
        decide_pending_egress(conn, egress_id, approve=True, decided_by="tester")
        with pytest.raises(ValueError, match="既に"):
            decide_pending_egress(conn, egress_id, approve=False, decided_by="tester")


# --------------------------------------------------------------------------- #
# triage_second_opinion の reviewed_at 列（所見が読まれる仕組み。pending_egress と同じ形）
# --------------------------------------------------------------------------- #
class TestSecondOpinionReviewedColumn:
    def test_ensure_column_is_idempotent(self, conn):
        from db_utils import ensure_second_opinion_reviewed_column

        ensure_second_opinion_reviewed_column(conn)
        ensure_second_opinion_reviewed_column(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(triage_second_opinion)").fetchall()}
        assert "reviewed_at" in cols

    def test_ensure_column_without_table_is_noop(self, tmp_path):
        import sqlite3 as s3

        from db_utils import ensure_second_opinion_reviewed_column

        p = tmp_path / "empty.db"
        c = s3.connect(str(p))
        ensure_second_opinion_reviewed_column(c)  # 例外にならないこと
        c.close()

    def test_existing_rows_are_null_after_backfill(self, conn):
        from db_utils import (
            ensure_second_opinion_reviewed_column,
            record_second_opinion,
        )

        record_second_opinion(
            conn, kind="minutes_extraction", content="content",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        ensure_second_opinion_reviewed_column(conn)
        row = conn.execute("SELECT reviewed_at FROM triage_second_opinion").fetchone()
        assert row["reviewed_at"] is None

    def test_list_findings_returns_newest_first(self, conn):
        from db_utils import list_second_opinion_findings, record_second_opinion

        record_second_opinion(
            conn, kind="minutes_extraction", content="1件目",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        record_second_opinion(
            conn, kind="minutes_extraction_recall", content="2件目",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        rows = list_second_opinion_findings(conn)
        assert [r["content_head"] for r in rows] == ["2件目", "1件目"]

    def test_list_findings_filters_by_kind(self, conn):
        from db_utils import list_second_opinion_findings, record_second_opinion

        record_second_opinion(
            conn, kind="minutes_extraction", content="a",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        record_second_opinion(
            conn, kind="minutes_extraction_recall", content="b",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        rows = list_second_opinion_findings(conn, kind="minutes_extraction_recall")
        assert len(rows) == 1
        assert rows[0]["kind"] == "minutes_extraction_recall"

    def test_list_findings_unreviewed_only(self, conn):
        from db_utils import (
            list_second_opinion_findings,
            mark_second_opinion_reviewed,
            record_second_opinion,
        )

        record_second_opinion(
            conn, kind="minutes_extraction", content="a",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        record_second_opinion(
            conn, kind="minutes_extraction", content="b",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        rows = list_second_opinion_findings(conn)
        mark_second_opinion_reviewed(conn, [rows[0]["id"]])
        remaining = list_second_opinion_findings(conn, unreviewed_only=True)
        assert len(remaining) == 1
        assert remaining[0]["id"] == rows[1]["id"]

    def test_list_findings_without_table_is_empty(self, tmp_path):
        import sqlite3 as s3

        from db_utils import list_second_opinion_findings

        p = tmp_path / "empty.db"
        c = s3.connect(str(p))
        assert list_second_opinion_findings(c) == []
        c.close()

    def test_mark_reviewed_sets_timestamp(self, conn):
        from db_utils import mark_second_opinion_reviewed, record_second_opinion

        record_second_opinion(
            conn, kind="minutes_extraction", content="a",
            primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
        )
        row_id = conn.execute("SELECT id FROM triage_second_opinion").fetchone()["id"]
        n = mark_second_opinion_reviewed(conn, [row_id])
        assert n == 1
        row = conn.execute(
            "SELECT reviewed_at FROM triage_second_opinion WHERE id=?", (row_id,)
        ).fetchone()
        assert row["reviewed_at"] is not None

    def test_mark_reviewed_unknown_id_returns_zero(self, conn):
        from db_utils import (
            ensure_second_opinion_reviewed_column,
            mark_second_opinion_reviewed,
        )

        ensure_second_opinion_reviewed_column(conn)
        n = mark_second_opinion_reviewed(conn, [9999])
        assert n == 0


# --------------------------------------------------------------------------- #
# ensure_tool_calls_table() が呼び出し側の未コミットトランザクションを
# 暗黙 COMMIT してしまう欠陥の回帰テスト（executescript() の implicit commit）。
# --------------------------------------------------------------------------- #
class TestCallerTransactionIsolation:
    def test_uncommitted_caller_work_is_not_committed_by_record_tool_call(self, pm_db_path):
        """欠陥の再発検出: tool_calls が既に存在する DB で、呼び出し側が未コミットの
        別テーブルへの書き込みを持ったまま record_tool_call を呼んでも、
        rollback すればその書き込みは残らないこと。"""
        a = sqlite3.connect(str(pm_db_path))
        a.row_factory = sqlite3.Row
        ensure_tool_calls_table(a)  # tool_calls を先に存在させておく

        a.execute("BEGIN")
        a.execute("INSERT INTO action_items (content) VALUES ('未コミットの作業')")
        _rec(a)
        a.rollback()
        a.close()

        b = sqlite3.connect(str(pm_db_path))
        b.row_factory = sqlite3.Row
        try:
            count = b.execute(
                "SELECT count(*) FROM action_items WHERE content='未コミットの作業'"
            ).fetchone()[0]
        finally:
            b.close()
        assert count == 0

    def test_record_tool_call_defers_commit_to_caller_transaction(self, pm_db_path):
        a = sqlite3.connect(str(pm_db_path))
        a.row_factory = sqlite3.Row
        ensure_tool_calls_table(a)

        a.execute("BEGIN")
        _rec(a)

        b = sqlite3.connect(str(pm_db_path))
        b.row_factory = sqlite3.Row
        try:
            # 呼び出し側がまだ commit していないので、別接続からはまだ見えない
            assert b.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
        finally:
            b.close()

        a.commit()
        a.close()

        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        try:
            assert c.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 1
        finally:
            c.close()

    def test_record_tool_call_commits_immediately_without_caller_transaction(self, pm_db_path):
        """呼び出し側がトランザクションを開いていない場合は、従来どおり
        record_tool_call 単体で確定すること（own_transaction の既存挙動）。"""
        a = sqlite3.connect(str(pm_db_path))
        a.row_factory = sqlite3.Row
        ensure_tool_calls_table(a)
        assert not a.in_transaction

        _rec(a)
        a.close()

        b = sqlite3.connect(str(pm_db_path))
        b.row_factory = sqlite3.Row
        try:
            assert b.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 1
        finally:
            b.close()

    def test_ensure_tool_calls_table_does_not_change_transaction_state(self, pm_db_path):
        """tool_calls が既にある DB に対して ensure_tool_calls_table を呼んでも
        conn.in_transaction が変化しないこと（executescript の暗黙 COMMIT を
        誘発しないこと）。"""
        a = sqlite3.connect(str(pm_db_path))
        a.row_factory = sqlite3.Row
        ensure_tool_calls_table(a)  # 1回目でテーブルを作成しておく

        a.execute("BEGIN")
        a.execute("INSERT INTO action_items (content) VALUES ('x')")
        assert a.in_transaction

        ensure_tool_calls_table(a)  # 2回目: テーブルは既にある
        assert a.in_transaction

        a.rollback()
        a.close()


# --------------------------------------------------------------------------- #
# open_db() の schema 初期化が素朴な `;` 分割のせいで tool_calls の append-only
# トリガをサイレントに作成し損ねていた欠陥の回帰テスト、および
# ensure_tool_calls_table() によるトリガ欠落の検知・修復の検証。
# --------------------------------------------------------------------------- #
class TestTriggerCreationAndRepair:
    def test_init_pm_db_creates_append_only_triggers(self, pm_db_path):
        """(b) の回帰検出: 新規作成した pm.db に append-only トリガが存在すること。"""
        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        try:
            names = {
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                    " AND name LIKE 'tool_calls_no_%'"
                )
            }
        finally:
            c.close()
        assert names == {"tool_calls_no_update", "tool_calls_no_delete"}

    def test_ensure_tool_calls_table_repairs_missing_triggers_with_warning(
        self, pm_db_path, caplog
    ):
        """(a) の検証: トリガだけを DROP した DB に対して ensure_tool_calls_table()
        を呼ぶと、トリガが再作成され WARNING が出ること。"""
        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        c.execute("DROP TRIGGER tool_calls_no_update")
        c.execute("DROP TRIGGER tool_calls_no_delete")
        c.commit()

        with caplog.at_level(logging.WARNING, logger="db_utils"):
            ensure_tool_calls_table(c)

        assert any(
            "append-only トリガが欠落していたため再作成しました" in r.message
            for r in caplog.records
        )
        names = {
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'tool_calls_no_%'"
            )
        }
        assert names == {"tool_calls_no_update", "tool_calls_no_delete"}
        c.close()

    def test_repaired_triggers_reject_update_and_delete(self, pm_db_path):
        """トリガ再作成後に UPDATE / DELETE が拒否されること。"""
        c = sqlite3.connect(str(pm_db_path))
        c.row_factory = sqlite3.Row
        c.execute("DROP TRIGGER tool_calls_no_update")
        c.execute("DROP TRIGGER tool_calls_no_delete")
        c.commit()

        ensure_tool_calls_table(c)
        record_tool_call(
            c, session_id="s1", seq=1, plane="read", tool_name="search_text",
            args={"query": "テスト"}, outcome="ok",
        )

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE tool_calls SET outcome='blocked'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM tool_calls")
        c.close()
