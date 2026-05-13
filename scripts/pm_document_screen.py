#!/usr/bin/env python3
"""
pm_document_screen.py

docs_*.db のドキュメントに対し、ローカルLLMで relevance (core/related/noise)
を判定し、CSVでエクスポート・人間精査後にインポートするスクリーニングツール。

relevance 列:
  core    — 富岳NEXTプロジェクトの本質的ナレッジ（設計資料・公式報告書・意思決定資料等）
  related — 関連するが本質ではない（補助資料・参考情報・過去事例等）
  noise   — プロジェクトと無関係 / 索引化するとノイズになる（雑談添付・個人メモ等）
  unknown — 判定不能（情報不足）

Usage:
  # メタデータのみでLLM判定（全 docs_*.db 対象、未判定のみ）
  python3 scripts/pm_document_screen.py --judge

  # 特定インデックスのみ / 全件再判定
  python3 scripts/pm_document_screen.py --judge --index-name pm
  python3 scripts/pm_document_screen.py --judge --force

  # CSVにエクスポート（精査用、noise を先頭に）
  python3 scripts/pm_document_screen.py --export --output screen.csv

  # 精査後のCSVをDBに反映
  python3 scripts/pm_document_screen.py --import screen.csv

  # relevance分布を集計
  python3 scripts/pm_document_screen.py --stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_utils import add_no_encrypt_arg, call_local_llm
from db_utils import open_db

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ARGUS_CONFIG = DATA_DIR / "argus_config.yaml"
QA_CONFIG_LEGACY = DATA_DIR / "qa_config.yaml"

VALID_RELEVANCE = {"core", "related", "noise", "unknown"}
BATCH_SIZE = 10

JUDGE_PROMPT = """あなたは「富岳NEXT」プロジェクト（次世代スーパーコンピュータ開発）の
ナレッジマネジメント担当です。Slackで共有されたBOXドキュメントのメタデータを見て、
RAG検索インデックスに残すべきか判定してください。

# プロジェクト文脈
富岳NEXTは理研・富士通・NVIDIA連携による次世代AI-HPCシステム。アプリケーション開発エリア
（HPCアプリケーションWG・ベンチマークWG）のプロジェクトマネジメントを支援している。
本質的ナレッジ = 設計方針・技術仕様・意思決定・議事録・公式報告書・開発成果・ベンチマーク結果等。

# 判定カテゴリ
- core    : プロジェクトの本質的ナレッジ。設計資料・公式報告書・意思決定資料・議事録・技術仕様
- related : 関連するが本質ではない。参考資料・過去事例・外部文献・補助資料
- noise   : 索引化すべきでない。雑談添付・個人メモ・関係ない資料・壊れたリンク・情報不足で意味不明
- unknown : タイトル等が欠損・不明瞭で判定不能

# 出力形式
各ドキュメントに対し次の JSON 配列を出力（順序は入力と同じ）:
[
  {{"id": <id>, "relevance": "core|related|noise|unknown", "reason": "<1行の根拠>"}},
  ...
]

# 入力ドキュメント
{documents}

