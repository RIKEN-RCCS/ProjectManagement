#!/usr/bin/env python3
"""
pm_knowledge_edit.py — knowledge.db への人手介入 CLI

普段使わない前提（蒸留は LLM が自動で行う）。
investigate などで「これは古い」「内容が違う」と気付いたときに、最小手数で
無効化・上書き連鎖・確度修正を行うための経路を提供する。

設計思想（docs/distill_policy.md 参照）:
- 物理削除しない（deleted=1 の論理削除のみ）
- 全変更は knowledge_audit に source='human_edit' で記録
- pm_relink.py と同じ CSV エクスポート/インポートのフローも提供

Usage:
  # 一覧
  python3 scripts/pm_knowledge_edit.py --list
  python3 scripts/pm_knowledge_edit.py --list --include-deleted

  # 1件詳細
  python3 scripts/pm_knowledge_edit.py --show KN-0042

  # 即時操作（多くはこれで足りる）
  python3 scripts/pm_knowledge_edit.py --invalidate KN-0042
  python3 scripts/pm_knowledge_edit.py --supersede KN-0042 KN-0099
  python3 scripts/pm_knowledge_edit.py --confidence KN-0042 low
  python3 scripts/pm_knowledge_edit.py --restore KN-0042

  # CSV 一括編集（pm_relink.py と同じ思想）
  python3 scripts/pm_knowledge_edit.py --export
  python3 scripts/pm_knowledge_edit.py --export --output knowledge_edit.csv
  python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv
  python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_utils import make_logger
from db_utils import open_knowledge_db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
KNOWLEDGE_DB = DATA_DIR / "knowledge.db"

# CSV で編集可能なフィールド
_EDITABLE_FIELDS = [
    "topic", "current_state", "rationale",
    "alternatives_rejected", "constraints_invariants",
    "tags", "owners", "decided_at", "last_validated_at",
    "confidence", "superseded_by", "deleted",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# audit ヘルパ
# --------------------------------------------------------------------------- #

def audit(conn, kid: str, field: str, old, new, *, actor: str | None) -> None:
    if str(old or "") == str(new or ""):
        return
    conn.execute(
        "INSERT INTO knowledge_audit"
        " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
        " VALUES (?, ?, ?, ?, ?, 'human_edit', ?)",
        (kid, field, str(old) if old is not None else None,
         str(new) if new is not None else None, now_iso(), actor),
    )


def fetch_record(conn, kid: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM knowledge WHERE id = ?", (kid,)
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# 即時操作
# --------------------------------------------------------------------------- #

def cmd_invalidate(conn, kid: str, *, actor: str | None, log) -> bool:
    rec = fetch_record(conn, kid)
    if not rec:
        log(f"[ERROR] {kid} が見つかりません")
        return False
    audit(conn, kid, "deleted", rec.get("deleted", 0), 1, actor=actor)
    conn.execute(
        "UPDATE knowledge SET deleted = 1, updated_at = ? WHERE id = ?",
        (now_iso(), kid),
    )
    conn.commit()
    log(f"[OK] {kid} を deleted=1 (論理削除) にしました")
    return True


def cmd_restore(conn, kid: str, *, actor: str | None, log) -> bool:
    rec = fetch_record(conn, kid)
    if not rec:
        log(f"[ERROR] {kid} が見つかりません")
        return False
    audit(conn, kid, "deleted", rec.get("deleted", 0), 0, actor=actor)
    conn.execute(
        "UPDATE knowledge SET deleted = 0, updated_at = ? WHERE id = ?",
        (now_iso(), kid),
    )
    conn.commit()
    log(f"[OK] {kid} を deleted=0 (復活) にしました")
    return True


def cmd_supersede(conn, old_id: str, new_id: str, *, actor: str | None, log) -> bool:
    if old_id == new_id:
        log("[ERROR] 同じ ID を指定しました")
        return False
    old_rec = fetch_record(conn, old_id)
    new_rec = fetch_record(conn, new_id)
    if not old_rec:
        log(f"[ERROR] {old_id} が見つかりません")
        return False
    if not new_rec:
        log(f"[ERROR] {new_id} が見つかりません")
        return False
    audit(conn, old_id, "superseded_by", old_rec.get("superseded_by"), new_id, actor=actor)
    conn.execute(
        "UPDATE knowledge SET superseded_by = ?, updated_at = ? WHERE id = ?",
        (new_id, now_iso(), old_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_relations"
        " (from_id, to_id, relation, note, created_at)"
        " VALUES (?, ?, 'supersedes', NULL, ?)",
        (new_id, old_id, now_iso()),
    )
    conn.commit()
    log(f"[OK] {old_id}.superseded_by = {new_id} に設定")
    return True


def cmd_confidence(conn, kid: str, value: str, *, actor: str | None, log) -> bool:
    if value not in ("high", "medium", "low"):
        log("[ERROR] confidence は high / medium / low のいずれかを指定")
        return False
    rec = fetch_record(conn, kid)
    if not rec:
        log(f"[ERROR] {kid} が見つかりません")
        return False
    audit(conn, kid, "confidence", rec.get("confidence"), value, actor=actor)
    conn.execute(
        "UPDATE knowledge SET confidence = ?, updated_at = ? WHERE id = ?",
        (value, now_iso(), kid),
    )
    conn.commit()
    log(f"[OK] {kid}.confidence = {value}")
    return True


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #

def cmd_list(conn, *, include_deleted: bool, log) -> None:
    where = "" if include_deleted else "WHERE COALESCE(deleted, 0) = 0"
    rows = conn.execute(
        f"SELECT id, kind, confidence, deleted, superseded_by, last_validated_at, topic"
        f" FROM knowledge {where} ORDER BY id"
    ).fetchall()
    if not rows:
        log("（レコードなし）")
        return
    log(f"{'ID':<10} {'KIND':<11} {'CONF':<7} {'DEL':<3} {'SUPERSEDE':<10}"
        f" {'VALIDATED':<11} TOPIC")
    log("-" * 100)
    for r in rows:
        log(
            f"{r['id']:<10} {r['kind']:<11} {r['confidence']:<7}"
            f" {'1' if r['deleted'] else '0':<3}"
            f" {(r['superseded_by'] or '-'):<10} {(r['last_validated_at'] or '-'):<11}"
            f" {r['topic']}"
        )


def cmd_show(conn, kid: str, log) -> None:
    rec = fetch_record(conn, kid)
    if not rec:
        log(f"[ERROR] {kid} が見つかりません")
        return
    for k in [
        "id", "kind", "topic", "current_state", "rationale",
        "alternatives_rejected", "constraints_invariants", "tags", "owners",
        "decided_at", "last_validated_at", "confidence",
        "superseded_by", "deleted", "created_at", "updated_at",
    ]:
        v = rec.get(k)
        log(f"{k:<24}: {v}")
    log("-- sources --")
    for r in conn.execute(
        "SELECT source_type, source_ref, weight, excerpt"
        " FROM knowledge_sources WHERE knowledge_id = ? ORDER BY added_at",
        (kid,),
    ).fetchall():
        excerpt = (r["excerpt"] or "")[:100].replace("\n", " ")
        log(f"  [{r['weight']}] {r['source_type']}/{r['source_ref']} : {excerpt}")
    log("-- relations --")
    for r in conn.execute(
        "SELECT relation, to_id, note FROM knowledge_relations WHERE from_id = ?"
        " UNION ALL SELECT relation, from_id, note FROM knowledge_relations WHERE to_id = ?"
        " ORDER BY 1",
        (kid, kid),
    ).fetchall():
        log(f"  {r[0]} → {r[1]} {('(' + r[2] + ')') if r[2] else ''}")
    log("-- audit (直近10) --")
    for r in conn.execute(
        "SELECT field, old_value, new_value, changed_at, source, actor"
        " FROM knowledge_audit WHERE knowledge_id = ?"
        " ORDER BY changed_at DESC LIMIT 10",
        (kid,),
    ).fetchall():
        actor = r["actor"] or "-"
        log(f"  {r['changed_at'][:19]} {r['source']:<11} {actor:<10}"
            f" {r['field']}: {r['old_value']!r} -> {r['new_value']!r}")


# --------------------------------------------------------------------------- #
# CSV エクスポート / インポート
# --------------------------------------------------------------------------- #

def cmd_export(conn, output: Path, *, include_deleted: bool, log) -> None:
    where = "" if include_deleted else "WHERE COALESCE(deleted, 0) = 0"
    rows = conn.execute(
        "SELECT id, kind, " + ", ".join(_EDITABLE_FIELDS) + f" FROM knowledge {where} ORDER BY id"
    ).fetchall()
    fields = ["id", "kind"] + _EDITABLE_FIELDS
    # Excel が UTF-8 として認識できるよう BOM 付きで書き出す（utf-8-sig）。
    # BOM は他ツール（pandas / Python csv reader）でも透過的に処理される。
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            d = {k: (r[k] if r[k] is not None else "") for k in fields}
            w.writerow(d)
    log(f"[OK] {len(rows)} 件を {output} にエクスポートしました")


def cmd_import(conn, csv_path: Path, *, actor: str | None, dry_run: bool, log) -> None:
    if not csv_path.exists():
        log(f"[ERROR] {csv_path} が見つかりません")
        return
    # utf-8-sig は BOM があれば自動で剥がし、BOM なし UTF-8 でも問題なく読める。
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    changes = 0
    skipped = 0
    not_found = 0

    for r in rows:
        kid = (r.get("id") or "").strip()
        if not kid:
            skipped += 1
            continue
        rec = fetch_record(conn, kid)
        if not rec:
            log(f"  [WARN] {kid}: 該当レコードなし、スキップ")
            not_found += 1
            continue

        updates: dict = {}
        for fld in _EDITABLE_FIELDS:
            if fld not in r:
                continue
            new_val = r[fld]
            if new_val is None:
                continue
            new_val = new_val.strip() if isinstance(new_val, str) else new_val
            old_val = rec.get(fld)

            # 空文字 → 文字列フィールドはスキップ（変更なし）
            if new_val == "":
                continue
            # deleted は 0/1
            if fld == "deleted":
                try:
                    new_int = int(new_val)
                except Exception:
                    continue
                if new_int not in (0, 1):
                    continue
                if (old_val or 0) != new_int:
                    updates["deleted"] = new_int
                continue
            # confidence
            if fld == "confidence" and new_val not in ("high", "medium", "low"):
                continue
            if str(old_val or "") != str(new_val):
                updates[fld] = new_val

        if not updates:
            continue

        log(f"  {kid}:")
        for k, v in updates.items():
            log(f"    {k}: {rec.get(k)!r} -> {v!r}")

        if dry_run:
            changes += 1
            continue

        for k, v in updates.items():
            audit(conn, kid, k, rec.get(k), v, actor=actor)
        cols = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [now_iso(), kid]
        conn.execute(
            f"UPDATE knowledge SET {cols}, updated_at = ? WHERE id = ?", params,
        )
        # supersede が指定された場合は relations にも反映
        if "superseded_by" in updates and updates["superseded_by"]:
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_relations"
                " (from_id, to_id, relation, note, created_at)"
                " VALUES (?, ?, 'supersedes', NULL, ?)",
                (updates["superseded_by"], kid, now_iso()),
            )
        changes += 1

    if not dry_run:
        conn.commit()
    log("---")
    log(f"変更: {changes} 件 / 該当なし: {not_found} 件 / 空行: {skipped} 件"
        + (" (dry-run)" if dry_run else ""))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="knowledge.db 人手編集 CLI")
    p.add_argument("--list", action="store_true", help="一覧表示")
    p.add_argument("--include-deleted", action="store_true", help="削除済みも含める")
    p.add_argument("--show", metavar="KN-XXXX", help="1件詳細")
    p.add_argument("--invalidate", metavar="KN-XXXX", help="deleted=1 にする")
    p.add_argument("--restore", metavar="KN-XXXX", help="deleted=0 にする")
    p.add_argument("--supersede", nargs=2, metavar=("OLD", "NEW"),
                   help="OLD.superseded_by = NEW を立てる")
    p.add_argument("--confidence", nargs=2, metavar=("ID", "LEVEL"),
                   help="confidence を high/medium/low に変更")
    p.add_argument("--export", action="store_true",
                   help="CSV にエクスポート")
    p.add_argument("--import", dest="import_csv", metavar="PATH",
                   help="CSV を読み込んで knowledge.db に反映")
    p.add_argument("--output", default="knowledge_edit.csv", metavar="PATH",
                   help="--export の出力先（デフォルト: knowledge_edit.csv）")
    p.add_argument("--actor", default=None, metavar="NAME",
                   help="変更者名（audit に記録）")
    p.add_argument("--dry-run", action="store_true",
                   help="--import 時に DB 更新せず変更内容のみ表示")
    p.add_argument("--no-encrypt", action="store_true", help="平文モード")
    p.add_argument("--log-output", default=None, metavar="PATH",
                   help="ログをファイルにも保存")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    log, close_log = make_logger(args.log_output)
    try:
        if not KNOWLEDGE_DB.exists():
            log(f"[ERROR] {KNOWLEDGE_DB} が見つかりません。"
                "まず scripts/pm_box_distill.py を実行してください")
            sys.exit(1)

        conn = open_knowledge_db(KNOWLEDGE_DB, no_encrypt=args.no_encrypt)
        try:
            if args.list:
                cmd_list(conn, include_deleted=args.include_deleted, log=log)
                return
            if args.show:
                cmd_show(conn, args.show, log)
                return
            if args.invalidate:
                cmd_invalidate(conn, args.invalidate, actor=args.actor, log=log)
                return
            if args.restore:
                cmd_restore(conn, args.restore, actor=args.actor, log=log)
                return
            if args.supersede:
                old_id, new_id = args.supersede
                cmd_supersede(conn, old_id, new_id, actor=args.actor, log=log)
                return
            if args.confidence:
                kid, level = args.confidence
                cmd_confidence(conn, kid, level, actor=args.actor, log=log)
                return
            if args.export:
                cmd_export(conn, Path(args.output),
                           include_deleted=args.include_deleted, log=log)
                return
            if args.import_csv:
                cmd_import(conn, Path(args.import_csv),
                           actor=args.actor, dry_run=args.dry_run, log=log)
                return
            log("操作が指定されていません。--help を参照してください。")
        finally:
            conn.close()
    finally:
        close_log()


if __name__ == "__main__":
    main()
