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
 11. model_pin_drift       — config/model_pin.yaml の宣言（served_model_name /
                             max_input_tokens / max_output_tokens）と実際の
                             エンドポイント（/v1/models）が一致しているか
                             （docs/security-architecture.md §4.6）。
                             `assert_model_allowed()` は呼び出しのたびに評価される
                             一方でネットワークに出ないため、宣言と実際のズレは
                             この日次検査だけが検知できる。**この検査だけは
                             ネットワークに出る**（他は全て読み取り専用）。
 12. egress_dest_unknown   — 設定に無い宛先（層3、docs/security-architecture.md
                             §4.7）への送信件数・宛先種類数の観測。層3はまだ
                             warn 段階（`ARGUS_EGRESS_TARGETS`）のため、
                             **これは違反ではなく enforce へ進める判断材料**。
                             `--silence-strict` を付けても exit code には
                             影響しない。
 13. anchor_consistency    — 外部アンカー（`--emit-anchor` が追記する
                             config/anchors/tool_call_anchor.jsonl）の各行が、
                             その行数時点の tool_calls.entry_hash と一致するか
                             （§4.4）。アンカー無し／空は判定不能（違反にしない）。
                             台帳が縮んでいる場合も違反にする。
 14. pending_egress_stale  — `pending_egress`（§4.2 の承認フロー）で
                             status='pending' のまま 7 日以上放置された行がある。
                             滞留は承認フローが機能していない証拠なので黙って溜めない。
 15. anchor_pushed         — ローカルの `anchors` ブランチ（外部アンカーの
                             commit 先）が origin（github.com、SSH）へ push
                             済みかを検査する（§4.4）。一致しなければ違反
                             （push されていないアンカーは外部化されておらず、
                             アンカーとして機能していない）。ローカル・
                             リモートのどちらにも `anchors` が無ければ判定不能
                             （まだ運用が始まっていない）。`git ls-remote` の
                             失敗・タイムアウト（10秒）も判定不能。
 16. second_opinion_findings_stale — `triage_second_opinion`（議事録経路への
                             第2系統・K3 recall の欠落検出所見）で kind が
                             minutes_extraction 系のもののうち、reviewed_at が
                             未設定のまま 14 日以上経過した行がある。読まれない
                             所見が溜まるのは、検査が動いていないのと同じである。
                             `_pretune` / `_t8192` で終わる kind（調整前の
                             試行記録）は対象外。

本ファイルの通常の検査（1〜14）は書き込みを一切行わない。値（channel/user ID 等）はログに出力しない
（state_key_drift は event_type のキー名のみを出力する。canary_hit / netguard_deny は
canary トークン・宛先ホスト・呼び出し元を出力するが、いずれも秘匿値ではない）。
model_pin_drift と anchor_pushed のみネットワークに出る（本ファイルは元来
ネットワークに出ない読み取り専用の検査だったが、この2つだけ性格が異なる。
前者は `--skip-model-pin` で退避できる）。

`--emit-anchor` は上記15検査とは独立した動作モードで、通常検査を一切行わず
外部アンカーファイル（config/anchors/tool_call_anchor.jsonl）へ1行追記するだけで
終了する。**このアンカーはファイルがこのマシンの外へ出て初めて意味を持つ**
（詳細は `emit_anchor()` の docstring を参照）。`scripts/bin/pm_selfcheck.sh` は
`--emit-anchor` の直後に、追記されたアンカーファイルを `anchors` ブランチへ
git の plumbing コマンドで commit+push する（`publish_anchor_branch()`）。
anchor_pushed はその push が実際に届いているかを日次で確かめる。

