#!/usr/bin/env python3
"""
pm_document_extract.py

Slack メッセージ中の Box リンクを `box shared-links:get` で box_file_id に解決し、
box_docs.db.slack_references テーブルに「誰がいつどのチャンネルでどのファイルを
共有したか」の履歴を蓄積する。

LLM はメッセージ本文から description と related_topic を抽出するためにのみ使う。
ファイルの真実源（タイトル・本文・relevance）は box CLI が直接クロールした
box_docs.db.box_files / doc_content に集約されている。

Usage:
  # 全チャンネルを処理（未処理のみ）
  python3 scripts/pm_document_extract.py

  # 特定チャンネル / 全件再処理
  python3 scripts/pm_document_extract.py -c C08M0249GRL
  python3 scripts/pm_document_extract.py --force

  # 確認用（DB保存なし）
  python3 scripts/pm_document_extract.py --dry-run

  # 一覧表示
  python3 scripts/pm_document_extract.py --list
"""

import argparse
import json
import logging
import re
import subprocess
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
BOX_DOCS_DB = DATA_DIR / "box_docs.db"

# Box リンク（共有URL: https://*.box.com/s/<token>）
BOX_URL_PATTERN = re.compile(
    r"https?://[^\s<>|]*box\.(com|net)[^\s<>|]*", re.IGNORECASE
)
SLACK_LINK_PATTERN = re.compile(
    r"<(https?://[^\s<>|]*box\.[^\s<>|]*)\|([^>]+)>", re.IGNORECASE
)

BATCH_SIZE = 5

# --------------------------------------------------------------------------- #
# 設定読み込み
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    config_path = ARGUS_CONFIG if ARGUS_CONFIG.exists() else QA_CONFIG_LEGACY
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def all_target_channels(config: dict) -> list[str]:
    chs = set()
    for idx_conf in config.get("indices", {}).values():
        chs.update(idx_conf.get("channels", []))
    return sorted(chs)


# --------------------------------------------------------------------------- #
# Slack DB から Box リンク収集
# --------------------------------------------------------------------------- #


def collect_box_messages(channel_id: str, no_encrypt: bool, since: str | None = None):
    db_path = DATA_DIR / f"{channel_id}.db"
    if not db_path.exists():
        return []

    conn = open_db(db_path, encrypt=not no_encrypt)
    results = []
    where_clause = "WHERE (text LIKE '%box.com%' OR text LIKE '%box.net%')"
    if since:
        where_clause += f" AND timestamp >= '{since}'"

    for row in conn.execute(
        f"SELECT thread_ts, channel_id, user_name, text, timestamp, permalink"
        f" FROM messages {where_clause} ORDER BY timestamp"
    ):
        urls = list(dict.fromkeys(
            m.group(0) for m in BOX_URL_PATTERN.finditer(row["text"] or "")
        ))
        link_texts = {m.group(1): m.group(2) for m in SLACK_LINK_PATTERN.finditer(row["text"] or "")}
        if urls:
            results.append({
                "thread_ts": row["thread_ts"], "channel_id": row["channel_id"],
                "user": row["user_name"], "timestamp": row["timestamp"],
                "text": row["text"], "permalink": row["permalink"],
                "urls": urls, "link_texts": link_texts, "source": "message",
            })

    for row in conn.execute(
        f"SELECT msg_ts AS thread_ts, channel_id, user_name, text, timestamp, permalink"
        f" FROM replies {where_clause} ORDER BY timestamp"
    ):
        urls = [m.group(0) for m in BOX_URL_PATTERN.finditer(row["text"] or "")]
        link_texts = {m.group(1): m.group(2) for m in SLACK_LINK_PATTERN.finditer(row["text"] or "")}
        if urls:
            results.append({
                "thread_ts": row["thread_ts"], "channel_id": row["channel_id"],
                "user": row["user_name"], "timestamp": row["timestamp"],
                "text": row["text"], "permalink": row["permalink"],
                "urls": urls, "link_texts": link_texts, "source": "reply",
            })

    conn.close()
    return results


# --------------------------------------------------------------------------- #
# Box CLI で共有URL → box_file_id 解決
# --------------------------------------------------------------------------- #


