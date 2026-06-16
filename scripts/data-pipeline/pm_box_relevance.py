#!/usr/bin/env python3
"""
pm_box_relevance.py

box_docs.db.box_files に対し、本文（doc_content.content_md）の冒頭を
ローカルLLMで読み取り、relevance (core/related/noise/unknown) を判定する
スクリーニングツール。判定結果は box_files.relevance に保存され、
pm_embed.py が relevance='noise' のファイルを索引から除外する。

relevance:
  core    — 富岳NEXTプロジェクトの本質的ナレッジ（設計資料・公式報告書・意思決定資料等）
  related — 関連するが本質ではない（補助資料・参考情報・過去事例等）
  noise   — プロジェクトと無関係 / 索引化するとノイズになる（雑談添付・個人メモ等）
  unknown — 判定不能（情報不足）

Usage:
  # 本文ベースでLLM判定（未判定のみ）
  python3 scripts/pm_box_relevance.py --judge

  # 全件再判定 / 特定 index_name のみ
  python3 scripts/pm_box_relevance.py --judge --force
  python3 scripts/pm_box_relevance.py --judge --index-name pm

  # CSVにエクスポート（精査用、noise を先頭に）
  python3 scripts/pm_box_relevance.py --export --output screen.csv

  # 精査後のCSVをDBに反映
  python3 scripts/pm_box_relevance.py --import screen.csv

  # relevance分布を集計
  python3 scripts/pm_box_relevance.py --stats
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_utils import add_no_encrypt_arg, call_argus_llm
from db_utils import open_db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
BOX_DOCS_DB = DATA_DIR / "box_docs.db"

VALID_RELEVANCE = {"core", "related", "noise", "unknown"}
BATCH_SIZE = 5  # 本文を渡すので少なめ
CONTENT_PREVIEW_CHARS = 2500

JUDGE_PROMPT = """あなたは「富岳NEXT」プロジェクト（次世代スーパーコンピュータ開発）の
ナレッジマネジメント担当です。Box に格納されたドキュメントの本文冒頭を見て、
RAG検索インデックスに残すべきか判定してください。

# プロジェクト文脈
富岳NEXTは理研・富士通・NVIDIA連携による次世代AI-HPCシステム。アプリケーション開発エリア
（HPCアプリケーションWG・ベンチマークWG）のプロジェクトマネジメントを支援している。
本質的ナレッジ = 設計方針・技術仕様・意思決定・議事録・公式報告書・開発成果・ベンチマーク結果等。

# 判定カテゴリ
- core    : プロジェクトの本質的ナレッジ。設計資料・公式報告書・意思決定資料・議事録・技術仕様
- related : 関連するが本質ではない。参考資料・過去事例・外部文献・補助資料
- noise   : 索引化すべきでない。雑談添付・個人メモ・関係ない資料・壊れたファイル・情報不足で意味不明
- unknown : 本文が空・抽出失敗・短すぎて判定不能

# 出力形式
各ドキュメントに対し次の JSON 配列を出力（順序は入力と同じ）:
[
  {{"box_file_id": "<id>", "relevance": "core|related|noise|unknown", "reason": "<1行の根拠>"}},
  ...
]

# 入力ドキュメント
{documents}

