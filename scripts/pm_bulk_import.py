#!/usr/bin/env python3
"""
pm_bulk_import.py

meetings/ ディレクトリ内の議事録ファイルを一括で pm.db に登録する。

ファイル名形式: YYYY-MM-DD_{会議名}.md
  - YYYY-MM-DD  → --held-at
  - {会議名}    → --meeting-name
  - _parsed.md で終わるファイルは対象外

Usage:
    python3 scripts/pm_bulk_import.py
    python3 scripts/pm_bulk_import.py --meetings-dir meetings/
    python3 scripts/pm_bulk_import.py --dry-run
    python3 scripts/pm_bulk_import.py --force
    python3 scripts/pm_bulk_import.py --since 2026-01-01

Options:
    --meetings-dir DIR      議事録ディレクトリ（デフォルト: meetings/）
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --since YYYY-MM-DD      この日付以降のファイルのみ対象
    --force                 既存レコードを上書き
    --dry-run               meeting_parser.py を実行せず対象ファイルを表示のみ
    --no-encrypt            DBを暗号化しない（平文モード）
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

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
    meeting_parser.py を呼び出す。
    戻り値: 成功したら True
    """
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "meeting_parser.py"),
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
    args = parser.parse_args()

    meetings_dir = Path(args.meetings_dir) if args.meetings_dir else DEFAULT_MEETINGS_DIR
    db_path = Path(args.db) if args.db else DEFAULT_DB

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

    success = skipped = failed = 0

    for i, file_path in enumerate(files, 1):
        held_at, meeting_name = parse_filename(file_path)
        print(f"[{i}/{len(files)}] {file_path.name}")
        print(f"  held_at      : {held_at}")
        print(f"  meeting_name : {meeting_name}")

        ok = run_meeting_parser(
            file_path, held_at, meeting_name, db_path,
            force=args.force, dry_run=args.dry_run, no_encrypt=args.no_encrypt,
        )
        if ok:
            # 既存レコードのスキップ判定（meeting_parser.py の出力から判断）
            success += 1
        else:
            failed += 1
        print()

    print(f"完了: 処理={success}件, 失敗={failed}件")


if __name__ == "__main__":
    main()
