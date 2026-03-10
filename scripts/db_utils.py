#!/usr/bin/env python3
"""
db_utils.py

SQLite / SQLCipher の接続を一元管理するユーティリティ。

暗号化モード（デフォルト）:
  - sqlcipher3 を使用して AES-256 でDBを暗号化する
  - 鍵の読み込み優先順位:
      1. 環境変数 PM_DB_KEY
      2. ~/.secrets/pm_db_key.txt

平文モード（--no-encrypt オプション等で切り替え）:
  - 標準の sqlite3 を使用する
  - 既存の平文DBをそのまま使いたい場合や、暗号化不要な場合に使用

CLI サブコマンド:
  --gen-key               暗号化鍵を生成して ~/.secrets/pm_db_key.txt に保存する
  --show-key-path         鍵ファイルのパスを表示する
  --migrate DB [DB ...]   平文DBを SQLCipher 暗号化DBに変換する
  --no-backup             --migrate 時にバックアップを作成しない
  --dry-run               --migrate 時に変換せず確認のみ行う
  --audit-log             audit_log（変更履歴）を表示する
  --db PATH               --audit-log 時の pm.db パス（デフォルト: data/pm.db）
  --limit N               --audit-log 時の表示件数（デフォルト: 30）
  --source SOURCE         --audit-log 時にソースで絞り込む（canvas_sync / relink）
  --id ID                 --audit-log 時にアクションアイテムIDで絞り込む
"""

import os
import secrets
import sqlite3 as _sqlite3
from pathlib import Path

# sqlcipher3 が利用可能かチェック
try:
    from sqlcipher3 import dbapi2 as _sqlcipher3
    SQLCIPHER_AVAILABLE = True
except ImportError:
    SQLCIPHER_AVAILABLE = False

DEFAULT_KEY_FILE = Path.home() / ".secrets" / "pm_db_key.txt"


# --------------------------------------------------------------------------- #
# 鍵の読み込み
# --------------------------------------------------------------------------- #
def load_key() -> str:
    """
    暗号化鍵を取得する。
    優先順位: 環境変数 PM_DB_KEY > ~/.secrets/pm_db_key.txt
    """
    key = os.getenv("PM_DB_KEY")
    if key:
        return key.strip()

    if DEFAULT_KEY_FILE.exists():
        key = DEFAULT_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key

    raise RuntimeError(
        "暗号化鍵が見つかりません。\n"
        "  環境変数 PM_DB_KEY を設定するか、\n"
        f"  {DEFAULT_KEY_FILE} に鍵を保存してください。\n"
        "  鍵の生成: python3 scripts/db_utils.py --gen-key"
    )


def gen_key() -> str:
    """32バイト（64文字）の16進数ランダム鍵を生成して保存する"""
    key = secrets.token_hex(32)
    DEFAULT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_KEY_FILE.write_text(key + "\n", encoding="utf-8")
    DEFAULT_KEY_FILE.chmod(0o600)
    return key


# --------------------------------------------------------------------------- #
# DB 接続
# --------------------------------------------------------------------------- #
def open_db(
    db_path: Path | str,
    *,
    encrypt: bool = True,
    row_factory: bool = True,
    schema: str | None = None,
    migrations: list[str] | None = None,
) -> _sqlite3.Connection:
    """
    DB を開いて接続を返す。

    Parameters
    ----------
    db_path : Path | str
        DBファイルのパス。
    encrypt : bool
        True（デフォルト）なら sqlcipher3 で暗号化接続する。
        False なら標準 sqlite3 で平文接続する。
    row_factory : bool
        True なら conn.row_factory = sqlite3.Row を設定する。
    schema : str | None
        初期化SQLスクリプト（CREATE TABLE IF NOT EXISTS ...）。
        指定した場合は接続後に executescript で実行する。
    migrations : list[str] | None
        マイグレーション用SQLのリスト。順番に execute する。

    Returns
    -------
    sqlite3.Connection（または sqlcipher3 の Connection）
    """
    db_path = Path(db_path)

    if encrypt:
        if not SQLCIPHER_AVAILABLE:
            raise RuntimeError(
                "sqlcipher3 がインストールされていません。\n"
                "  uv pip install sqlcipher3\n"
                "または --no-encrypt オプションで平文モードを使用してください。"
            )
        key = load_key()
        conn = _sqlcipher3.connect(db_path)
        # パスフレーズをSQLエスケープして PRAGMA key に渡す
        escaped = key.replace("'", "''")
        conn.execute(f"PRAGMA key='{escaped}'")
    else:
        conn = _sqlite3.connect(db_path)

    if row_factory:
        conn.row_factory = _sqlcipher3.Row if encrypt and SQLCIPHER_AVAILABLE else _sqlite3.Row

    if schema:
        conn.executescript(schema)

    if migrations:
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass  # 既に適用済みのマイグレーションはスキップ
        conn.commit()

    return conn


