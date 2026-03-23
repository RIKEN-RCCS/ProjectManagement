#!/usr/bin/env python3
"""
pm_relink.py

アクションアイテムと決定事項をCSVを介して一括編集する。
LLMは使用しない。

1ファイルに2セクション（アクションアイテム / 決定事項）を出力・入力する。

Usage:
    # 未紐づけアイテム + 全決定事項をCSVにエクスポート
    python3 scripts/pm_relink.py --export

    # 全アイテムをエクスポート
    python3 scripts/pm_relink.py --export --all

    # 編集済みCSVをDBに反映
    python3 scripts/pm_relink.py --import relink.csv

    # 反映内容を確認のみ（DB更新なし）
    python3 scripts/pm_relink.py --import relink.csv --dry-run

    # 一覧表示
    python3 scripts/pm_relink.py --list

Options:
    --export            アクションアイテム + 決定事項をCSVにエクスポート
    --import PATH       CSVを読み込んでDBを更新
    --list              一覧表示して終了
    --all               --export / --list 時に全アイテム対象（デフォルトは milestone_id IS NULL のみ）
    --output PATH       --export 時の出力ファイルパス（デフォルト: relink.csv）
    --db PATH           pm.db のパス（デフォルト: data/pm.db）
    --no-encrypt        平文モード（暗号化なし）
    --dry-run           DB更新なし・変更内容を表示のみ

アクションアイテムの編集可能列:
    assignee     空欄 → NULL（担当者なし）
    due_date     空欄 → NULL（期限なし）
    milestone_id 空欄 → NULL（紐づけ解除）
    content      空欄の場合はスキップ（変更なし）
    status       空欄の場合はスキップ。'open' または 'closed' を推奨

決定事項の編集可能列:
    content      空欄の場合はスキップ（変更なし）
    decided_at   空欄の場合はスキップ（変更なし）
"""

import argparse
import csv
import io
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import open_db
from cli_utils import add_no_encrypt_arg, add_dry_run_arg, add_since_arg

# アクションアイテムの編集可能フィールド
AI_EDITABLE_FIELDS = ["assignee", "due_date", "milestone_id", "content", "status"]
AI_NULLABLE_FIELDS = {"assignee", "due_date", "milestone_id"}

# 決定事項の編集可能フィールド（空欄はスキップ）
DEC_EDITABLE_FIELDS = ["content", "decided_at"]

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

_SECTION_ACTIONS   = "# === アクションアイテム ==="
_SECTION_DECISIONS = "# === 決定事項 ==="


def write_audit_log(conn, table_name: str, record_id: int, field: str,
                    old_value, new_value, source: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            table_name,
            str(record_id),
            field,
            str(old_value) if old_value is not None else None,
            str(new_value) if new_value is not None else None,
            datetime.now(timezone.utc).isoformat(),
            source,
        ),
    )


# --------------------------------------------------------------------------- #
# ヘルパー
# --------------------------------------------------------------------------- #

def fetch_milestones(conn) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='milestones'"
    ).fetchone()
    if not exists:
        return []
    rows = conn.execute(
        "SELECT milestone_id, name, due_date FROM milestones ORDER BY due_date"
    ).fetchall()
    return [dict(r) for r in rows]


def milestone_header(milestones: list[dict]) -> str:
    if not milestones:
        return "# Milestones: (未登録)"
    parts = [
        f"{m['milestone_id']}={m['name']}({m.get('due_date') or '未定'})"
        for m in milestones
    ]
    return "# Milestones: " + " / ".join(parts)


def format_ai_source(a: dict) -> str:
    if a.get("source") == "meeting":
        kind = a.get("meeting_kind") or ""
        held = a.get("meeting_held_at") or ""
        return f"{kind} ({held})" if held else kind
    ref = a.get("source_ref") or ""
    return ref if ref else "Slack"


def format_dec_source(d: dict) -> str:
    if d.get("source") == "meeting":
        return d.get("source_ref") or "meeting"
    ref = d.get("source_ref") or ""
    return ref if ref else "Slack"


