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

さらにセキュリティ監視として以下を検査する
（docs/security-architecture.md §4.3。--no-security-checks で無効化）:

  6. canary_hit            — active な canary トークンがログに出現した。canary は
                             本来どこにも現れない文字列なので 1 件でも異常。最優先で
                             調査し、発火セッションのログを保全する
  7. netguard_deny         — net_guard が allow-list 外の宛先を記録した。warn モード
                             では通ってしまうため、この検査が唯一の気づき方になる。
                             enforce 後は 1 件でも異常
  8. tool_call_chain       — tool_calls のハッシュ連鎖が壊れている（§4.4）。
                             検出できるのは事故による破損まで（外部アンカーは Phase 3）
  9. canary_alive          — 植えた canary が box_docs / qa_index から消えている。
                             **発火検知だけでは餌が腐っても気づけない**ため対で要る
 10. silent_control        — tool_calls に「動いていれば必ず出る」種類の記録が
                             観測期間内に1件も無い。canary / net_guard 自体は
                             呼ばれなければ検知できない（2026-08-01 に Slack 出力
                             ファネルの conn 未配線・第2系統トリアージの呼び出し元
                             不在の2件が判明。共通のシグナルは「動いている証拠が
                             一件も無い」ことだった）

書き込みは一切行わない。値（channel/user ID 等）はログに出力しない
（state_key_drift は event_type のキー名のみを出力する。canary_hit / netguard_deny は
canary トークン・宛先ホスト・呼び出し元を出力するが、いずれも秘匿値ではない）。

Usage:
    python3 scripts/quality/pm_selfcheck.py
    python3 scripts/quality/pm_selfcheck.py --days 7
    python3 scripts/quality/pm_selfcheck.py --json
    python3 scripts/quality/pm_selfcheck.py --db data/pm.db --state-db data/patrol_state.db
    python3 scripts/quality/pm_selfcheck.py --logs-dir logs --days 1
    python3 scripts/quality/pm_selfcheck.py --no-security-checks
    python3 scripts/quality/pm_selfcheck.py --silence-days 14
    python3 scripts/quality/pm_selfcheck.py --silence-strict
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
from db_utils import open_db, table_exists

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
# 6-7. セキュリティ監視（docs/security-architecture.md §4.3）
# --------------------------------------------------------------------------- #

# 1 ファイルから読む上限。巨大ログで検査ジョブが張り付くのを防ぐ。
# 超過分は読まずに truncated として報告する（黙って打ち切らない）。
_LOG_SCAN_MAX_BYTES = 32 * 1024 * 1024

_NETGUARD_DENY_MARKER = "[NETGUARD] verdict=deny"

# ログ行先頭のタイムスタンプ（例: "2026-08-01 01:22:15 [ERROR] ..."）
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# 報告するグループ数の上限。超過分は件数だけ集計して other_groups に出す。
_DENY_GROUP_LIMIT = 20


def _recent_log_files(logs_dir: Path, days: int) -> list[Path]:
    """logs_dir 直下の *.log のうち、mtime が days 以内のものを返す。"""
    if not logs_dir.is_dir():
        return []
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    out = []
    for path in sorted(logs_dir.glob("*.log")):
        try:
            if path.stat().st_mtime >= cutoff:
                out.append(path)
        except OSError:
            continue
    return out


def _iter_log_lines(paths: list[Path]) -> tuple[list[tuple[Path, str]], list[str]]:
    """(ファイル, 行) の列と、途中で打ち切ったファイル名の一覧を返す。"""
    lines: list[tuple[Path, str]] = []
    truncated: list[str] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                read = 0
                for line in f:
                    read += len(line)
                    if read > _LOG_SCAN_MAX_BYTES:
                        truncated.append(path.name)
                        break
                    lines.append((path, line))
        except OSError:
            continue
    return lines, truncated


