#!/usr/bin/env python3
"""
pm_document_extract.py

Slack メッセージ中の BOX リンクから共有ドキュメントのメタデータを LLM で抽出し、
ドキュメントレジストリDB に保存する。

DBファイルは argus_config.yaml の indices 定義に倣い4種類:
  data/docs_pm.db       — pm 相当（全チャンネル横断）
  data/docs_pm-hpc.db   — pm-hpc 相当
  data/docs_pm-bmt.db   — pm-bmt 相当
  data/docs_pm-pmo.db   — pm-pmo 相当

各チャンネルのメッセージは argus_config.yaml の indices.{name}.channels に従って
対応するDBに格納される。1チャンネルが複数 indices に属する場合は全てに格納する。

Usage:
  # 全チャンネルを処理（未処理のみ）
  python3 scripts/pm_document_extract.py

  # 確認用（DB保存なし）
  python3 scripts/pm_document_extract.py --dry-run

  # 全件再処理
  python3 scripts/pm_document_extract.py --force

  # 特定チャンネルのみ
  python3 scripts/pm_document_extract.py -c C08M0249GRL

  # 一覧表示
  python3 scripts/pm_document_extract.py --list
  python3 scripts/pm_document_extract.py --list --index-name pm-hpc
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_utils import (
    add_dry_run_arg,
    add_no_encrypt_arg,
    add_output_arg,
    add_since_arg,
    call_local_llm,
)
from db_utils import open_db

import yaml

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ARGUS_CONFIG = DATA_DIR / "argus_config.yaml"
QA_CONFIG_LEGACY = DATA_DIR / "qa_config.yaml"

BOX_URL_PATTERN = re.compile(
    r"https?://[^\s<>|]*box\.(com|net)[^\s<>|]*", re.IGNORECASE
)

# Slack のリンク形式 <URL|テキスト> からテキスト部分を抽出
SLACK_LINK_PATTERN = re.compile(
    r"<(https?://[^\s<>|]*box\.[^\s<>|]*)\|([^>]+)>", re.IGNORECASE
)

BATCH_SIZE = 5  # LLM に一度に渡すメッセージ数

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    type        TEXT,
    description TEXT,
    url         TEXT NOT NULL,
    shared_by   TEXT,
    shared_at   TEXT,
    channel_id  TEXT,
    permalink   TEXT,
    related_topic TEXT,
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extract_state (
    channel_id  TEXT NOT NULL,
    thread_ts   TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, thread_ts)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_url
    ON documents(url);
"""

# --------------------------------------------------------------------------- #
# 設定読み込み
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    config_path = ARGUS_CONFIG if ARGUS_CONFIG.exists() else QA_CONFIG_LEGACY
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_channel_to_indices(config: dict) -> dict[str, list[str]]:
    """チャンネルID → 所属インデックス名リストのマッピングを構築"""
    mapping: dict[str, list[str]] = {}
    for idx_name, idx_conf in config.get("indices", {}).items():
        for ch in idx_conf.get("channels", []):
            mapping.setdefault(ch, []).append(idx_name)
    return mapping


def get_docs_db_path(index_name: str) -> Path:
    return DATA_DIR / f"docs_{index_name}.db"


# --------------------------------------------------------------------------- #
# BOX リンク収集
# --------------------------------------------------------------------------- #


