#!/usr/bin/env python3
"""指定日より前の古い AI / decisions を一括「完了/確認済み」にする。

- action_items: status='open' のもののうち extracted_at < CUTOFF を status='closed' に
- decisions  : acknowledged_at IS NULL のもののうち decided_at < CUTOFF を ack 済みに

全変更は audit_log に source='close_old_<cutoff>' で記録するため可逆。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_utils import open_pm_db  # noqa: E402


def collect(conn, cutoff: str) -> tuple[list[dict], list[dict]]:
    ai = conn.execute(
        """
        SELECT id, content, assignee, due_date, status, extracted_at, channel_id, source
        FROM action_items
        WHERE status='open' AND COALESCE(deleted,0)=0 AND extracted_at < ?
        """,
        (cutoff,),
    ).fetchall()
    dec = conn.execute(
        """
        SELECT id, content, decided_at, channel_id, source, acknowledged_at
        FROM decisions
        WHERE COALESCE(deleted,0)=0 AND acknowledged_at IS NULL AND decided_at < ?
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in ai], [dict(r) for r in dec]


def apply(conn, ai_targets: list[dict], dec_targets: list[dict],
          source_tag: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for r in ai_targets:
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
            " VALUES ('action_items', ?, 'status', 'open', 'closed', ?, ?)",
            (str(r["id"]), now, source_tag),
        )
        cur.execute("UPDATE action_items SET status='closed' WHERE id=?", (r["id"],))
    for r in dec_targets:
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
            " VALUES ('decisions', ?, 'acknowledged_at', NULL, ?, ?, ?)",
            (str(r["id"]), now, now, source_tag),
        )
        cur.execute("UPDATE decisions SET acknowledged_at=? WHERE id=?", (now, r["id"]))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff", default="2026-04-01",
                    help="この日付より前の AI / decisions を対象（既定: 2026-04-01）")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-encrypt", action="store_true")
    ap.add_argument("--ai-only", action="store_true")
    ap.add_argument("--decisions-only", action="store_true")
    args = ap.parse_args()

    conn = open_pm_db(REPO_ROOT / args.db, no_encrypt=args.no_encrypt)
    ai_targets, dec_targets = collect(conn, args.cutoff)
    if args.ai_only:
        dec_targets = []
    if args.decisions_only:
        ai_targets = []

    print(f"cutoff           = {args.cutoff}")
    print(f"対象 action_items: {len(ai_targets)} 件 (extracted_at < {args.cutoff} かつ status='open')")
    print(f"対象 decisions   : {len(dec_targets)} 件 (decided_at < {args.cutoff} かつ acknowledged_at IS NULL)")
    print()

    if ai_targets:
        from collections import Counter
        print("--- action_items 古さ分布 (extracted_at) ---")
        bucket = Counter()
        for r in ai_targets:
            ex = (r.get("extracted_at") or "")[:7]
            bucket[ex] += 1
        for k, v in sorted(bucket.items()):
            print(f"  {k}  {v}")
    if dec_targets:
        from collections import Counter
        print()
        print("--- decisions 古さ分布 (decided_at) ---")
        bucket = Counter()
        for r in dec_targets:
            ex = (r.get("decided_at") or "")[:7]
            bucket[ex] += 1
        for k, v in sorted(bucket.items()):
            print(f"  {k}  {v}")

    if args.dry_run:
        print()
        print("[INFO] --dry-run のため DB 更新なし")
        return 0

    print()
    print("[INFO] 適用中...")
    source_tag = f"close_old_{args.cutoff}"
    apply(conn, ai_targets, dec_targets, source_tag)
    print(f"[完了] action_items={len(ai_targets)} 件 status='closed', "
          f"decisions={len(dec_targets)} 件 acknowledged_at 設定")
    print(f"       audit_log の source = '{source_tag}'")
    print()
    print("復元したい場合の例:")
    print(f"  python3 scripts/db_utils.py --audit-log --source {source_tag} --limit 30")
    return 0


if __name__ == "__main__":
    sys.exit(main())
