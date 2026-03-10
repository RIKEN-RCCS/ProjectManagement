#!/usr/bin/env python3
"""
pm_relink.py

アクションアイテムとマイルストーンの紐づけをCSVを介して一括編集する。
LLMは使用しない。

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
"""

import argparse
import csv
import io
import sys
from pathlib import Path

# scripts/ ディレクトリをパスに追加
sys.path.insert(0, str(Path(__file__).parent))
from db_utils import open_db


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
        SELECT a.id, a.assignee, a.due_date, a.milestone_id, a.content
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

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "assignee", "due_date", "milestone_id", "content"])
    for item in items:
        writer.writerow([
            item["id"],
            item["assignee"] or "",
            item["due_date"] or "",
            item["milestone_id"] or "",
            item["content"] or "",
        ])
    lines.append(buf.getvalue().rstrip("\n"))

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")

    label = "全件" if all_items else "milestone_id IS NULL のみ"
    print(f"[INFO] {len(items)} 件をエクスポートしました（{label}）: {output_path}")
    print(f"[INFO] milestone_id 列を編集後、--import で反映してください")


# --------------------------------------------------------------------------- #
# インポート
# --------------------------------------------------------------------------- #

def cmd_import(conn, csv_path: Path, dry_run: bool):
    if not csv_path.exists():
        print(f"[ERROR] ファイルが見つかりません: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # コメント行を除いてCSVをパース
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    data_lines = [l for l in lines if not l.startswith("#")]
    reader = csv.DictReader(data_lines)

    updates: list[tuple[str | None, int]] = []  # (new_milestone_id, id)
    skipped = 0

    for row in reader:
        try:
            item_id = int(row["id"])
        except (KeyError, ValueError):
            print(f"[WARN] id が不正な行をスキップ: {row}", file=sys.stderr)
            skipped += 1
            continue

        new_mid = row.get("milestone_id", "").strip() or None
        updates.append((new_mid, item_id))

    if not updates:
        print("[INFO] 更新対象なし")
        return

    # 現在値を取得して差分を表示
    placeholders = ",".join("?" * len(updates))
    ids = [u[1] for u in updates]
    current = {
        r["id"]: r["milestone_id"]
        for r in conn.execute(
            f"SELECT id, milestone_id FROM action_items WHERE id IN ({placeholders})", ids
        ).fetchall()
    }

    changed = [(new_mid, item_id) for new_mid, item_id in updates
               if current.get(item_id) != new_mid]
    unchanged = len(updates) - len(changed)

    if not changed:
        print(f"[INFO] 変更なし（{unchanged} 件すべて現在値と同一）")
        return

    print(f"[INFO] 変更: {len(changed)} 件 / 変更なし: {unchanged} 件 / スキップ: {skipped} 件")
    print()

    # 変更内容プレビュー
    for new_mid, item_id in changed:
        old = current.get(item_id) or "NULL"
        new = new_mid or "NULL"
        print(f"  ID:{item_id:4d}  {old:6s} → {new}")

    if dry_run:
        print()
        print("[DRY-RUN] DB は更新されていません")
        return

    # 確認プロンプト
    print()
    ans = input(f"上記 {len(changed)} 件を更新しますか？ [y/N]: ").strip().lower()
    if ans != "y":
        print("[INFO] キャンセルしました")
        return

    for new_mid, item_id in changed:
        conn.execute(
            "UPDATE action_items SET milestone_id = ? WHERE id = ?",
            (new_mid, item_id),
        )
    conn.commit()
    print(f"[OK] {len(changed)} 件を更新しました")


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="アクションアイテムとマイルストーンの紐づけをCSV経由で一括編集する")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="CSVにエクスポート")
    group.add_argument("--import", dest="import_path", metavar="PATH", help="CSVを読み込んでDBを更新")

    parser.add_argument("--all", action="store_true", help="--export 時に全件対象（デフォルトは NULL のみ）")
    parser.add_argument("--output", default="relink.csv", metavar="PATH", help="--export 時の出力ファイルパス（デフォルト: relink.csv）")
    parser.add_argument("--db", default="data/pm.db", metavar="PATH", help="pm.db のパス（デフォルト: data/pm.db）")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード（暗号化なし）")
    parser.add_argument("--dry-run", action="store_true", help="DB更新なし・変更内容を表示のみ")

    args = parser.parse_args()

    conn = open_db(args.db, encrypt=not args.no_encrypt)

    if args.export:
        cmd_export(conn, args.all, Path(args.output))
    else:
        cmd_import(conn, Path(args.import_path), args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