def collect_box_messages(channel_id: str, no_encrypt: bool, since: str | None = None):
    """指定チャンネルの Slack DB から BOX リンクを含むメッセージを収集"""
    db_path = DATA_DIR / f"{channel_id}.db"
    if not db_path.exists():
        return []

    conn = open_db(db_path, encrypt=not no_encrypt)

    results = []
    where_clause = "WHERE (text LIKE '%box.com%' OR text LIKE '%box.net%')"
    if since:
        where_clause += f" AND timestamp >= '{since}'"

    # messages
    for row in conn.execute(
        f"SELECT thread_ts, channel_id, user_name, text, timestamp, permalink FROM messages {where_clause} ORDER BY timestamp"
    ):
        urls = [m.group(0) for m in BOX_URL_PATTERN.finditer(row["text"] or "")]
        link_texts = {
            m.group(1): m.group(2)
            for m in SLACK_LINK_PATTERN.finditer(row["text"] or "")
        }
        if urls:
            results.append(
                {
                    "thread_ts": row["thread_ts"],
                    "channel_id": row["channel_id"],
                    "user": row["user_name"],
                    "timestamp": row["timestamp"],
                    "text": row["text"],
                    "permalink": row["permalink"],
                    "urls": urls,
                    "link_texts": link_texts,
                    "source": "message",
                }
            )

    # replies
    for row in conn.execute(
        f"SELECT msg_ts AS thread_ts, thread_ts AS parent_ts, channel_id, user_name, text, timestamp, permalink FROM replies {where_clause} ORDER BY timestamp"
    ):
        urls = [m.group(0) for m in BOX_URL_PATTERN.finditer(row["text"] or "")]
        link_texts = {
            m.group(1): m.group(2)
            for m in SLACK_LINK_PATTERN.finditer(row["text"] or "")
        }
        if urls:
            results.append(
                {
                    "thread_ts": row["thread_ts"],
                    "channel_id": row["channel_id"],
                    "user": row["user_name"],
                    "timestamp": row["timestamp"],
                    "text": row["text"],
                    "permalink": row["permalink"],
                    "urls": urls,
                    "link_texts": link_texts,
                    "source": "reply",
                }
            )

    conn.close()
    return results


# --------------------------------------------------------------------------- #
# LLM 抽出
# --------------------------------------------------------------------------- #

EXTRACT_PROMPT_TEMPLATE = """以下のSlackメッセージには、BOXで共有されたファイルへのリンクが含まれています。
各メッセージから、共有されたファイル/フォルダの情報を構造化して抽出してください。

出力は以下のJSON配列形式で。メッセージ内に複数のBOXリンクがある場合はそれぞれ別エントリとして出力してください:
[
  {{
    "title": "ファイル/フォルダのタイトル（リンクテキストがあればそれを使用、なければ文脈から推定）",
    "type": "種別（録画/スライド/報告書/議事録/テンプレート/フォルダ/資料/Excel/Word/その他）",
    "description": "何のファイルか（1-2文で簡潔に）",
    "url": "BOX URL",
    "related_topic": "関連するプロジェクトのトピック/活動（簡潔に）"
  }}
]

{messages}

JSONのみを出力してください。マークダウンのコードブロック記法は不要です。"""


def format_message_for_prompt(msg: dict, idx: int) -> str:
    link_text_info = ""
    if msg["link_texts"]:
        pairs = [f"  {url} → {text}" for url, text in msg["link_texts"].items()]
        link_text_info = "リンクテキスト:\n" + "\n".join(pairs) + "\n"

    return f"""--- メッセージ{idx + 1} ---
投稿者: {msg['user']}
日付: {msg['timestamp'][:10]}
URL: {', '.join(msg['urls'])}
{link_text_info}本文: {msg['text'][:600]}"""


def _get_local_llm_params() -> tuple[str, str, str]:
    """ローカルLLMの接続パラメータを取得。未設定ならエラー"""
    import os
    base_url = os.environ.get("OPENAI_API_BASE")
    if not base_url:
        raise RuntimeError(
            "OPENAI_API_BASE が未設定です。ローカルLLMが必須です。\n"
            "  export OPENAI_API_BASE='http://localhost:8000/v1'"
        )
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    from cli_utils import detect_vllm_model
    model = detect_vllm_model(base_url)
    return model, base_url, api_key


