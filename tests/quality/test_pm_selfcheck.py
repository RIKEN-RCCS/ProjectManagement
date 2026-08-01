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


# --------------------------------------------------------------------------- #
# 6-7. セキュリティ監視（canary_hit / netguard_deny）
# --------------------------------------------------------------------------- #
def _write_log(logs_dir: Path, name: str, body: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def _insert_canary(conn, token: str, kind: str = "hostname") -> None:
    from db_utils import ensure_canary_table

    ensure_canary_table(conn)
    conn.execute(
        "INSERT INTO canary_tokens (token, planted_in, row_ref, planted_at, active, kind, notes)"
        " VALUES (?, 'box_docs', 'doc:1', ?, 1, ?, NULL)",
        (token, datetime.now(UTC).isoformat(), kind),
    )
    conn.commit()


def test_check_netguard_deny_groups_by_destination(tmp_path):
    logs = tmp_path / "logs"
    _write_log(
        logs, "qa.log",
        "[NETGUARD] verdict=deny host=evil.example ip=- port=443"
        " caller=scripts/argus/qa_engine.py:12 stage=resolve\n"
        "[NETGUARD] verdict=deny host=evil.example ip=- port=443"
        " caller=scripts/argus/qa_engine.py:12 stage=resolve\n"
        "[NETGUARD] verdict=allow host=slack.com ip=- port=443 caller=x.py:1 stage=resolve\n"
        "無関係なログ行\n",
    )
    violations = pm_selfcheck.check_netguard_deny(logs, days=7)
    assert len(violations) == 1
    v = violations[0]
    assert v["check"] == "netguard_deny"
    assert v["destination"] == "evil.example"
    assert v["count"] == 2
    assert v["stage"] == "resolve"
    assert "qa.log" in v["log_files"]


def test_check_netguard_deny_uses_ip_when_host_absent(tmp_path):
    logs = tmp_path / "logs"
    _write_log(
        logs, "web.log",
        "[NETGUARD] verdict=deny host=- ip=203.0.113.9 port=80 caller=a.py:2 stage=connect\n",
    )
    violations = pm_selfcheck.check_netguard_deny(logs, days=7)
    assert violations[0]["destination"] == "203.0.113.9"
    assert violations[0]["stage"] == "connect"


def test_check_netguard_deny_ignores_old_logs(tmp_path):
    import os

    logs = tmp_path / "logs"
    path = _write_log(
        logs, "old.log",
        "[NETGUARD] verdict=deny host=evil.example ip=- port=443 caller=a.py:1 stage=resolve\n",
    )
    old = datetime.now(UTC).timestamp() - 30 * 86400
    os.utime(path, (old, old))
    assert pm_selfcheck.check_netguard_deny(logs, days=7) == []


def test_check_netguard_deny_clean_logs(tmp_path):
    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", "[NETGUARD] verdict=allow host=slack.com ip=- port=443 caller=a.py:1 stage=resolve\n")
    assert pm_selfcheck.check_netguard_deny(logs, days=7) == []


def test_check_canary_hits_detects_token_in_log(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    token = "docs-deadbeef.internal-check.invalid"
    _insert_canary(conn, token)

    logs = tmp_path / "logs"
    _write_log(
        logs, "qa.log",
        f"[NETGUARD] verdict=deny host={token} ip=- port=443 caller=a.py:1 stage=resolve\n",
    )
    violations = pm_selfcheck.check_canary_hits(conn, logs, days=7)
    conn.close()

    assert len(violations) == 1
    assert violations[0]["check"] == "canary_hit"
    assert violations[0]["token"] == token
    assert violations[0]["kind"] == "hostname"
    assert violations[0]["count"] == 1


def test_check_canary_hits_ignores_revoked(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    token = "ARGUS-CANARY-abcd1234"
    _insert_canary(conn, token, kind="text")
    conn.execute("UPDATE canary_tokens SET active = 0 WHERE token = ?", (token,))
    conn.commit()

    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", f"leaked {token} somewhere\n")
    violations = pm_selfcheck.check_canary_hits(conn, logs, days=7)
    conn.close()
    assert violations == []


def test_check_canary_hits_without_table_is_noop(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    conn.execute("DROP TABLE IF EXISTS canary_tokens")
    conn.commit()
    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", "何もない\n")
    violations = pm_selfcheck.check_canary_hits(conn, logs, days=7)
    conn.close()
    assert violations == []


def test_run_checks_includes_security_checks_when_logs_dir_given(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    token = "docs-cafebabe.internal-check.invalid"
    _insert_canary(conn, token)
    logs = tmp_path / "logs"
    _write_log(
        logs, "qa.log",
        f"[NETGUARD] verdict=deny host={token} ip=- port=443 caller=a.py:1 stage=resolve\n",
    )
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-07-31", logs_dir=logs)
    conn.close()
    checks = {v["check"] for v in violations}
    assert "canary_hit" in checks
    assert "netguard_deny" in checks


def test_run_checks_skips_security_checks_without_logs_dir(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    _insert_canary(conn, "docs-0badf00d.internal-check.invalid")
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-07-31")
    conn.close()
    assert violations == []


# --------------------------------------------------------------------------- #
# netguard_deny の解消済み申告（ack）
# --------------------------------------------------------------------------- #
def _write_ack(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "netguard_ack.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _deny_line(ts: str, host: str = "localhost", port: int = 50021, stage: str = "resolve") -> str:
    return (
        f"{ts} [ERROR] [NETGUARD] verdict=deny host={host} ip=- port={port}"
        f" caller=scripts/tts/pm_tts.py:111 stage={stage}\n"
    )


def test_ack_suppresses_lines_before_fixed_at(tmp_path):
    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", _deny_line("2026-08-01 01:22:15"))
    acks = pm_selfcheck.load_netguard_ack(
        _write_ack(tmp_path, """
resolved:
  - destination: "localhost"
    port: 50021
    stage: "resolve"
    fixed_at: "2026-08-01 01:30:00"
    reason: "テスト"
""")
    )
    assert pm_selfcheck.check_netguard_deny(logs, days=7, acks=acks) == []


def test_ack_does_not_suppress_recurrence_after_fixed_at(tmp_path):
    """修正後に同じ宛先が再発したら、ack があっても必ず報告される。"""
    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", _deny_line("2026-08-01 09:00:00"))
    acks = pm_selfcheck.load_netguard_ack(
        _write_ack(tmp_path, """
resolved:
  - destination: "localhost"
    port: 50021
    fixed_at: "2026-08-01 01:30:00"
    reason: "テスト"
""")
    )
    violations = pm_selfcheck.check_netguard_deny(logs, days=7, acks=acks)
    assert len(violations) == 1
    assert violations[0]["destination"] == "localhost"


def test_ack_does_not_suppress_lines_without_timestamp(tmp_path):
    """タイムスタンプが読めない行は抑制しない（判断材料が無いので保守的に報告）。"""
    logs = tmp_path / "logs"
    _write_log(
        logs, "box.log",
        "[NETGUARD] verdict=deny host=localhost ip=- port=50021 caller=a.py:1 stage=resolve\n",
    )
    acks = pm_selfcheck.load_netguard_ack(
        _write_ack(tmp_path, """
resolved:
  - destination: "localhost"
    port: 50021
    fixed_at: "2026-08-01 01:30:00"
    reason: "テスト"
""")
    )
    assert len(pm_selfcheck.check_netguard_deny(logs, days=7, acks=acks)) == 1


def test_ack_port_mismatch_is_not_suppressed(tmp_path):
    logs = tmp_path / "logs"
    _write_log(logs, "qa.log", _deny_line("2026-08-01 01:22:15", port=9999))
    acks = pm_selfcheck.load_netguard_ack(
        _write_ack(tmp_path, """
resolved:
  - destination: "localhost"
    port: 50021
    fixed_at: "2026-08-01 01:30:00"
    reason: "テスト"
""")
    )
    assert len(pm_selfcheck.check_netguard_deny(logs, days=7, acks=acks)) == 1


def test_load_netguard_ack_missing_file_is_empty(tmp_path):
    assert pm_selfcheck.load_netguard_ack(tmp_path / "nope.yaml") == []


def test_run_checks_uses_security_days_for_log_scan(pm_db_path, tmp_path):
    """データ検査は --days、ログ走査は --security-days の窓を使う。"""
    import os

    logs = tmp_path / "logs"
    # 宛先はリポジトリの config/netguard_ack.yaml に載っていないものを使う
    # （既定の ack が読まれるため、載っている宛先だと抑制されてしまう）
    path = _write_log(
        logs, "qa.log", _deny_line("2026-07-28 01:00:00", host="evil.example", port=443)
    )
    old = datetime.now(UTC).timestamp() - 3 * 86400
    os.utime(path, (old, old))

    conn = _open_plain(pm_db_path)
    v_wide = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-01", logs_dir=logs, security_days=7
    )
    v_narrow = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-01", logs_dir=logs, security_days=1
    )
    conn.close()
    assert any(v["check"] == "netguard_deny" for v in v_wide)
    assert not any(v["check"] == "netguard_deny" for v in v_narrow)


# --------------------------------------------------------------------------- #
# 10. 沈黙している制御の検出
# --------------------------------------------------------------------------- #
def _insert_tool_call(conn, *, plane, tool_name, ts=None):
    """tool_calls に1行追記する（append-only テーブルなので ts は挿入時に直接指定する）。

    check_tool_call_chain（検査8）を巻き込まないよう、prev_hash/entry_hash は
    record_tool_call() と同じ方法で正しく連鎖させる（ts だけ差し替える）。
    """
    import hashlib
    import uuid

    from db_utils import GENESIS_HASH

    call_id = uuid.uuid4().hex
    ts = ts or datetime.now(UTC).isoformat()
    args_json = "{}"
    outcome = "ok"
    last = conn.execute(
        "SELECT entry_hash FROM tool_calls ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    prev_hash = last["entry_hash"] if last else GENESIS_HASH
    payload = f"{prev_hash}{call_id}{ts}{tool_name}{args_json}{outcome}"
    entry_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO tool_calls (call_id, session_id, seq, ts, plane, tool_name, args_json,"
        " model, model_revision, outcome, prev_hash, entry_hash)"
        " VALUES (?, 'test', 0, ?, ?, ?, ?, '', '', ?, ?, ?)",
        (call_id, ts, plane, tool_name, args_json, outcome, prev_hash, entry_hash),
    )
    conn.commit()
    return {"call_id": call_id}


def test_check_silent_control_no_violations_when_ledger_is_empty(pm_db_path):
    """tool_calls が空（＝台帳自体が未観測）なら、どのキーも違反として報告しない。

    「30日間記録が無い」が「制御の沈黙」なのか「台帳がまだ30日分存在しない」なのかは
    区別できないため、台帳が空の場合は判定不能として扱い違反リストに入れない。
    """
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()
    assert violations == []


def test_check_silent_control_indeterminate_when_ledger_younger_than_period(pm_db_path):
    """台帳の最古の記録が観測期間の開始より新しければ、そのキーは判定不能（違反にしない）。"""
    conn = _open_plain(pm_db_path)
    # 台帳の唯一の行（＝最古の行でもある）が「今」なので、egress_other(30日) /
    # read_tools(7日) はどちらも観測期間分の履歴が無く判定不能になる。
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postMessage",
        ts=datetime.now(UTC).isoformat(),
    )
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()

    assert violations == []


def test_check_silent_control_reports_all_keys_when_ledger_old_enough_and_silent(pm_db_path):
    """台帳が十分古く、かつ観測期間内に記録が無ければ従来どおり沈黙として報告する。"""
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    # plane='mutate' はどのキーの where 句にも一致しない。台帳の年齢だけを作るための行。
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()

    keys = {v["key"] for v in violations}
    assert keys == {"egress_slack", "egress_other", "read_tools"}
    for v in violations:
        assert v["check"] == "silent_control"
        assert "expected_within_days" in v
        assert "区別できない" in v["note"]


def test_check_silent_control_not_reported_when_recent_slack_egress_exists(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    # 台帳の年齢を作ってから（十分古くしてから）検証する。
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postMessage",
        ts=datetime.now(UTC).isoformat(),
    )
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()

    keys = {v["key"] for v in violations}
    assert "egress_slack" not in keys
    assert "egress_other" in keys
    assert "read_tools" in keys


def test_check_silent_control_reports_when_only_old_rows_exist(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="egress", tool_name="slack:chat_postMessage", ts=old_ts)
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()

    keys = {v["key"] for v in violations}
    assert "egress_slack" in keys


def test_check_silent_control_days_override_applies_to_all_keys(pm_db_path):
    conn = _open_plain(pm_db_path)
    ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    _insert_tool_call(conn, plane="egress", tool_name="slack:chat_postMessage", ts=ts)
    # 既定の egress_slack=7日では沈黙扱いだが、--silence-days 14 なら検出されない
    violations_default = pm_selfcheck.check_silent_control(conn)
    violations_override = pm_selfcheck.check_silent_control(conn, days_override=14)
    conn.close()

    assert "egress_slack" in {v["key"] for v in violations_default}
    assert "egress_slack" not in {v["key"] for v in violations_override}


def test_check_silent_control_without_tool_calls_table_is_noop(pm_db_path):
    conn = _open_plain(pm_db_path)
    conn.execute("DROP TABLE IF EXISTS tool_calls")
    conn.commit()
    violations = pm_selfcheck.check_silent_control(conn)
    conn.close()
    assert violations == []


def test_run_checks_includes_silent_control_when_logs_dir_given(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    # 台帳の年齢を作らないと全キー判定不能になり silent_control が1件も出ない。
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    logs = tmp_path / "logs"
    logs.mkdir()
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-08-01", logs_dir=logs)
    conn.close()
    assert any(v["check"] == "silent_control" for v in violations)


def test_run_checks_skips_silent_control_without_logs_dir(pm_db_path):
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-08-01")
    conn.close()
    assert not any(v["check"] == "silent_control" for v in violations)


def _run_main(monkeypatch, argv):
    import sys

    monkeypatch.setattr(sys, "argv", ["pm_selfcheck.py", *argv])
    return pm_selfcheck.main()


def _seed_old_anchor_tool_call(db_path: Path) -> None:
    """台帳の年齢を作るための古い行を1件入れる（main() は自前で接続するため直接書き込む）。"""
    conn = _open_plain(db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    conn.close()


def test_main_silent_control_is_warning_only_by_default(pm_db_path, tmp_path, monkeypatch):
    _seed_old_anchor_tool_call(pm_db_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    state_db = tmp_path / "no_such_patrol_state.db"
    rc = _run_main(
        monkeypatch,
        [
            "--db", str(pm_db_path), "--no-encrypt",
            "--state-db", str(state_db),
            "--logs-dir", str(logs),
            "--days", "7",
        ],
    )
    assert rc == 0


def test_main_silent_control_strict_fails_exit_code(pm_db_path, tmp_path, monkeypatch):
    _seed_old_anchor_tool_call(pm_db_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    state_db = tmp_path / "no_such_patrol_state.db"
    rc = _run_main(
        monkeypatch,
        [
            "--db", str(pm_db_path), "--no-encrypt",
            "--state-db", str(state_db),
            "--logs-dir", str(logs),
            "--days", "7",
            "--silence-strict",
        ],
    )
    assert rc == 1
