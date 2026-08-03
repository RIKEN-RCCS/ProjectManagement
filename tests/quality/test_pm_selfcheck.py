"""pm_selfcheck.py の check_ 関数単位のテスト（一時DB、本番DB不使用）。"""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from quality import pm_selfcheck


@pytest.fixture(autouse=True)
def _isolate_anchor_path(tmp_path, monkeypatch):
    """`pm_selfcheck._DEFAULT_ANCHOR_PATH` を一時パスへ差し替える。

    既定値はリポジトリ実体の `config/anchors/tool_call_anchor.jsonl` を指すため、
    `--anchor-path` を明示しないテスト（および main() 経由のテスト）が実ファイルの
    状態（`--emit-anchor` の運用実行で書かれた行数等）に依存してしまう。特に
    `anchor_consistency` は「台帳の行数がアンカーの rows より少なければ違反」を
    見るため、テストの小さな一時 DB と実ファイルの rows が食い違うと誤検出になる。
    """
    monkeypatch.setattr(
        pm_selfcheck, "_DEFAULT_ANCHOR_PATH", tmp_path / "tool_call_anchor.jsonl",
    )
    yield


def _make_git_fake(*, local_sha=None, remote_sha=None, ls_remote_fails=False,
                    ls_remote_raises=None):
    """rev-parse / ls-remote の呼び出しに応じた偽の subprocess.run を返す。"""

    def _fake_run(args, cwd=None, capture_output=None, text=None, timeout=None):
        if "rev-parse" in args:
            if local_sha is None:
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, returncode=0, stdout=f"{local_sha}\n", stderr="")
        if "ls-remote" in args:
            if ls_remote_raises is not None:
                raise ls_remote_raises
            if ls_remote_fails:
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="fatal: unreachable")
            if remote_sha is None:
                return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args, returncode=0, stdout=f"{remote_sha}\trefs/heads/anchors\n", stderr="",
            )
        raise AssertionError(f"unexpected git invocation: {args}")

    return _fake_run


@pytest.fixture(autouse=True)
def _stub_git_subprocess(monkeypatch):
    """`check_anchor_pushed` が本物の git / ネットワークを叩かないようにする。

    `run_checks()` は無条件に `anchor_pushed` を実行する（他の検査と同じ形に
    そろえるため）。それ以外の検査を見ているテストが実リポジトリ・実
    ネットワークへ副作用を持たないよう、既定では「anchors がローカルにも
    origin にも無い」（判定不能・違反なし）を返す偽の subprocess.run に
    差し替える。`anchor_pushed` 自体を検証するテストは、各テスト内で
    `monkeypatch.setattr(pm_selfcheck.subprocess, "run", _make_git_fake(...))`
    により個別に上書きする。
    """
    monkeypatch.setattr(pm_selfcheck.subprocess, "run", _make_git_fake())
    yield


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
def _insert_tool_call(conn, *, plane, tool_name, ts=None, args_json="{}", outcome="ok"):
    """tool_calls に1行追記する（append-only テーブルなので ts は挿入時に直接指定する）。

    check_tool_call_chain（検査8）を巻き込まないよう、prev_hash/entry_hash は
    record_tool_call() と同じ方法で正しく連鎖させる（ts だけ差し替える）。
    """
    import hashlib
    import uuid

    from db_utils import GENESIS_HASH

    call_id = uuid.uuid4().hex
    ts = ts or datetime.now(UTC).isoformat()
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


# --------------------------------------------------------------------------- #
# 11. モデル pin ドリフト（実ネットワークアクセスは行わない。check_endpoints をモック）
# --------------------------------------------------------------------------- #
def test_check_model_pin_drift_reports_mismatch(monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch",
             "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
    violations = pm_selfcheck.check_model_pin_drift()
    assert len(violations) == 1
    assert violations[0]["check"] == "model_pin_drift"
    assert violations[0]["model"] == "glm-5.2"


def test_check_model_pin_drift_error_is_not_violation_but_logged(monkeypatch, capsys):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "error", "detail": "接続できません"},
        ],
    )
    violations = pm_selfcheck.check_model_pin_drift()
    assert violations == []
    captured = capsys.readouterr()
    assert "glm-5.2" in captured.err
    assert "判定できません" in captured.err