def extract_documents_batch(messages: list[dict], logger) -> list[dict]:
    """メッセージバッチからローカルLLMでドキュメント情報を抽出"""
    msg_texts = "\n\n".join(
        format_message_for_prompt(m, i) for i, m in enumerate(messages)
    )
    prompt = EXTRACT_PROMPT_TEMPLATE.format(messages=msg_texts)

    try:
        model, base_url, api_key = _get_local_llm_params()
        result = call_local_llm(
            prompt, model=model, base_url=base_url, api_key=api_key,
            max_tokens=2048, timeout=180,
        )
    except Exception as e:
        logger.error(f"LLM呼び出しエラー: {e}")
        return []

    # JSON パース
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)

    try:
        docs = json.loads(result)
        if isinstance(docs, list):
            return docs
    except json.JSONDecodeError:
        # JSON 配列部分を抽出して再試行
        match = re.search(r"\[.*\]", result, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        logger.error(f"JSON パース失敗: {result[:200]}")

    return []


# --------------------------------------------------------------------------- #
# DB 保存
# --------------------------------------------------------------------------- #


def save_documents(
    docs_db_path: Path,
    documents: list[dict],
    no_encrypt: bool,
    dry_run: bool,
    logger,
) -> int:
    """ドキュメントをDBに保存。重複URL はスキップ。"""
    if dry_run:
        return len(documents)

    conn = open_db(docs_db_path, encrypt=not no_encrypt, schema=SCHEMA)
    saved = 0
    for doc in documents:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO documents
                   (title, type, description, url, shared_by, shared_at,
                    channel_id, permalink, related_topic, extracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    doc.get("title", "不明"),
                    doc.get("type"),
                    doc.get("description"),
                    doc["url"],
                    doc.get("shared_by"),
                    doc.get("shared_at"),
                    doc.get("channel_id"),
                    doc.get("permalink"),
                    doc.get("related_topic"),
                    datetime.now().isoformat(),
                ),
            )
            if conn.total_changes:
                saved += 1
        except Exception as e:
            logger.error(f"保存エラー ({doc.get('url', '?')}): {e}")
    conn.commit()
    conn.close()
    return saved


