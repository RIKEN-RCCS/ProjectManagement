"""tool_calls 台帳（docs/security-architecture.md §4.4）のテスト。

ハッシュ連鎖・追記専用・plane 判定・記録の fail-open を検証する。
"""
from __future__ import annotations

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
