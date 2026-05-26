#!/usr/bin/env python3
"""
migrate_qa_to_unified.py — qa_*.db を qa_index.db に統合する一回限りのマイグレーション。

各 qa_{index_name}.db に分かれていた chunks / index_state を 1 つの
data/qa_index.db にマージする。1 つのチャンクが複数 index に属する場合は
chunk_indexes(chunk_id, index_name) junction で複数行を持つ。

重複判定: (source_db, record_id, source_type, content) が同じものは同一チャンクと
みなして1行に集約し、chunk_indexes に複数 index_name を追加する。

Usage:
  # 件数確認のみ
  python3 scripts/migrate_qa_to_unified.py --dry-run

  # 実行（旧DBはそのまま残す）
  python3 scripts/migrate_qa_to_unified.py

  # 実行後に旧DBを .bak にリネーム
  python3 scripts/migrate_qa_to_unified.py --rename
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_utils import open_db


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_db   TEXT NOT NULL,
    record_id   TEXT,
    held_at     TEXT,
    content     TEXT NOT NULL,
    tokens      TEXT,
    source_ref  TEXT,
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_indexes (
    chunk_id   INTEGER NOT NULL,
    index_name TEXT NOT NULL,
    PRIMARY KEY (chunk_id, index_name),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_source_record
    ON chunks(source_db, record_id);
CREATE INDEX IF NOT EXISTS idx_chunk_indexes_index_name
    ON chunk_indexes(index_name);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_tokens USING fts5(
    tokens,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);

-- index_state は (source_db, index_name) 単位で管理
CREATE TABLE IF NOT EXISTS index_state (
    source_db    TEXT NOT NULL,
    index_name   TEXT NOT NULL,
    last_indexed TEXT,
    PRIMARY KEY (source_db, index_name)
);
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data",
                        help="データディレクトリ（デフォルト: data）")
    parser.add_argument("--target", default="data/qa_index.db",
                        help="統合先 DB ファイル（デフォルト: data/qa_index.db）")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB 書き込みなし・件数のみ表示")
    parser.add_argument("--rename", action="store_true",
                        help="マージ完了後に旧 qa_*.db を .bak にリネーム")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    target_path = Path(args.target).resolve()

    sources = sorted(p for p in data_dir.glob("qa_*.db")
                     if p.name != target_path.name)
    if not sources:
        print(f"[ERROR] {data_dir} に qa_*.db が見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"=== QA インデックス統合: {len(sources)} 個 → {target_path} ===")

    # ファイル名 qa_{index_name}.db から index_name を抽出
    def index_name_of(p: Path) -> str:
        stem = p.stem  # "qa_pm" など
        return stem.removeprefix("qa_") if stem.startswith("qa_") else stem

    if args.dry_run:
        for src in sources:
            try:
                c = open_db(src, encrypt=False)
                n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                print(f"  {src.name} ({index_name_of(src)}): {n} chunks")
                c.close()
            except Exception as e:
                print(f"  {src.name}: ERROR {e}")
        return

    # ターゲットを作成
    if target_path.exists():
        print(f"[WARN] {target_path} が既に存在します。新規 chunks を追加します。")
    dst = open_db(target_path, encrypt=False, schema=SCHEMA)

    # 既存ターゲット内の (source_db, record_id, source_type, content) → chunk_id をキャッシュ
    existing_key_to_id: dict[tuple, int] = {}
    for r in dst.execute(
        "SELECT id, source_db, record_id, source_type, content FROM chunks"
    ):
        key = (r["source_db"], r["record_id"], r["source_type"], r["content"])
        existing_key_to_id[key] = r["id"]

    grand_chunks_read = 0
    grand_chunks_inserted = 0
    grand_chunks_dedup = 0
    grand_links = 0

    for src in sources:
        idx = index_name_of(src)
        try:
            sc = open_db(src, encrypt=False)
        except Exception as e:
            print(f"  [SKIP] {src.name}: 開けません ({e})")
            continue

        try:
            rows = sc.execute(
                "SELECT source_type, source_db, record_id, held_at, content,"
                " tokens, source_ref, indexed_at FROM chunks"
            ).fetchall()
        except Exception as e:
            print(f"  [SKIP] {src.name}: chunks 読み込み失敗 ({e})")
            sc.close()
            continue

        ins = 0
        dedup = 0
        link_added = 0
        for r in rows:
            key = (r["source_db"], r["record_id"], r["source_type"], r["content"])
            if key in existing_key_to_id:
                chunk_id = existing_key_to_id[key]
                dedup += 1
            else:
                cur = dst.execute(
                    "INSERT INTO chunks"
                    " (source_type, source_db, record_id, held_at,"
                    "  content, tokens, source_ref, indexed_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (r["source_type"], r["source_db"], r["record_id"], r["held_at"],
                     r["content"], r["tokens"], r["source_ref"], r["indexed_at"]),
                )
                chunk_id = cur.lastrowid
                existing_key_to_id[key] = chunk_id
                ins += 1
            # Junction
            try:
                dst.execute(
                    "INSERT OR IGNORE INTO chunk_indexes (chunk_id, index_name) VALUES (?,?)",
                    (chunk_id, idx),
                )
                if dst.total_changes:
                    link_added += 1
            except Exception:
                pass

        # index_state も移植
        try:
            for s in sc.execute(
                "SELECT source_db, last_indexed FROM index_state"
            ):
                dst.execute(
                    "INSERT OR REPLACE INTO index_state"
                    " (source_db, index_name, last_indexed) VALUES (?,?,?)",
                    (s["source_db"], idx, s["last_indexed"]),
                )
        except Exception:
            pass

        sc.close()
        dst.commit()

        grand_chunks_read += len(rows)
        grand_chunks_inserted += ins
        grand_chunks_dedup += dedup
        grand_links += link_added

        print(f"  {src.name} → {idx}:"
              f" 読込 {len(rows)}, 新規 {ins}, dedup {dedup}, junction +{link_added}")

        if args.rename:
            bak = src.with_suffix(src.suffix + ".bak")
            i = 2
            while bak.exists():
                bak = src.with_suffix(f".bak{i}")
                i += 1
            src.rename(bak)
            print(f"    → {bak.name} にリネーム")

    # FTS5 を再構築
    print("\n--- FTS5 再構築中 ---")
    dst.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    dst.execute("INSERT INTO fts_tokens(fts_tokens) VALUES('rebuild')")
    dst.commit()

    final_chunks = dst.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    final_links = dst.execute("SELECT COUNT(*) FROM chunk_indexes").fetchone()[0]
    by_idx = dst.execute(
        "SELECT index_name, COUNT(*) FROM chunk_indexes GROUP BY index_name"
    ).fetchall()

    print()
    print("=== 集計 ===")
    print(f"  読み込み chunks: {grand_chunks_read}")
    print(f"  新規 INSERT:    {grand_chunks_inserted}")
    print(f"  dedup:          {grand_chunks_dedup}")
    print(f"  junction 追加:   {grand_links}")
    print()
    print(f"  qa_index.db chunks:        {final_chunks}")
    print(f"  qa_index.db chunk_indexes: {final_links}")
    print(f"  index_name 別件数:")
    for r in by_idx:
        print(f"    {r[0]}: {r[1]}")

    dst.close()


if __name__ == "__main__":
    main()
