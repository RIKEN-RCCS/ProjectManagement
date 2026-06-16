#!/usr/bin/env python3
"""
pm_knowledge_inspect.py — knowledge.db の重複・多重抽出を検証する診断スクリプト

蒸留が暴れていないかを観測するための読み取り専用ツール。
件数が想定を超えた（例: 500件超）ときに最初に走らせる。

検査項目:
  (1) 1 source_ref から派生したナレッジ数（多い順）
  (2) topic 完全一致の重複
  (3) topic 先頭 N 文字一致の重複
  (4) current_state 正規化一致の重複
  (5) 同じ source_ref 内で current_state 先頭が一致する組

Usage:
  python3 scripts/pm_knowledge_inspect.py
  python3 scripts/pm_knowledge_inspect.py --top 50
  python3 scripts/pm_knowledge_inspect.py --skip-source --skip-near
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_utils import open_knowledge_db, open_db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KDB = REPO_ROOT / "data" / "knowledge.db"
BOX = REPO_ROOT / "data" / "box_docs.db"


def per_source(top: int = 30) -> None:
    """1 source_ref から派生したナレッジ数（多い順）。"""
    print(f"\n=== (1) 1 ソースあたりの派生レコード数 (Top {top}) ===")
    conn = open_knowledge_db(KDB, no_encrypt=False)
    rows = conn.execute(
        "SELECT s.source_type, s.source_ref, COUNT(DISTINCT s.knowledge_id) AS n"
        " FROM knowledge_sources s"
        " JOIN knowledge k ON k.id = s.knowledge_id"
        " WHERE COALESCE(k.deleted, 0) = 0"
        " GROUP BY s.source_type, s.source_ref"
        " ORDER BY n DESC LIMIT ?",
        (top,),
    ).fetchall()
    conn.close()

    box_names: dict[str, str] = {}
    if BOX.exists():
        bx = open_db(BOX, encrypt=True)
        try:
            for r in bx.execute(
                "SELECT box_file_id, name, folder_path FROM box_files"
            ).fetchall():
                box_names[r["box_file_id"]] = (
                    f"{r['folder_path'] or ''}/{r['name']}".lstrip("/")
                )
        finally:
            bx.close()

    print(f"{'#':>3}  {'TYPE':<10} {'REF':<30} NAME")
    for r in rows:
        name = box_names.get(r["source_ref"], "")
        print(f"{r['n']:>3}  {r['source_type']:<10} {r['source_ref']:<30} {name[:80]}")


def topic_dup(prefix_len: int = 20) -> None:
    """topic 完全一致 / 先頭 N 文字一致の重複。"""
    conn = open_knowledge_db(KDB, no_encrypt=False)
    print("\n=== (2) topic 完全一致 ===")
    rows = conn.execute(
        "SELECT topic, COUNT(*) FROM knowledge"
        " WHERE COALESCE(deleted,0)=0 GROUP BY topic"
        " HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC LIMIT 30"
    ).fetchall()
    if not rows:
        print("  （該当なし）")
    for t, n in rows:
        print(f"  {n:>3}  {t}")

    print(f"\n=== (3) topic 先頭 {prefix_len} 文字一致 ===")
    rows = conn.execute(
        "SELECT id, topic FROM knowledge WHERE COALESCE(deleted,0)=0"
    ).fetchall()
    groups: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        prefix = (r["topic"] or "")[:prefix_len]
        groups[prefix].append(r["id"])
    found = False
    for prefix, ids in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(ids) < 3:
            break
        found = True
        suffix = "..." if len(ids) > 5 else ""
        print(f"  {len(ids):>3}  {prefix!r}: {', '.join(ids[:5])}{suffix}")
    if not found:
        print("  （3件以上の先頭一致なし）")
    conn.close()


def current_state_dup(min_group: int = 2) -> None:
    """current_state の空白除去・小文字化後に一致するもの。"""
    print("\n=== (4) current_state 正規化一致 ===")
    conn = open_knowledge_db(KDB, no_encrypt=False)
    rows = conn.execute(
        "SELECT id, topic, current_state FROM knowledge"
        " WHERE COALESCE(deleted,0)=0"
    ).fetchall()
    norm_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        norm = "".join((r["current_state"] or "").split()).lower()
        norm_groups[norm].append((r["id"], r["topic"]))
    found = False
    for norm, items in sorted(norm_groups.items(), key=lambda x: -len(x[1])):
        if len(items) < min_group:
            break
        found = True
        print(f"\n  {len(items)} 件: {norm[:80]!r}")
        for kid, t in items[:8]:
            print(f"    - {kid} {t[:60]}")
        if len(items) > 8:
            print(f"    ...他 {len(items) - 8} 件")
    if not found:
        print("  （正規化一致の重複なし）")
    conn.close()


def near_dup_within_source(prefix_len: int = 30) -> None:
    """同じ source_ref から派生したレコードのうち、current_state 先頭が一致するもの。"""
    print(f"\n=== (5) 同じ source_ref 内で current_state 先頭{prefix_len}文字が一致 ===")
    conn = open_knowledge_db(KDB, no_encrypt=False)
    rows = conn.execute(
        "SELECT s.source_ref, k.id, k.topic, k.current_state"
        " FROM knowledge_sources s JOIN knowledge k ON k.id = s.knowledge_id"
        " WHERE s.source_type='box_file' AND COALESCE(k.deleted,0)=0"
    ).fetchall()
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[r["source_ref"]].append(dict(r))
    flagged = 0
    for src, items in by_source.items():
        if len(items) < 2:
            continue
        prefix_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in items:
            p = "".join((r["current_state"] or "").split())[:prefix_len].lower()
            prefix_groups[p].append((r["id"], r["topic"]))
        for prefix, ids in prefix_groups.items():
            if len(ids) >= 2:
                flagged += 1
                if flagged <= 30:
                    print(f"\n  source={src}  prefix={prefix[:40]!r}")
                    for kid, t in ids:
                        print(f"    - {kid} {t[:60]}")
    if not flagged:
        print("  （該当なし）")
    elif flagged > 30:
        print(f"\n  ...他 {flagged - 30} 組")
    conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="knowledge.db 重複・多重抽出の診断")
    p.add_argument("--top", type=int, default=30,
                   help="(1) で表示する上位件数（デフォルト30）")
    p.add_argument("--topic-prefix", type=int, default=20,
                   help="(3) の topic 先頭一致の文字数（デフォルト20）")
    p.add_argument("--cs-prefix", type=int, default=30,
                   help="(5) の current_state 先頭一致の文字数（デフォルト30）")
    p.add_argument("--skip-source", action="store_true",
                   help="(1) をスキップ")
    p.add_argument("--skip-topic", action="store_true",
                   help="(2)(3) をスキップ")
    p.add_argument("--skip-cs", action="store_true",
                   help="(4) をスキップ")
    p.add_argument("--skip-near", action="store_true",
                   help="(5) をスキップ")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not KDB.exists():
        print(f"[ERROR] {KDB} が見つかりません", file=sys.stderr)
        sys.exit(1)
    if not args.skip_source:
        per_source(top=args.top)
    if not args.skip_topic:
        topic_dup(prefix_len=args.topic_prefix)
    if not args.skip_cs:
        current_state_dup()
    if not args.skip_near:
        near_dup_within_source(prefix_len=args.cs_prefix)


if __name__ == "__main__":
    main()