def open_db_plain(db_path: Path | str, *, row_factory: bool = True) -> _sqlite3.Connection:
    """平文（非暗号化）でDBを開く。読み取り専用操作や移行スクリプト用。"""
    return open_db(db_path, encrypt=False, row_factory=row_factory)


# --------------------------------------------------------------------------- #
# 平文DB → 暗号化DB 変換
# --------------------------------------------------------------------------- #
def is_encrypted(db_path: Path) -> bool:
    """DBが暗号化済みかどうかを判定する"""
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        conn.close()
        return False
    except _sqlite3.DatabaseError:
        return True


def migrate_db(db_path: Path, *, backup: bool = True, dry_run: bool = False) -> bool:
    """
    平文DBを SQLCipher 暗号化DBに変換する。

    Parameters
    ----------
    db_path  : 変換対象のDBファイルパス
    backup   : True なら変換前に .db.bak を作成する（デフォルト: True）
    dry_run  : True なら変換せず確認のみ行う

    Returns
    -------
    bool: 変換を実施した場合 True、スキップの場合 False
    """
    import shutil
    import tempfile

    if not SQLCIPHER_AVAILABLE:
        raise RuntimeError(
            "sqlcipher3 がインストールされていません。\n"
            "  uv pip install sqlcipher3"
        )

    print(f"\n[INFO] 対象: {db_path}")

    if not db_path.exists():
        print("  [SKIP] ファイルが存在しません")
        return False

    if is_encrypted(db_path):
        print("  [SKIP] 既に暗号化済みです")
        return False

    plain_conn = _sqlite3.connect(db_path)
    plain_conn.row_factory = _sqlite3.Row
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

    key = load_key()
    escaped = key.replace("'", "''")

    with tempfile.NamedTemporaryFile(
        suffix=".db", dir=db_path.parent, delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        enc_conn = _sqlcipher3.connect(tmp_path)
        enc_conn.execute(f"PRAGMA key='{escaped}'")

        schema_rows = plain_conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE sql IS NOT NULL AND tbl_name != 'sqlite_sequence'"
            " ORDER BY rootpage"
        ).fetchall()
        for row in schema_rows:
            enc_conn.execute(row[0])
        enc_conn.commit()

        for t in tables:
            if t == "sqlite_sequence":
                continue
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

        if backup:
            bak_path = db_path.with_suffix(".db.bak")
            shutil.copy2(db_path, bak_path)
            print(f"  バックアップ: {bak_path}")

        shutil.move(tmp_path, db_path)
        print(f"  [OK] 暗号化完了: {db_path}")

    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        plain_conn.close()
        print(f"  [ERROR] 変換失敗: {e}")
        return False

    # 検証
    try:
        conn = open_db(db_path)
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            expected = row_counts[t]
            if count != expected:
                print(f"  [WARN] {t}: 期待={expected}行, 実際={count}行")
            else:
                print(f"  検証OK: {t} ({count} 行)")
        conn.close()
    except Exception as e:
        print(f"  [ERROR] 検証失敗: {e}")
        return False

    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="db_utils CLI")
    parser.add_argument("--gen-key", action="store_true", help="暗号化鍵を生成して保存する")
    parser.add_argument("--show-key-path", action="store_true", help="鍵ファイルのパスを表示する")
    parser.add_argument("--migrate", nargs="+", metavar="DB", help="平文DBを SQLCipher 暗号化DBに変換する")
    parser.add_argument("--no-backup", action="store_true", help="--migrate 時にバックアップを作成しない")
    parser.add_argument("--dry-run", action="store_true", help="--migrate 時に変換せず確認のみ")
    parser.add_argument("--audit-log", action="store_true", help="audit_log を表示する")
    parser.add_argument("--db", default="data/pm.db", metavar="PATH", help="--audit-log 時の pm.db パス（デフォルト: data/pm.db）")
    parser.add_argument("--no-encrypt", action="store_true", help="--audit-log 時に平文モードで接続する")
    parser.add_argument("--limit", type=int, default=30, metavar="N", help="--audit-log 時の表示件数（デフォルト: 30）")
    parser.add_argument("--source", metavar="SOURCE", help="--audit-log 時にソースで絞り込む（canvas_sync / relink）")
    parser.add_argument("--id", type=int, metavar="ID", help="--audit-log 時にアクションアイテムIDで絞り込む")
    args = parser.parse_args()

    if args.gen_key:
        if DEFAULT_KEY_FILE.exists():
            print(f"[WARN] 既に鍵ファイルが存在します: {DEFAULT_KEY_FILE}")
            ans = input("上書きしますか？ [y/N]: ").strip().lower()
            if ans != "y":
                print("キャンセルしました")
                sys.exit(0)
        key = gen_key()
        print(f"[OK] 鍵を生成しました: {DEFAULT_KEY_FILE}")
        print(f"     パーミッション: {oct(DEFAULT_KEY_FILE.stat().st_mode)}")
    elif args.show_key_path:
        print(DEFAULT_KEY_FILE)
    elif args.migrate:
        try:
            load_key()
            print(f"[INFO] 鍵ファイル: {DEFAULT_KEY_FILE}")
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print("\n鍵を生成するには: python3 scripts/db_utils.py --gen-key", file=sys.stderr)
            sys.exit(1)
        if args.dry_run:
            print("[INFO] --dry-run モード（変換しない）")
        success = skipped = 0
        for db_file in args.migrate:
            if migrate_db(Path(db_file), backup=not args.no_backup, dry_run=args.dry_run):
                success += 1
            else:
                skipped += 1
        print(f"\n完了: 変換={success}件, スキップ={skipped}件")
    elif args.audit_log:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"ERROR: {db_path} が見つかりません", file=sys.stderr)
            sys.exit(1)
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        # テーブルが存在するか確認
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone()
        if not exists:
            print("audit_log テーブルが存在しません。pm_sync_canvas.py または pm_relink.py を実行すると自動作成されます。")
            conn.close()
            sys.exit(0)
        where_clauses = []
        params: list = []
        if args.source:
            where_clauses.append("source = ?")
            params.append(args.source)
        if args.id:
            where_clauses.append("record_id = ?")
            params.append(str(args.id))
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(args.limit)
        rows = conn.execute(
            f"SELECT changed_at, source, record_id, field, old_value, new_value "
            f"FROM audit_log {where} ORDER BY changed_at DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
        if not rows:
            print("該当する変更履歴はありません。")
            sys.exit(0)
        print(f"{'日時':20s}  {'ソース':12s}  {'ID':4s}  {'フィールド':15s}  {'変更前':20s}  変更後")
        print("-" * 90)
        for r in rows:
            dt = r["changed_at"][:19].replace("T", " ")
            old = str(r["old_value"]) if r["old_value"] is not None else "NULL"
            new = str(r["new_value"]) if r["new_value"] is not None else "NULL"
            print(f"{dt:20s}  {r['source']:12s}  {r['record_id']:4s}  {r['field']:15s}  {old:20s}  {new}")
    else:
        parser.print_help()
