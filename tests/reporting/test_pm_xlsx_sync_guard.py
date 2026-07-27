"""pm_xlsx_sync.py の鮮度ガード（シートより新しい pm.db 変更を巻き戻さない）の純テスト。

背景: Box XLSX エクスポート後に patrol 等が pm.db を先に更新すると、古いシートを
読んだ xlsx_sync がその変更を巻き戻してしまう事故が発生した。audit_log の
(record_id, field) 単位の最新変更時刻とシート基準時刻（ワークブック作成日時 →
Box modified_at → ローカル mtime の優先順）を比較し、シートより新しい変更がある
フィールドは同期をスキップするガードを検証する。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from reporting import pm_xlsx_sync as sync

_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,
    source     TEXT
)"""

BASE_TS = datetime(2026, 7, 27, 16, 45, 0, tzinfo=UTC)


def _open(pm_db_path):
    conn = sqlite3.connect(pm_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_AUDIT_LOG_DDL)
    conn.commit()
    return conn


def _insert_ai(conn, content="content", assignee="foo", status="open", note=None) -> int:
    cur = conn.execute(
        "INSERT INTO action_items (content, assignee, status, note) VALUES (?, ?, ?, ?)",
        (content, assignee, status, note),
    )
    conn.commit()
    return cur.lastrowid


def _insert_audit(
    conn, table_name: str, record_id: int, changed_at: str, source: str | None,
    field: str = "status", old_value: str | None = "open", new_value: str | None = "closed",
):
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (table_name, str(record_id), field, old_value, new_value, changed_at, source),
    )
    conn.commit()


def _noop_log(msg):
    pass


# --------------------------------------------------------------------------- #
# ガード発動: pm.db 側の変更がシート基準より新しい
# --------------------------------------------------------------------------- #

