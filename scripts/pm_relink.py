#!/usr/bin/env python3
"""
pm_relink.py

アクションアイテムの各フィールドをCSVを介して一括編集する。
LLMは使用しない。

編集可能なフィールド: assignee / due_date / milestone_id / content / status

Usage:
    # 未紐づけ（milestone_id IS NULL）のアイテムをCSVにエクスポート
    python3 scripts/pm_relink.py --export

    # 全アイテムをエクスポート
    python3 scripts/pm_relink.py --export --all

    # 編集済みCSVをDBに反映（空欄 = NULL で上書き）
    python3 scripts/pm_relink.py --import relink.csv

    # 反映内容を確認のみ（DB更新なし）
    python3 scripts/pm_relink.py --import relink.csv --dry-run

Options:
    --export            アクションアイテムをCSVにエクスポート
    --import PATH       CSVを読み込んでDBを更新
    --all               --export 時に全件対象（デフォルトは milestone_id IS NULL のみ）
    --output PATH       --export 時の出力ファイルパス（デフォルト: relink.csv）
    --db PATH           pm.db のパス（デフォルト: data/pm.db）
    --no-encrypt        平文モード（暗号化なし）
    --dry-run           DB更新なし・変更内容を表示のみ

各列のNULL扱い:
    assignee     空欄 → NULL（担当者なし）
    due_date     空欄 → NULL（期限なし）
    milestone_id 空欄 → NULL（紐づけ解除）
    content      空欄の場合はスキップ（内容を空にはできない）
    status       空欄の場合はスキップ。'open' または 'closed' を推奨
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

# 編集可能なフィールド一覧
EDITABLE_FIELDS = ["assignee", "due_date", "milestone_id", "content", "status"]
# 空欄をNULLとして扱うフィールド（content/statusは空欄→スキップ）
NULLABLE_FIELDS = {"assignee", "due_date", "milestone_id"}

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


def write_audit_log(conn, record_id: int, field: str, old_value, new_value, source: str) -> None:
    """変更前の値を audit_log に記録する（dry_run 時は呼ばない）"""
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES ('action_items', ?, ?, ?, ?, ?, ?)",
        (
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
    rows = conn.execute(
        "SELECT milestone_id, name, due_date FROM milestones ORDER BY due_date"
    ).fetchall()
    return [dict(r) for r in rows]


def milestone_header(milestones: list[dict]) -> str:
    """CSVの先頭コメント行用にマイルストーン一覧を文字列化する"""
    if not milestones:
        return "# Milestones: (未登録)"
    parts = [
        f"{m['milestone_id']}={m['name']}({m.get('due_date') or '未定'})"
        for m in milestones
    ]
    return "# Milestones: " + " / ".join(parts)


def fetch_action_items(conn, all_items: bool) -> list[dict]:
    where = "" if all_items else "WHERE a.milestone_id IS NULL"
    rows = conn.execute(f"""
        SELECT a.id, a.assignee, a.due_date, a.milestone_id, a.status, a.content
        FROM action_items a
        {where}
        ORDER BY a.due_date IS NULL, a.due_date, a.id
    """).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# エクスポート
# --------------------------------------------------------------------------- #

def cmd_export(conn, all_items: bool, output_path: Path):
    milestones = fetch_milestones(conn)
    items = fetch_action_items(conn, all_items)

    if not items:
        label = "全件" if all_items else "milestone_id IS NULL"
        print(f"[INFO] 対象アイテムなし（{label}）")
        return

    lines = []
    lines.append(milestone_header(milestones))
    lines.append("# 編集可能な列: assignee / due_date / milestone_id / content / status")
    lines.append("# assignee / due_date / milestone_id は空欄 → NULL（解除）")
    lines.append("# content / status は空欄の場合スキップ（変更なし）")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "assignee", "due_date", "milestone_id", "status", "content"])
    for item in items:
        writer.writerow([
            item["id"],
            item["assignee"] or "",
            item["due_date"] or "",
            item["milestone_id"] or "",
            item["status"] or "",
            item["content"] or "",
        ])
    lines.append(buf.getvalue().rstrip("\n"))

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")

    label = "全件" if all_items else "milestone_id IS NULL のみ"
    print(f"[INFO] {len(items)} 件をエクスポートしました（{label}）: {output_path}")
    print(f"[INFO] 各列を編集後、--import で反映してください")


# --------------------------------------------------------------------------- #
# インポート
# --------------------------------------------------------------------------- #

def parse_row_values(row: dict) -> dict[str, str | None]:
    """CSVの1行から更新すべきフィールドと値を返す（スキップは含めない）"""
    result = {}
    for field in EDITABLE_FIELDS:
        if field not in row:
            continue
        raw = row[field].strip()
        if field in NULLABLE_FIELDS:
            result[field] = raw or None  # 空文字 → None
        else:
            # content / status: 空欄はスキップ（変更なし）
            if raw:
                result[field] = raw
    return result


def cmd_import(conn, csv_path: Path, dry_run: bool):
    if not csv_path.exists():
        print(f"[ERROR] ファイルが見つかりません: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # コメント行を除いてCSVをパース
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    data_lines = [l for l in lines if not l.startswith("#")]
    reader = csv.DictReader(data_lines)

    csv_rows: dict[int, dict[str, str | None]] = {}
    skipped = 0

    for row in reader:
        try:
            item_id = int(row["id"])
        except (KeyError, ValueError):
            print(f"[WARN] id が不正な行をスキップ: {row}", file=sys.stderr)
            skipped += 1
            continue
        csv_rows[item_id] = parse_row_values(row)

    if not csv_rows:
        print("[INFO] 更新対象なし")
        return

    # 現在値をDBから取得
    placeholders = ",".join("?" * len(csv_rows))
    current: dict[int, dict] = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT id, assignee, due_date, milestone_id, content, status"
            f" FROM action_items WHERE id IN ({placeholders})",
            list(csv_rows.keys()),
        ).fetchall()
    }

    # 差分を収集: list of (item_id, field, old_value, new_value)
    changes: list[tuple[int, str, any, any]] = []
    for item_id, new_values in csv_rows.items():
        if item_id not in current:
            print(f"[WARN] ID {item_id} はDBに存在しません。スキップします。", file=sys.stderr)
            skipped += 1
            continue
        cur = current[item_id]
        for field, new_val in new_values.items():
            old_val = cur.get(field)
            if old_val != new_val:
                changes.append((item_id, field, old_val, new_val))

    if not changes:
        print(f"[INFO] 変更なし（{len(csv_rows)} 件すべて現在値と同一）")
        return

    changed_items = len({item_id for item_id, _, _, _ in changes})
    skipped_msg = f" / スキップ: {skipped} 件" if skipped else ""
    print(f"[INFO] 変更: {len(changes)} フィールド / {changed_items} アイテム{skipped_msg}")
    print()

    # 変更内容プレビュー（アイテムIDごとにグループ表示）
    by_item: dict[int, list] = defaultdict(list)
    for item_id, field, old_val, new_val in changes:
        by_item[item_id].append((field, old_val, new_val))

    for item_id in sorted(by_item):
        print(f"  ID:{item_id:4d}")
        for field, old_val, new_val in by_item[item_id]:
            old_str = str(old_val) if old_val is not None else "NULL"
            new_str = str(new_val) if new_val is not None else "NULL"
            print(f"    {field:<14}: {old_str} → {new_str}")

    if dry_run:
        print()
        print("[DRY-RUN] DB は更新されていません")
        return

    print()
    ans = input(
        f"上記 {changed_items} アイテム（{len(changes)} フィールド）を更新しますか？ [y/N]: "
    ).strip().lower()
    if ans != "y":
        print("[INFO] キャンセルしました")
        return

    # audit_log に記録してからアイテムごとにまとめてUPDATE
    for item_id, field, old_val, new_val in changes:
        write_audit_log(conn, item_id, field, old_val, new_val, "relink")

    for item_id, field_changes in by_item.items():
        set_clause = ", ".join(f"{field} = ?" for field, _, _ in field_changes)
        values = [new_val for _, _, new_val in field_changes] + [item_id]
        conn.execute(f"UPDATE action_items SET {set_clause} WHERE id = ?", values)

    conn.commit()
    print(f"[OK] {changed_items} アイテム（{len(changes)} フィールド）を更新しました")


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="アクションアイテムの各フィールドをCSV経由で一括編集する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="CSVにエクスポート")
    group.add_argument("--import", dest="import_path", metavar="PATH", help="CSVを読み込んでDBを更新")

    parser.add_argument("--all", action="store_true",
                        help="--export 時に全件対象（デフォルトは milestone_id IS NULL のみ）")
    parser.add_argument("--output", default="relink.csv", metavar="PATH",
                        help="--export 時の出力ファイルパス（デフォルト: relink.csv）")
    parser.add_argument("--db", default="data/pm.db", metavar="PATH",
                        help="pm.db のパス（デフォルト: data/pm.db）")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード（暗号化なし）")
    parser.add_argument("--dry-run", action="store_true", help="DB更新なし・変更内容を表示のみ")

    args = parser.parse_args()

    conn = open_db(args.db, encrypt=not args.no_encrypt, migrations=[_AUDIT_LOG_DDL])

    if args.export:
        cmd_export(conn, args.all, Path(args.output))
    else:
        cmd_import(conn, Path(args.import_path), args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