JSON配列のみ出力。コードブロック記法不要。"""


def load_config() -> dict:
    path = ARGUS_CONFIG if ARGUS_CONFIG.exists() else QA_CONFIG_LEGACY
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_docs_db_paths(index_name: str | None) -> list[tuple[str, Path]]:
    config = load_config()
    indices = config.get("indices", {})
    names = [index_name] if index_name else list(indices.keys())
    result = []
    for n in names:
        p = DATA_DIR / f"docs_{n}.db"
        if p.exists():
            result.append((n, p))
    return result


def ensure_relevance_columns(conn) -> None:
    cur = conn.execute("PRAGMA table_info(documents)")
    cols = {r[1] for r in cur.fetchall()}
    for col, decl in [
        ("relevance", "TEXT"),
        ("relevance_reason", "TEXT"),
        ("relevance_judged_at", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")
    conn.commit()


def format_doc_for_prompt(row) -> str:
    parts = [f"id={row['id']}"]
    parts.append(f"title={row['title'] or '(不明)'}")
    if row["type"]:
        parts.append(f"type={row['type']}")
    if row["description"]:
        parts.append(f"desc={row['description'][:200]}")
    if row["shared_by"]:
        parts.append(f"shared_by={row['shared_by']}")
    if row["related_topic"]:
        parts.append(f"topic={row['related_topic']}")
    return " | ".join(parts)


def _get_llm_params():
    import os
    base_url = os.environ.get("OPENAI_API_BASE")
    if not base_url:
        raise RuntimeError(
            "OPENAI_API_BASE 未設定。export OPENAI_API_BASE='http://localhost:8000/v1'"
        )
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    from cli_utils import detect_vllm_model
    return detect_vllm_model(base_url), base_url, api_key


def judge_batch(rows: list, logger) -> dict[int, tuple[str, str]]:
    """Returns {id: (relevance, reason)}."""
    if not rows:
        return {}
    doc_lines = "\n".join(format_doc_for_prompt(r) for r in rows)
    prompt = JUDGE_PROMPT.format(documents=doc_lines)
    try:
        model, base_url, api_key = _get_llm_params()
        result = call_local_llm(
            prompt, model=model, base_url=base_url, api_key=api_key,
            max_tokens=2048, timeout=180,
        )
    except Exception as e:
        logger.error(f"LLMエラー: {e}")
        return {}

    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)

    parsed = None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", result, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(parsed, list):
        logger.error(f"JSONパース失敗: {result[:200]}")
        return {}

    out: dict[int, tuple[str, str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            doc_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        rel = str(item.get("relevance", "unknown")).lower().strip()
        if rel not in VALID_RELEVANCE:
            rel = "unknown"
        reason = str(item.get("reason", ""))[:300]
        out[doc_id] = (rel, reason)
    return out


def cmd_judge(args, logger) -> None:
    targets = get_docs_db_paths(args.index_name)
    if not targets:
        print("対象DBなし")
        return

    total_updated = 0
    for idx_name, db_path in targets:
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        ensure_relevance_columns(conn)

        where = "" if args.force else "WHERE relevance IS NULL"
        rows = conn.execute(
            f"SELECT id, title, type, description, shared_by, related_topic "
            f"FROM documents {where} ORDER BY id"
        ).fetchall()

        if not rows:
            print(f"[{idx_name}] 判定対象なし")
            conn.close()
            continue

        print(f"[{idx_name}] 判定対象: {len(rows)} 件")
        now = datetime.now().isoformat()
        processed = 0

        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start : start + BATCH_SIZE]
            verdicts = judge_batch(batch, logger)
            processed += len(batch)
            print(f"  [{processed}/{len(rows)}] {idx_name} バッチ処理 "
                  f"(判定: {len(verdicts)}/{len(batch)})")

            if args.dry_run:
                for r in batch:
                    v = verdicts.get(r["id"])
                    if v:
                        print(f"    id={r['id']} {v[0]:7s} {r['title'][:50]} — {v[1][:60]}")
                continue

            for doc_id, (rel, reason) in verdicts.items():
                conn.execute(
                    "UPDATE documents SET relevance=?, relevance_reason=?, "
                    "relevance_judged_at=? WHERE id=?",
                    (rel, reason, now, doc_id),
                )
                total_updated += 1
            conn.commit()

        conn.close()

    print(f"\n完了: {total_updated} 件更新" + (" (dry-run)" if args.dry_run else ""))


def cmd_export(args, logger) -> None:
    targets = get_docs_db_paths(args.index_name)
    rows_all: list[dict] = []
    for idx_name, db_path in targets:
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        ensure_relevance_columns(conn)
        for r in conn.execute(
            "SELECT id, title, type, description, shared_by, shared_at, "
            "channel_id, related_topic, url, relevance, relevance_reason "
            "FROM documents ORDER BY id"
        ):
            rows_all.append({
                "index_name": idx_name,
                "id": r["id"],
                "relevance": r["relevance"] or "unknown",
                "final_relevance": r["relevance"] or "unknown",
                "relevance_reason": r["relevance_reason"] or "",
                "title": r["title"] or "",
                "type": r["type"] or "",
                "shared_by": r["shared_by"] or "",
                "shared_at": r["shared_at"] or "",
                "channel_id": r["channel_id"] or "",
                "related_topic": r["related_topic"] or "",
                "description": (r["description"] or "")[:300],
                "url": r["url"] or "",
            })
        conn.close()

    # noise を先頭にソート → unknown → related → core
    order = {"noise": 0, "unknown": 1, "related": 2, "core": 3}
    rows_all.sort(key=lambda x: (order.get(x["relevance"], 9), x["index_name"], x["id"]))

    fields = ["index_name", "id", "relevance", "final_relevance", "relevance_reason",
              "title", "type", "shared_by", "shared_at", "channel_id",
              "related_topic", "description", "url"]
    out_path = Path(args.output)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows_all:
            w.writerow(r)
    print(f"書き出し完了: {out_path} ({len(rows_all)} 行)")


def cmd_import(args, logger) -> None:
    in_path = Path(args.import_csv)
    if not in_path.exists():
        print(f"ファイルなし: {in_path}")
        sys.exit(1)

    # index_name ごとに docs をまとめて更新
    updates: dict[str, list[tuple[int, str]]] = {}
    with open(in_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            idx = row["index_name"]
            final = row.get("final_relevance", "").strip().lower()
            if final not in VALID_RELEVANCE:
                continue
            try:
                doc_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            updates.setdefault(idx, []).append((doc_id, final))

    if not updates:
        print("有効な更新行なし")
        return

    total = 0
    now = datetime.now().isoformat()
    for idx_name, items in updates.items():
        db_path = DATA_DIR / f"docs_{idx_name}.db"
        if not db_path.exists():
            print(f"[WARN] {db_path} が存在しません。スキップ")
            continue
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        ensure_relevance_columns(conn)
        changed = 0
        for doc_id, final in items:
            cur = conn.execute("SELECT relevance FROM documents WHERE id=?", (doc_id,))
            existing = cur.fetchone()
            if not existing:
                continue
            if existing["relevance"] == final:
                continue
            if args.dry_run:
                print(f"  [DRY] {idx_name} id={doc_id} {existing['relevance']} → {final}")
            else:
                conn.execute(
                    "UPDATE documents SET relevance=?, relevance_judged_at=? WHERE id=?",
                    (final, now, doc_id),
                )
            changed += 1
        if not args.dry_run:
            conn.commit()
        conn.close()
        print(f"[{idx_name}] 更新: {changed} 件")
        total += changed

    print(f"\n完了: {total} 件" + (" (dry-run)" if args.dry_run else ""))


def cmd_stats(args, logger) -> None:
    targets = get_docs_db_paths(args.index_name)
    print(f"{'index':12s} {'core':>6s} {'related':>8s} {'noise':>6s} "
          f"{'unknown':>8s} {'未判定':>8s} {'合計':>6s}")
    print("-" * 60)
    for idx_name, db_path in targets:
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        ensure_relevance_columns(conn)
        counts = {"core": 0, "related": 0, "noise": 0, "unknown": 0, None: 0}
        for r in conn.execute("SELECT relevance, COUNT(*) FROM documents GROUP BY relevance"):
            counts[r[0]] = r[1]
        total = sum(counts.values())
        conn.close()
        print(f"{idx_name:12s} {counts['core']:>6d} {counts['related']:>8d} "
              f"{counts['noise']:>6d} {counts['unknown']:>8d} {counts[None]:>8d} "
              f"{total:>6d}")


def main() -> None:
    parser = argparse.ArgumentParser(description="docs_*.db のドキュメントを relevance 判定・精査")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--judge", action="store_true", help="LLMで relevance を判定")
    g.add_argument("--export", action="store_true", help="CSV にエクスポート")
    g.add_argument("--import", dest="import_csv", metavar="PATH", help="CSV をインポート")
    g.add_argument("--stats", action="store_true", help="relevance 分布を集計")

    parser.add_argument("--index-name", default=None, help="特定インデックスのみ")
    parser.add_argument("--force", action="store_true", help="判定済みも再判定（--judge）")
    parser.add_argument("--output", default="docs_screen.csv", help="--export の出力先")
    parser.add_argument("--dry-run", action="store_true", help="DB更新なし")
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("pm_document_screen")

    if args.judge:
        cmd_judge(args, logger)
    elif args.export:
        cmd_export(args, logger)
    elif args.import_csv:
        cmd_import(args, logger)
    elif args.stats:
        cmd_stats(args, logger)


if __name__ == "__main__":
    main()
