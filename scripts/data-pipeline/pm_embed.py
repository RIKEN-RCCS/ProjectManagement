#!/usr/bin/env python3
"""
pm_embed.py - FTS5インデックス構築スクリプト

argus_config.yaml（旧 qa_config.yaml）の定義に従い、会議議事録本文・Slack要約を
インデックスDB（qa_pm.db / qa_pm-hpc.db / qa_pm-pmo.db）に書き込む。

使い方:
  python3 scripts/pm_embed.py                          # 全インデックス差分更新
  python3 scripts/pm_embed.py --full-rebuild           # 全インデックス全件再構築
  python3 scripts/pm_embed.py --dry-run                # 件数確認のみ
"""

import argparse
import logging
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_utils import open_db

# --- スキーマ定義 ---
SCHEMA_CHUNKS = """
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
CREATE INDEX IF NOT EXISTS idx_chunks_source_record
    ON chunks(source_db, record_id);
"""

SCHEMA_CHUNK_INDEXES = """
CREATE TABLE IF NOT EXISTS chunk_indexes (
    chunk_id   INTEGER NOT NULL,
    index_name TEXT NOT NULL,
    PRIMARY KEY (chunk_id, index_name),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunk_indexes_index_name
    ON chunk_indexes(index_name);
"""

SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);
"""

SCHEMA_INDEX_STATE = """
CREATE TABLE IF NOT EXISTS index_state (
    source_db    TEXT NOT NULL,
    index_name   TEXT NOT NULL,
    last_indexed TEXT,
    PRIMARY KEY (source_db, index_name)
);
"""

# tokens カラムの追加（既存DBへの移行用）
SCHEMA_ADD_TOKENS_COLUMN = "ALTER TABLE chunks ADD COLUMN tokens TEXT"

SCHEMA_CHUNK_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id    INTEGER PRIMARY KEY,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    embedded_at TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
"""

# SudachiPy形態素解析トークンによるFTS5インデックス
SCHEMA_FTS_TOKENS = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tokens USING fts5(
    tokens,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);