def _parse_log_timestamp(line: str) -> datetime | None:
    """ログ行先頭の `YYYY-MM-DD HH:MM:SS` を UTC aware な datetime として返す。

    タイムスタンプを持たないログ（シェルスクリプトの echo 等）では None。
    ローカル時刻（JST）で書かれているものとして解釈する。
    """
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone()


def load_netguard_ack(path: Path | None = None) -> list[dict]:
    """解消済み deny の申告（ack）を読む。

    「直したのに古いログ行が残っていて鳴り続ける」を止めるための仕組み。
    `fixed_at` より**前**の行だけを抑制するので、修正後に同じ宛先がまた出れば
    もう一度違反として報告される（恒久的な mute にはならない）。
    """
    ack_path = path or (REPO_ROOT / "config" / "netguard_ack.yaml")
    if not ack_path.is_file():
        return []
    try:
        import yaml

        data = yaml.safe_load(ack_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    entries = data.get("resolved") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict) or "destination" not in e or "fixed_at" not in e:
            continue
        fixed_at = str(e["fixed_at"])
        try:
            dt = datetime.strptime(fixed_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(fixed_at, "%Y-%m-%d")
            except ValueError:
                continue
        out.append(
            {
                "destination": str(e["destination"]),
                "port": str(e["port"]) if e.get("port") is not None else None,
                "stage": str(e["stage"]) if e.get("stage") else None,
                "fixed_at": dt.astimezone(),
                "reason": e.get("reason"),
            }
        )
    return out


def _ack_covers(acks: list[dict], stage: str, destination: str, port: str, ts: datetime | None) -> bool:
    """この deny 行が「解消済みの古い行」として抑制対象かを返す。

    タイムスタンプが読めない行は抑制しない（保守的側に倒す — 抑制の判断材料が
    無いのに黙らせると、再発を見逃す）。
    """
    if ts is None:
        return False
    for ack in acks:
        if ack["destination"] != destination:
            continue
        if ack["port"] is not None and ack["port"] != port:
            continue
        if ack["stage"] is not None and ack["stage"] != stage:
            continue
        if ts < ack["fixed_at"]:
            return True
    return False


def check_netguard_deny(
    logs_dir: Path, days: int, acks: list[dict] | None = None
) -> list[dict]:
    """net_guard が allow-list 外の宛先を記録していないか検査する。

    warn モードでは通っている（記録だけ）ので、この検査が唯一の気づき方になる。
    enforce へ倒した後は deny がゼロであることが正常状態なので、1 件でも異常。

    `config/netguard_ack.yaml` に解消済みとして申告された宛先は、その `fixed_at` より
    前のログ行だけ抑制する（修正後の再発は必ず報告される）。
    """
    paths = _recent_log_files(logs_dir, days)
    if not paths:
        return []
    if acks is None:
        acks = load_netguard_ack()
    lines, truncated = _iter_log_lines(paths)

    groups: dict[tuple[str, str, object], dict] = {}
    suppressed = 0
    for path, line in lines:
        if _NETGUARD_DENY_MARKER not in line:
            continue
        fields = {}
        for token in line.split():
            if "=" in token:
                k, _, v = token.partition("=")
                fields[k] = v
        label = fields.get("host") if fields.get("host", "-") != "-" else fields.get("ip", "-")
        stage = fields.get("stage", "-")
        port = fields.get("port", "-")
        if _ack_covers(acks, stage, label or "-", port, _parse_log_timestamp(line)):
            suppressed += 1
            continue
        key = (stage, label or "-", port)
        group = groups.setdefault(key, {"count": 0, "callers": [], "files": set()})
        group["count"] += 1
        group["files"].add(path.name)
        caller = fields.get("caller", "-")
        if caller not in group["callers"] and len(group["callers"]) < 3:
            group["callers"].append(caller)

    if suppressed:
        # 抑制したこと自体は必ず見せる（黙って減らすと「静かになった」と誤読される）
        print(
            f"[INFO] netguard_deny: 解消済み申告により {suppressed} 行を抑制しました"
            " (config/netguard_ack.yaml)",
            file=sys.stderr,
        )

    ordered = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)
    violations = []
    for (stage, label, port), info in ordered[:_DENY_GROUP_LIMIT]:
        violations.append(
            {
                "check": "netguard_deny",
                "stage": stage,
                "destination": label,
                "port": port,
                "count": info["count"],
                "callers": ",".join(info["callers"]),
                "log_files": ",".join(sorted(info["files"])),
            }
        )
    if len(ordered) > _DENY_GROUP_LIMIT:
        violations.append(
            {
                "check": "netguard_deny",
                "other_groups": len(ordered) - _DENY_GROUP_LIMIT,
                "note": "報告上限を超えた宛先グループ（net_guard.py --summarize-log で全件確認）",
            }
        )
    if truncated:
        violations.append(
            {
                "check": "netguard_deny",
                "note": "サイズ上限で読み切れなかったログがある（検査は不完全）",
                "truncated_files": ",".join(sorted(set(truncated))),
            }
        )
    return violations


