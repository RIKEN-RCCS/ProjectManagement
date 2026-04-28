#!/usr/bin/env python3
"""
pm_embed.py - FTS5インデックス構築スクリプト

argus_config.yaml（旧 qa_config.yaml）の定義に従い、会議議事録本文・Slack要約を
インデックスDB（qa_pm.db / qa_pm-hpc.db / qa_pm-bmt.db / qa_pm-pmo.db）に書き込む。

使い方:
  python3 scripts/pm_embed.py                          # 全インデックス差分更新
  python3 scripts/pm_embed.py --full-rebuild           # 全インデックス全件再構築
  python3 scripts/pm_embed.py --index-name pm-bmt      # 特定インデックスのみ
  python3 scripts/pm_embed.py --dry-run                # 件数確認のみ
"""

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    source_ref  TEXT,
    indexed_at  TEXT NOT NULL
);
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
    source_db    TEXT PRIMARY KEY,
    last_indexed TEXT
);
"""

# tokens カラムの追加（既存DBへの移行用）
SCHEMA_ADD_TOKENS_COLUMN = "ALTER TABLE chunks ADD COLUMN tokens TEXT"

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
    return datetime.now(timezone.utc).isoformat()


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
    """インデックスDB（平文sqlite3）を開く。"""
    conn = sqlite3.connect(str(index_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA_CHUNKS)
    conn.execute(SCHEMA_FTS)
    conn.execute(SCHEMA_INDEX_STATE)
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


def get_last_indexed(index_conn: sqlite3.Connection, source_db: str) -> str | None:
    row = index_conn.execute(
        "SELECT last_indexed FROM index_state WHERE source_db = ?", (source_db,)
    ).fetchone()
    return row["last_indexed"] if row else None


def set_last_indexed(index_conn: sqlite3.Connection, source_db: str, ts: str) -> None:
    index_conn.execute(
        "INSERT OR REPLACE INTO index_state (source_db, last_indexed) VALUES (?, ?)",
        (source_db, ts),
    )


def insert_chunks(index_conn: sqlite3.Connection, chunk_rows: list[dict]) -> int:
    if not chunk_rows:
        return 0
    index_conn.executemany(
        """INSERT INTO chunks (source_type, source_db, record_id, held_at, content, tokens, source_ref, indexed_at)
           VALUES (:source_type, :source_db, :record_id, :held_at, :content, :tokens, :source_ref, :indexed_at)""",
        chunk_rows,
    )
    return len(chunk_rows)


def delete_source_chunks(index_conn: sqlite3.Connection, source_db: str) -> None:
    index_conn.execute("DELETE FROM chunks WHERE source_db = ?", (source_db,))


def rebuild_fts(index_conn: sqlite3.Connection) -> None:
    index_conn.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    index_conn.execute("INSERT INTO fts_tokens(fts_tokens) VALUES('rebuild')")


# --- minutes/{kind}.db からの抽出（minutes_content のみ）---

def index_minutes_content(
    index_conn: sqlite3.Connection,
    db_path: Path,
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
    logger.info(f"    minutes_content {db_path.stem}: {len(chunk_rows)} チャンク")

    if dry_run:
        return len(chunk_rows)

    delete_source_chunks(index_conn, source_db)
    count = insert_chunks(index_conn, chunk_rows)
    set_last_indexed(index_conn, source_db, now)
    return count


# --- {channel_id}.db からの抽出（生メッセージ: スレッド単位でまとめて索引化）---

def index_slack_raw(
    index_conn: sqlite3.Connection,
    db_path: Path,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """Slack チャンネルDBの messages + replies テーブルをスレッド単位で索引化する。

    差分更新: index_state の last_indexed より新しい fetched_at を持つ
    スレッド（messages.thread_ts）のみ再処理する。
    """
    source_db = db_path.name
    last_indexed = None if full_rebuild else get_last_indexed(index_conn, source_db)

    try:
        src_conn = open_db(db_path)
    except Exception as e:
        logger.warning(f"    {source_db}: 開けませんでした - {e}")
        return 0

    chunk_rows: list[dict] = []
    now = now_iso()

    # 差分対象スレッドを特定: last_indexed 以降に fetch されたスレッド
    if last_indexed:
        target_threads = {
            row[0] for row in src_conn.execute(
                """SELECT DISTINCT thread_ts FROM messages WHERE fetched_at > ?
                   UNION
                   SELECT DISTINCT thread_ts FROM replies WHERE fetched_at > ?""",
                (last_indexed, last_indexed),
            )
        }
        if not target_threads:
            src_conn.close()
            logger.info(f"    slack_raw {source_db}: 差分なし")
            return 0
        # 差分対象スレッドの古いチャンクを削除（再生成するため）
        if not dry_run:
            for ts in target_threads:
                index_conn.execute(
                    "DELETE FROM chunks WHERE source_db = ? AND record_id = ?",
                    (source_db, ts),
                )
    else:
        target_threads = None  # 全件対象

    # スレッドごとにメッセージを集約
    # messages: 親投稿（thread_ts が PK）
    if target_threads is not None:
        placeholders = ",".join("?" * len(target_threads))
        where_msg = f"WHERE thread_ts IN ({placeholders})"
        where_rep = f"WHERE thread_ts IN ({placeholders})"
        params_msg = list(target_threads)
        params_rep = list(target_threads)
    else:
        where_msg = ""
        where_rep = ""
        params_msg = []
        params_rep = []

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

    logger.info(f"    slack_raw {source_db}: {len(chunk_rows)} チャンク（{len(parents)} スレッド）")

    if dry_run:
        return len(chunk_rows)

    if full_rebuild:
        delete_source_chunks(index_conn, source_db)
    count = insert_chunks(index_conn, chunk_rows)
    set_last_indexed(index_conn, source_db, now)
    return count



# --- docs_{index_name}.db からの抽出（ドキュメントレジストリ）---

def index_docs(
    index_conn: sqlite3.Connection,
    db_path: Path,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """docs_{name}.db の documents テーブルを索引化する。"""
    source_db = db_path.name

    if not db_path.exists():
        return 0

    try:
        src_conn = open_db(db_path, encrypt=True)
    except Exception as e:
        logger.warning(f"    {source_db}: 開けませんでした - {e}")
        return 0

    chunk_rows: list[dict] = []
    now = now_iso()

    for row in src_conn.execute(
        "SELECT id, title, type, description, shared_by, shared_at, url, related_topic, permalink FROM documents"
    ):
        text_parts = []
        if row["title"]:
            text_parts.append(row["title"])
        if row["type"]:
            text_parts.append(f"種別: {row['type']}")
        if row["description"]:
            text_parts.append(row["description"])
        if row["shared_by"]:
            text_parts.append(f"共有者: {row['shared_by']}")
        if row["related_topic"]:
            text_parts.append(f"トピック: {row['related_topic']}")
        if row["url"]:
            text_parts.append(f"URL: {row['url']}")

        content = "\n".join(text_parts)
        chunk_rows.append({
            "source_type": "document",
            "source_db": source_db,
            "record_id": str(row["id"]),
            "held_at": row["shared_at"],
            "content": content,
            "tokens": sudachi_tokenize(content),
            "source_ref": row["permalink"],
            "indexed_at": now,
        })

    src_conn.close()
    logger.info(f"    documents {db_path.stem}: {len(chunk_rows)} チャンク")

    if dry_run:
        return len(chunk_rows)

    delete_source_chunks(index_conn, source_db)
    count = insert_chunks(index_conn, chunk_rows)
    set_last_indexed(index_conn, source_db, now)
    return count


# --- メイン ---

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

    delete_source_chunks(index_conn, source_db)
    count = insert_chunks(index_conn, chunk_rows)
    set_last_indexed(index_conn, source_db, now)
    return count


# --- メイン ---

def build_index(
    index_name: str,
    index_cfg: dict,
    data_dir: Path,
    full_rebuild: bool,
    dry_run: bool,
    logger: logging.Logger,
    *,
    web_only: bool = False,
) -> int:
    """1つのインデックスを構築する。追加したチャンク数を返す。"""
    db_path = Path(index_cfg["db"])
    minutes_kinds = index_cfg.get("minutes") or []
    channel_ids = index_cfg.get("channels") or []

    logger.info(f"[{index_name}] → {db_path}")

    if not web_only:
        logger.info(f"  minutes: {minutes_kinds or '(なし)'}")
        logger.info(f"  channels: {channel_ids or '(なし)'}")

        if not minutes_kinds and not channel_ids:
            logger.warning(f"  ソースが未設定です。argus_config.yaml を編集してください。")
            return 0

    index_conn = open_index_db(db_path)

    if full_rebuild and not dry_run and not web_only:
        logger.info(f"  全件再構築: 既存インデックスをクリア")
        index_conn.execute("DELETE FROM chunks")
        index_conn.execute("DELETE FROM index_state")
        index_conn.commit()

    total = 0

    if not web_only:
        # minutes_content
        for kind in minutes_kinds:
            minutes_path = data_dir / "minutes" / f"{kind}.db"
            if not minutes_path.exists():
                logger.warning(f"  {minutes_path} が見つかりません")
                continue
            total += index_minutes_content(index_conn, minutes_path, full_rebuild, dry_run, logger)

        # slack_raw（生メッセージ）
        for channel_id in channel_ids:
            channel_path = data_dir / f"{channel_id}.db"
            if not channel_path.exists():
                logger.warning(f"  {channel_path} が見つかりません")
                continue
            total += index_slack_raw(index_conn, channel_path, full_rebuild, dry_run, logger)

        # documents（ドキュメントレジストリ）
        docs_path = data_dir / f"docs_{index_name}.db"
        if docs_path.exists():
            total += index_docs(index_conn, docs_path, full_rebuild, dry_run, logger)

    # web（外部記事）
    web_articles_path = data_dir / "web_articles.db"
    if web_articles_path.exists():
        total += index_web(index_conn, web_articles_path, index_name, full_rebuild, dry_run, logger)

    if not dry_run:
        index_conn.commit()
        logger.info(f"  FTS5 再構築中...")
        rebuild_fts(index_conn)
        index_conn.commit()
        db_total = index_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        logger.info(f"  完了: {db_total} チャンク in {db_path}")

    index_conn.close()
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("pm_embed")

    if _init_sudachi():
        logger.info("SudachiPy: 初期化完了（形態素解析インデックスを構築します）")
    else:
        logger.warning("SudachiPy: 利用不可（trigramのみでインデックスを構築します）")

    if args.config:
        config_path = Path(args.config)
    else:
        config_path = Path("data/argus_config.yaml")
        if not config_path.exists():
            config_path = Path("data/qa_config.yaml")
    if not config_path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    config = load_qa_config(config_path)
    data_dir = Path(args.data_dir)

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

    total = 0
    for index_name, index_cfg in indices.items():
        total += build_index(
            index_name, index_cfg, data_dir,
            args.full_rebuild, args.dry_run, logger,
            web_only=args.web_only,
        )

    if args.dry_run:
        logger.info(f"\n[DRY-RUN] 合計 {total} チャンク（書き込みなし）")
    else:
        logger.info(f"\n全インデックス更新完了")


if __name__ == "__main__":
    main()