Usage:
    python3 scripts/quality/pm_selfcheck.py
    python3 scripts/quality/pm_selfcheck.py --days 7
    python3 scripts/quality/pm_selfcheck.py --json
    python3 scripts/quality/pm_selfcheck.py --db data/pm.db --state-db data/patrol_state.db
    python3 scripts/quality/pm_selfcheck.py --logs-dir logs --days 1
    python3 scripts/quality/pm_selfcheck.py --no-security-checks
    python3 scripts/quality/pm_selfcheck.py --emit-anchor
    python3 scripts/quality/pm_selfcheck.py --silence-days 14
    python3 scripts/quality/pm_selfcheck.py --silence-strict
    python3 scripts/quality/pm_selfcheck.py --skip-model-pin
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from cli_utils import add_db_arg, add_no_encrypt_arg, resolve_db_path
from db_utils import open_db, table_exists

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 外部アンカー（§4.4）の既定パス。`data/` は .gitignore 対象（機密DBを含むため）
# なので、git 管理下に置けて実値を含まない config/ 配下に置く。
_DEFAULT_ANCHOR_PATH = REPO_ROOT / "config" / "anchors" / "tool_call_anchor.jsonl"

# pending_egress（§4.2 の承認フロー）が pending のまま放置されたとみなす日数。
_PENDING_EGRESS_STALE_DAYS = 7

# triage_second_opinion（第2系統・K3 recall の欠落検出所見）が未レビューのまま
# 放置されたとみなす日数。
_SECOND_OPINION_FINDINGS_STALE_DAYS = 14
# 調整前の試行記録（本番運用の所見ではない）を示す kind の接尾辞。レビュー対象外。
_SECOND_OPINION_EXEMPT_KIND_SUFFIXES = ("_pretune", "_t8192")

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
# 11. モデル pin ドリフト（config/model_pin.yaml、docs/security-architecture.md §4.6）
# --------------------------------------------------------------------------- #
def check_model_pin_drift() -> list[dict]:
    """宣言したモデル（id・max_input/output_tokens）が実際のエンドポイントと
    一致しているかを検査する。

    **この検査だけがネットワークに出る。** `assert_model_allowed()`（呼び出しの
    たびに評価される関数）はネットワークに出ないため、宣言と実際のズレは
    この日次検査だけが検知できる。供給元（RIKYU）に更新通知の取り決めを
    依頼できないため、これが唯一の気づき手段になる。

    endpoint_env 未設定（skip）・到達不能（error）は違反にしない。ただし
    **判定不能を黙って通さない** — 理由を必ず標準出力に出す。判定不能の
    理由を握りつぶすと、検査が入っているのに何も見ていない状態（no-op）に
    なる（2026-08-01 に別の検査で複数回踏んだ型）。

    タイムアウトは短め（10秒）にし、エンドポイント障害で selfcheck 全体が
    落ちないよう例外はすべてこの関数の中で吸収する。
    """
    try:
        from utils.model_pin import check_endpoints
    except Exception as e:
        print(
            f"[WARN] model_pin_drift: model_pin モジュールの読み込みに失敗しました"
            f"（この検査をスキップします）: {e}",
            file=sys.stderr,
        )
        return []

    try:
        rows = check_endpoints(timeout=10)
    except Exception as e:
        print(
            f"[WARN] model_pin_drift: check_endpoints の実行に失敗しました"
            f"（この検査をスキップします）: {e}",
            file=sys.stderr,
        )
        return []

    violations = []
    for row in rows:
        status = row.get("status")
        if status == "ok":
            continue
        if status == "mismatch":
            violations.append({"check": "model_pin_drift", **row})
            continue
        # skip（endpoint_env 未設定）・error（到達不能）は判定不能。違反にはしない。
        print(
            f"[INFO] model_pin_drift: {row.get('model')} は判定できません"
            f"（status={status}）: {row.get('detail')}",
            file=sys.stderr,
        )
    return violations


# --------------------------------------------------------------------------- #
# 12. 宛先粒度（層3）の未知送信の観測（docs/security-architecture.md §4.7）
# --------------------------------------------------------------------------- #

# egress_dest_unknown の既定観測期間（日）。silent_control の egress_slack と
# 揃える（`tool_calls` plane='egress' を見る点が同じであるため）。
_EGRESS_DEST_UNKNOWN_DEFAULT_DAYS = 7