def test_check_model_pin_drift_skip_is_not_violation_but_logged(monkeypatch, capsys):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "bge-m3", "status": "skip", "detail": "endpoint_env が未設定: []"},
        ],
    )
    violations = pm_selfcheck.check_model_pin_drift()
    assert violations == []
    captured = capsys.readouterr()
    assert "bge-m3" in captured.err
    assert "判定できません" in captured.err


def test_check_model_pin_drift_ok_has_no_violations(monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [{"model": "glm-5.2", "status": "ok", "detail": "served=glm-5.2 実在=あり"}],
    )
    assert pm_selfcheck.check_model_pin_drift() == []


def test_run_checks_includes_model_pin_drift(pm_db_path, monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch", "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-08-03")
    conn.close()
    assert any(v["check"] == "model_pin_drift" for v in violations)


def test_run_checks_model_pin_drift_independent_of_logs_dir(pm_db_path, monkeypatch):
    """logs_dir が無効（None）でもこの検査は実行される（silent_control と同じ扱い）。"""
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch", "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-03", logs_dir=None,
    )
    conn.close()
    assert any(v["check"] == "model_pin_drift" for v in violations)


def test_run_checks_skips_model_pin_drift_when_disabled(pm_db_path, monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch", "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-03", model_pin_enabled=False,
    )
    conn.close()
    assert not any(v["check"] == "model_pin_drift" for v in violations)


def test_main_skip_model_pin_flag_skips_the_check(pm_db_path, tmp_path, monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch", "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
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
            "--skip-model-pin",
        ],
    )
    assert rc == 0


def test_main_no_security_checks_also_skips_model_pin_drift(pm_db_path, tmp_path, monkeypatch):
    from utils import model_pin

    monkeypatch.setattr(
        model_pin, "check_endpoints",
        lambda timeout=10: [
            {"model": "glm-5.2", "status": "mismatch", "detail": "served=glm-5.2 実在=なし（0件）"},
        ],
    )
    state_db = tmp_path / "no_such_patrol_state.db"
    rc = _run_main(
        monkeypatch,
        [
            "--db", str(pm_db_path), "--no-encrypt",
            "--state-db", str(state_db),
            "--days", "7",
            "--no-security-checks",
        ],
    )
    assert rc == 0


# --------------------------------------------------------------------------- #
# 12. 宛先粒度（層3）の未知送信の観測（docs/security-architecture.md §4.7）
# --------------------------------------------------------------------------- #
def test_check_egress_dest_unknown_no_violations_when_ledger_is_empty(pm_db_path):
    """台帳が空（＝まだ観測していない）なら判定不能として扱い、報告しない。"""
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.check_egress_dest_unknown(conn)
    conn.close()
    assert violations == []


def test_check_egress_dest_unknown_indeterminate_when_ledger_younger_than_period(pm_db_path):
    conn = _open_plain(pm_db_path)
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral",
        ts=datetime.now(UTC).isoformat(),
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    violations = pm_selfcheck.check_egress_dest_unknown(conn)
    conn.close()
    assert violations == []


def test_check_egress_dest_unknown_reports_count_and_distinct_destinations(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXXX", "chars": 10, "dest_known": false}',
    )
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    # dest_known=true の行は集計対象外
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postMessage", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXXXX", "chars": 10, "dest_known": true}',
    )
    violations = pm_selfcheck.check_egress_dest_unknown(conn)
    conn.close()

    assert len(violations) == 1
    v = violations[0]
    assert v["check"] == "egress_dest_unknown"
    assert v["count"] == 3
    assert v["distinct_destinations"] == 2
    assert "違反ではない" in v["note"]


