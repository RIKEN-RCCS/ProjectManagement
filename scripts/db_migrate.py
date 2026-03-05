#!/usr/bin/env python3
"""
db_migrate.py

既存の平文 SQLite DB を SQLCipher 暗号化 DB に変換する。

Usage:
    # pm.db を暗号化（バックアップを自動作成）
    python3 scripts/db_migrate.py data/pm.db

    # 複数のDBをまとめて変換
    python3 scripts/db_migrate.py data/pm.db data/C08SXA4M7JT.db

    # バックアップなしで変換（注意）
    python3 scripts/db_migrate.py data/pm.db --no-backup

    # 動作確認のみ（変換しない）
    python3 scripts/db_migrate.py data/pm.db --dry-run

注意:
    - 変換後の元ファイルは .bak にリネームされる（--no-backup で省略可）
    - 変換は元ファイルと同名で上書きされる
    - 暗号化鍵は db_utils.py の手順で事前に生成・設定しておくこと
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_utils import gen_key, load_key, open_db, DEFAULT_KEY_FILE

try:
    from sqlcipher3 import dbapi2 as sqlcipher3
except ImportError:
    print("ERROR: sqlcipher3 がインストールされていません。", file=sys.stderr)
    print("  uv pip install sqlcipher3", file=sys.stderr)
    sys.exit(1)


def is_encrypted(db_path: Path) -> bool:
    """DBが暗号化済みかどうかを判定する"""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        conn.close()
        return False
    except sqlite3.DatabaseError:
        return True


def migrate_db(db_path: Path, backup: bool, dry_run: bool) -> bool:
    """
    平文DBを暗号化DBに変換する。
    戻り値: 成功したら True
    """
    print(f"\n[INFO] 対象: {db_path}")

    if not db_path.exists():
        print(f"  [SKIP] ファイルが存在しません")
        return False

    if is_encrypted(db_path):
        print(f"  [SKIP] 既に暗号化済みです")
        return False

    # 平文DBのテーブル・データを確認
    plain_conn = sqlite3.connect(db_path)
    plain_conn.row_factory = sqlite3.Row
    tables = [
        r["name"]
        for r in plain_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    row_counts = {}
    for t in tables:
        count = plain_conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        row_counts[t] = count
        print(f"  テーブル: {t} ({count} 行)")

    if dry_run:
        print("  [dry-run] 変換をスキップしました")
        plain_conn.close()
        return True

    # 一時ファイルに暗号化DBを作成
    key = load_key()
    escaped = key.replace("'", "''")

    with tempfile.NamedTemporaryFile(
        suffix=".db", dir=db_path.parent, delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        enc_conn = sqlcipher3.connect(tmp_path)
        enc_conn.execute(f"PRAGMA key='{escaped}'")

        # スキーマ（CREATE TABLE / INDEX 等）をコピー（内部管理テーブルを除く）
        schema_rows = plain_conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE sql IS NOT NULL AND tbl_name != 'sqlite_sequence'"
            " ORDER BY rootpage"
        ).fetchall()
        for row in schema_rows:
            enc_conn.execute(row[0])
        enc_conn.commit()

        # 各テーブルのデータをコピー
        for t in tables:
            if t == "sqlite_sequence":
                continue  # AUTOINCREMENT管理テーブルは自動更新されるためスキップ
            rows = plain_conn.execute(f"SELECT * FROM [{t}]").fetchall()
            if not rows:
                continue
            placeholders = ", ".join(["?"] * len(rows[0]))
            enc_conn.executemany(
                f"INSERT INTO [{t}] VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
        enc_conn.commit()

        plain_conn.close()
        enc_conn.close()

        # バックアップ作成
        if backup:
            bak_path = db_path.with_suffix(".db.bak")
            shutil.copy2(db_path, bak_path)
            print(f"  バックアップ: {bak_path}")

        # 暗号化DBで上書き
        shutil.move(tmp_path, db_path)
        print(f"  [OK] 暗号化完了: {db_path}")

    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        print(f"  [ERROR] 変換失敗: {e}", file=sys.stderr)
        return False

    # 検証
    try:
        conn = open_db(db_path, schema=None)
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            expected = row_counts[t]
            if count != expected:
                print(f"  [WARN] {t}: 期待={expected}行, 実際={count}行")
            else:
                print(f"  検証OK: {t} ({count} 行)")
        conn.close()
    except Exception as e:
        print(f"  [ERROR] 検証失敗: {e}", file=sys.stderr)
        return False

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="平文SQLite DB を SQLCipher 暗号化DBに変換する")
    parser.add_argument("db_files", nargs="+", help="変換対象のDBファイル")
    parser.add_argument("--no-backup", action="store_true", help="バックアップを作成しない")
    parser.add_argument("--dry-run", action="store_true", help="変換せず確認のみ")
    args = parser.parse_args()

    # 鍵の確認
    try:
        load_key()
        print(f"[INFO] 鍵ファイル: {DEFAULT_KEY_FILE}")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("\n鍵を生成するには:", file=sys.stderr)
        print("  python3 scripts/db_utils.py --gen-key", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("[INFO] --dry-run モード（変換しない）")

    success = 0
    skipped = 0
    failed = 0

    for db_file in args.db_files:
        db_path = Path(db_file)
        result = migrate_db(db_path, backup=not args.no_backup, dry_run=args.dry_run)
        if result:
            success += 1
        else:
            skipped += 1

    print(f"\n完了: 変換={success}件, スキップ={skipped}件, 失敗={failed}件")


if __name__ == "__main__":
    main()