JSON配列のみ出力。コードブロック記法不要。"""


def format_doc_for_prompt(row) -> str:
    name = row["name"] or "(名前なし)"
    folder = row["folder_path"] or ""
    content = (row["content_md"] or "").strip()[:CONTENT_PREVIEW_CHARS]
    parts = [f"=== box_file_id={row['box_file_id']} ==="]
    parts.append(f"path: {folder}/{name}" if folder else f"name: {name}")
    if row["file_format"]:
        parts.append(f"format: {row['file_format']}")
    if content:
        parts.append(f"本文(冒頭{CONTENT_PREVIEW_CHARS}字):\n{content}")
    else:
        parts.append("(本文なし)")
    return "\n".join(parts)


def judge_batch(rows: list, logger) -> dict[str, tuple[str, str]]:
    """Returns {box_file_id: (relevance, reason)}."""
    if not rows:
        return {}
    doc_lines = "\n\n".join(format_doc_for_prompt(r) for r in rows)
    prompt = JUDGE_PROMPT.format(documents=doc_lines)
    try:
        result = call_argus_llm(prompt, max_tokens=2048, timeout=300)
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

    out: dict[str, tuple[str, str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("box_file_id") or "").strip()
        if not fid:
            continue
        rel = str(item.get("relevance", "unknown")).lower().strip()
        if rel not in VALID_RELEVANCE:
            rel = "unknown"
        reason = str(item.get("reason", ""))[:300]
        out[fid] = (rel, reason)
    return out


def cmd_judge(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return

    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)

    where = ["dc.content_md IS NOT NULL"]
    if not args.force:
        where.append("(bf.relevance IS NULL OR bf.relevance = '')")
    if args.index_name:
        where.append(f"bf.index_name LIKE '%\"{args.index_name}\"%'")
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"SELECT bf.box_file_id, bf.name, bf.folder_path, bf.file_format,"
        f" dc.content_md"
        f" FROM box_files bf JOIN doc_content dc"
        f" ON bf.box_file_id = dc.box_file_id"
        f" WHERE {where_sql}"
        f" ORDER BY bf.box_file_id"
    ).fetchall()

    if not rows:
        print("判定対象なし")
        conn.close()
        return

    print(f"判定対象: {len(rows)} 件")
    now = datetime.now().isoformat()
    total_updated = 0
    processed = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        verdicts = judge_batch(batch, logger)
        processed += len(batch)
        print(f"  [{processed}/{len(rows)}] バッチ処理 (判定: {len(verdicts)}/{len(batch)})")

        if args.dry_run:
            for r in batch:
                v = verdicts.get(r["box_file_id"])
                if v:
                    print(f"    {r['box_file_id']} {v[0]:7s} {(r['name'] or '')[:50]} — {v[1][:60]}")
            continue

        for fid, (rel, reason) in verdicts.items():
            conn.execute(
                "UPDATE box_files SET relevance=?, relevance_reason=?,"
                " relevance_judged_at=? WHERE box_file_id=?",
                (rel, reason, now, fid),
            )
            total_updated += 1
        conn.commit()

    conn.close()
    print(f"\n完了: {total_updated} 件更新" + (" (dry-run)" if args.dry_run else ""))


def cmd_export(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return
    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)

    where = ""
    params: list = []
    if args.index_name:
        where = "WHERE index_name LIKE ?"
        params = [f'%"{args.index_name}"%']
    rows = conn.execute(
        f"SELECT box_file_id, name, folder_path, file_format, modified_at,"
        f" index_name, source_name, relevance, relevance_reason"
        f" FROM box_files {where} ORDER BY relevance, name", params
    ).fetchall()
    conn.close()

    out_rows = []
    for r in rows:
        out_rows.append({
            "box_file_id": r["box_file_id"],
            "relevance": r["relevance"] or "",
            "final_relevance": r["relevance"] or "",
            "relevance_reason": r["relevance_reason"] or "",
            "name": r["name"] or "",
            "folder_path": r["folder_path"] or "",
            "file_format": r["file_format"] or "",
            "modified_at": r["modified_at"] or "",
            "index_name": r["index_name"] or "",
            "source_name": r["source_name"] or "",
        })

    order = {"noise": 0, "unknown": 1, "": 2, "related": 3, "core": 4}
    out_rows.sort(key=lambda x: (order.get(x["relevance"], 9), x["name"]))

    fields = ["box_file_id", "relevance", "final_relevance", "relevance_reason",
              "name", "folder_path", "file_format", "modified_at",
              "index_name", "source_name"]
    out_path = Path(args.output)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)
    print(f"書き出し完了: {out_path} ({len(out_rows)} 行)")


_INVALID_FID_RE = re.compile(r"[^0-9]")


def _looks_like_box_file_id(s: str) -> bool:
    """純粋に数字のみで構成された box_file_id か（Excel指数表記を弾く）。"""
    return bool(s) and _INVALID_FID_RE.search(s) is None


def cmd_import(args, logger) -> None:
    in_path = Path(args.import_csv)
    if not in_path.exists():
        print(f"ファイルなし: {in_path}")
        sys.exit(1)

    rows: list[dict] = []
    with open(in_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            final = (row.get("final_relevance") or "").strip().lower()
            if final not in VALID_RELEVANCE:
                continue
            rows.append({
                "box_file_id": (row.get("box_file_id") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "folder_path": (row.get("folder_path") or "").strip(),
                "final": final,
            })

    if not rows:
        print("有効な更新行なし")
        return

    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    now = datetime.now().isoformat()
    changed = 0
    skipped = 0
    fid_lookup_failed = 0

    for row in rows:
        fid = row["box_file_id"]
        existing = None
        if _looks_like_box_file_id(fid):
            existing = conn.execute(
                "SELECT box_file_id, relevance FROM box_files WHERE box_file_id=?", (fid,)
            ).fetchone()

        # box_file_id でマッチしないとき (folder_path, name) で逆引き
        if existing is None and row["name"]:
            cands = conn.execute(
                "SELECT box_file_id, relevance FROM box_files"
                " WHERE name=? AND COALESCE(folder_path,'')=?",
                (row["name"], row["folder_path"]),
            ).fetchall()
            if len(cands) == 1:
                existing = cands[0]
            elif len(cands) > 1:
                logger.warning(
                    f"name+folder_path で複数候補 ({len(cands)} 件): {row['folder_path']}/{row['name']}"
                )
                fid_lookup_failed += 1
                continue

        if existing is None:
            fid_lookup_failed += 1
            continue

        if existing["relevance"] == row["final"]:
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {existing['box_file_id']} {existing['relevance']} → {row['final']}")
        else:
            conn.execute(
                "UPDATE box_files SET relevance=?, relevance_judged_at=?"
                " WHERE box_file_id=?",
                (row["final"], now, existing["box_file_id"]),
            )
        changed += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"完了: {changed} 件更新"
          f"（変化なし {skipped}, 行特定不能 {fid_lookup_failed}）"
          + (" (dry-run)" if args.dry_run else ""))


def cmd_stats(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return
    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    counts = {"core": 0, "related": 0, "noise": 0, "unknown": 0, None: 0}
    for r in conn.execute("SELECT relevance, COUNT(*) FROM box_files GROUP BY relevance"):
        counts[r[0]] = r[1]
    total = sum(counts.values())
    conn.close()
    print(f"core    : {counts['core']:>6d}")
    print(f"related : {counts['related']:>6d}")
    print(f"noise   : {counts['noise']:>6d}")
    print(f"unknown : {counts['unknown']:>6d}")
    print(f"未判定  : {counts[None]:>6d}")
    print(f"合計    : {total:>6d}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="box_docs.db のドキュメントを本文ベースで relevance 判定・精査"
    )
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
    logger = logging.getLogger("pm_box_relevance")

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
