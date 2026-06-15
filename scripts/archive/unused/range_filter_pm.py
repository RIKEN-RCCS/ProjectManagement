#!/usr/bin/env python3
"""argus_config.yaml indices.{name} の範囲外 AI / decisions を deleted=1 にする。

slack 由来は channel_id、meeting 由来は meetings.kind で判定。
全変更は audit_log に source='range_filter_{index}' で記録するため、
復元したい場合は pm_relink.py や直接 SQL で deleted=0 に戻せる。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_utils import open_pm_db  # noqa: E402


def load_index_scope(config_path: Path, index_name: str) -> tuple[set[str], set[str]]:
    cfg = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    indices = cfg.get("indices") or {}
    if index_name not in indices:
        raise SystemExit(f"index '{index_name}' is not defined in {config_path}")
    entry = indices[index_name] or {}
    channels = {c.strip() for c in (entry.get("channels") or []) if c and c.strip()}
    minutes = {m for m in (entry.get("minutes") or []) if m}
    return channels, minutes


def collect_targets(conn, channels: set[str], minutes_kinds: set[str]) -> tuple[list[dict], list[dict]]:
    cph = ",".join(["?"] * len(channels)) if channels else "''"
    mph = ",".join(["?"] * len(minutes_kinds)) if minutes_kinds else "''"

    ai_rows = conn.execute(
        f"""
        SELECT a.id, a.content, a.assignee, a.due_date, a.source,
               a.channel_id, a.source_ref,
               (
                 SELECT m.kind FROM meetings m
                 WHERE a.source='meeting' AND a.source_ref LIKE '%' || m.meeting_id || '%'
                 LIMIT 1
               ) AS meeting_kind
        FROM action_items a
        WHERE COALESCE(a.deleted,0)=0
          AND (
            (a.source='slack'   AND (a.channel_id IS NULL OR a.channel_id NOT IN ({cph})))
            OR
            (a.source='meeting' AND NOT EXISTS (
                SELECT 1 FROM meetings m
                WHERE a.source_ref LIKE '%' || m.meeting_id || '%'
                  AND m.kind IN ({mph})
            ))
          )
        """,
        [*sorted(channels), *sorted(minutes_kinds)],
    ).fetchall()

    dec_rows = conn.execute(
        f"""
        SELECT d.id, d.content, d.decided_at, d.source,
               d.channel_id, d.source_ref,
               (
                 SELECT m.kind FROM meetings m
                 WHERE d.source='meeting' AND d.source_ref LIKE '%' || m.meeting_id || '%'
                 LIMIT 1
               ) AS meeting_kind
        FROM decisions d
        WHERE COALESCE(d.deleted,0)=0
          AND (
            (d.source='slack'   AND (d.channel_id IS NULL OR d.channel_id NOT IN ({cph})))
            OR
            (d.source='meeting' AND NOT EXISTS (
                SELECT 1 FROM meetings m
                WHERE d.source_ref LIKE '%' || m.meeting_id || '%'
                  AND m.kind IN ({mph})
            ))
          )
        """,
        [*sorted(channels), *sorted(minutes_kinds)],
    ).fetchall()

    return [dict(r) for r in ai_rows], [dict(r) for r in dec_rows]


def apply_delete(conn, ai_targets: list[dict], dec_targets: list[dict],
                 source_tag: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for r in ai_targets:
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
            " VALUES ('action_items', ?, 'deleted', '0', '1', ?, ?)",
            (str(r["id"]), now, source_tag),
        )
        cur.execute("UPDATE action_items SET deleted=1 WHERE id=?", (r["id"],))
    for r in dec_targets:
        cur.execute(
            "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
            " VALUES ('decisions', ?, 'deleted', '0', '1', ?, ?)",
            (str(r["id"]), now, source_tag),
        )
        cur.execute("UPDATE decisions SET deleted=1 WHERE id=?", (r["id"],))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index-name", default="pm",
                    help="argus_config.yaml の index 名（既定: pm）")
    ap.add_argument("--config", default="data/argus_config.yaml")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="件数と内訳のみ表示し DB 更新しない")
    ap.add_argument("--no-encrypt", action="store_true")
    ap.add_argument("--ai-only", action="store_true",
                    help="action_items のみ対象。decisions は触らない")
    args = ap.parse_args()

    channels, minutes_kinds = load_index_scope(REPO_ROOT / args.config, args.index_name)
    conn = open_pm_db(REPO_ROOT / args.db, no_encrypt=args.no_encrypt)
    ai_targets, dec_targets = collect_targets(conn, channels, minutes_kinds)
    if args.ai_only:
        dec_targets = []

    print(f"index_name      = {args.index_name}")
    print(f"channels (pm)   = {len(channels)} 件")
    print(f"minutes  (pm)   = {len(minutes_kinds)} 件")
    print()
    print(f"対象 action_items: {len(ai_targets)} 件")
    print(f"対象 decisions   : {len(dec_targets)} 件")
    print()

    # 内訳サマリー
    def _by_channel(rows):
        from collections import Counter
        return Counter((r.get("channel_id") or f"(meeting:{r.get('meeting_kind') or 'unknown'})") for r in rows)

    if ai_targets:
        print("--- action_items 内訳 (channel_id / meeting_kind) ---")
        for k, v in _by_channel(ai_targets).most_common(15):
            print(f"  {k:30s}  {v}")
    if dec_targets:
        print()
        print("--- decisions 内訳 ---")
        for k, v in _by_channel(dec_targets).most_common(15):
            print(f"  {k:30s}  {v}")

    if args.dry_run:
        print()
        print("[INFO] --dry-run のため DB 更新なし")
        return 0

    print()
    print("[INFO] 適用中...")
    apply_delete(conn, ai_targets, dec_targets, f"range_filter_{args.index_name}")
    print(f"[完了] action_items={len(ai_targets)} 件, decisions={len(dec_targets)} 件 を deleted=1 に更新")
    print(f"       audit_log の source = 'range_filter_{args.index_name}'")
    print()
    print("復元したい場合の例:")
    print("  python3 scripts/db_utils.py --audit-log --source range_filter_" + args.index_name + " --limit 30")
    print("  -- 個別復元（pm_relink.py の --import で deleted=0 にする）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
