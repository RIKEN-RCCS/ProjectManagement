#!/usr/bin/env python3
"""
pm_knowledge_dedupe.py — 後追い de-duplication

knowledge.db の current_state 正規化一致で重複しているレコードをグループ化し、
各グループから 1 件 (keeper) を残して残りを superseded_by で連鎖させる。
keeper の knowledge_sources には他レコードのソースもマージする
（複数ファイルから抽出された同一決定の根拠を保持する）。

物理削除はしない（全変更は audit に記録、deleted は触らない）。
keeper の選び方: confidence(high優先) → last_validated_at 降順 →
created_at 昇順 → KN-XXXX の数値小さい順。

Usage:
  # まずプランを CSV に出して目で確認（DB変更なし）
  python3 scripts/pm_knowledge_dedupe.py --plan dedupe_plan.csv

  # CSV を見て問題なければ実行
  python3 scripts/pm_knowledge_dedupe.py --apply

  # CSV を編集して keeper を変えたい場合（id 列を変更してから）
  python3 scripts/pm_knowledge_dedupe.py --apply --plan dedupe_plan.csv

  # 一致モード切り替え
  python3 scripts/pm_knowledge_dedupe.py --plan plan.csv --mode topic
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_utils import open_knowledge_db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KDB = REPO_ROOT / "data" / "knowledge.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(s: str | None) -> str:
    return "".join((s or "").split()).lower()


# --------------------------------------------------------------------------- #
# グルーピング
# --------------------------------------------------------------------------- #

def build_groups(conn, mode: str) -> list[list[dict]]:
    """重複グループ（2件以上）のリストを返す。各要素はレコード dict のリスト。

    mode:
      "current_state" — current_state 正規化一致 (デフォルト)
      "topic"         — topic + current_state 正規化のペア一致
                        (topic だけだと別意味の同名が混じる可能性があるため)
    """
    rows = conn.execute(
        "SELECT id, kind, topic, current_state, confidence,"
        " last_validated_at, decided_at, created_at, superseded_by"
        " FROM knowledge"
        " WHERE COALESCE(deleted, 0) = 0 AND superseded_by IS NULL"
    ).fetchall()

    bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rec = dict(r)
        if mode == "topic":
            key = (normalize(rec["topic"]), normalize(rec["current_state"]))
        else:
            key = normalize(rec["current_state"])
        if not (key if isinstance(key, str) else key[0] or key[1]):
            continue
        bucket[key].append(rec)

    return [items for items in bucket.values() if len(items) >= 2]


def pick_keeper(group: list[dict]) -> dict:
    """グループから keeper を 1 件選ぶ。"""
    conf_order = {"high": 0, "medium": 1, "low": 2}

    def sort_key(r: dict) -> tuple:
        cid = r["id"]
        try:
            num = int(cid.split("-")[1])
        except Exception:
            num = 999_999
        return (
            conf_order.get((r.get("confidence") or "").lower(), 9),
            -(int(("".join(c for c in (r["last_validated_at"] or "")
                           if c.isdigit()) or "0"))),
            -(int(("".join(c for c in (r["decided_at"] or "")
                           if c.isdigit()) or "0"))),
            r["created_at"] or "",
            num,
        )

    return sorted(group, key=sort_key)[0]


# --------------------------------------------------------------------------- #
# プラン生成・CSV 入出力
# --------------------------------------------------------------------------- #

def write_plan_csv(groups: list[list[dict]], path: Path) -> int:
    """dedupe プランを CSV に書く。

    ヘッダ: group_id, action, id, keeper_id, kind, confidence, last_validated_at,
            topic, current_state
    action は 'keep' or 'supersede_by_keeper'。
    """
    fields = [
        "group_id", "action", "id", "keeper_id", "kind", "confidence",
        "last_validated_at", "topic", "current_state",
    ]
    n = 0
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for gi, group in enumerate(groups, 1):
            keeper = pick_keeper(group)
            for r in group:
                action = "keep" if r["id"] == keeper["id"] else "supersede_by_keeper"
                w.writerow({
                    "group_id": gi,
                    "action": action,
                    "id": r["id"],
                    "keeper_id": keeper["id"],
                    "kind": r["kind"],
                    "confidence": r["confidence"],
                    "last_validated_at": r["last_validated_at"] or "",
                    "topic": r["topic"] or "",
                    "current_state": (r["current_state"] or "")[:160],
                })
                n += 1
    return n


def load_plan_csv(path: Path) -> list[tuple[str, str]]:
    """CSV から (loser_id, keeper_id) のリストを返す。"""
    pairs: list[tuple[str, str]] = []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("action") == "supersede_by_keeper":
                lid = (row.get("id") or "").strip().upper()
                kid = (row.get("keeper_id") or "").strip().upper()
                if lid and kid and lid != kid:
                    pairs.append((lid, kid))
    return pairs


# --------------------------------------------------------------------------- #
# DB 適用
# --------------------------------------------------------------------------- #

def apply_pairs(conn, pairs: list[tuple[str, str]], *, actor: str, log) -> int:
    """各 (loser, keeper) ペアに対し:
    1. loser.knowledge_sources を keeper にコピー (重複は INSERT OR IGNORE)
    2. loser.superseded_by = keeper を設定
    3. knowledge_relations に supersedes 行を追加
    4. knowledge_audit に source='merge' / actor=<実行者> で記録
    """
    now = now_iso()
    applied = 0
    for loser, keeper in pairs:
        cur = conn.execute(
            "SELECT id, superseded_by FROM knowledge WHERE id = ?", (loser,)
        ).fetchone()
        if not cur:
            log(f"  [SKIP] {loser}: 該当レコードなし")
            continue
        if not conn.execute(
            "SELECT 1 FROM knowledge WHERE id = ?", (keeper,)
        ).fetchone():
            log(f"  [SKIP] keeper {keeper} が見つかりません ({loser} はスキップ)")
            continue
        if cur["superseded_by"] == keeper:
            log(f"  [SKIP] {loser}: 既に {keeper} に supersede 済み")
            continue

        # 1. sources を keeper に複製
        sources = conn.execute(
            "SELECT source_type, source_ref, weight, excerpt"
            " FROM knowledge_sources WHERE knowledge_id = ?",
            (loser,),
        ).fetchall()
        for s in sources:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_sources"
                " (knowledge_id, source_type, source_ref, weight, excerpt, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (keeper, s["source_type"], s["source_ref"],
                 # マージ時は補強扱い (元 keeper の primary を侵さない)
                 "supporting" if s["weight"] == "primary" else s["weight"],
                 s["excerpt"], now),
            )

        # 2. superseded_by を立てる
        conn.execute(
            "UPDATE knowledge SET superseded_by = ?, updated_at = ? WHERE id = ?",
            (keeper, now, loser),
        )
        # 3. relations に supersedes 行
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_relations"
            " (from_id, to_id, relation, note, created_at)"
            " VALUES (?, ?, 'supersedes', 'auto-dedupe', ?)",
            (keeper, loser, now),
        )
        # 4. audit
        conn.execute(
            "INSERT INTO knowledge_audit"
            " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
            " VALUES (?, 'superseded_by', ?, ?, ?, 'merge', ?)",
            (loser, cur["superseded_by"], keeper, now, actor),
        )
        applied += 1

    return applied


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="knowledge.db 後追い dedupe")
    p.add_argument("--mode", choices=["current_state", "topic"],
                   default="current_state",
                   help="重複検出のキー（デフォルト: current_state）")
    p.add_argument("--plan", default=None, metavar="PATH",
                   help="dedupe プランの CSV パス（書き出し or 読み込み）")
    p.add_argument("--apply", action="store_true",
                   help="DB に変更を適用する。--plan を指定すれば編集後の CSV を使う。"
                        "未指定時は内部で生成したプランをそのまま適用")
    p.add_argument("--actor", default="dedupe",
                   help="audit の actor 列に記録する名前")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not KDB.exists():
        print(f"[ERROR] {KDB} が見つかりません", file=sys.stderr)
        sys.exit(1)

    conn = open_knowledge_db(KDB, no_encrypt=False)

    # CSV からペアを読む場合
    pairs: list[tuple[str, str]] | None = None
    if args.apply and args.plan and Path(args.plan).exists():
        pairs = load_plan_csv(Path(args.plan))
        print(f"[INFO] {args.plan} から {len(pairs)} ペアを読み込みました")

    # プラン生成（or 表示）
    if pairs is None:
        groups = build_groups(conn, args.mode)
        total_recs = sum(len(g) for g in groups)
        will_supersede = total_recs - len(groups)
        print(f"[INFO] mode={args.mode} groups={len(groups)}"
              f" total_records={total_recs} will_supersede={will_supersede}")

        if args.plan:
            n = write_plan_csv(groups, Path(args.plan))
            print(f"[OK] {n} 行を {args.plan} に書き出しました")

        # apply 用にペアを生成
        pairs = []
        for group in groups:
            keeper = pick_keeper(group)
            for r in group:
                if r["id"] != keeper["id"]:
                    pairs.append((r["id"], keeper["id"]))

    if not args.apply:
        print("[INFO] --apply 未指定のためここで終了します")
        if not args.plan:
            print("       (プランを CSV に保存するには --plan PATH を指定してください)")
        conn.close()
        return

    # 適用
    print(f"[INFO] {len(pairs)} 件の supersede を適用します...")
    def log(s: str) -> None:
        print(s)
    applied = apply_pairs(conn, pairs, actor=args.actor, log=log)
    conn.commit()
    conn.close()
    print(f"[OK] {applied} 件を適用しました（actor={args.actor}）")


if __name__ == "__main__":
    main()
