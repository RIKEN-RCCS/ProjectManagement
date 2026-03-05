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

鍵ファイルの作成方法:
  python3 scripts/db_utils.py --gen-key
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
# CLI（鍵生成）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="db_utils CLI")
    parser.add_argument("--gen-key", action="store_true", help="暗号化鍵を生成して保存する")
    parser.add_argument("--show-key-path", action="store_true", help="鍵ファイルのパスを表示する")
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
    else:
        parser.print_help()