def fetch_action_items(conn, all_items: bool, since: str | None = None) -> list[dict]:
    conds, params = [], []
    if not all_items:
        conds.append("a.milestone_id IS NULL")
    if since:
        conds.append("a.extracted_at >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"""
        SELECT a.id, a.assignee, a.due_date, a.milestone_id, a.status, a.content,
               a.note, a.source, a.source_ref, m.kind AS meeting_kind, m.held_at AS meeting_held_at
        FROM action_items a
        LEFT JOIN meetings m ON a.meeting_id = m.meeting_id
        {where}
        ORDER BY a.due_date IS NULL, a.due_date, a.id
    """, params).fetchall()
    return [dict(r) for r in rows]


def fetch_decisions(conn, since: str | None = None) -> list[dict]:
    conds, params = [], []
    if since:
        conds.append("extracted_at >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"""
        SELECT id, content, decided_at, source, source_ref
        FROM decisions
        {where}
        ORDER BY decided_at IS NULL, decided_at, id
    """, params).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# エクスポート
# --------------------------------------------------------------------------- #

def cmd_export(conn, all_items: bool, output_path: Path, since: str | None = None):
    milestones = fetch_milestones(conn)
    items = fetch_action_items(conn, all_items, since=since)
    decisions = fetch_decisions(conn, since=since)

    lines = []
    lines.append(milestone_header(milestones))
    lines.append("#")

    # --- アクションアイテムセクション ---
    lines.append(_SECTION_ACTIONS)
    lines.append("# 編集可能: assignee / due_date / milestone_id / content / status")
    lines.append("# assignee / due_date / milestone_id は空欄 → NULL（解除）")
    lines.append("# content / status は空欄の場合スキップ（変更なし）")
    lines.append("# source / note は参照用（読み取り専用）")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "assignee", "due_date", "milestone_id", "status", "content", "source", "note"])
    for item in items:
        writer.writerow([
            item["id"],
            item["assignee"] or "",
            item["due_date"] or "",
            item["milestone_id"] or "",
            item["status"] or "",
            item["content"] or "",
            format_ai_source(item),
            item["note"] or "",
        ])
    lines.append(buf.getvalue().rstrip("\n"))

    # --- 決定事項セクション ---
    lines.append("")
    lines.append(_SECTION_DECISIONS)
    lines.append("# 編集可能: content / decided_at（空欄はスキップ）")
    lines.append("# source は参照用（読み取り専用）")

    buf2 = io.StringIO()
    writer2 = csv.writer(buf2, lineterminator="\n")
    writer2.writerow(["id", "content", "decided_at", "source"])
    for d in decisions:
        writer2.writerow([
            d["id"],
            d["content"] or "",
            d["decided_at"] or "",
            format_dec_source(d),
        ])
    lines.append(buf2.getvalue().rstrip("\n"))

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")

    label = "全件" if all_items else "milestone_id IS NULL のみ"
    since_msg = f", since={since}" if since else ""
    print(f"[INFO] アクションアイテム {len(items)} 件 + 決定事項 {len(decisions)} 件をエクスポートしました（{label}{since_msg}）: {output_path}")
    print(f"[INFO] 各列を編集後、--import で反映してください")


# --------------------------------------------------------------------------- #
# インポート
# --------------------------------------------------------------------------- #

def _split_sections(text: str) -> tuple[list[str], list[str]]:
    """ファイルテキストをアクションアイテム行と決定事項行に分割する"""
    action_lines: list[str] = []
    decision_lines: list[str] = []
    current = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _SECTION_ACTIONS:
            current = "actions"
            continue
        if stripped == _SECTION_DECISIONS:
            current = "decisions"
            continue
        if current == "actions":
            action_lines.append(line)
        elif current == "decisions":
            decision_lines.append(line)

    return action_lines, decision_lines


def _parse_action_rows(lines: list[str]) -> dict[int, dict[str, str | None]]:
    data_lines = [l for l in lines if not l.startswith("#") and l.strip()]
    reader = csv.DictReader(data_lines)
    result: dict[int, dict[str, str | None]] = {}
    for row in reader:
        try:
            item_id = int(row["id"])
        except (KeyError, ValueError):
            print(f"[WARN] アクションアイテム: id が不正な行をスキップ: {row}", file=sys.stderr)
            continue
        values: dict[str, str | None] = {}
        for field in AI_EDITABLE_FIELDS:
            if field not in row:
                continue
            raw = row[field].strip()
            if field in AI_NULLABLE_FIELDS:
                values[field] = raw or None
            else:
                if raw:
                    values[field] = raw
        result[item_id] = values
    return result


def _parse_decision_rows(lines: list[str]) -> dict[int, dict[str, str]]:
    data_lines = [l for l in lines if not l.startswith("#") and l.strip()]
    reader = csv.DictReader(data_lines)
    result: dict[int, dict[str, str]] = {}
    for row in reader:
        try:
            dec_id = int(row["id"])
        except (KeyError, ValueError):
            print(f"[WARN] 決定事項: id が不正な行をスキップ: {row}", file=sys.stderr)
            continue
        values: dict[str, str] = {}
        for field in DEC_EDITABLE_FIELDS:
            if field not in row:
                continue
            raw = row[field].strip()
            if raw:  # 空欄はスキップ
                values[field] = raw
        result[dec_id] = values
    return result


def _apply_changes(conn, table: str, csv_rows: dict[int, dict], select_fields: list[str],
                   dry_run: bool) -> tuple[int, int]:
    """差分を計算してDBに反映する。(変更フィールド数, 変更レコード数) を返す"""
    if not csv_rows:
        return 0, 0

    placeholders = ",".join("?" * len(csv_rows))
    field_list = ", ".join(select_fields)
    current: dict[int, dict] = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT id, {field_list} FROM {table} WHERE id IN ({placeholders})",
            list(csv_rows.keys()),
        ).fetchall()
    }

    changes: list[tuple[int, str, any, any]] = []
    for item_id, new_values in csv_rows.items():
        if item_id not in current:
            print(f"[WARN] {table} ID {item_id} はDBに存在しません。スキップします。", file=sys.stderr)
            continue
        cur = current[item_id]
        for field, new_val in new_values.items():
            old_val = cur.get(field)
            if old_val != new_val:
                changes.append((item_id, field, old_val, new_val))

    if not changes:
        return 0, 0

    by_item: dict[int, list] = defaultdict(list)
    for item_id, field, old_val, new_val in changes:
        by_item[item_id].append((field, old_val, new_val))

    label = "アクションアイテム" if table == "action_items" else "決定事項"
    for item_id in sorted(by_item):
        print(f"  [{label}] ID:{item_id:4d}")
        for field, old_val, new_val in by_item[item_id]:
            old_str = str(old_val) if old_val is not None else "NULL"
            new_str = str(new_val) if new_val is not None else "NULL"
            print(f"    {field:<14}: {old_str} → {new_str}")

    if not dry_run:
        for item_id, field, old_val, new_val in changes:
            write_audit_log(conn, table, item_id, field, old_val, new_val, "relink")
        for item_id, field_changes in by_item.items():
            set_clause = ", ".join(f"{field} = ?" for field, _, _ in field_changes)
            values = [new_val for _, _, new_val in field_changes] + [item_id]
            conn.execute(f"UPDATE {table} SET {set_clause} WHERE id = ?", values)

    return len(changes), len(by_item)


def cmd_import(conn, csv_path: Path, dry_run: bool):
    if not csv_path.exists():
        print(f"[ERROR] ファイルが見つかりません: {csv_path}", file=sys.stderr)
        sys.exit(1)

    text = csv_path.read_text(encoding="utf-8")
    action_lines, decision_lines = _split_sections(text)

    ai_rows  = _parse_action_rows(action_lines)
    dec_rows = _parse_decision_rows(decision_lines)

    if not ai_rows and not dec_rows:
        print("[INFO] 更新対象なし")
        return

    total_fields = total_items = 0

    # アクションアイテム
    f, i = _apply_changes(conn, "action_items", ai_rows, AI_EDITABLE_FIELDS, dry_run)
    total_fields += f
    total_items  += i

    # 決定事項
    f, i = _apply_changes(conn, "decisions", dec_rows, DEC_EDITABLE_FIELDS, dry_run)
    total_fields += f
    total_items  += i

    if total_fields == 0:
        print(f"[INFO] 変更なし（現在値と同一）")
        return

    print()
    print(f"[INFO] 変更: {total_fields} フィールド / {total_items} レコード")

    if dry_run:
        print("[DRY-RUN] DB は更新されていません")
        return

    ans = input(f"上記 {total_items} レコード（{total_fields} フィールド）を更新しますか？ [y/N]: ").strip().lower()
    if ans != "y":
        print("[INFO] キャンセルしました")
        return

    conn.commit()
    print(f"[OK] {total_items} レコード（{total_fields} フィールド）を更新しました")


# --------------------------------------------------------------------------- #
# リスト表示
# --------------------------------------------------------------------------- #

def cmd_list(conn, all_items: bool, since: str | None = None):
    items = fetch_action_items(conn, all_items, since=since)
    label = "全件" if all_items else "milestone_id IS NULL のみ"
    since_msg = f"（since={since}）" if since else ""

    print(f"【アクションアイテム】（{label}{since_msg}）")
    print("─" * 120)
    print(f"{'ID':>4}  {'担当者':<12}  {'期限':<12}  {'MS':<4}  {'状況':<6}  {'出典':<28}  {'内容':<40}  対応状況")
    print("-" * 120)
    for item in items:
        print(
            f"{item['id']:>4}  "
            f"{(item['assignee'] or '(未定)')[:12]:<12}  "
            f"{(item['due_date'] or '(なし)')[:12]:<12}  "
            f"{(item['milestone_id'] or '-')[:4]:<4}  "
            f"{(item['status'] or '')[:6]:<6}  "
            f"{format_ai_source(item)[:28]:<28}  "
            f"{(item['content'] or '')[:40]:<40}  "
            f"{(item['note'] or '')[:20]}"
        )
    print(f"\n合計: {len(items)} 件")

    decisions = fetch_decisions(conn, since=since)
    print()
    print(f"【決定事項】{since_msg}")
    print("─" * 120)
    print(f"{'ID':>4}  {'決定日':<12}  {'出典':<30}  {'内容'}")
    print("-" * 120)
    for d in decisions:
        print(
            f"{d['id']:>4}  "
            f"{(d['decided_at'] or '')[:12]:<12}  "
            f"{format_dec_source(d)[:30]:<30}  "
            f"{(d['content'] or '')[:60]}"
        )
    print(f"\n合計: {len(decisions)} 件")


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="アクションアイテムと決定事項をCSV経由で一括編集する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true",
                       help="アクションアイテム + 決定事項をCSVにエクスポート")
    group.add_argument("--import", dest="import_path", metavar="PATH",
                       help="CSVを読み込んでDBを更新")
    group.add_argument("--list", action="store_true",
                       help="アクションアイテムと決定事項を一覧表示")

    parser.add_argument("--all", action="store_true",
                        help="--export / --list 時に全アイテム対象（デフォルトは milestone_id IS NULL のみ）")
    parser.add_argument("--output", default="relink.csv", metavar="PATH",
                        help="--export 時の出力ファイルパス（デフォルト: relink.csv）")
    parser.add_argument("--db", default="data/pm.db", metavar="PATH",
                        help="pm.db のパス（デフォルト: data/pm.db）")
    add_no_encrypt_arg(parser)
    add_dry_run_arg(parser)
    add_since_arg(parser, "（--export / --list 時のフィルタ）")

    args = parser.parse_args()
    conn = open_db(args.db, encrypt=not args.no_encrypt, migrations=[_AUDIT_LOG_DDL])

    if args.export:
        cmd_export(conn, args.all, Path(args.output), since=args.since)
    elif args.list:
        cmd_list(conn, args.all, since=args.since)
    else:
        cmd_import(conn, Path(args.import_path), args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