def test_check_egress_dest_unknown_no_report_when_all_destinations_known(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postMessage", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXXXX", "chars": 10, "dest_known": true}',
    )
    violations = pm_selfcheck.check_egress_dest_unknown(conn)
    conn.close()
    assert violations == []


def test_check_egress_dest_unknown_days_override_applies(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=ts,
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    violations_default = pm_selfcheck.check_egress_dest_unknown(conn)
    violations_override = pm_selfcheck.check_egress_dest_unknown(conn, days_override=14)
    conn.close()
    assert violations_default == []  # 既定 7 日では窓の外
    assert len(violations_override) == 1


def test_check_egress_dest_unknown_without_tool_calls_table_is_noop(pm_db_path):
    conn = _open_plain(pm_db_path)
    conn.execute("DROP TABLE IF EXISTS tool_calls")
    conn.commit()
    violations = pm_selfcheck.check_egress_dest_unknown(conn)
    conn.close()
    assert violations == []


def test_run_checks_includes_egress_dest_unknown(pm_db_path):
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    violations = pm_selfcheck.run_checks(conn, None, days=7, today="2026-08-03")
    conn.close()
    assert any(v["check"] == "egress_dest_unknown" for v in violations)


def test_main_egress_dest_unknown_never_affects_exit_code_even_with_strict(
    pm_db_path, tmp_path, monkeypatch,
):
    """egress_dest_unknown は観測であり、--silence-strict を付けても違反にならない。

    silent_control（他のキー）が別途鳴らないよう、egress_slack/egress_other/
    read_tools のいずれも直近の記録で満たしておく（この検査自体の効果だけを見る）。
    """
    conn = _open_plain(pm_db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    _insert_tool_call(conn, plane="mutate", tool_name="noop:anchor", ts=old_ts)
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_tool_call(
        conn, plane="egress", tool_name="slack:chat_postEphemeral", ts=recent_ts,
        args_json='{"channel": "C0XXXXXXX", "chars": 10, "dest_known": false}',
    )
    _insert_tool_call(conn, plane="egress", tool_name="canvas:post", ts=recent_ts)
    _insert_tool_call(conn, plane="read", tool_name="search_text", ts=recent_ts)
    conn.close()

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
    assert rc == 0


# --------------------------------------------------------------------------- #
# 13. 外部アンカー（emit_anchor / anchor_consistency、docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #
def test_emit_anchor_appends_one_line(pm_db_path, tmp_path):
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")

    anchor_path = tmp_path / "anchor.jsonl"
    record = pm_selfcheck.emit_anchor(conn, anchor_path)
    conn.close()

    assert record is not None
    assert record["rows"] == 1
    lines = [ln for ln in anchor_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


def test_emit_anchor_does_not_duplicate_when_chain_unchanged(pm_db_path, tmp_path):
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")

    anchor_path = tmp_path / "anchor.jsonl"
    first = pm_selfcheck.emit_anchor(conn, anchor_path)
    second = pm_selfcheck.emit_anchor(conn, anchor_path)
    conn.close()

    assert first is not None
    assert second is None
    lines = [ln for ln in anchor_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


def test_emit_anchor_appends_again_when_chain_grows(pm_db_path, tmp_path):
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")
    anchor_path = tmp_path / "anchor.jsonl"
    pm_selfcheck.emit_anchor(conn, anchor_path)

    record_tool_call(conn, session_id="s", seq=2, plane="read", tool_name="b", args={}, outcome="ok")
    second = pm_selfcheck.emit_anchor(conn, anchor_path)
    conn.close()

    assert second is not None
    assert second["rows"] == 2
    lines = [ln for ln in anchor_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


def test_emit_anchor_is_none_when_ledger_empty(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    assert pm_selfcheck.emit_anchor(conn, tmp_path / "anchor.jsonl") is None
    conn.close()


def test_check_anchor_consistency_indeterminate_without_file(pm_db_path, tmp_path, capsys):
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.check_anchor_consistency(conn, tmp_path / "missing.jsonl")
    conn.close()
    assert violations == []
    assert "判定できません" in capsys.readouterr().err


def test_check_anchor_consistency_indeterminate_when_file_empty(pm_db_path, tmp_path, capsys):
    anchor_path = tmp_path / "anchor.jsonl"
    anchor_path.write_text("", encoding="utf-8")
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.check_anchor_consistency(conn, anchor_path)
    conn.close()
    assert violations == []
    assert "判定できません" in capsys.readouterr().err


def test_check_anchor_consistency_no_violation_when_matching(pm_db_path, tmp_path):
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")
    record_tool_call(conn, session_id="s", seq=2, plane="read", tool_name="b", args={}, outcome="ok")
    anchor_path = tmp_path / "anchor.jsonl"
    pm_selfcheck.emit_anchor(conn, anchor_path)

    violations = pm_selfcheck.check_anchor_consistency(conn, anchor_path)
    conn.close()
    assert violations == []


def test_check_anchor_consistency_detects_rewritten_history(pm_db_path, tmp_path):
    """台帳を改変した状態で違反を出すこと。

    append-only トリガがあるので UPDATE では再現できない。テーブルごと
    作り直す（DROP TABLE → 再作成）ことで「新しい DB を作り直す」改竄を再現する。
    """
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")
    record_tool_call(conn, session_id="s", seq=2, plane="read", tool_name="b", args={}, outcome="ok")
    anchor_path = tmp_path / "anchor.jsonl"
    pm_selfcheck.emit_anchor(conn, anchor_path)
    conn.close()

    conn2 = _open_plain(pm_db_path)
    conn2.execute("DROP TABLE tool_calls")
    conn2.commit()
    ensure_tool_calls_table(conn2)
    record_tool_call(conn2, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")
    record_tool_call(
        conn2, session_id="s", seq=2, plane="read", tool_name="tampered", args={}, outcome="ok",
    )

    violations = pm_selfcheck.check_anchor_consistency(conn2, anchor_path)
    conn2.close()

    assert len(violations) == 1
    assert violations[0]["check"] == "anchor_consistency"


def test_check_anchor_consistency_detects_shrunk_ledger(pm_db_path, tmp_path):
    from db_utils import ensure_tool_calls_table, record_tool_call

    conn = _open_plain(pm_db_path)
    ensure_tool_calls_table(conn)
    record_tool_call(conn, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")
    record_tool_call(conn, session_id="s", seq=2, plane="read", tool_name="b", args={}, outcome="ok")
    anchor_path = tmp_path / "anchor.jsonl"
    pm_selfcheck.emit_anchor(conn, anchor_path)
    conn.close()

    conn2 = _open_plain(pm_db_path)
    conn2.execute("DROP TABLE tool_calls")
    conn2.commit()
    ensure_tool_calls_table(conn2)
    record_tool_call(conn2, session_id="s", seq=1, plane="read", tool_name="a", args={}, outcome="ok")

    violations = pm_selfcheck.check_anchor_consistency(conn2, anchor_path)
    conn2.close()

    assert len(violations) == 1
    assert "縮んでいる" in violations[0]["reason"]


def test_run_checks_includes_anchor_consistency_indeterminate_as_no_violation(pm_db_path, tmp_path):
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-03", anchor_path=tmp_path / "missing.jsonl",
    )
    conn.close()
    assert not any(v["check"] == "anchor_consistency" for v in violations)


# --------------------------------------------------------------------------- #
# 14. 承認待ち egress の滞留（pending_egress_stale、docs/security-architecture.md §4.2）
# --------------------------------------------------------------------------- #
def _insert_pending_egress(conn, *, ts, status="pending", target="t"):
    from db_utils import ensure_pending_egress_table

    ensure_pending_egress_table(conn)
    conn.execute(
        "INSERT INTO pending_egress (ts, target, content_sha256, content, chars,"
        " block_reason, status) VALUES (?, ?, 'sha', 'content', 7, 'reason', ?)",
        (ts, target, status),
    )
    conn.commit()


def test_check_pending_egress_stale_detects_old_pending(pm_db_path):
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    conn = _open_plain(pm_db_path)
    _insert_pending_egress(conn, ts=old_ts)
    violations = pm_selfcheck.check_pending_egress_stale(conn)
    conn.close()
    assert len(violations) == 1
    assert violations[0]["check"] == "pending_egress_stale"


def test_check_pending_egress_stale_ignores_recent_pending(pm_db_path):
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    conn = _open_plain(pm_db_path)
    _insert_pending_egress(conn, ts=recent_ts)
    violations = pm_selfcheck.check_pending_egress_stale(conn)
    conn.close()
    assert violations == []


def test_check_pending_egress_stale_ignores_decided_rows(pm_db_path):
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    conn = _open_plain(pm_db_path)
    _insert_pending_egress(conn, ts=old_ts, status="approved")
    violations = pm_selfcheck.check_pending_egress_stale(conn)
    conn.close()
    assert violations == []


def test_check_pending_egress_stale_without_table_is_noop(pm_db_path):
    conn = _open_plain(pm_db_path)
    violations = pm_selfcheck.check_pending_egress_stale(conn)
    conn.close()
    assert violations == []


def test_run_checks_includes_pending_egress_stale(pm_db_path, tmp_path):
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    conn = _open_plain(pm_db_path)
    _insert_pending_egress(conn, ts=old_ts)
    violations = pm_selfcheck.run_checks(
        conn, None, days=7, today="2026-08-03", anchor_path=tmp_path / "missing.jsonl",
    )
    conn.close()
    assert any(v["check"] == "pending_egress_stale" for v in violations)


# --------------------------------------------------------------------------- #
# 15. アンカーの push 状態（anchor_pushed、docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #
# subprocess.run は必ずモンキーパッチし、実際の git コマンドは実行しない
# （本番リポジトリの ref を変更しない）。`_make_git_fake` はファイル先頭で定義済み。
def test_check_anchor_pushed_no_violation_when_refs_match(monkeypatch):
    monkeypatch.setattr(
        pm_selfcheck.subprocess, "run",
        _make_git_fake(local_sha="abc123", remote_sha="abc123"),
    )
    assert pm_selfcheck.check_anchor_pushed() == []


def test_check_anchor_pushed_detects_mismatch(monkeypatch):
    monkeypatch.setattr(
        pm_selfcheck.subprocess, "run",
        _make_git_fake(local_sha="abc123", remote_sha="def456"),
    )
    violations = pm_selfcheck.check_anchor_pushed()
    assert len(violations) == 1
    assert violations[0]["check"] == "anchor_pushed"
    assert violations[0]["local"] == "abc123"
    assert violations[0]["remote"] == "def456"


def test_check_anchor_pushed_indeterminate_when_ls_remote_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        pm_selfcheck.subprocess, "run",
        _make_git_fake(local_sha="abc123", ls_remote_fails=True),
    )
    violations = pm_selfcheck.check_anchor_pushed()
    assert violations == []
    assert "判定できません" in capsys.readouterr().err


def test_check_anchor_pushed_indeterminate_when_ls_remote_times_out(monkeypatch, capsys):
    monkeypatch.setattr(
        pm_selfcheck.subprocess, "run",
        _make_git_fake(
            local_sha="abc123",
            ls_remote_raises=subprocess.TimeoutExpired(cmd="git ls-remote", timeout=10),
        ),
    )
    violations = pm_selfcheck.check_anchor_pushed()
    assert violations == []
    assert "判定できません" in capsys.readouterr().err


def test_check_anchor_pushed_indeterminate_when_neither_side_has_anchors(monkeypatch, capsys):
    monkeypatch.setattr(
        pm_selfcheck.subprocess, "run",
        _make_git_fake(local_sha=None, remote_sha=None),
    )
    violations = pm_selfcheck.check_anchor_pushed()
    assert violations == []
    err = capsys.readouterr().err
    assert "判定できません" in err
    assert "まだ運用が始まっていません" in err