def check_canary_hits(
    pm_conn: sqlite3.Connection, logs_dir: Path, days: int
) -> list[dict]:
    """active な canary トークンがログに現れていないか検査する（§4.3 検知点）。

    canary は「本来どこにも出てこない文字列」なので、1 件でも現れたら異常。
    ログに現れる典型は (a) net_guard が canary ホスト名の解決を deny した、
    (b) LLM の出力・ツール引数に canary 文字列が混じった、のいずれか。

    canary_tokens テーブルが無い pm.db（未導入）では空リストを返す。
    """
    if not table_exists(pm_conn, "canary_tokens"):
        return []
    rows = pm_conn.execute(
        "SELECT token, kind, planted_in FROM canary_tokens WHERE active = 1"
    ).fetchall()
    tokens = {row["token"]: dict(row) for row in rows}
    if not tokens:
        return []

    paths = _recent_log_files(logs_dir, days)
    if not paths:
        return []
    lines, _truncated = _iter_log_lines(paths)

    hits: dict[tuple[str, str], int] = {}
    for path, line in lines:
        for token in tokens:
            if token in line:
                key = (token, path.name)
                hits[key] = hits.get(key, 0) + 1

    return [
        {
            "check": "canary_hit",
            "token": token,
            "kind": tokens[token]["kind"],
            "planted_in": tokens[token]["planted_in"],
            "log_file": file_name,
            "count": count,
        }
        for (token, file_name), count in sorted(hits.items())
    ]


def check_tool_call_chain(pm_conn: sqlite3.Connection) -> list[dict]:
    """tool_calls のハッシュ連鎖が壊れていないか検査する（§4.4）。

    **検出できるのは事故による破損であって、意図的な改竄ではない。**
    検証者は改竄されうる側と同じプロセス・同じ UNIX ユーザで動くため、コード実行を
    取られればエントリと連鎖の頭の両方を書き換えられる。外部アンカー（日次のハッシュ
    投稿）は Phase 3 のブローカー待ち。
    """
    if not table_exists(pm_conn, "tool_calls"):
        return []
    from db_utils import verify_tool_call_chain

    return [
        {"check": "tool_call_chain", "call_id": b["call_id"], "reason": b["reason"]}
        for b in verify_tool_call_chain(pm_conn)
    ]