def resolve_box_url(url: str, logger) -> tuple[str, str] | None:
    """box CLI で共有URLを box_file_id に解決。

    Returns:
        (box_file_id, name) または None
    """
    # /s/<token> 形式以外（直接 file/<id> 形式など）はそのままパースを試みる
    m = re.search(r"/file/(\d+)", url)
    if m:
        return (m.group(1), "")
    if "/s/" not in url:
        return None
    try:
        res = subprocess.run(
            ["box", "shared-links:get", url, "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"box CLI 呼び出し失敗 {url}: {e}")
        return None
    if res.returncode != 0:
        logger.info(f"  解決不能 {url}: {res.stderr.strip()[:120]}")
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if data.get("type") != "file":
        return None  # フォルダ等は対象外
    return (str(data["id"]), data.get("name") or "")


# --------------------------------------------------------------------------- #
# LLM で description / related_topic だけ抽出
# --------------------------------------------------------------------------- #

EXTRACT_PROMPT = """以下のSlackメッセージから、共有された Box ファイルに関する文脈を抽出してください。
ファイル名そのものは Box から別途取得しているので不要です。
共有時に投稿者がコメントしたメモ・トピック・経緯のみを返します。

出力 JSON:
[
  {{"url": "BOX URL", "description": "共有時のコメントから読み取れる説明（1-2文、なければ空）",
    "related_topic": "関連プロジェクトトピック（簡潔、なければ空）"}},
  ...
]

メッセージ:
{messages}

JSONのみ。コードブロック不要。"""


def format_message_for_prompt(msg: dict, idx: int) -> str:
    link_text_info = ""
    if msg["link_texts"]:
        pairs = [f"  {url} → {text}" for url, text in msg["link_texts"].items()]
        link_text_info = "リンクテキスト:\n" + "\n".join(pairs) + "\n"
    return (f"--- メッセージ{idx + 1} ---\n"
            f"投稿者: {msg['user']}\n"
            f"日付: {msg['timestamp'][:10]}\n"
            f"URL: {', '.join(msg['urls'])}\n"
            f"{link_text_info}本文: {(msg['text'] or '')[:600]}")


def _llm_params():
    import os
    base_url = os.environ.get("OPENAI_API_BASE")
    if not base_url:
        raise RuntimeError("OPENAI_API_BASE が未設定です")
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    from cli_utils import detect_vllm_model
    return detect_vllm_model(base_url), base_url, api_key


def extract_context_batch(messages: list[dict], logger) -> dict[str, dict]:
    """{url: {description, related_topic}} を返す。LLM失敗時は空dict。"""
    if not messages:
        return {}
    msg_texts = "\n\n".join(format_message_for_prompt(m, i) for i, m in enumerate(messages))
    prompt = EXTRACT_PROMPT.format(messages=msg_texts)
    try:
        model, base_url, api_key = _llm_params()
        result = call_local_llm(prompt, model=model, base_url=base_url, api_key=api_key,
                                max_tokens=1024, timeout=120)
    except Exception as e:
        logger.error(f"LLM呼び出しエラー: {e}")
        return {}
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)
    try:
        items = json.loads(result)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", result, re.DOTALL)
        if not m:
            return {}
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(items, list):
        return {}
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("url"):
            out[item["url"]] = {
                "description": (item.get("description") or "").strip()[:500],
                "related_topic": (item.get("related_topic") or "").strip()[:200],
            }
    return out


# --------------------------------------------------------------------------- #
# DB 保存
# --------------------------------------------------------------------------- #


def save_slack_reference(conn, *, box_file_id, channel_id, thread_ts, slack_permalink,
                         shared_by, shared_at, description, related_topic, shared_url):
    conn.execute(
        "INSERT OR IGNORE INTO slack_references"
        " (box_file_id, channel_id, thread_ts, slack_permalink, shared_by,"
        "  shared_at, description, related_topic, shared_url, extracted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (box_file_id, channel_id, thread_ts, slack_permalink, shared_by,
         shared_at, description, related_topic, shared_url,
         datetime.now().isoformat()),
    )


def is_extracted(conn, channel_id: str, thread_ts: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM extract_state WHERE channel_id=? AND thread_ts=?",
        (channel_id, thread_ts),
    ).fetchone()
    return row is not None


def mark_extracted(conn, channel_id: str, thread_ts: str):
    conn.execute(
        "INSERT OR IGNORE INTO extract_state (channel_id, thread_ts, extracted_at)"
        " VALUES (?, ?, ?)",
        (channel_id, thread_ts, datetime.now().isoformat()),
    )


# --------------------------------------------------------------------------- #
# 一覧表示
# --------------------------------------------------------------------------- #