def check_egress_dest_unknown(
    pm_conn: sqlite3.Connection, days_override: int | None = None
) -> list[dict]:
    """設定に無い宛先（層3）への送信が観測期間内にどれだけあったかを集計する。

    層1（ホスト）は net_guard、層2（ツール）は agent_tools.py の registry で
    allow-list を持つが、層3（どのチャンネル・どの相手か）はまだ warn 段階
    （`ARGUS_EGRESS_TARGETS`）で `dest_known` を記録しているだけであり、
    enforce にはしていない。

    **これは観測であり違反ではない。** enforce へ進めるかどうかを判断する
    ための分布（件数・宛先の種類数）を報告するだけで、`--silence-strict` を
    付けても exit code には一切影響しない（`main()` 側で常に除外する）。
    宛先の実値はログに出さない（件数・種類数のみ）。

    silent_control と同じく、台帳（`tool_calls`）が観測期間より新しい場合は
    判定不能として扱い、違反リストには入れない（標準出力に理由だけ出す）。
    """
    if not table_exists(pm_conn, "tool_calls"):
        return []
    days = days_override if days_override is not None else _EGRESS_DEST_UNKNOWN_DEFAULT_DAYS
    cutoff_dt = datetime.now(UTC) - timedelta(days=days)
    cutoff = cutoff_dt.isoformat()

    oldest_ts = _oldest_tool_call_ts(pm_conn)
    if oldest_ts is None:
        print(
            "[INFO] egress_dest_unknown: 判定できません"
            "（tool_calls 台帳が空か最古の記録を解釈できないため）",
            file=sys.stderr,
        )
        return []
    if oldest_ts > cutoff_dt:
        print(
            f"[INFO] egress_dest_unknown: 判定できません"
            f"（台帳の最古の記録={oldest_ts.isoformat()} が観測期間の開始"
            f"（{cutoff_dt.isoformat()}）より新しいため。要求期間={days}日）",
            file=sys.stderr,
        )
        return []

    rows = pm_conn.execute(
        "SELECT args_json FROM tool_calls WHERE plane = 'egress' AND ts >= ?",
        (cutoff,),
    ).fetchall()

    count = 0
    destinations: set[str] = set()
    for row in rows:
        raw = row["args_json"]
        if not raw:
            continue
        try:
            args = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict) or args.get("dest_known") is not False:
            continue
        count += 1
        dest = args.get("channel") or args.get("dest") or ""
        if dest:
            destinations.add(dest)

    if count == 0:
        return []

    return [
        {
            "check": "egress_dest_unknown",
            "count": count,
            "distinct_destinations": len(destinations),
            "observed_within_days": days,
            "note": (
                "設定に無い宛先（層3）への送信の観測。enforce へ進める判断の"
                "材料であり、違反ではない（warn段階、ARGUS_EGRESS_TARGETS）"
            ),
        }
    ]