def check_canary_alive(pm_conn: sqlite3.Connection, data_dir: Path) -> list[dict]:
    """植えた canary がまだ「モデルが読む場所」に居るかを検査する（§4.3）。

    **発火検知だけでは不十分**。植えた行が消える／索引から落ちると、監視は
    「異常なし」を出し続ける — 餌が腐っても気づけない状態になる。
    `planted_in='box_docs'` の canary について、box_docs.db に本文が残っていることと、
    qa_index.db にチャンクがあることを確認する。
    """
    if not table_exists(pm_conn, "canary_tokens"):
        return []
    rows = pm_conn.execute(
        "SELECT token, planted_in, row_ref FROM canary_tokens"
        " WHERE active = 1 AND planted_in = 'box_docs'"
    ).fetchall()
    if not rows:
        return []

    violations = []
    box_db = data_dir / "box_docs.db"
    index_db = data_dir / "qa_index.db"
    for r in rows:
        token, row_ref = r["token"], r["row_ref"]
        in_box = _row_contains(box_db, "SELECT 1 FROM doc_content WHERE content_md LIKE ? LIMIT 1",
                               (f"%{token}%",))
        in_index = _row_contains(index_db, "SELECT 1 FROM chunks WHERE content LIKE ? LIMIT 1",
                                 (f"%{token}%",))
        if in_box is False or in_index is False:
            violations.append({
                "check": "canary_alive", "token": token, "row_ref": row_ref or "-",
                "in_box_docs": in_box, "in_qa_index": in_index,
                "note": "植えた canary が読める場所から消えています（監視が空振りします）",
            })
        elif in_box is None or in_index is None:
            # **判定不能を「異常なし」に含めない。** 検査できていないことこそ報告する
            # （黙って通すと、生存確認が動いていないのに動いているように見える）
            violations.append({
                "check": "canary_alive", "token": token, "row_ref": row_ref or "-",
                "in_box_docs": in_box, "in_qa_index": in_index,
                "note": "canary の生存を確認できませんでした（DB を開けない／表が無い）",
            })
    return violations


def _row_contains(db_path: Path, sql: str, params: tuple) -> bool | None:
    """存在確認。開けない／問い合わせ不能なら None（判定不能）を返す。

    **暗号化と平文の両方を試す。** `qa_index.db` は平文で、`box_docs.db` は暗号化という
    実態がある（2026-08-01 実測。docs/architecture.md の表は誤り）。片方だけ試すと
    「判定不能」を量産し、生存確認が黙って空振りする。
    """
    if not db_path.exists():
        return None
    from db_utils import open_db

    for encrypt in (True, False):
        conn = None
        try:
            conn = open_db(db_path, encrypt=encrypt)
            return conn.execute(sql, params).fetchone() is not None
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


# --------------------------------------------------------------------------- #
# 10. 沈黙している制御の検出（docs/security-architecture.md、2026-08-01 の反省）
# --------------------------------------------------------------------------- #

# tool_calls に「動いていれば必ず出る」はずの種類と、その既定観測期間（日）。
# where 句は tool_calls のカラムのみを参照する（values は使わない）。
_SILENCE_SPECS: dict[str, dict] = {
    "egress_slack": {
        "where": "plane = 'egress' AND tool_name LIKE 'slack:%'",
        "default_days": 7,
        "description": "Slackへの出力（brief/risk/patrol/canvas_reportが動いていれば必ず出る）",
    },
    "egress_other": {
        "where": "plane = 'egress' AND tool_name NOT LIKE 'slack:%'",
        "default_days": 30,
        "description": "Canvas / Box への出力",
    },
    "read_tools": {
        "where": "plane = 'read'",
        "default_days": 7,
        "description": "investigate の調査ツール呼び出し",
    },
}