def list_references(no_encrypt: bool, since: str | None, channel_id: str | None):
    if not BOX_DOCS_DB.exists():
        print("box_docs.db がありません")
        return
    conn = open_db(BOX_DOCS_DB, encrypt=not no_encrypt)
    where = []
    params: list = []
    if since:
        where.append("sr.shared_at >= ?")
        params.append(since)
    if channel_id:
        where.append("sr.channel_id = ?")
        params.append(channel_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT sr.shared_at, sr.shared_by, sr.channel_id, sr.box_file_id,"
        f" sr.shared_url, sr.description, bf.name, bf.relevance"
        f" FROM slack_references sr"
        f" LEFT JOIN box_files bf ON sr.box_file_id = bf.box_file_id"
        f" {where_sql} ORDER BY sr.shared_at DESC", params
    ).fetchall()
    conn.close()
    print(f"slack_references: {len(rows)} 件")
    for r in rows:
        print(f"  [{r['shared_at'] or '?'}] {r['shared_by'] or '-'} ({r['channel_id'] or '-'})")
        print(f"    file: {r['name'] or '(未解決)'} [{r['box_file_id'] or '-'}] relevance={r['relevance'] or '-'}")
        if r["description"]:
            print(f"    desc: {r['description']}")
        print(f"    url:  {r['shared_url']}")
        print()


# --------------------------------------------------------------------------- #
# メイン処理
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(
        description="Slack の Box リンク → box_docs.db.slack_references"
    )
    parser.add_argument("-c", "--channel", default=None)
    parser.add_argument("--force", action="store_true", help="抽出済みも再処理")
    parser.add_argument("--list", action="store_true", dest="show_list")
    add_since_arg(parser)
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_output_arg(parser)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("pm_document_extract")

    if args.show_list:
        list_references(args.no_encrypt, args.since, args.channel)
        return

    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db がありません: {BOX_DOCS_DB}")
        sys.exit(1)

    config = load_config()
    target_channels = [args.channel] if args.channel else all_target_channels(config)

    box_conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)

    # 全メッセージ収集
    channel_messages: list[tuple[str, list[dict]]] = []
    total_messages = 0
    for channel_id in target_channels:
        messages = collect_box_messages(channel_id, args.no_encrypt, args.since)
        if not args.force:
            messages = [m for m in messages
                        if not is_extracted(box_conn, channel_id, m["thread_ts"])]
        if messages:
            channel_messages.append((channel_id, messages))
            total_messages += len(messages)

    if total_messages == 0:
        print("処理対象のメッセージなし")
        box_conn.close()
        return

    print(f"処理対象: {total_messages} 件（{len(channel_messages)} チャンネル）")
    processed = 0
    saved_refs = 0
    resolved = 0
    unresolved = 0

    for channel_id, messages in channel_messages:
        for batch_start in range(0, len(messages), BATCH_SIZE):
            batch = messages[batch_start : batch_start + BATCH_SIZE]
            processed += len(batch)
            print(f"[{processed}/{total_messages}] {channel_id} 処理中...")
            ctx_map = extract_context_batch(batch, logger)

            for msg in batch:
                for url in msg["urls"]:
                    resolved_pair = resolve_box_url(url, logger)
                    if resolved_pair:
                        box_file_id, _name = resolved_pair
                        resolved += 1
                    else:
                        box_file_id = None
                        unresolved += 1
                    ctx = ctx_map.get(url, {})
                    if args.dry_run:
                        print(f"  url={url}\n    fid={box_file_id}"
                              f" desc={ctx.get('description','')[:80]}")
                        continue
                    save_slack_reference(
                        box_conn,
                        box_file_id=box_file_id,
                        channel_id=channel_id,
                        thread_ts=msg["thread_ts"],
                        slack_permalink=msg["permalink"],
                        shared_by=msg["user"],
                        shared_at=(msg["timestamp"] or "")[:10],
                        description=ctx.get("description") or "",
                        related_topic=ctx.get("related_topic") or "",
                        shared_url=url,
                    )
                    saved_refs += 1

            if not args.dry_run:
                for msg in batch:
                    mark_extracted(box_conn, channel_id, msg["thread_ts"])
                box_conn.commit()

    box_conn.close()
    print(f"\n完了: slack_references {saved_refs} 件保存"
          f"（解決済み {resolved} / 未解決 {unresolved}）"
          + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
