"""pm_selfcheck.py の check_ 関数単位のテスト（一時DB、本番DB不使用）。"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from quality import pm_selfcheck


def _insert_action_item(conn, **kwargs):
    defaults = {
        "content": "test item",
        "assignee": "someone",
        "due_date": None,
        "status": "open",
        "note": None,
        "source": "meeting",
        "extracted_at": "2026-06-01",
        "deleted": 0,
    }
    defaults.update(kwargs)
    cur = conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, status, note, source, extracted_at, deleted)"
        " VALUES (:content, :assignee, :due_date, :status, :note, :source, :extracted_at, :deleted)",
        defaults,
    )
    conn.commit()
    return cur.lastrowid


def _insert_audit_log(conn, *, table_name, record_id, field, old_value, new_value, changed_at, source):
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (table_name, str(record_id), field, old_value, new_value, changed_at, source),
    )
    conn.commit()


def _open_plain(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# 1. 未来日付
# --------------------------------------------------------------------------- #
def test_check_future_dates_detects_future_extracted_at(pm_db_path):
    conn = _open_plain(pm_db_path)
    today = "2026-07-27"
    future_id = _insert_action_item(conn, extracted_at="2099-01-01")
    _insert_action_item(conn, extracted_at="2026-06-01")

    violations = pm_selfcheck.check_future_dates(conn, today)
    conn.close()

    assert len(violations) == 1
    assert violations[0]["id"] == str(future_id)
    assert violations[0]["table"] == "action_items"


def test_check_future_dates_no_violation_for_past_dates(pm_db_path):
    conn = _open_plain(pm_db_path)
    _insert_action_item(conn, extracted_at="2020-01-01")
    violations = pm_selfcheck.check_future_dates(conn, "2026-07-27")
    conn.close()
    assert violations == []


# --------------------------------------------------------------------------- #
# 2. 日付逆転クローズ
# --------------------------------------------------------------------------- #
def test_check_date_reversal_closes_detects_stale_evidence(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(
        conn, extracted_at="2026-06-09", status="closed",
        note="2026-07-01 自動クローズ: [LLM判定/HIGH] 2026-05-18のBox文書で完了報告",
    )
    now = datetime.now(UTC).isoformat()
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed", changed_at=now, source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="note",
        old_value="", new_value="2026-07-01 自動クローズ: [LLM判定/HIGH] 2026-05-18のBox文書で完了報告",
        changed_at=now, source="argus_auto",
    )

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_date_reversal_closes(conn, cutoff)
    conn.close()

    assert len(violations) == 1
    assert violations[0]["id"] == str(ai_id)
    assert "2026-05-18" in violations[0]["cited_dates"]


def test_check_date_reversal_closes_no_violation_when_evidence_is_recent(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(
        conn, extracted_at="2026-06-09", status="closed",
        note="2026-07-01 自動クローズ: [LLM判定/HIGH] 2026-06-20のSlackで完了報告",
    )
    now = datetime.now(UTC).isoformat()
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed", changed_at=now, source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="note",
        old_value="", new_value="2026-07-01 自動クローズ: [LLM判定/HIGH] 2026-06-20のSlackで完了報告",
        changed_at=now, source="argus_auto",
    )

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_date_reversal_closes(conn, cutoff)
    conn.close()
    assert violations == []


# --------------------------------------------------------------------------- #
# 3. 巻き戻りパターン
# --------------------------------------------------------------------------- #
def test_check_rollback_pattern_detects_xlsx_sync_revert_within_24h(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(conn, status="open")
    t0 = datetime.now(UTC)
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed",
        changed_at=t0.isoformat(), source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="closed", new_value="open",
        changed_at=(t0 + timedelta(hours=1)).isoformat(), source="xlsx_sync",
    )

    cutoff = (t0 - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_rollback_pattern(conn, cutoff)
    conn.close()

    assert len(violations) == 1
    assert violations[0]["id"] == str(ai_id)


def test_check_rollback_pattern_no_violation_when_revert_after_24h(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(conn, status="open")
    t0 = datetime.now(UTC)
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed",
        changed_at=t0.isoformat(), source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="closed", new_value="open",
        changed_at=(t0 + timedelta(hours=48)).isoformat(), source="xlsx_sync",
    )

    cutoff = (t0 - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_rollback_pattern(conn, cutoff)
    conn.close()
    assert violations == []


def test_check_rollback_pattern_excludes_resolved_rollback(pm_db_path):
    """巻き戻し後に manual_restore 等で再クローズ済みなら違反としない（解消済み）。"""
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(conn, status="closed")
    t0 = datetime.now(UTC)
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed",
        changed_at=t0.isoformat(), source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="closed", new_value="open",
        changed_at=(t0 + timedelta(hours=1)).isoformat(), source="xlsx_sync",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed",
        changed_at=(t0 + timedelta(hours=2)).isoformat(), source="manual_restore",
    )

    cutoff = (t0 - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_rollback_pattern(conn, cutoff)
    conn.close()
    assert violations == []


def test_check_rollback_pattern_reports_unresolved_rollback(pm_db_path):
    """巻き戻し後に再クローズされていなければ未解消として違反を報告する。"""
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(conn, status="open")
    t0 = datetime.now(UTC)
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed",
        changed_at=t0.isoformat(), source="argus_auto",
    )
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="closed", new_value="open",
        changed_at=(t0 + timedelta(hours=1)).isoformat(), source="xlsx_sync",
    )
    # 再クローズなし → 未解消のまま

    cutoff = (t0 - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_rollback_pattern(conn, cutoff)
    conn.close()
    assert len(violations) == 1
    assert violations[0]["id"] == str(ai_id)


# --------------------------------------------------------------------------- #
# 4. note 消失クローズ
# --------------------------------------------------------------------------- #
def test_check_missing_close_note_detects_empty_note(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(conn, status="closed", note=None)
    now = datetime.now(UTC).isoformat()
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed", changed_at=now, source="argus_auto",
    )

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_missing_close_note(conn, cutoff)
    conn.close()

    assert len(violations) == 1
    assert violations[0]["id"] == str(ai_id)


def test_check_missing_close_note_no_violation_when_note_present(pm_db_path):
    conn = _open_plain(pm_db_path)
    ai_id = _insert_action_item(
        conn, status="closed", note="2026-07-01 自動クローズ: 完了報告あり",
    )
    now = datetime.now(UTC).isoformat()
    _insert_audit_log(
        conn, table_name="action_items", record_id=ai_id, field="status",
        old_value="open", new_value="closed", changed_at=now, source="argus_auto",
    )

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    violations = pm_selfcheck.check_missing_close_note(conn, cutoff)
    conn.close()
    assert violations == []


# --------------------------------------------------------------------------- #
# 5. 状態キードリフト
# --------------------------------------------------------------------------- #
def test_check_state_key_drift_detects_unknown_key(tmp_path):
    state_db = tmp_path / "patrol_state.db"
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,"
        " target_key TEXT NOT NULL, sent_at TEXT NOT NULL,"
        " channel_id TEXT, message_ts TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications (event_type, target_key, sent_at) VALUES (?, ?, ?)",
        ("totally_unknown_key", "ai:1", datetime.now(UTC).isoformat()),
    )
    conn.execute(
        "INSERT INTO notifications (event_type, target_key, sent_at) VALUES (?, ?, ?)",
        ("overdue_reminder", "ai:2", datetime.now(UTC).isoformat()),
    )
    conn.commit()

    violations = pm_selfcheck.check_state_key_drift(conn)
    conn.close()

    assert any(v["event_type"] == "totally_unknown_key" for v in violations)
    assert not any(v["event_type"] == "overdue_reminder" for v in violations)


def test_check_state_key_drift_allows_historical_keys(tmp_path):
    state_db = tmp_path / "patrol_state.db"
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,"
        " target_key TEXT NOT NULL, sent_at TEXT NOT NULL,"
        " channel_id TEXT, message_ts TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications (event_type, target_key, sent_at) VALUES (?, ?, ?)",
        ("overdue", "ai:1", datetime.now(UTC).isoformat()),
    )
    conn.commit()

    violations = pm_selfcheck.check_state_key_drift(conn)
    conn.close()
    assert violations == []


# --------------------------------------------------------------------------- #
# run_checks 統合
# --------------------------------------------------------------------------- #
def test_run_checks_returns_no_violations_for_clean_db(pm_db_path):
    conn = _open_plain(pm_db_path)
    _insert_action_item(conn, extracted_at="2026-06-01", status="open")
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-07-27")
    conn.close()
    assert violations == []