def _oldest_tool_call_ts(pm_conn: sqlite3.Connection) -> datetime | None:
    """tool_calls 全体の最古の ts を aware datetime で返す（空・解釈不能なら None）。"""
    row = pm_conn.execute("SELECT MIN(ts) AS ts FROM tool_calls").fetchone()
    raw = row["ts"] if row else None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    # `datetime.fromisoformat()` はタイムゾーン無しの ts に対して naive datetime を
    # 返す。呼び出し元は aware な cutoff と比較するため、tzinfo が無ければ UTC を
    # 付与する（付与しないと naive/aware 比較で TypeError になり selfcheck 全体が落ちる）。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def check_silent_control(
    pm_conn: sqlite3.Connection, days_override: int | None = None
) -> list[dict]:
    """tool_calls に期待される種類の記録が観測期間内に1件も無いか検査する。

    背景（2026-08-01）: Slack 出力ファネルの canary 検査と egress 記録が呼び出し
    25 箇所で丸ごとスキップされていた欠陥と、実装・テストはあるが production の
    呼び出し元が存在しない第2系統トリアージの欠陥が同日に見つかった。どちらも
    テストは通り lint も通っていたが、**「動いている証拠が1件も無い」ことだけが
    共通のシグナル**だった。この検査はそれを機械的に検出する。

    **この検査が証明するのは「観測期間内に記録が無い」ことだけである。**
    制御そのものが壊れているのか、その機能をそもそも運用上動かしていない
    （意図的に止めている）だけなのかは、この検査では区別できない（P10: 対策が
    何を証明するかを明示する）。運用を止めていれば沈黙は正常であり、
    「沈黙 = 異常」と機械的に決めつけない。原因の切り分けは人が行う。

    **「記録が無い」には「制御が動いていない」と「まだ観測していない」の2つが
    あり、両者を同じ違反として扱うと、運用開始直後は必ず誤警報になる。**
    `tool_calls` 自体が新設されたばかりで観測期間分の履歴が無い場合、期間内に
    0件でも制御の沈黙とは断定できないため「判定不能」として区別し、違反リストには
    入れない（黙って飛ばさず、判定できなかった旨は標準出力に出す）。
    """
    if not table_exists(pm_conn, "tool_calls"):
        return []
    violations = []
    now = datetime.now(UTC)
    oldest_ts = _oldest_tool_call_ts(pm_conn)
    for key, spec in _SILENCE_SPECS.items():
        days = days_override if days_override is not None else spec["default_days"]
        cutoff_dt = now - timedelta(days=days)
        cutoff = cutoff_dt.isoformat()

        row = pm_conn.execute(
            f"SELECT COUNT(*) AS n FROM tool_calls WHERE {spec['where']} AND ts >= ?",
            (cutoff,),
        ).fetchone()
        count = row["n"]
        if count > 0:
            continue

        # count == 0 だけでは「沈黙」と断定できない。台帳自体が観測期間より
        # 若い（＝まだその期間分を観測していない）場合、期間内に0件なのは
        # 制御の沈黙ではなく単に履歴が無いだけの可能性がある。判定不能として
        # 区別する（黙って飛ばさず、標準出力に判定できなかった旨を出す）。
        if oldest_ts is None:
            print(
                f"[INFO] silent_control: {key} は判定できません"
                f"（tool_calls 台帳が空か最古の記録を解釈できないため。要求期間={days}日）",
                file=sys.stderr,
            )
            continue
        if oldest_ts > cutoff_dt:
            print(
                f"[INFO] silent_control: {key} は判定できません"
                f"（台帳の最古の記録={oldest_ts.isoformat()} が観測期間の開始"
                f"（{cutoff_dt.isoformat()}）より新しいため。要求期間={days}日）",
                file=sys.stderr,
            )
            continue

        violations.append(
            {
                "check": "silent_control",
                "key": key,
                "expected_within_days": days,
                "description": spec["description"],
                "note": (
                    "観測期間内に記録が1件も無い。制御が壊れているのか、その機能を"
                    "そもそも運用上動かしていないだけなのかはこの検査では区別できない"
                    "（原因の切り分けは人が行う）"
                ),
            }
        )
    return violations


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def run_checks(
    pm_conn: sqlite3.Connection,
    state_conn: sqlite3.Connection | None,
    days: int,
    today: str,
    logs_dir: Path | None = None,
    security_days: int = 1,
    silence_days: int | None = None,
    security_checks_enabled: bool = True,
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
    violations += check_tool_call_chain(pm_conn)
    if logs_dir is not None:
        # セキュリティ監視のログ走査は独立した窓（既定 1 日）で行う。データ検査の
        # --days 7 と同じ窓にすると、解消済みの deny が 1 週間鳴り続けて監視が
        # ノイズになる（2026-08-01 に実際に起きた）。
        violations += check_canary_hits(pm_conn, logs_dir, security_days)
        violations += check_netguard_deny(logs_dir, security_days)
        # canary の生存確認もセキュリティ検査の一部（--no-security-checks で一緒に切れる）
        violations += check_canary_alive(pm_conn, REPO_ROOT / "data")
    # silent_control は pm.db だけで完結する検査であり、ログ走査（logs_dir）とは
    # 無関係。`--logs-dir` に無効なディレクトリを渡した場合でも独立に実行する
    # （そこに相乗りさせると、沈黙を検出する検査自体が logs_dir 不在で黙って
    # 沈黙してしまう）。--no-security-checks では従来どおり一緒にスキップする。
    if security_checks_enabled:
        violations += check_silent_control(pm_conn, silence_days)
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
    parser.add_argument(
        "--logs-dir", default=None, metavar="PATH",
        help="canary_hit / netguard_deny が走査するログディレクトリ（既定: <repo>/logs）",
    )
    parser.add_argument(
        "--no-security-checks", action="store_true",
        help="canary_hit / netguard_deny をスキップする（データ検査のみ行う）",
    )
    parser.add_argument(
        "--security-days", type=int, default=1, metavar="N",
        help="canary_hit / netguard_deny が走査するログの対象期間（日数、デフォルト: 1）。"
             "データ検査の --days とは独立（解消済みの deny が鳴り続けるのを避けるため）",
    )
    parser.add_argument(
        "--silence-days", type=int, default=None, metavar="N",
        help="silent_control が全キー共通で使う観測期間（日数）。省略時はキーごとの既定値"
             "（egress_slack/read_tools=7日、egress_other=30日）を使う",
    )
    parser.add_argument(
        "--silence-strict", action="store_true",
        help="silent_control の沈黙を他の検査と同じ違反として扱い exit code 1 にする"
             "（既定では警告として出力するのみで exit code に反映しない）",
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
    security_checks_enabled = not args.no_security_checks
    logs_dir = None
    if security_checks_enabled:
        logs_dir = Path(args.logs_dir) if args.logs_dir else REPO_ROOT / "logs"
        if not logs_dir.is_dir():
            print(
                "[WARN] ログディレクトリが見つかりません"
                "（canary_hit / netguard_deny / canary_alive はスキップ）: "
                f"{logs_dir}",
                file=sys.stderr,
            )
            logs_dir = None
    violations = run_checks(
        pm_conn, state_conn, args.days, today, logs_dir, args.security_days,
        args.silence_days, security_checks_enabled,
    )

    pm_conn.close()
    if state_conn is not None:
        state_conn.close()

    # silent_control は既定では警告扱い（沈黙 = 異常と決めつけない。運用を止めて
    # いれば沈黙は正常なので、誤警報で監視全体が無効化される方向に圧力がかかる
    # のを避ける）。--silence-strict を付けたときだけ他の検査と同じ exit 1 対象。
    silent_violations = [v for v in violations if v["check"] == "silent_control"]
    hard_violations = [v for v in violations if v["check"] != "silent_control"]
    exit_violations = violations if args.silence_strict else hard_violations

    if args.json:
        print(json.dumps(violations, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"OK: 違反なし（--days {args.days}, today={today}）")
        else:
            by_check: dict[str, list[dict]] = {}
            for v in violations:
                by_check.setdefault(v["check"], []).append(v)
            if exit_violations:
                print(
                    f"NG: {len(exit_violations)} 件の違反を検出しました（--days {args.days}, today={today}）"
                )
            else:
                print(
                    f"OK: 違反なし。ただし {len(silent_violations)} 件の沈黙警告があります"
                    f"（--silence-strict を付けると違反として扱われます）（--days {args.days}, today={today}）"
                )
            for check_name, items in sorted(by_check.items()):
                print(f"\n[{check_name}] {len(items)} 件")
                for item in items:
                    detail = {k: v for k, v in item.items() if k != "check"}
                    print(f"  {detail}")

    return 1 if exit_violations else 0


if __name__ == "__main__":
    sys.exit(main())