def mark_extracted(
    docs_db_path: Path,
    channel_id: str,
    thread_ts: str,
    no_encrypt: bool,
):
    """抽出済みスレッドを記録"""
    conn = open_db(docs_db_path, encrypt=not no_encrypt, schema=SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO extract_state (channel_id, thread_ts, extracted_at) VALUES (?, ?, ?)",
        (channel_id, thread_ts, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def is_extracted(
    docs_db_path: Path, channel_id: str, thread_ts: str, no_encrypt: bool
) -> bool:
    """抽出済みかチェック"""
    if not docs_db_path.exists():
        return False
    conn = open_db(docs_db_path, encrypt=not no_encrypt, schema=SCHEMA)
    row = conn.execute(
        "SELECT 1 FROM extract_state WHERE channel_id = ? AND thread_ts = ?",
        (channel_id, thread_ts),
    ).fetchone()
    conn.close()
    return row is not None


# --------------------------------------------------------------------------- #
# 一覧表示
# --------------------------------------------------------------------------- #


def list_documents(index_name: str | None, no_encrypt: bool, since: str | None):
    config = load_config()
    indices = config.get("indices", {})

    if index_name:
        target_indices = {index_name: indices[index_name]} if index_name in indices else {}
    else:
        target_indices = indices

    for idx_name in target_indices:
        db_path = get_docs_db_path(idx_name)
        if not db_path.exists():
            continue

        conn = open_db(db_path, encrypt=not no_encrypt, schema=SCHEMA)
        where = ""
        params: list = []
        if since:
            where = "WHERE shared_at >= ?"
            params = [since]

        rows = conn.execute(
            f"SELECT title, type, shared_by, shared_at, channel_id, url, related_topic FROM documents {where} ORDER BY shared_at DESC",
            params,
        ).fetchall()
        conn.close()

        if not rows:
            continue

        print(f"\n{'=' * 60}")
        print(f"  {idx_name} ({db_path.name}): {len(rows)} 件")
        print(f"{'=' * 60}")
        for r in rows:
            print(f"  [{r['shared_at'] or '?'}] {r['title']}")
            print(f"    種別: {r['type'] or '-'}  共有者: {r['shared_by'] or '-'}")
            print(f"    トピック: {r['related_topic'] or '-'}")
            print(f"    URL: {r['url']}")
            print()


# --------------------------------------------------------------------------- #
# Canvas 投稿
# --------------------------------------------------------------------------- #

# チャンネルID → 表示名
_CHANNEL_NAMES: dict[str, str] = {
    "C08M0249GRL": "20_アプリケーション開発エリア",
    "C08SXA4M7JT": "20_1_リーダ会議メンバ",
    "C08LSJP4R6K": "21_hpcアプリケーションwg",
    "C093DQFSCRH": "21_1_ブロック1",
    "C093LP1J15G": "21_2_ブロック2",
    "C08MJ0NF5UZ": "22_ベンチマークwg",
    "C096ER1A0LU": "23_benchmark_framework",
    "C0A6AC59AHM": "24_ai-hpc-application",
    "C08PE3K9N72": "pmo",
}


def format_documents_for_canvas(
    index_name: str, no_encrypt: bool, since: str | None
) -> str:
    """指定インデックスのドキュメント一覧を Canvas 用 Markdown に整形"""
    from canvas_utils import sanitize_for_canvas

    db_path = get_docs_db_path(index_name)
    if not db_path.exists():
        return ""

    conn = open_db(db_path, encrypt=not no_encrypt, schema=SCHEMA)
    where = ""
    params: list = []
    if since:
        where = "WHERE shared_at >= ?"
        params = [since]

    rows = conn.execute(
        f"""SELECT title, type, description, shared_by, shared_at,
                   channel_id, url, related_topic
            FROM documents {where} ORDER BY shared_at DESC""",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Document Registry ({index_name})",
        f"最終更新: {now} / {len(rows)} 件\n",
    ]

    # 月別にグルーピング
    current_month = ""
    for r in rows:
        month = (r["shared_at"] or "不明")[:7]
        if month != current_month:
            current_month = month
            lines.append(f"\n## {month}\n")

        ch_name = _CHANNEL_NAMES.get(r["channel_id"] or "", r["channel_id"] or "-")
        lines.append(f"### {r['title']}")
        lines.append(f"- 種別: {r['type'] or '-'}")
        lines.append(f"- 説明: {r['description'] or '-'}")
        lines.append(f"- 共有者: {r['shared_by'] or '-'} ({r['shared_at'] or '-'})")
        lines.append(f"- チャンネル: {ch_name}")
        lines.append(f"- トピック: {r['related_topic'] or '-'}")
        lines.append(f"- URL: {r['url']}")
        lines.append("")

    content = "\n".join(lines)
    return sanitize_for_canvas(content)


def post_documents_to_canvas(
    canvas_id: str,
    index_name: str,
    no_encrypt: bool,
    since: str | None,
    dry_run: bool,
):
    """ドキュメント一覧を Canvas に投稿（全削除→新規挿入）"""
    from canvas_utils import post_to_canvas

    content = format_documents_for_canvas(index_name, no_encrypt, since)
    if not content:
        print(f"インデックス {index_name} にドキュメントなし")
        return

    if dry_run:
        print(content)
        return

    post_to_canvas(canvas_id, content)


# --------------------------------------------------------------------------- #
# メイン処理
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(
        description="Slack BOXリンクからドキュメントレジストリを構築"
    )
    parser.add_argument(
        "-c", "--channel", default=None, help="特定チャンネルのみ処理"
    )
    parser.add_argument(
        "--index-name", default=None, help="特定インデックスのみ処理"
    )
    parser.add_argument(
        "--force", action="store_true", help="抽出済みも再処理"
    )
    parser.add_argument(
        "--list", action="store_true", dest="show_list", help="ドキュメント一覧を表示"
    )
    parser.add_argument(
        "--post-to-canvas", action="store_true",
        help="ドキュメント一覧を Canvas に投稿",
    )
    parser.add_argument(
        "--canvas-id", default=None,
        help="投稿先 Canvas ID（--post-to-canvas 時に必須）",
    )
    add_since_arg(parser)
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_output_arg(parser)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("pm_document_extract")

    if args.post_to_canvas:
        if not args.canvas_id:
            parser.error("--post-to-canvas には --canvas-id が必須です")
        if not args.index_name:
            parser.error("--post-to-canvas には --index-name が必須です")
        post_documents_to_canvas(
            args.canvas_id, args.index_name, args.no_encrypt, args.since, args.dry_run
        )
        return

    if args.show_list:
        list_documents(args.index_name, args.no_encrypt, args.since)
        return

    config = load_config()
    ch_to_indices = build_channel_to_indices(config)

    # 処理対象チャンネルを決定
    if args.channel:
        target_channels = [args.channel]
    else:
        target_channels = sorted(set(
            ch for channels in ch_to_indices.values() for ch in channels
        ))
        # ch_to_indices のキーに含まれない独立チャンネルも追加
        all_channels = set()
        for idx_conf in config.get("indices", {}).values():
            all_channels.update(idx_conf.get("channels", []))
        target_channels = sorted(all_channels)

    total_extracted = 0
    total_saved = 0

    # 全チャンネルのメッセージを先に収集して全体件数を把握
    channel_messages: list[tuple[str, list[str], list[dict]]] = []
    total_messages = 0

    for channel_id in target_channels:
        index_names = ch_to_indices.get(channel_id, [config.get("default_index", "pm")])
        if args.index_name and args.index_name not in index_names:
            continue

        messages = collect_box_messages(channel_id, args.no_encrypt, args.since)
        if not messages:
            continue

        # 抽出済みフィルタ（force でなければスキップ）
        if not args.force:
            primary_db = get_docs_db_path(index_names[0])
            messages = [
                m
                for m in messages
                if not is_extracted(primary_db, channel_id, m["thread_ts"], args.no_encrypt)
            ]

        if not messages:
            continue

        channel_messages.append((channel_id, index_names, messages))
        total_messages += len(messages)

    if total_messages == 0:
        print("処理対象のメッセージなし")
        return

    print(f"処理対象: {total_messages} 件（{len(channel_messages)} チャンネル）\n")

    processed = 0

    for channel_id, index_names, messages in channel_messages:
        # バッチ処理
        for batch_start in range(0, len(messages), BATCH_SIZE):
            batch = messages[batch_start : batch_start + BATCH_SIZE]
            processed += len(batch)
            print(f"[{processed}/{total_messages}] {channel_id} 処理中...")
            docs = extract_documents_batch(batch, logger)

            if not docs:
                continue

            # メッセージのメタ情報を付与
            for doc in docs:
                url = doc.get("url", "")
                # 対応するメッセージを探す
                for msg in batch:
                    if url in msg["urls"] or any(url in u for u in msg["urls"]):
                        doc.setdefault("shared_by", msg["user"])
                        doc.setdefault("shared_at", msg["timestamp"][:10])
                        doc.setdefault("channel_id", channel_id)
                        doc.setdefault("permalink", msg["permalink"])
                        break
                else:
                    doc.setdefault("channel_id", channel_id)
                    doc.setdefault("shared_at", batch[0]["timestamp"][:10])

            total_extracted += len(docs)

            if args.dry_run:
                for doc in docs:
                    print(
                        f"  [{doc.get('shared_at')}] {doc.get('title')} "
                        f"({doc.get('type')}) - {doc.get('related_topic')}"
                    )
                    print(f"    URL: {doc.get('url')}")
            else:
                # 全対応インデックスDBに保存
                for idx_name in index_names:
                    if args.index_name and idx_name != args.index_name:
                        continue
                    db_path = get_docs_db_path(idx_name)
                    saved = save_documents(
                        db_path, docs, args.no_encrypt, args.dry_run, logger
                    )
                    total_saved += saved

            # 抽出済み記録
            if not args.dry_run:
                for msg in batch:
                    for idx_name in index_names:
                        if args.index_name and idx_name != args.index_name:
                            continue
                        mark_extracted(
                            get_docs_db_path(idx_name),
                            channel_id,
                            msg["thread_ts"],
                            args.no_encrypt,
                        )

    print(f"\n完了: {total_extracted} 件抽出", end="")
    if not args.dry_run:
        print(f", {total_saved} 件保存")
    else:
        print(" (dry-run)")


if __name__ == "__main__":
    main()
