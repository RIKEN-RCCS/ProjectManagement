"""glossary.py — pm.db の glossary テーブルへのアクセスユーティリティ

terminology（単一キーワード＋別表記）とは異なり、コデザイン項目・ルール・
リファレンス等の構造化複数行テキストを管理する。

全 CRUD 関数は conn パラメータを受け取り、呼び出し元から既存の
コネクション（例: pm_api.py の _get_conn()）を渡せる。
conn が None の場合は内部で _open_pm() により新規接続する。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _pm_db_path() -> Path:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "data" / "pm.db"


_GLOSSARY_DDL = (
    "CREATE TABLE IF NOT EXISTS glossary ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "title TEXT NOT NULL,"
    "content TEXT NOT NULL,"
    "category TEXT DEFAULT '',"
    "updated_at TEXT,"
    "created_at TEXT)"
)


def _open_pm(db_path: Path | None = None):
    """pm.db を開く（暗号化対応）。glossary テーブルを自動作成する。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from db_utils import SQLCIPHER_AVAILABLE, open_db
    path = db_path or _pm_db_path()
    if not path.exists():
        print(f"[WARN] glossary: pm.db が見つかりません: {path}")
        return None
    if not SQLCIPHER_AVAILABLE:
        print("[INFO] glossary: sqlcipher3 未インストール（コンテナ環境）— pm.db 用語集をスキップします")
        return None
    try:
        conn = open_db(path, encrypt=True, row_factory=True, migrations=[_GLOSSARY_DDL])
        return conn
    except Exception as e:
        print(f"[WARN] glossary: pm.db への接続に失敗しました: {e}")
        return None


def _ensure_table(conn: sqlite3.Connection):
    """既存の接続に対して glossary テーブルが存在することを保証する。"""
    conn.execute(_GLOSSARY_DDL)
    conn.commit()


# --------------------------------------------------------------------------- #
# 読み取り API
# --------------------------------------------------------------------------- #

def load_all(
    category: str | None = None,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """glossary の全行を dict リストで返す。category 指定でフィルタ可能。"""
    own_conn = conn is None
    if conn is None:
        conn = _open_pm(db_path)
    if conn is None:
        return []
    try:
        if own_conn:
            _ensure_table(conn)
        if category:
            rows = conn.execute(
                "SELECT * FROM glossary WHERE category = ? ORDER BY id",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM glossary ORDER BY category, id"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own_conn:
            conn.close()


# --------------------------------------------------------------------------- #
# 書き込み API
# --------------------------------------------------------------------------- #

def add(
    title: str,
    content: str,
    category: str = "",
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """glossary に1件追加し、id を返す。"""
    own_conn = conn is None
    if conn is None:
        conn = _open_pm(db_path)
    if conn is None:
        return None
    now_ts = datetime.now(UTC).isoformat()
    try:
        if own_conn:
            _ensure_table(conn)
        cur = conn.execute(
            "INSERT INTO glossary (title, content, category, updated_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, content, category, now_ts, now_ts),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if own_conn:
            conn.close()


def update(
    id: int,
    title: str,
    content: str,
    category: str = "",
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """glossary の1件を更新する。"""
    own_conn = conn is None
    if conn is None:
        conn = _open_pm(db_path)
    if conn is None:
        return False
    now_ts = datetime.now(UTC).isoformat()
    try:
        if own_conn:
            _ensure_table(conn)
        conn.execute(
            "UPDATE glossary SET title=?, content=?, category=?, updated_at=? WHERE id=?",
            (title, content, category, now_ts, id),
        )
        conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


def delete(
    id: int,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """glossary の1件を削除する。"""
    own_conn = conn is None
    if conn is None:
        conn = _open_pm(db_path)
    if conn is None:
        return False
    try:
        if own_conn:
            _ensure_table(conn)
        conn.execute("DELETE FROM glossary WHERE id=?", (id,))
        conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


# --------------------------------------------------------------------------- #
# プロンプト注入用
# --------------------------------------------------------------------------- #

def build_reference(
    categories: list[str] | None = None,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """プロンプト注入用の Markdown セクション文字列を返す。

    glossary の内容をカテゴリ別に整形する。
    categories が None の場合は全件、指定時は該当カテゴリのみ。
    """
    own_conn = conn is None
    if conn is None:
        conn = _open_pm(db_path)
    if conn is None:
        return ""
    try:
        if own_conn:
            _ensure_table(conn)
        if categories:
            placeholders = ",".join("?" * len(categories))
            rows = conn.execute(
                f"SELECT * FROM glossary WHERE category IN ({placeholders}) ORDER BY category, id",
                categories,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM glossary ORDER BY category, id"
            ).fetchall()
    finally:
        if own_conn:
            conn.close()

    if not rows:
        print("[INFO] glossary: テーブルにエントリがありません（スキップ）")
        return ""

    print(f"[INFO] glossary: {len(rows)} 件のエントリを抽出しました")
    lines = ["\n### プロジェクト用語集 (glossary)"]
    current_cat = None
    for row in rows:
        cat = row["category"] or "(その他)"
        if cat != current_cat:
            lines.append(f"\n**{cat}:**")
            current_cat = cat
        title = row["title"]
        content = row["content"].strip()
        lines.append(f"- {title}")
        for c_line in content.split("\n"):
            lines.append(f"  {c_line}")
    return "\n".join(lines) + "\n"