# --------------------------------------------------------------------------- #
# 13. 外部アンカー（docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #
def emit_anchor(
    pm_conn: sqlite3.Connection, anchor_path: Path = _DEFAULT_ANCHOR_PATH,
) -> dict | None:
    """`tool_calls` の連鎖の頭を外部アンカーファイルへ1行追記する（§4.4）。

    `--emit-anchor` からのみ呼ばれる、通常の検査（read-only）とは独立した書き込みモード。

    **このアンカーは、ファイルがこのマシンの外へ出て初めて意味を持つ。**
    同じファイルシステム上にある限り、台帳を書き換えられる攻撃者はアンカーも書き換えられる。
    **git へコミットして push した時点で外部（third-party hosted・追記的）に出る。**

    **push は運用に載せている**（2026-08-03〜）。`scripts/bin/pm_selfcheck.sh` が
    この関数の直後に、追記後のアンカーファイルを専用ブランチ `anchors` へ
    git の低レベルコマンド（hash-object/mktree/commit-tree/update-ref、いずれも
    作業ツリー・インデックス・main を一切変更しない）で commit し、
    `git push origin anchors:anchors` する（`publish_anchor_branch()`、
    `main` は日次コミットの対象から外してある）。push の成否は
    `anchor_pushed` 検査（本ファイル）が日次で確かめる。

    **公開されるのは `ts` / `rows` / `entry_hash` のみ**で、本文・チャンネル・
    モデル名は一切含まれない。ただし `rows`（tool_calls の行数）は活動量を
    露出する — public リポジトリへ載る以上、その程度は受容している。

    **`git push` は subprocess 経由なので net_guard の対象外**である
    （`box` CLI と同じ扱い。外向き通信の allow-list は github.com を
    覆っていない）。

    証明範囲は変わらない。push 済みのアンカーより後に、それ以前の記録が
    書き換えられていないことしか証明しない。**記録された `tool_calls` の
    内容が真実（実際にそのツールが呼ばれたか）かどうかには何も言わない** —
    証明するのは連鎖の不変性であって、記録内容の真正性ではない。

    本文・チャンネル・モデル名などは一切含めない（ハッシュと件数だけ）。
    直前の行と `entry_hash` が同じなら追記しない（台帳が動いていない日は行を増やさない）。
    台帳（`tool_calls`）が空なら None を返す。
    """
    from db_utils import tool_call_anchor

    anchor = tool_call_anchor(pm_conn)
    if anchor is None:
        return None

    last_hash = None
    if anchor_path.is_file():
        for line in reversed(anchor_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                last_hash = json.loads(line).get("entry_hash")
            except (ValueError, TypeError):
                last_hash = None
            break

    if last_hash == anchor["entry_hash"]:
        return None  # 台帳が動いていない。追記しない。

    record = {
        "ts": datetime.now(UTC).isoformat(),
        "rows": anchor["count"],
        "entry_hash": anchor["entry_hash"],
    }
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    with anchor_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def check_anchor_consistency(
    pm_conn: sqlite3.Connection, anchor_path: Path = _DEFAULT_ANCHOR_PATH,
) -> list[dict]:
    """外部アンカーファイルの各行が現在の tool_calls 台帳と整合するか検証する（§4.4）。

    行ごとに「`rows` 行目までの連鎖の頭の `entry_hash`」が記録値と一致するかを見る。
    一致しなければ違反（過去のアンカーと現在の台帳が食い違う＝過去分が書き換えられた）。
    台帳の行数がアンカーの `rows` より少ない場合も違反（台帳が縮んでいる）。

    アンカーファイルが無い／空なら判定不能として扱い、違反リストには入れない
    （黙って飛ばさず、標準出力に理由を出す）。
    """
    if not anchor_path.is_file():
        print(
            f"[INFO] anchor_consistency: 判定できません"
            f"（アンカーファイルがありません: {anchor_path}）",
            file=sys.stderr,
        )
        return []

    lines = [ln for ln in anchor_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        print(
            f"[INFO] anchor_consistency: 判定できません"
            f"（アンカーファイルが空です: {anchor_path}）",
            file=sys.stderr,
        )
        return []

    if not table_exists(pm_conn, "tool_calls"):
        return []

    total_rows = pm_conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0]

    violations = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            violations.append({
                "check": "anchor_consistency",
                "reason": "アンカー行の解析に失敗しました（形式不正）",
            })
            continue
        rows = entry.get("rows")
        expected_hash = entry.get("entry_hash")
        if not isinstance(rows, int) or not expected_hash:
            violations.append({
                "check": "anchor_consistency",
                "reason": "アンカー行に rows / entry_hash が欠けています",
            })
            continue
        if total_rows < rows:
            violations.append({
                "check": "anchor_consistency", "rows": rows,
                "reason": (
                    f"台帳の行数（{total_rows}）がアンカー記録時（{rows}）より"
                    "少ない（台帳が縮んでいる）"
                ),
            })
            continue
        row = pm_conn.execute(
            "SELECT entry_hash FROM tool_calls ORDER BY rowid LIMIT 1 OFFSET ?",
            (rows - 1,),
        ).fetchone()
        actual_hash = row["entry_hash"] if row else None
        if actual_hash != expected_hash:
            violations.append({
                "check": "anchor_consistency", "rows": rows,
                "reason": "過去に固定したアンカーと現在の台帳が食い違う（過去分が書き換えられた可能性）",
            })
    return violations


# --------------------------------------------------------------------------- #
# 14. 承認待ち egress の滞留（docs/security-architecture.md §4.2）
# --------------------------------------------------------------------------- #
def check_pending_egress_stale(
    pm_conn: sqlite3.Connection, days: int = _PENDING_EGRESS_STALE_DAYS,
) -> list[dict]:
    """`pending_egress` に `days` 日以上 pending のまま滞留した行がないか検査する。

    滞留は「承認フローが機能していない」ことの証拠なので黙って溜めない。
    """
    if not table_exists(pm_conn, "pending_egress"):
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = pm_conn.execute(
        "SELECT id, ts, target FROM pending_egress WHERE status = 'pending' AND ts < ?",
        (cutoff,),
    ).fetchall()
    return [
        {
            "check": "pending_egress_stale",
            "id": row["id"],
            "target": row["target"],
            "ts": row["ts"],
            "note": f"{days}日以上未承認のまま滞留しています（承認フローが機能していない可能性）",
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# 15. アンカーの push 状態（docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #

# git ls-remote / rev-parse のタイムアウト（秒）。cron を止めないための上限。
_GIT_REMOTE_TIMEOUT = 10


def _run_git(args: list[str], repo_root: Path) -> subprocess.CompletedProcess | Exception:
    """git を subprocess で実行する（shell=True は使わない）。

    タイムアウト・実行不能（git 未インストール等）はここで吸収し、例外を返す。
    """
    try:
        return subprocess.run(
            args, cwd=repo_root, capture_output=True, text=True,
            timeout=_GIT_REMOTE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return e


def check_anchor_pushed(repo_root: Path = REPO_ROOT) -> list[dict]:
    """ローカルの `anchors` ブランチが origin へ push 済みかを検査する（§4.4）。

    外部アンカーは「ファイルがこのマシンの外へ出て初めて意味を持つ」
    （`emit_anchor()` の docstring 参照）。ローカルの `refs/heads/anchors` だけが
    あって origin に無ければ、台帳を書き換えられる攻撃者はローカルのアンカーも
    一緒に書き換えられるため、アンカーとして機能していない。**一致しなければ
    違反**とする。

    ローカル・リモートのどちらにも `anchors` が無い場合は「まだ運用が始まって
    いない」として判定不能（違反にしない）。`git ls-remote` が失敗・タイムアウト
    した場合（ネットワーク不通等）も判定不能。黙って飛ばさず理由を標準出力に出す。

    **この検査は `git ls-remote` で origin（github.com）へ通信する。**
    model_pin_drift と並び、本ファイルの検査の中でネットワークに出る数少ない
    例外である。
    """
    local_res = _run_git(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/anchors"], repo_root,
    )
    local_sha = None
    if isinstance(local_res, subprocess.CompletedProcess) and local_res.returncode == 0:
        local_sha = local_res.stdout.strip() or None

    remote_res = _run_git(["git", "ls-remote", "origin", "anchors"], repo_root)
    if not isinstance(remote_res, subprocess.CompletedProcess):
        print(
            f"[INFO] anchor_pushed: 判定できません"
            f"（git ls-remote の実行に失敗しました: {remote_res}）",
            file=sys.stderr,
        )
        return []
    if remote_res.returncode != 0:
        print(
            f"[INFO] anchor_pushed: 判定できません"
            f"（git ls-remote が失敗しました: {remote_res.stderr.strip()}）",
            file=sys.stderr,
        )
        return []

    remote_sha = None
    lines = remote_res.stdout.strip().splitlines()
    if lines:
        remote_sha = lines[0].split()[0]

    if local_sha is None and remote_sha is None:
        print(
            "[INFO] anchor_pushed: 判定できません"
            "（anchors ブランチがローカルにも origin にもありません。"
            "まだ運用が始まっていません）",
            file=sys.stderr,
        )
        return []

    if local_sha != remote_sha:
        return [{
            "check": "anchor_pushed",
            "local": local_sha or "(none)",
            "remote": remote_sha or "(none)",
            "reason": (
                "ローカルの anchors ブランチと origin の anchors ブランチが"
                "一致しません（push されていないアンカーは外部化されておらず、"
                "アンカーとして機能していません）"
            ),
        }]
    return []


# --------------------------------------------------------------------------- #
# 16. 第2系統トリアージ所見の滞留（second_opinion_findings_stale）
# --------------------------------------------------------------------------- #
def check_second_opinion_findings_stale(
    pm_conn: sqlite3.Connection, days: int = _SECOND_OPINION_FINDINGS_STALE_DAYS,
) -> list[dict]:
    """`triage_second_opinion` の議事録経路の所見が未レビューのまま滞留していないか検査する。

    **読まれない所見が溜まるのは、検査が動いていないのと同じである。**
    K3 recall（kind=minutes_extraction_recall）・R8対策の第2系統
    （kind=minutes_extraction）とその調整試行（`_pretune` / `_t8192` 接尾辞）を含む
    `minutes_extraction` 系の kind のみが対象。`_pretune` / `_t8192` で終わる kind
    は調整前の試行記録であり、本番運用の所見ではないためレビュー対象から除外する。

    本ファイルの検査は読み取り専用（`main()` が `PRAGMA query_only = ON` を張る）ため、
    ここで `reviewed_at` 列の後付け（`ensure_second_opinion_reviewed_column`、ALTER TABLE
    を伴う）は行わない。列がまだ後付けされていない pm.db では判定不能として扱う
    （黙って飛ばさず理由を標準出力に出す。列の後付けは別途 pm_screen.py 側の経路で行う）。
    """
    if not table_exists(pm_conn, "triage_second_opinion"):
        return []
    cols = {r[1] for r in pm_conn.execute("PRAGMA table_info(triage_second_opinion)").fetchall()}
    if "reviewed_at" not in cols:
        print(
            "[INFO] second_opinion_findings_stale: 判定できません"
            "（triage_second_opinion に reviewed_at 列がまだありません）",
            file=sys.stderr,
        )
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    # 3ゲート審査で DROP と判定された所見はレビュー対象に数えない（既定の表示から
    # 外れているものを「読まれていない」と責めるのは筋が違う）。**未審査（NULL）は
    # 数える** — 審査が動いていない状態を滞留として検出したいため。
    # gate_verdict 列がまだ無い pm.db でも動くよう、列の有無で条件を切り替える。
    gate_cond = " AND (gate_verdict IS NULL OR gate_verdict != 'DROP')" \
        if "gate_verdict" in cols else ""
    rows = pm_conn.execute(
        "SELECT id, ts, kind FROM triage_second_opinion"
        " WHERE kind LIKE 'minutes_extraction%' AND reviewed_at IS NULL AND ts < ?"
        + gate_cond
        + " ORDER BY ts ASC",
        (cutoff,),
    ).fetchall()
    rows = [r for r in rows if not r["kind"].endswith(_SECOND_OPINION_EXEMPT_KIND_SUFFIXES)]
    if not rows:
        return []
    return [{
        "check": "second_opinion_findings_stale",
        "count": len(rows),
        "oldest_ts": rows[0]["ts"],
        "note": (
            f"{days}日以上未レビューの第2系統・K3recallの所見が滞留しています。"
            "読まれない所見が溜まるのは、検査が動いていないのと同じである"
        ),
    }]


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
    model_pin_enabled: bool = True,
    anchor_path: Path | None = None,
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
    # anchor_consistency / pending_egress_stale は tool_call_chain と同じく
    # logs_dir / --no-security-checks とは無関係な pm.db（＋アンカーファイル）
    # 単独の検査であり、無条件に実行する。
    violations += check_anchor_consistency(pm_conn, anchor_path or _DEFAULT_ANCHOR_PATH)
    violations += check_anchor_pushed()
    violations += check_pending_egress_stale(pm_conn)
    violations += check_second_opinion_findings_stale(pm_conn)
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
        # egress_dest_unknown も pm.db だけで完結し logs_dir とは無関係
        # （silent_control と同じ扱い）。観測であり違反ではないため main() 側で
        # 常に exit code から除外する。
        violations += check_egress_dest_unknown(pm_conn, silence_days)
    # model_pin_drift も logs_dir / pm.db とは無関係（silent_control と同じ扱い）。
    # ネットワークに出る検査のため、--skip-model-pin で個別に退避できるようにする。
    if security_checks_enabled and model_pin_enabled:
        violations += check_model_pin_drift()
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
    parser.add_argument(
        "--skip-model-pin", action="store_true",
        help="model_pin_drift だけをスキップする（ネットワークに出たくない場合の退避路）",
    )
    parser.add_argument(
        "--emit-anchor", action="store_true",
        help="通常の検査を行わず、tool_calls の連鎖の頭を外部アンカーファイルへ1行追記する"
             "（既存の検査とは独立した動作モード。§4.4。詳細は emit_anchor() の docstring）",
    )
    parser.add_argument(
        "--anchor-path", default=None, metavar="PATH",
        help="外部アンカーファイルのパス（既定: config/anchors/tool_call_anchor.jsonl）",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.db, REPO_ROOT / "data" / "pm.db")
    state_db_path = (
        Path(args.state_db) if args.state_db else REPO_ROOT / "data" / "patrol_state.db"
    )
    anchor_path = Path(args.anchor_path) if args.anchor_path else _DEFAULT_ANCHOR_PATH

    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        return 1

    pm_conn = open_db(db_path, encrypt=not args.no_encrypt)
    pm_conn.execute("PRAGMA query_only = ON")

    if args.emit_anchor:
        record = emit_anchor(pm_conn, anchor_path)
        pm_conn.close()
        if record is None:
            print(
                "OK: アンカーは追記しませんでした"
                "（tool_calls が空、または直前のアンカーと entry_hash が同じです）"
            )
        else:
            print(
                f"OK: アンカーを追記しました rows={record['rows']}"
                f" entry_hash={record['entry_hash']} -> {anchor_path}"
            )
        return 0

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
        args.silence_days, security_checks_enabled, not args.skip_model_pin,
        anchor_path,
    )

    pm_conn.close()
    if state_conn is not None:
        state_conn.close()

    # silent_control は既定では警告扱い（沈黙 = 異常と決めつけない。運用を止めて
    # いれば沈黙は正常なので、誤警報で監視全体が無効化される方向に圧力がかかる
    # のを避ける）。--silence-strict を付けたときだけ他の検査と同じ exit 1 対象。
    #
    # egress_dest_unknown は観測であり違反ではない（層3は warn 段階）。
    # --silence-strict を付けても exit code には一切影響させない。
    silent_violations = [v for v in violations if v["check"] == "silent_control"]
    egress_dest_violations = [v for v in violations if v["check"] == "egress_dest_unknown"]
    hard_violations = [
        v for v in violations
        if v["check"] not in ("silent_control", "egress_dest_unknown")
    ]
    exit_violations = hard_violations + (silent_violations if args.silence_strict else [])

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
                extra = []
                if silent_violations:
                    extra.append(f"{len(silent_violations)} 件の沈黙警告")
                if egress_dest_violations:
                    extra.append(
                        f"{len(egress_dest_violations)} 件の宛先未知観測"
                        "（enforceへ進める判断材料、違反ではない）"
                    )
                if extra:
                    print(
                        f"OK: 違反なし。ただし {'、'.join(extra)} があります"
                        f"（--silence-strict は沈黙警告のみを違反として扱います）"
                        f"（--days {args.days}, today={today}）"
                    )
                else:
                    print(f"OK: 違反なし（--days {args.days}, today={today}）")
            for check_name, items in sorted(by_check.items()):
                print(f"\n[{check_name}] {len(items)} 件")
                for item in items:
                    detail = {k: v for k, v in item.items() if k != "check"}
                    print(f"  {detail}")

    return 1 if exit_violations else 0


if __name__ == "__main__":
    sys.exit(main())
