#!/usr/bin/env python3
"""
pm_meeting_bulk_import.py

meetings/ ディレクトリ内の議事録ファイルを一括で pm.db に登録する。

ファイル名形式: YYYY-MM-DD_{会議名}.md
  - YYYY-MM-DD  → --held-at
  - {会議名}    → --meeting-name
  - _parsed.md で終わるファイルは対象外

Usage:
    python3 scripts/pm_meeting_bulk_import.py
    python3 scripts/pm_meeting_bulk_import.py --meetings-dir meetings/
    python3 scripts/pm_meeting_bulk_import.py --dry-run
    python3 scripts/pm_meeting_bulk_import.py --force
    python3 scripts/pm_meeting_bulk_import.py --since 2026-01-01
    python3 scripts/pm_meeting_bulk_import.py --list
    python3 scripts/pm_meeting_bulk_import.py --list --since 2026-02-01
    python3 scripts/pm_meeting_bulk_import.py --delete 2026-03-02_Leader_Meeting
    python3 scripts/pm_meeting_bulk_import.py --delete 2026-03-02_Leader_Meeting --dry-run

Options:
    --meetings-dir DIR      議事録ディレクトリ（デフォルト: meetings/）
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --since YYYY-MM-DD      この日付以降のファイルのみ対象
    --force                 既存レコードを上書き（--delete と組み合わせると確認プロンプトをスキップ）
    --dry-run               pm_meeting_import.py を実行せず対象ファイルを表示のみ
                            （--delete と組み合わせると削除内容を表示するのみ）
    --no-encrypt            DBを暗号化しない（平文モード）
    --list                  pm.db にインポート済みの議事録一覧を表示して終了
    --delete MEETING_ID     指定した meeting_id の議事録をDBから削除する
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEETINGS_DIR = REPO_ROOT / "meetings"
DEFAULT_DB = REPO_ROOT / "data" / "pm.db"


def parse_filename(path: Path) -> tuple[str, str] | None:
    """
    ファイル名から (held_at, meeting_name) を抽出する。
    パースできない場合は None を返す。
    """
    name = path.stem  # 拡張子なし
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", name)
    if not m:
        return None
    return m.group(1), m.group(2)


def collect_files(meetings_dir: Path, since: str | None) -> list[Path]:
    """対象ファイルを収集してソートして返す"""
    files = []
    for p in sorted(meetings_dir.glob("*.md")):
        if p.name.endswith("_parsed.md"):
            continue
        parsed = parse_filename(p)
        if parsed is None:
            print(f"[SKIP] ファイル名の形式が不正: {p.name}")
            continue
        held_at, _ = parsed
        if since and held_at < since:
            continue
        files.append(p)
    return files


def run_meeting_parser(
    file_path: Path,
    held_at: str,
    meeting_name: str,
    db_path: Path,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
) -> bool:
    """
    pm_meeting_import.py を呼び出す。
    戻り値: 成功したら True
    """
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "pm_meeting_import.py"),
        str(file_path),
        "--held-at", held_at,
        "--meeting-name", meeting_name,
        "--db", str(db_path),
    ]
    if force:
        cmd.append("--force")
    if dry_run:
        cmd.append("--dry-run")
    if no_encrypt:
        cmd.append("--no-encrypt")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            print(f"    {line}")
    if result.returncode != 0:
        print(f"  [ERROR] 終了コード {result.returncode}")
        if result.stderr:
            for line in result.stderr.splitlines():
                print(f"    STDERR: {line}")
        return False
    return True


def _open_db_or_exit(db_path: Path, no_encrypt: bool):
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


def list_imported(db_path: Path, since: str | None, no_encrypt: bool) -> None:
    """pm.db にインポート済みの議事録一覧を表示する"""
    conn = _open_db_or_exit(db_path, no_encrypt)

    query = """
        SELECT
            m.meeting_id,
            m.held_at,
            m.kind,
            m.file_path,
            m.parsed_at,
            COUNT(DISTINCT a.id) AS action_items,
            COUNT(DISTINCT d.id) AS decisions
        FROM meetings m
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id
        LEFT JOIN decisions d    ON d.meeting_id = m.meeting_id
    """
    params: list = []
    if since:
        query += " WHERE m.held_at >= ?"
        params.append(since)
    query += " GROUP BY m.meeting_id ORDER BY m.held_at DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("インポート済み議事録はありません。")
        return

    print(f"{'開催日':<12} {'AI':>3} {'決定':>3}  {'登録日時':<20}  {'meeting_id'}")
    print("-" * 90)
    for r in rows:
        held_at    = r["held_at"]    or ""
        parsed_at  = (r["parsed_at"] or "")[:19]
        meeting_id = r["meeting_id"] or ""
        ai_count   = r["action_items"]
        d_count    = r["decisions"]
        print(f"{held_at:<12} {ai_count:>3} {d_count:>3}  {parsed_at:<20}  {meeting_id}")

    print(f"\n合計: {len(rows)} 件")
    print("\n※ 削除は: python3 scripts/pm_meeting_bulk_import.py --delete <meeting_id>")


def delete_meeting(
    db_path: Path, meeting_id: str, dry_run: bool, force: bool, no_encrypt: bool
) -> None:
    """指定した meeting_id の議事録を pm.db から削除する"""
    conn = _open_db_or_exit(db_path, no_encrypt)

    row = conn.execute(
        """
        SELECT m.meeting_id, m.held_at, m.kind, m.file_path,
               COUNT(DISTINCT a.id) AS ai_count,
               COUNT(DISTINCT d.id) AS d_count
        FROM meetings m
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id
        LEFT JOIN decisions d    ON d.meeting_id = m.meeting_id
        WHERE m.meeting_id = ?
        GROUP BY m.meeting_id
        """,
        (meeting_id,),
    ).fetchone()

    if not row:
        print(f"ERROR: meeting_id '{meeting_id}' は pm.db に存在しません。", file=sys.stderr)
        print("  --list で一覧を確認してください。", file=sys.stderr)
        conn.close()
        sys.exit(1)

    print(f"削除対象:")
    print(f"  meeting_id : {row['meeting_id']}")
    print(f"  開催日     : {row['held_at']}")
    print(f"  会議種別   : {row['kind']}")
    print(f"  ファイル   : {row['file_path']}")
    print(f"  アクションアイテム: {row['ai_count']} 件")
    print(f"  決定事項          : {row['d_count']} 件")

    if dry_run:
        print("\n[INFO] --dry-run のため削除をスキップしました")
        conn.close()
        return

    if not force:
        answer = input("\n本当に削除しますか？ [y/N]: ").strip().lower()
        if answer != "y":
            print("削除をキャンセルしました。")
            conn.close()
            return

    conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM decisions    WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meetings     WHERE meeting_id = ?", (meeting_id,))
    conn.commit()
    conn.close()

    print(f"\n✓ meeting_id '{meeting_id}' を削除しました。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="meetings/ の議事録を一括で pm.db に登録する"
    )
    parser.add_argument("--meetings-dir", default=None, help="議事録ディレクトリ")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--since", default=None, help="この日付以降のみ対象 (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    parser.add_argument("--dry-run", action="store_true", help="実行内容を表示するのみ（DB保存なし）")
    parser.add_argument("--no-encrypt", action="store_true", help="DBを暗号化しない（平文モード）")
    parser.add_argument("--list", action="store_true", help="インポート済み議事録一覧を表示して終了")
    parser.add_argument("--delete", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id の議事録をDBから削除する")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_DB

    if args.list:
        list_imported(db_path, args.since, args.no_encrypt)
        return

    if args.delete:
        delete_meeting(db_path, args.delete, args.dry_run, args.force, args.no_encrypt)
        return

    meetings_dir = Path(args.meetings_dir) if args.meetings_dir else DEFAULT_MEETINGS_DIR

    if not meetings_dir.exists():
        print(f"ERROR: ディレクトリが見つかりません: {meetings_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 議事録ディレクトリ: {meetings_dir}")
    print(f"[INFO] pm.db            : {db_path}")
    if args.since:
        print(f"[INFO] since            : {args.since}")
    if args.dry_run:
        print("[INFO] --dry-run モード（DB保存なし）")

    files = collect_files(meetings_dir, args.since)
    print(f"[INFO] 対象ファイル     : {len(files)} 件\n")

    if not files:
        print("対象ファイルなし。終了します。")
        return

    # 既にDBに登録済みの meeting_id を取得（DB が存在する場合のみ）
    imported_ids: set[str] = set()
    if db_path.exists():
        try:
            conn = open_db(db_path, encrypt=not args.no_encrypt)
            rows = conn.execute("SELECT meeting_id FROM meetings").fetchall()
            imported_ids = {r["meeting_id"] for r in rows}
            conn.close()
        except Exception:
            pass  # DB が壊れている等の場合は全件処理にフォールバック

    success = skipped = failed = 0

    for i, file_path in enumerate(files, 1):
        held_at, meeting_name = parse_filename(file_path)
        meeting_id = file_path.stem
        print(f"[{i}/{len(files)}] {file_path.name}")
        print(f"  held_at      : {held_at}")
        print(f"  meeting_name : {meeting_name}")

        if meeting_id in imported_ids and not args.force:
            print(f"  [SKIP] 既にDBに登録済み（--force で上書き可能）")
            skipped += 1
            print()
            continue

        ok = run_meeting_parser(
            file_path, held_at, meeting_name, db_path,
            force=args.force, dry_run=args.dry_run, no_encrypt=args.no_encrypt,
        )
        if ok:
            success += 1
        else:
            failed += 1
        print()

    print(f"完了: 処理={success}件, スキップ={skipped}件, 失敗={failed}件")


if __name__ == "__main__":
    main()