def test_newer_pmdb_change_blocks_sync(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed", note="2026-07-27 自動クローズ: 根拠")
    # patrol が 17:00 に自動クローズ（シート基準16:45 より新しい）。
    # close_action_item は status と note の両方を audit_log に記録する
    # （_append_close_note が note の audit も書くようになったため、fieldスコープの
    #  ガードが両方を検知できる）。
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T17:00:00+00:00", "argus_auto",
                  field="status")
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T17:00:00+00:00", "argus_auto",
                  field="note", old_value=None, new_value="2026-07-27 自動クローズ: 根拠")

    # シートは 16:45 時点の値（open のまま）
    rows = {ai_id: {"status": "open", "note": None}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 0
    assert items == 0
    assert skipped == 1
    row = conn.execute("SELECT status, note FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "closed"
    assert row["note"] == "2026-07-27 自動クローズ: 根拠"
    conn.close()


# --------------------------------------------------------------------------- #
# フィールド単位ガード: 無関係なフィールド（note）の新しい変更は
# 別フィールド（assignee）の人手編集を巻き戻さない
# --------------------------------------------------------------------------- #

def test_guard_is_field_scoped_unrelated_field_change_does_not_block(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, assignee="旧担当者", note="旧メモ")
    # patrol が note だけを新しく更新（シート基準より新しい）
    _insert_audit(
        conn, "action_items", ai_id, "2026-07-27T17:00:00+00:00", "argus_auto",
        field="note", old_value="旧メモ", new_value="新メモ",
    )

    # シート側は assignee を人手編集（note は変更していない）
    rows = {ai_id: {"assignee": "新担当者"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 1
    assert items == 1
    assert skipped == 0
    row = conn.execute("SELECT assignee FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["assignee"] == "新担当者"
    conn.close()


def test_guard_is_field_scoped_blocks_only_conflicting_field(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, assignee="旧担当者", status="closed")
    # status だけ patrol が新しく変更
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T17:00:00+00:00", "argus_auto",
                  field="status", old_value="open", new_value="closed")

    # シートは status(open) と assignee(新担当者) を両方編集しようとする
    rows = {ai_id: {"status": "open", "assignee": "新担当者"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    # status は破棄、assignee は反映
    assert changes == 1
    assert skipped == 1
    row = conn.execute("SELECT status, assignee FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "closed"
    assert row["assignee"] == "新担当者"
    conn.close()


# --------------------------------------------------------------------------- #
# ガード非発動: pm.db 側の変更がシート基準より古い
# --------------------------------------------------------------------------- #

def test_older_pmdb_change_allows_sync(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    # 変更はシート基準(16:45)より前(10:00)
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T10:00:00+00:00", "argus_auto")

    rows = {ai_id: {"status": "open"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 1
    assert items == 1
    assert skipped == 0
    row = conn.execute("SELECT status FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "open"
    conn.close()


# --------------------------------------------------------------------------- #
# 基準時刻取得失敗 → 全行同期 + warning（従来挙動へ退化）
# --------------------------------------------------------------------------- #

def test_base_time_none_disables_guard(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T23:00:00+00:00", "argus_auto")

    rows = {ai_id: {"status": "open"}}
    # base_time=None は「シート基準時刻取得失敗」を表す
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=None)

    assert changes == 1
    assert items == 1
    assert skipped == 0
    row = conn.execute("SELECT status FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "open"
    conn.close()


# --------------------------------------------------------------------------- #
# --force はガードを無視する
# --------------------------------------------------------------------------- #

def test_force_flag_ignores_guard_via_none_base_time(pm_db_path):
    """--force は呼び出し側で base_time=None を渡すことでガードを無効化する。"""
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T23:00:00+00:00", "argus_auto")

    rows = {ai_id: {"status": "open"}}
    # --force 相当: base_time=None
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=None)

    assert changes == 1
    assert skipped == 0
    conn.close()


# --------------------------------------------------------------------------- #
# xlsx_sync 由来の変更しかない行はガード対象外
# --------------------------------------------------------------------------- #

def test_only_xlsx_sync_audit_is_not_guarded(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    # 自分自身の過去同期による変更のみ（新しい時刻でも対象外）
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T23:00:00+00:00", "xlsx_sync")

    rows = {ai_id: {"status": "open"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 1
    assert skipped == 0
    row = conn.execute("SELECT status FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "open"
    conn.close()


# --------------------------------------------------------------------------- #
# NULL source は xlsx_sync 除外の対象にならない（ガード対象になる）
# --------------------------------------------------------------------------- #

def test_null_source_audit_is_still_guarded(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T23:00:00+00:00", None)

    rows = {ai_id: {"status": "open"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 0
    assert skipped == 1
    conn.close()


# --------------------------------------------------------------------------- #
# audit_log の changed_at がパース不能 → 安全側でスキップ
# --------------------------------------------------------------------------- #

def test_unparsable_changed_at_blocks_sync_safely(pm_db_path):
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    _insert_audit(conn, "action_items", ai_id, "not-a-timestamp", "argus_auto")

    rows = {ai_id: {"status": "open"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 0
    assert skipped == 1
    row = conn.execute("SELECT status FROM action_items WHERE id = ?", (ai_id,)).fetchone()
    assert row["status"] == "closed"
    conn.close()


def test_unparsable_candidate_among_multiple_forces_safe_side(pm_db_path):
    """同じ (record_id, field) に複数の audit_log 行があり、うち1件がパース不能な場合、
    最大値が取れても安全側（パース不能扱い）を優先する。"""
    conn = _open(pm_db_path)
    ai_id = _insert_ai(conn, status="closed")
    _insert_audit(conn, "action_items", ai_id, "2026-07-27T10:00:00+00:00", "argus_auto")
    _insert_audit(conn, "action_items", ai_id, "garbage-timestamp", "argus_auto")

    rows = {ai_id: {"status": "open"}}
    changes, items, skipped = sync._apply_action_items(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 0
    assert skipped == 1
    conn.close()


# --------------------------------------------------------------------------- #
# decisions 側も同様にガードされる（実際に差分が発生し、ブロック経路を踏む）
# --------------------------------------------------------------------------- #

def test_decisions_guard_blocks_newer_change(pm_db_path):
    conn = _open(pm_db_path)
    cur = conn.execute(
        "INSERT INTO decisions (content, acknowledged_at) VALUES (?, ?)",
        ("決定内容(旧)", None),
    )
    conn.commit()
    dec_id = cur.lastrowid
    _insert_audit(
        conn, "decisions", dec_id, "2026-07-27T17:00:00+00:00", "web_ui",
        field="content", old_value="決定内容(旧)", new_value="決定内容(Web編集済み)",
    )

    rows = {dec_id: {"content": "決定内容(シート編集)"}}
    changes, items, skipped = sync._apply_decisions(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 0
    assert items == 0
    assert skipped == 1
    row = conn.execute("SELECT content FROM decisions WHERE id = ?", (dec_id,)).fetchone()
    assert row["content"] == "決定内容(旧)"
    conn.close()


def test_decisions_guard_allows_older_change(pm_db_path):
    conn = _open(pm_db_path)
    cur = conn.execute(
        "INSERT INTO decisions (content, acknowledged_at) VALUES (?, ?)",
        ("決定内容(旧)", None),
    )
    conn.commit()
    dec_id = cur.lastrowid
    _insert_audit(
        conn, "decisions", dec_id, "2026-07-27T10:00:00+00:00", "web_ui",
        field="content", old_value="X", new_value="決定内容(旧)",
    )

    rows = {dec_id: {"content": "決定内容(シート編集)"}}
    changes, items, skipped = sync._apply_decisions(conn, rows, dry_run=False, log=_noop_log, base_time=BASE_TS)

    assert changes == 1
    assert items == 1
    assert skipped == 0
    row = conn.execute("SELECT content FROM decisions WHERE id = ?", (dec_id,)).fetchone()
    assert row["content"] == "決定内容(シート編集)"
    conn.close()


# --------------------------------------------------------------------------- #
# _process_xlsx: 基準時刻の優先順（ワークブック作成日時 → Box modified_at →
# ローカル mtime）とログ文言
# --------------------------------------------------------------------------- #

class _FakeCell:
    def __init__(self, value):
        self.value = value


class _FakeWorksheet:
    def __init__(self, header: list[str]):
        self._header = [_FakeCell(h) for h in header]

    def __getitem__(self, idx):
        if idx == 1:
            return self._header
        raise NotImplementedError

    def iter_rows(self, min_row=2, values_only=True):
        return iter([])


class _FakeProperties:
    def __init__(self, created):
        self.created = created


class _FakeWorkbook:
    def __init__(self, created):
        self.properties = _FakeProperties(created)
        self.sheetnames = [sync.SHEET_AI, sync.SHEET_DEC]
        self._sheets = {
            sync.SHEET_AI: _FakeWorksheet(
                ["id", "内容", "担当者", "期限", "MS", "状況", "対応状況", "削除"]),
            sync.SHEET_DEC: _FakeWorksheet(["id", "内容", "決定日", "確認済み", "削除"]),
        }

    def __getitem__(self, name):
        return self._sheets[name]


class _Args:
    def __init__(self, force=False, dry_run=False, no_encrypt=True):
        self.force = force
        self.dry_run = dry_run
        self.no_encrypt = no_encrypt


def _fake_load_workbook(created):
    def _loader(path, data_only=True):
        return _FakeWorkbook(created)
    return _loader


def test_process_xlsx_prefers_workbook_created_over_fallback(pm_db_path, monkeypatch):
    conn = _open(pm_db_path)
    conn.close()

    created = datetime(2026, 7, 27, 16, 45, 0)  # naive（openpyxl の想定通り UTC 扱い）
    fallback_ts = datetime(2026, 7, 27, 20, 0, 0, tzinfo=UTC)  # created より後だが優先されないはず
    monkeypatch.setattr(sync, "load_workbook", _fake_load_workbook(created))

    logs: list[str] = []
    sync._process_xlsx(
        "dummy.xlsx", pm_db_path, _Args(), logs.append,
        fallback_ts=fallback_ts, fallback_label="Box modified_at",
    )

    assert any("ワークブック作成日時" in m and "2026-07-27T16:45:00" in m for m in logs)


def test_process_xlsx_falls_back_to_box_modified_at_when_created_missing(pm_db_path, monkeypatch):
    conn = _open(pm_db_path)
    conn.close()

    fallback_ts = datetime(2026, 7, 27, 18, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(sync, "load_workbook", _fake_load_workbook(None))

    logs: list[str] = []
    sync._process_xlsx(
        "dummy.xlsx", pm_db_path, _Args(), logs.append,
        fallback_ts=fallback_ts, fallback_label="Box modified_at",
    )

    assert any("Box modified_at" in m and "2026-07-27T18:00:00" in m for m in logs)


def test_process_xlsx_warns_when_no_basis_available(pm_db_path, monkeypatch):
    conn = _open(pm_db_path)
    conn.close()

    monkeypatch.setattr(sync, "load_workbook", _fake_load_workbook(None))

    logs: list[str] = []
    sync._process_xlsx(
        "dummy.xlsx", pm_db_path, _Args(), logs.append,
        fallback_ts=None, fallback_label="",
    )

    assert any("鮮度ガード無効で実行" in m for m in logs)


def test_process_xlsx_force_skips_guard_message(pm_db_path, monkeypatch):
    conn = _open(pm_db_path)
    conn.close()

    monkeypatch.setattr(sync, "load_workbook", _fake_load_workbook(BASE_TS))

    logs: list[str] = []
    sync._process_xlsx(
        "dummy.xlsx", pm_db_path, _Args(force=True), logs.append,
        fallback_ts=None, fallback_label="",
    )

    assert any("--force 指定のため鮮度ガードを無視して同期します" in m for m in logs)


# --------------------------------------------------------------------------- #
# _parse_iso_dt / _ensure_aware_utc の単体テスト
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expect_none", [
    ("2026-07-27T16:45:00+00:00", False),
    ("2026-07-27T16:45:00Z", False),
    ("2026-07-27T16:45:00", False),
    ("garbage", True),
    (None, True),
    ("", True),
])
def test_parse_iso_dt(raw, expect_none):
    dt = sync._parse_iso_dt(raw)
    if expect_none:
        assert dt is None
    else:
        assert dt is not None
        assert dt.tzinfo is not None


def test_ensure_aware_utc_treats_naive_as_utc():
    naive = datetime(2026, 7, 27, 12, 0, 0)
    aware = sync._ensure_aware_utc(naive)
    assert aware.tzinfo is not None
    assert aware.hour == 12


# --------------------------------------------------------------------------- #
# _fetch_latest_audit / _guard_blocked の単体テスト
# --------------------------------------------------------------------------- #

def test_fetch_latest_audit_groups_by_record_and_field(pm_db_path):
    conn = _open(pm_db_path)
    _insert_audit(conn, "action_items", 1, "2026-07-27T10:00:00+00:00", "argus_auto",
                  field="status")
    _insert_audit(conn, "action_items", 1, "2026-07-27T12:00:00+00:00", "argus_auto",
                  field="note")
    latest = sync._fetch_latest_audit(conn, "action_items", [1])
    assert set(latest.keys()) == {(1, "status"), (1, "note")}
    conn.close()


def test_fetch_latest_audit_excludes_xlsx_sync(pm_db_path):
    conn = _open(pm_db_path)
    _insert_audit(conn, "action_items", 1, "2026-07-27T23:00:00+00:00", "xlsx_sync")
    latest = sync._fetch_latest_audit(conn, "action_items", [1])
    assert latest == {}
    conn.close()


def test_fetch_latest_audit_includes_null_source(pm_db_path):
    conn = _open(pm_db_path)
    _insert_audit(conn, "action_items", 1, "2026-07-27T23:00:00+00:00", None)
    latest = sync._fetch_latest_audit(conn, "action_items", [1])
    assert (1, "status") in latest
    conn.close()


def test_guard_blocked_true_when_newer():
    latest = {(1, "status"): (
        sync._parse_iso_dt("2026-07-27T17:00:00+00:00"), "2026-07-27T17:00:00+00:00", "argus_auto")}
    assert sync._guard_blocked(latest, 1, "status", BASE_TS, "AI", _noop_log) is True


def test_guard_blocked_false_when_older():
    latest = {(1, "status"): (
        sync._parse_iso_dt("2026-07-27T10:00:00+00:00"), "2026-07-27T10:00:00+00:00", "argus_auto")}
    assert sync._guard_blocked(latest, 1, "status", BASE_TS, "AI", _noop_log) is False


def test_guard_blocked_false_when_no_audit():
    assert sync._guard_blocked({}, 1, "status", BASE_TS, "AI", _noop_log) is False


def test_guard_blocked_false_when_different_field():
    latest = {(1, "note"): (
        sync._parse_iso_dt("2026-07-27T23:00:00+00:00"), "2026-07-27T23:00:00+00:00", "argus_auto")}
    assert sync._guard_blocked(latest, 1, "status", BASE_TS, "AI", _noop_log) is False


def test_guard_blocked_true_when_unparsable():
    latest = {(1, "status"): (None, "not-a-timestamp", "argus_auto")}
    assert sync._guard_blocked(latest, 1, "status", BASE_TS, "AI", _noop_log) is True


def test_guard_blocked_message_mentions_force_and_no_reexport_wording():
    """「最新シートで再実行」ではなく --force による意図的上書きを案内する。"""
    messages: list[str] = []
    latest = {(1, "status"): (
        sync._parse_iso_dt("2026-07-27T17:00:00+00:00"), "2026-07-27T17:00:00+00:00", "argus_auto")}
    sync._guard_blocked(latest, 1, "status", BASE_TS, "AI", messages.append)
    assert any("--force" in m for m in messages)
    assert not any("最新シートで再実行" in m for m in messages)
