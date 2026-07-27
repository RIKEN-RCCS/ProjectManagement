#!/usr/bin/env python3
"""
pm_selfcheck.py

pm.db / patrol_state.db のデータ不変条件を検査する読み取り専用スクリプト。
LOG.md に記録された以下のバグクラスの再発を検出する:

  1. future_dates          — action_items / decisions の extracted_at が未来日付
                             （クラス e、AI #2583 型）
  2. date_reversal_close   — argus_auto が自動クローズした際、note に記録された
                             根拠日付がすべて extracted_at より前（クラス e、AI #3056 型）
  3. rollback_pattern      — argus_auto のクローズが 24 時間以内に xlsx_sync に
                             よって open へ巻き戻され、かつ未解消（xlsx_sync 以外の
                             ソースで再クローズされていない）のもの（クラス f）
  4. missing_close_note    — argus_auto でクローズされたが note に自動クローズの
                             根拠が残っていない
  5. state_key_drift       — patrol_state.db notifications.event_type が
                             既知のイベントキー集合の外にある（クラス c の運用時監視）

書き込みは一切行わない。値（channel/user ID 等）はログに出力しない
（state_key_drift は event_type のキー名のみを出力する）。

Usage:
    python3 scripts/quality/pm_selfcheck.py
    python3 scripts/quality/pm_selfcheck.py --days 7
    python3 scripts/quality/pm_selfcheck.py --json
    python3 scripts/quality/pm_selfcheck.py --db data/pm.db --state-db data/patrol_state.db
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from cli_utils import add_db_arg, add_no_encrypt_arg, resolve_db_path
from db_utils import open_db

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# patrol_state.db notifications.event_type の歴史的キー
# （現行ソースには存在しないが過去に記録されたことがあるキー）
_HISTORICAL_EVENT_TYPES = {"overdue", "stale"}


# --------------------------------------------------------------------------- #
# 1. 未来日付
# --------------------------------------------------------------------------- #
def check_future_dates(conn: sqlite3.Connection, today: str) -> list[dict]:
    violations = []
    for table in ("action_items", "decisions"):
        rows = conn.execute(
            f"SELECT id, extracted_at FROM {table}"
            " WHERE extracted_at IS NOT NULL AND (deleted IS NULL OR deleted = 0)"
        ).fetchall()
        for row in rows:
            extracted_at = row["extracted_at"]
            date_part = (extracted_at or "")[:10]
            if _DATE_RE.fullmatch(date_part) and date_part > today:
                violations.append(
                    {
                        "check": "future_dates",
                        "table": table,
                        "id": str(row["id"]),
                        "extracted_at": extracted_at,
                    }
                )
    return violations


# --------------------------------------------------------------------------- #
# 2. 日付逆転クローズ
# --------------------------------------------------------------------------- #
def check_date_reversal_closes(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    violations = []
    status_rows = conn.execute(
        "SELECT record_id, changed_at FROM audit_log"
        " WHERE table_name = 'action_items' AND field = 'status'"
        " AND new_value = 'closed' AND source = 'argus_auto' AND changed_at >= ?",
        (cutoff,),
    ).fetchall()

    for row in status_rows:
        rid, closed_at = row["record_id"], row["changed_at"]
        note_row = conn.execute(
            "SELECT old_value, new_value FROM audit_log"
            " WHERE table_name = 'action_items' AND field = 'note' AND record_id = ?"
            " AND source = 'argus_auto' AND changed_at >= ?"
            " ORDER BY changed_at DESC LIMIT 1",
            (rid, cutoff),
        ).fetchone()
        if not note_row:
            continue

        old_value = note_row["old_value"] or ""
        new_value = note_row["new_value"] or ""
        entry = new_value[len(old_value):].lstrip("\n") if new_value.startswith(old_value) else new_value
        m = re.match(r"^\d{4}-\d{2}-\d{2}\s+自動クローズ:\s*(.*)$", entry, re.S)
        evidence_text = m.group(1) if m else entry
        cited_dates = _DATE_RE.findall(evidence_text)
        if not cited_dates:
            continue

        ai_row = conn.execute(
            "SELECT extracted_at FROM action_items WHERE id = ?", (rid,)
        ).fetchone()
        if not ai_row or not ai_row["extracted_at"]:
            continue
        extracted_date = ai_row["extracted_at"][:10]
        if not _DATE_RE.fullmatch(extracted_date):
            continue

        if all(d < extracted_date for d in cited_dates):
            violations.append(
                {
                    "check": "date_reversal_close",
                    "id": str(rid),
                    "closed_at": closed_at,
                    "extracted_at": extracted_date,
                    "cited_dates": sorted(set(cited_dates)),
                }
            )
    return violations


# --------------------------------------------------------------------------- #
# 3. 巻き戻りパターン
# --------------------------------------------------------------------------- #
def check_rollback_pattern(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    """argus_auto クローズが 24 時間以内に xlsx_sync で open へ巻き戻されたペアを検出する。

    ただし、巻き戻し後に xlsx_sync 以外のソース（manual_restore・
    restore_autoclose_20260724 等）で再度 status='closed' に戻された記録が
    あれば「解消済み」として除外し、未解消の巻き戻りのみを違反として報告する。
    """
    violations = []
    auto_closes = conn.execute(
        "SELECT table_name, record_id, changed_at FROM audit_log"
        " WHERE field = 'status' AND old_value = 'open' AND new_value = 'closed'"
        " AND source = 'argus_auto' AND changed_at >= ?",
        (cutoff,),
    ).fetchall()

    for row in auto_closes:
        table, rid, closed_at = row["table_name"], row["record_id"], row["changed_at"]
        revert = conn.execute(
            "SELECT changed_at FROM audit_log"
            " WHERE table_name = ? AND record_id = ? AND field = 'status'"
            " AND old_value = 'closed' AND new_value = 'open' AND source = 'xlsx_sync'"
            " AND changed_at > ? ORDER BY changed_at ASC LIMIT 1",
            (table, rid, closed_at),
        ).fetchone()
        if not revert:
            continue
        try:
            dt_closed = datetime.fromisoformat(closed_at)
            dt_reverted = datetime.fromisoformat(revert["changed_at"])
        except ValueError:
            continue
        if dt_reverted - dt_closed > timedelta(hours=24):
            continue

        restored = conn.execute(
            "SELECT changed_at FROM audit_log"
            " WHERE table_name = ? AND record_id = ? AND field = 'status'"
            " AND new_value = 'closed' AND source != 'xlsx_sync'"
            " AND changed_at > ? ORDER BY changed_at ASC LIMIT 1",
            (table, rid, revert["changed_at"]),
        ).fetchone()
        if restored:
            continue  # 解消済み（後で再クローズされている）

        violations.append(
            {
                "check": "rollback_pattern",
                "table": table,
                "id": str(rid),
                "closed_at": closed_at,
                "reverted_at": revert["changed_at"],
            }
        )
    return violations


# --------------------------------------------------------------------------- #
# 4. note 消失クローズ
# --------------------------------------------------------------------------- #
def check_missing_close_note(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    violations = []
    rows = conn.execute(
        "SELECT DISTINCT record_id FROM audit_log"
        " WHERE table_name = 'action_items' AND field = 'status'"
        " AND new_value = 'closed' AND source = 'argus_auto' AND changed_at >= ?",
        (cutoff,),
    ).fetchall()

    for row in rows:
        rid = row["record_id"]
        ai = conn.execute(
            "SELECT status, note FROM action_items WHERE id = ?", (rid,)
        ).fetchone()
        if not ai or ai["status"] != "closed":
            continue
        note = ai["note"] or ""
        if not note.strip() or "自動クローズ" not in note:
            violations.append({"check": "missing_close_note", "id": str(rid)})
    return violations


# --------------------------------------------------------------------------- #
# 5. 状態キードリフト
# --------------------------------------------------------------------------- #
def _collect_known_event_types() -> set[str]:
    """patrol/ ソースから already_notified / record_notification のキーを収集する。"""
    patrol_dir = SCRIPTS_DIR / "argus" / "patrol"
    already_re = re.compile(r'already_notified\(\s*"([^"]+)"')
    record_re = re.compile(r'record_notification\(\s*"([^"]+)"')
    variable_re = re.compile(r'record_notification\(\s*\n?\s*([A-Za-z_][A-Za-z0-9_]*)\s*,')

    keys: set[str] = set()
    has_variable_call = False
    for p in sorted(patrol_dir.glob("*.py")):
        if p.name == "__init__.py":
            continue
        src = p.read_text(encoding="utf-8")
        keys.update(already_re.findall(src))
        keys.update(record_re.findall(src))
        if variable_re.search(src):
            has_variable_call = True

    if has_variable_call:
        try:
            from argus.patrol import actions

            keys.update(actions._REMINDER_EVENT_TYPES.values())
        except ImportError:
            pass

    return keys | _HISTORICAL_EVENT_TYPES


def check_state_key_drift(state_conn: sqlite3.Connection) -> list[dict]:
    known = _collect_known_event_types()
    violations = []
    rows = state_conn.execute(
        "SELECT DISTINCT event_type FROM notifications"
    ).fetchall()
    for row in rows:
        event_type = row[0]
        if event_type not in known:
            violations.append({"check": "state_key_drift", "event_type": event_type})
    return violations


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def run_checks(
    pm_conn: sqlite3.Connection,
    state_conn: sqlite3.Connection | None,
    days: int,
    today: str,
) -> list[dict]:
    # audit_log.changed_at は datetime.now(UTC).isoformat()（"+00:00" 付き）で
    # 記録されるため、cutoff も UTC aware で揃える。today（ローカル暦日）基準だと
    # JST/UTC の 9 時間ずれで境界付近の判定が最大 9 時間ぶれる。
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    violations: list[dict] = []
    violations += check_future_dates(pm_conn, today)
    violations += check_date_reversal_closes(pm_conn, cutoff)
    violations += check_rollback_pattern(pm_conn, cutoff)
    violations += check_missing_close_note(pm_conn, cutoff)
    if state_conn is not None:
        violations += check_state_key_drift(state_conn)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pm.db / patrol_state.db のデータ不変条件を検査する（読み取り専用）"
    )
    add_db_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument(
        "--state-db", default=None, metavar="PATH",
        help="patrol_state.db のパス（デフォルト: data/patrol_state.db）",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="クローズ関連チェックの対象期間（日数、デフォルト: 7）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="結果をJSON形式で出力する",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.db, REPO_ROOT / "data" / "pm.db")
    state_db_path = (
        Path(args.state_db) if args.state_db else REPO_ROOT / "data" / "patrol_state.db"
    )

    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        return 1

    pm_conn = open_db(db_path, encrypt=not args.no_encrypt)
    pm_conn.execute("PRAGMA query_only = ON")

    state_conn = None
    if state_db_path.exists():
        uri = f"file:{state_db_path}?mode=ro"
        state_conn = sqlite3.connect(uri, uri=True)
        state_conn.row_factory = sqlite3.Row
    else:
        print(
            f"[WARN] patrol_state.db が見つかりません（state_key_drift はスキップ）: {state_db_path}",
            file=sys.stderr,
        )

    today = date.today().isoformat()
    violations = run_checks(pm_conn, state_conn, args.days, today)

    pm_conn.close()
    if state_conn is not None:
        state_conn.close()

    if args.json:
        print(json.dumps(violations, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"OK: 違反なし（--days {args.days}, today={today}）")
        else:
            by_check: dict[str, list[dict]] = {}
            for v in violations:
                by_check.setdefault(v["check"], []).append(v)
            print(f"NG: {len(violations)} 件の違反を検出しました（--days {args.days}, today={today}）")
            for check_name, items in sorted(by_check.items()):
                print(f"\n[{check_name}] {len(items)} 件")
                for item in items:
                    detail = {k: v for k, v in item.items() if k != "check"}
                    print(f"  {detail}")

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