"""

CHUNK_MAX_CHARS = 1000
CHUNK_OVERLAP_CHARS = 100


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- SudachiPy 形態素解析 ---

_sudachi_tokenizer = None
_sudachi_split_mode = None
_SUDACHI_TARGET_POS = {"名詞", "動詞", "形容詞", "副詞"}


def _init_sudachi() -> bool:
    """SudachiPy の初期化。利用可能なら True を返す。"""
    global _sudachi_tokenizer, _sudachi_split_mode
    try:
        import sudachipy
        try:
            # sudachipy v0.5.0+
            _sudachi_tokenizer = sudachipy.Dictionary().create()
            _sudachi_split_mode = sudachipy.SplitMode.C
            return True
        except Exception:
            # 旧API
            from sudachipy import tokenizer as tm
            _sudachi_tokenizer = tm.Tokenizer()
            _sudachi_split_mode = tm.Tokenizer.SplitMode.C
            return True
    except ImportError:
        return False


def sudachi_tokenize(text: str) -> str:
    """SudachiPyで形態素解析し、検索用トークン文字列（スペース区切り）を返す。
    SudachiPyが利用不可の場合は空文字列を返す。
    """
    if _sudachi_tokenizer is None:
        return ""
    try:
        morphemes = _sudachi_tokenizer.tokenize(text, _sudachi_split_mode)
        tokens: list[str] = []
        seen: set[str] = set()
        for m in morphemes:
            pos = m.part_of_speech()[0]
            if pos in _SUDACHI_TARGET_POS:
                form = m.dictionary_form()
                if len(form) >= 2 and form not in seen:
                    seen.add(form)
                    tokens.append(form)
        return " ".join(tokens)
    except Exception:
        return ""


def load_qa_config(config_path: Path) -> dict:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("indices", {})
    cfg.setdefault("channel_map", {})
    cfg.setdefault("default_index", "pm")
    return cfg


def open_index_db(index_db_path: Path) -> sqlite3.Connection:
    """統合インデックスDB qa_index.db（平文sqlite3）を開く。"""
    conn = sqlite3.connect(str(index_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for ddl in (SCHEMA_CHUNKS, SCHEMA_CHUNK_INDEXES, SCHEMA_FTS, SCHEMA_INDEX_STATE, SCHEMA_CHUNK_EMBEDDINGS):
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
    # tokens カラムの追加（既存DBの移行）
    try:
        conn.execute(SCHEMA_ADD_TOKENS_COLUMN)
    except sqlite3.OperationalError:
        pass  # already exists
    conn.execute(SCHEMA_FTS_TOKENS)
    conn.commit()
    return conn


def split_into_chunks(text: str, max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """テキストを段落単位で分割し、max_chars以下のチャンクに収める。"""
    if not text or not text.strip():
        return []
    paragraphs = re.split(r"\n\s*\n", text.strip())
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            if len(para) <= max_chars:
                tail = current[-overlap:] if current and overlap else ""
                current = (tail + "\n\n" + para).strip() if tail else para
            else:
                for i in range(0, len(para), max_chars - overlap):
                    seg = para[i:i + max_chars]
                    if seg.strip():
                        chunks.append(seg.strip())
                current = ""
    if current:
        chunks.append(current)
    return chunks


def get_last_indexed(index_conn: sqlite3.Connection, source_db: str,
                     index_name: str) -> str | None:
    row = index_conn.execute(
        "SELECT last_indexed FROM index_state WHERE source_db = ? AND index_name = ?",
        (source_db, index_name),
    ).fetchone()
    return row["last_indexed"] if row else None


def set_last_indexed(index_conn: sqlite3.Connection, source_db: str,
                     index_name: str, ts: str) -> None:
    index_conn.execute(
        "INSERT OR REPLACE INTO index_state (source_db, index_name, last_indexed)"
        " VALUES (?, ?, ?)",
        (source_db, index_name, ts),
    )


def insert_chunks(index_conn: sqlite3.Connection, chunk_rows: list[dict],
                  index_name: str) -> int:
    """chunk_rows を INSERT し、それぞれを chunk_indexes に index_name で紐付ける。
    重複（同じ source_db / record_id / source_type / content）は既存 chunk を再利用する。
    """
    if not chunk_rows:
        return 0
    inserted = 0
    for r in chunk_rows:
        existing = index_conn.execute(
            "SELECT id FROM chunks WHERE source_db = ? AND record_id IS ? AND"
            " source_type = ? AND content = ?",
            (r["source_db"], r["record_id"], r["source_type"], r["content"]),
        ).fetchone()
        if existing:
            chunk_id = existing["id"]
        else:
            cur = index_conn.execute(
                "INSERT INTO chunks"
                " (source_type, source_db, record_id, held_at, content, tokens,"
                "  source_ref, indexed_at)"
                " VALUES (:source_type, :source_db, :record_id, :held_at,"
                "         :content, :tokens, :source_ref, :indexed_at)",
                r,
            )
            chunk_id = cur.lastrowid
            inserted += 1
        index_conn.execute(
            "INSERT OR IGNORE INTO chunk_indexes (chunk_id, index_name) VALUES (?, ?)",
            (chunk_id, index_name),
        )
    return inserted


def delete_source_chunks(index_conn: sqlite3.Connection, source_db: str,
                         index_name: str) -> None:
    """指定 source_db のチャンクを 1 つの index_name から外す。
    他 index にも属していればチャンク本体は残す。どこからも参照されなくなった
    チャンクのみ削除する。"""
    # まず junction だけ外す
    index_conn.execute(
        "DELETE FROM chunk_indexes WHERE chunk_id IN ("
        " SELECT id FROM chunks WHERE source_db = ?) AND index_name = ?",
        (source_db, index_name),
    )
    # どの index からも参照されなくなったチャンクを削除
    index_conn.execute(
        "DELETE FROM chunks WHERE source_db = ? AND id NOT IN ("
        " SELECT chunk_id FROM chunk_indexes)",
        (source_db,),
    )


def rebuild_fts(index_conn: sqlite3.Connection) -> None:
    index_conn.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    index_conn.execute("INSERT INTO fts_tokens(fts_tokens) VALUES('rebuild')")


# --- minutes/{kind}.db からの抽出（minutes_content のみ）---

def index_minutes_content(
    index_conn: sqlite3.Connection,
    db_path: Path,
    index_name: str,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """minutes/{kind}.db の minutes_content テーブルのみを索引化する。"""
    source_db = f"minutes/{db_path.name}"

    try:
        src_conn = open_db(db_path)
    except Exception as e:
        logger.warning(f"  {source_db}: 開けませんでした - {e}")
        return 0

    chunk_rows: list[dict] = []
    now = now_iso()

    # instances から held_at, slack_file_permalink を取得
    instances: dict[str, dict] = {}
    for row in src_conn.execute("SELECT meeting_id, held_at, slack_file_permalink FROM instances"):
        instances[row["meeting_id"]] = {
            "held_at": row["held_at"],
            "source_ref": row["slack_file_permalink"],
        }

    for row in src_conn.execute("SELECT id, meeting_id, content FROM minutes_content"):
        inst = instances.get(row["meeting_id"], {})
        for chunk in split_into_chunks(row["content"] or ""):
            chunk_rows.append({
                "source_type": "minutes_content",
                "source_db": source_db,
                "record_id": str(row["id"]),
                "held_at": inst.get("held_at"),
                "content": chunk,
                "tokens": sudachi_tokenize(chunk),
                "source_ref": inst.get("source_ref"),
                "indexed_at": now,
            })

    src_conn.close()
    logger.info(f"    minutes_content {db_path.stem} → {index_name}: {len(chunk_rows)} チャンク")

    if dry_run:
        return len(chunk_rows)

    delete_source_chunks(index_conn, source_db, index_name)
    count = insert_chunks(index_conn, chunk_rows, index_name)
    set_last_indexed(index_conn, source_db, index_name, now)
    return count


# --- {channel_id}.db からの抽出（生メッセージ: スレッド単位でまとめて索引化）---

def index_slack_raw(
    index_conn: sqlite3.Connection,
    db_path: Path,
    index_name: str,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
    channel_id: str,
) -> int:
    """統合 Slack DB (data/slack.db) の messages + replies を、
    指定チャンネル分だけスレッド単位で索引化する。

    source_db キーは "{channel_id}.db" 形式（過去の C*.db 索引と互換）。

    差分更新: index_state の last_indexed より新しい fetched_at を持つ
    スレッド（messages.thread_ts）のみ再処理する。
    """
    source_db = f"{channel_id}.db"
    last_indexed = None if full_rebuild else get_last_indexed(
        index_conn, source_db, index_name)

    try:
        src_conn = open_db(db_path)
    except Exception as e:
        logger.warning(f"    {source_db}: 開けませんでした - {e}")
        return 0

    # WHERE 句のベース（チャンネル絞り込み）
    base_msg = "WHERE channel_id = ?"
    base_rep = "WHERE channel_id = ?"
    base_params: list = [channel_id]

    chunk_rows: list[dict] = []
    now = now_iso()

    # 差分対象スレッドを特定: last_indexed 以降に fetch されたスレッド
    if last_indexed:
        target_threads = {
            row[0] for row in src_conn.execute(
                f"""SELECT DISTINCT thread_ts FROM messages {base_msg} AND fetched_at > ?
                    UNION
                    SELECT DISTINCT thread_ts FROM replies {base_rep} AND fetched_at > ?""",
                base_params + [last_indexed] + base_params + [last_indexed],
            )
        }
        if not target_threads:
            src_conn.close()
            logger.info(f"    slack_raw {source_db}: 差分なし")
            return 0
        # 差分対象スレッドの古いチャンクをこの index から外す
        if not dry_run:
            for ts in target_threads:
                index_conn.execute(
                    "DELETE FROM chunk_indexes"
                    " WHERE index_name = ? AND chunk_id IN ("
                    "  SELECT id FROM chunks WHERE source_db = ? AND record_id = ?)",
                    (index_name, source_db, ts),
                )
                # どの index からも参照されなくなったら本体も削除
                index_conn.execute(
                    "DELETE FROM chunks WHERE source_db = ? AND record_id = ?"
                    " AND id NOT IN (SELECT chunk_id FROM chunk_indexes)",
                    (source_db, ts),
                )
    else:
        target_threads = None  # 全件対象

    # スレッドごとにメッセージを集約
    if target_threads is not None:
        placeholders = ",".join("?" * len(target_threads))
        where_msg = f"{base_msg} AND thread_ts IN ({placeholders})"
        where_rep = f"{base_rep} AND thread_ts IN ({placeholders})"
        params_msg = base_params + list(target_threads)
        params_rep = base_params + list(target_threads)
    else:
        where_msg = base_msg
        where_rep = base_rep
        params_msg = list(base_params)
        params_rep = list(base_params)

    # 親メッセージを取得
    parents: dict[str, dict] = {}
    for row in src_conn.execute(
        f"""SELECT thread_ts, user_name, text, timestamp, permalink
            FROM messages {where_msg} ORDER BY timestamp ASC""",
        params_msg,
    ):
        parents[row["thread_ts"]] = {
            "user_name": row["user_name"] or "unknown",
            "text": (row["text"] or "").replace("\n", " "),
            "timestamp": row["timestamp"] or "",
            "permalink": row["permalink"],
            "lines": [],
        }

    # 返信を取得してスレッドに付加
    for row in src_conn.execute(
        f"""SELECT thread_ts, user_name, text, timestamp
            FROM replies {where_rep} ORDER BY timestamp ASC""",
        params_rep,
    ):
        ts = row["thread_ts"]
        if ts not in parents:
            continue
        parents[ts]["lines"].append(
            f"  {row['user_name'] or 'unknown'}: {(row['text'] or '').replace(chr(10), ' ')}"
        )

    src_conn.close()

    # スレッド単位でテキストを組み立てチャンク化
    for thread_ts, p in parents.items():
        held_at = p["timestamp"][:10] if p["timestamp"] else None
        header = f"[{p['timestamp'][:16]}] {p['user_name']}: {p['text']}"
        body = header
        if p["lines"]:
            body += "\n" + "\n".join(p["lines"])
        for chunk in split_into_chunks(body):
            chunk_rows.append({
                "source_type": "slack_raw",
                "source_db": source_db,
                "record_id": thread_ts,
                "held_at": held_at,
                "content": chunk,
                "tokens": sudachi_tokenize(chunk),
                "source_ref": p["permalink"],
                "indexed_at": now,
            })

    logger.info(f"    slack_raw {source_db} → {index_name}: {len(chunk_rows)} チャンク（{len(parents)} スレッド）")

    if dry_run:
        return len(chunk_rows)

    if full_rebuild:
        delete_source_chunks(index_conn, source_db, index_name)
    count = insert_chunks(index_conn, chunk_rows, index_name)
    set_last_indexed(index_conn, source_db, index_name, now)
    return count



# --- box_docs.db からの抽出（BOXドキュメント本文）---

def index_box_doc_content(
    index_conn: sqlite3.Connection,
    box_docs_db_path: Path,
    index_name: str,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """box_docs.db の doc_content テーブル（変換済みMarkdown本文）を索引化する。
    box_files.index_name はJSON配列（例: '["pm", "pm-hpc"]'）。
    index_name がその配列に含まれるファイルのみ対象とする。
    """
    import json as _json

    source_db = box_docs_db_path.name

    if not box_docs_db_path.exists():
        return 0

    try:
        src_conn = open_db(box_docs_db_path, encrypt=True)
    except Exception as e:
        logger.warning(f"    {source_db}: 開けませんでした - {e}")
        return 0

    chunk_rows: list[dict] = []
    now = now_iso()

    try:
        rows = src_conn.execute(
            "SELECT dc.box_file_id, dc.content_md, bf.name, bf.modified_at,"
            " bf.folder_path, bf.index_name"
            " FROM doc_content dc JOIN box_files bf ON dc.box_file_id = bf.box_file_id"
            " WHERE COALESCE(bf.relevance, '') != 'noise'"
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"    {source_db}: テーブル読み込みエラー - {e}")
        src_conn.close()
        return 0

    matched = 0
    for row in rows:
        idx_raw = row["index_name"] or ""
        try:
            targets = _json.loads(idx_raw)
        except Exception:
            targets = [idx_raw] if idx_raw else []
        if index_name not in targets:
            continue

        content_md = row["content_md"] or ""
        if not content_md.strip():
            continue

        matched += 1
        heading = row["name"] or ""
        if row["folder_path"]:
            heading = f"{row['folder_path']}/{heading}"

        for chunk in split_into_chunks(content_md):
            prefixed = f"【{heading}】\n{chunk}" if heading else chunk
            chunk_rows.append({
                "source_type": "box_document",
                "source_db": source_db,
                "record_id": str(row["box_file_id"]),
                "held_at": (row["modified_at"] or "")[:10] or None,
                "content": prefixed,
                "tokens": sudachi_tokenize(prefixed),
                "source_ref": f"https://app.box.com/file/{row['box_file_id']}",
                "indexed_at": now,
            })

    src_conn.close()
    logger.info(f"    box_documents {index_name}: {len(chunk_rows)} チャンク ({matched} ファイル)")

    if dry_run:
        return len(chunk_rows)

    delete_source_chunks(index_conn, source_db, index_name)
    count = insert_chunks(index_conn, chunk_rows, index_name)
    set_last_indexed(index_conn, source_db, index_name, now)
    return count


# --- web_articles.db からの抽出 ---

def index_web(
    index_conn: sqlite3.Connection,
    articles_db_path: Path,
    index_name: str,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """data/web_articles.db の記事を索引化する。
    target_indices JSON配列に index_name が含まれる記事のみ対象とする。
    """
    import json as _json

    source_db = "web_articles.db"

    try:
        src_conn = sqlite3.connect(str(articles_db_path))
        src_conn.row_factory = sqlite3.Row
    except Exception as e:
        logger.warning(f"  {source_db}: 開けませんでした - {e}")
        return 0

    chunk_rows: list[dict] = []
    now = now_iso()

    for row in src_conn.execute(
        "SELECT id, source_name, url, title, published_at, content, summary, target_indices FROM articles"
    ):
        try:
            targets = _json.loads(row["target_indices"] or "[]")
        except Exception:
            targets = []
        if index_name not in targets:
            continue

        body = ""
        if row["title"]:
            body += row["title"] + "\n\n"
        body += (row["content"] or row["summary"] or "")

        for chunk in split_into_chunks(body):
            chunk_rows.append({
                "source_type": "web",
                "source_db": source_db,
                "record_id": str(row["id"]),
                "held_at": (row["published_at"] or "")[:10] or None,
                "content": chunk,
                "tokens": sudachi_tokenize(chunk),
                "source_ref": row["url"],
                "indexed_at": now,
            })

    src_conn.close()
    logger.info(f"    web {index_name}: {len(chunk_rows)} チャンク")

    if dry_run:
        return len(chunk_rows)

    delete_source_chunks(index_conn, source_db, index_name)
    count = insert_chunks(index_conn, chunk_rows, index_name)
    set_last_indexed(index_conn, source_db, index_name, now)
    return count


# --- メイン ---

def build_index(
    index_name: str,
    index_cfg: dict,
    data_dir: Path,
    index_conn: sqlite3.Connection,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
    *,
    web_only: bool = False,
) -> int:
    """1つの index_name を統合 qa_index.db に取り込む。追加したチャンク数を返す。"""
    minutes_kinds = index_cfg.get("minutes") or []
    channel_ids = index_cfg.get("channels") or []

    logger.info(f"[{index_name}] → qa_index.db")

    if not web_only:
        logger.info(f"  minutes: {minutes_kinds or '(なし)'}")
        logger.info(f"  channels: {channel_ids or '(なし)'}")

        if not minutes_kinds and not channel_ids:
            logger.warning("  ソースが未設定です。argus_config.yaml を編集してください。")
            return 0

    if full_rebuild and not dry_run and not web_only:
        logger.info(f"  全件再構築: {index_name} に紐づく chunk_indexes をクリア")
        index_conn.execute("DELETE FROM chunk_indexes WHERE index_name = ?", (index_name,))
        index_conn.execute("DELETE FROM index_state WHERE index_name = ?", (index_name,))
        # どの index からも参照されなくなったチャンクは削除
        index_conn.execute(
            "DELETE FROM chunks WHERE id NOT IN (SELECT chunk_id FROM chunk_indexes)"
        )
        index_conn.commit()

    total = 0

    if not web_only:
        # minutes_content
        for kind in minutes_kinds:
            minutes_path = data_dir / "minutes" / f"{kind}.db"
            if not minutes_path.exists():
                logger.warning(f"  {minutes_path} が見つかりません")
                continue
            total += index_minutes_content(
                index_conn, minutes_path, index_name,
                full_rebuild, dry_run, logger,
            )

        # slack_raw（生メッセージ）: 統合 DB (data/slack.db) からチャンネル別に索引化
        slack_db = data_dir / "slack.db"
        if not slack_db.exists():
            logger.warning(f"  {slack_db} が見つかりません — slack_raw 索引化をスキップ")
        else:
            for channel_id in channel_ids:
                total += index_slack_raw(
                    index_conn, slack_db, index_name,
                    full_rebuild, dry_run, logger,
                    channel_id=channel_id,
                )

        # box_documents（BOXドキュメント本文）
        box_docs_path = data_dir / "box_docs.db"
        if box_docs_path.exists():
            total += index_box_doc_content(
                index_conn, box_docs_path, index_name,
                full_rebuild, dry_run, logger,
            )

    # web（外部記事）
    web_articles_path = data_dir / "web_articles.db"
    if web_articles_path.exists():
        total += index_web(
            index_conn, web_articles_path, index_name,
            full_rebuild, dry_run, logger,
        )

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="QAインデックスDB (FTS5) を構築する")
    parser.add_argument("--full-rebuild", action="store_true", help="全件再構築（差分なし）")
    parser.add_argument("--web-only", action="store_true",
                        help="web_articles.db のみ再インデックス（minutes/slack/docs をスキップ）")
    parser.add_argument("--index-name", help="特定インデックスのみ処理（argus_config.yaml のキー名）")
    parser.add_argument("--config", default=None, help="設定ファイルのパス（デフォルト: data/argus_config.yaml）")
    parser.add_argument("--data-dir", default="data", help="data/ ディレクトリのパス")
    parser.add_argument("--dry-run", action="store_true", help="書き込みなしで件数のみ表示")
    parser.add_argument("--skip-embed", action="store_true",
                        help="embedding ベクトル計算をスキップ（FTS5 のみ更新）")
    parser.add_argument("--embed-backfill", action="store_true",
                        help="既存チャンクに embedding を後追い計算する")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("pm_embed")

    if _init_sudachi():
        logger.info("SudachiPy: 初期化完了（形態素解析インデックスを構築します）")
    else:
        logger.warning("SudachiPy: 利用不可（trigramのみでインデックスを構築します）")

    # CWD 非依存のため REPO_ROOT 起点の絶対パスを使う（cron 実行で CWD=$HOME のケース対応）
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = _REPO_ROOT / config_path
    else:
        config_path = _REPO_ROOT / "data" / "argus_config.yaml"
        if not config_path.exists():
            config_path = _REPO_ROOT / "data" / "qa_config.yaml"
    if not config_path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    config = load_qa_config(config_path)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = _REPO_ROOT / data_dir

    if args.dry_run:
        logger.info("[DRY-RUN] 書き込みは行いません")
    if args.web_only:
        logger.info("[WEB-ONLY] web_articles.db のみ処理します")

    indices = config.get("indices", {})
    if args.index_name:
        if args.index_name not in indices:
            logger.error(f"インデックス '{args.index_name}' は argus_config.yaml に定義されていません")
            logger.error(f"定義済み: {list(indices.keys())}")
            sys.exit(1)
        indices = {args.index_name: indices[args.index_name]}

    qa_index_path = data_dir / "qa_index.db"
    logger.info(f"統合インデックス DB: {qa_index_path}")
    index_conn = open_index_db(qa_index_path)

    total = 0
    for index_name, index_cfg in indices.items():
        total += build_index(
            index_name, index_cfg, data_dir, index_conn,
            args.full_rebuild, args.dry_run, logger,
            web_only=args.web_only,
        )

    if not args.dry_run:
        index_conn.commit()
        logger.info("FTS5 再構築中...")
        rebuild_fts(index_conn)
        index_conn.commit()
        db_chunks = index_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        db_links = index_conn.execute("SELECT COUNT(*) FROM chunk_indexes").fetchone()[0]
        logger.info(f"完了: chunks={db_chunks}, chunk_indexes={db_links} in {qa_index_path}")
        # index_name 別件数
        for r in index_conn.execute(
            "SELECT index_name, COUNT(*) FROM chunk_indexes GROUP BY index_name ORDER BY index_name"
        ):
            logger.info(f"  {r[0]}: {r[1]}")
    index_conn.close()

    if args.dry_run:
        logger.info(f"\n[DRY-RUN] 合計 {total} チャンク（書き込みなし）")
    else:
        logger.info("\n全インデックス更新完了")


if __name__ == "__main__":
    main()
