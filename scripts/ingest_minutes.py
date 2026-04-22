#!/usr/bin/env python3
"""
ingest_minutes.py

議事録DB（data/minutes/{kind}.db）→ pm.db への転記プラグイン。
元ロジックは pm_minutes_to_pm.py から移植。pm_minutes_to_pm.py は後方互換 CLI ラッパーとして残す。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db, init_pm_db as _init_pm_db, normalize_assignee
from ingest_plugin import IngestContext


# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"


# --------------------------------------------------------------------------- #
# 議事録 DB 接続
# --------------------------------------------------------------------------- #
def init_minutes_db(db_file: Path, no_encrypt: bool = False):
    from pm_minutes_import import init_minutes_db as _init
    return _init(db_file, no_encrypt=no_encrypt)


# --------------------------------------------------------------------------- #
# 転記コア
# --------------------------------------------------------------------------- #
def transfer_meeting(
    pm_conn,
    minutes_conn,
    meeting_id: str,
    held_at: str,
    kind: str,
    file_path: str | None,
    force: bool,
    dry_run: bool,
    log=print,
) -> str:
    """Returns: "ok" | "skipped" """
    existing = pm_conn.execute(
        "SELECT meeting_id FROM meetings WHERE held_at = ? AND kind = ?", (held_at, kind)
    ).fetchone()

    if existing and not force:
        log(f"  [SKIP] {held_at}/{kind} は既に pm.db に存在します（--force で上書き可能）")
        return "skipped"

    mc_row = minutes_conn.execute(
        "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1", (meeting_id,)
    ).fetchone()
    summary = (mc_row["content"][:500] if mc_row else "") or ""

    decisions = minutes_conn.execute(
        "SELECT content, source_context FROM decisions WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()

    action_items = minutes_conn.execute(
        "SELECT content, assignee, due_date FROM action_items WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()

    log(f"  decisions   : {len(decisions)} 件")
    for d in decisions:
        ctx = f" [出典: {d['source_context']}]" if d["source_context"] else ""
        log(f"    - {d['content']}{ctx}")
    log(f"  action_items: {len(action_items)} 件")
    for a in action_items:
        assignee = a["assignee"] or "未定"
        due = f" (期限: {a['due_date']})" if a["due_date"] else ""
        log(f"    [{assignee}] {a['content']}{due}")

    if dry_run:
        return "ok"

    now = datetime.now().isoformat()
    source_ref = file_path or ""

    if force:
        pm_conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
        pm_conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
        pm_conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))

    pm_conn.execute(
        "INSERT OR IGNORE INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (meeting_id, held_at, kind, source_ref, summary, now),
    )

    for d in decisions:
        pm_conn.execute(
            "INSERT INTO decisions"
            " (meeting_id, content, decided_at, source, source_ref, source_context, extracted_at)"
            " VALUES (?, ?, ?, 'meeting', ?, ?, ?)",
            (meeting_id, d["content"], held_at, source_ref, d["source_context"], held_at),
        )

    for a in action_items:
        pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at)"
            " VALUES (?, ?, ?, ?, 'open', 'meeting', ?, ?)",
            (meeting_id, a["content"], normalize_assignee(a["assignee"]), a["due_date"],
             source_ref, held_at),
        )

    pm_conn.commit()
    return "ok"


# --------------------------------------------------------------------------- #
# 1つの議事録 DB を処理
# --------------------------------------------------------------------------- #
def process_minutes_db(
    db_file: Path,
    pm_conn,
    since: str | None,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
    log=print,
) -> tuple[int, int]:
    """Returns: (ok_count, skipped_count)"""
    kind = db_file.stem

    try:
        minutes_conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
    except Exception as e:
        log(f"[ERROR] DB接続失敗: {db_file}: {e}")
        return 0, 0

    query = "SELECT meeting_id, held_at, file_path FROM instances"
    params: list = []
    if since:
        query += " WHERE held_at >= ?"
        params.append(since)
    query += " ORDER BY held_at"

    instances = minutes_conn.execute(query, params).fetchall()
    minutes_conn.close()

    ok = skipped = 0
    for inst in instances:
        meeting_id = inst["meeting_id"]
        held_at    = inst["held_at"]
        file_path  = inst["file_path"]

        log(f"\n[{kind}] {meeting_id} ({held_at})")

        minutes_conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
        status = transfer_meeting(
            pm_conn, minutes_conn, meeting_id, held_at, kind, file_path,
            force=force, dry_run=dry_run, log=log,
        )
        minutes_conn.close()

        if status == "ok":
            ok += 1
        else:
            skipped += 1

    return ok, skipped


# --------------------------------------------------------------------------- #
# pm.db 削除
# --------------------------------------------------------------------------- #
def delete_from_pm(pm_conn, meeting_id: str, dry_run: bool) -> None:
    existing = pm_conn.execute(
        "SELECT meeting_id, held_at, kind FROM meetings WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()

    if not existing:
        print(f"[ERROR] meeting_id '{meeting_id}' が pm.db に見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 削除対象: {meeting_id} ({existing['held_at']}, {existing['kind']})")

    if dry_run:
        print("[INFO] --dry-run のため削除をスキップしました")
        return

    pm_conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    pm_conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
    pm_conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
    pm_conn.commit()
    print(f"[INFO] {meeting_id} を pm.db から削除しました")


# --------------------------------------------------------------------------- #
# pm.db 一覧表示
# --------------------------------------------------------------------------- #
def list_pm(db_path: Path, kind_filter: str | None, since: str | None, no_encrypt: bool) -> None:
    if not db_path.exists():
        print(f"[ERROR] pm.db が見つかりません: {db_path}", file=sys.stderr)
        return

    conn = open_db(db_path, encrypt=not no_encrypt)

    query = """
        SELECT
            m.meeting_id,
            m.held_at,
            m.kind,
            m.parsed_at,
            COUNT(DISTINCT d.id)  AS d_count,
            COUNT(DISTINCT a.id)  AS ai_count
        FROM meetings m
        LEFT JOIN decisions    d ON d.meeting_id = m.meeting_id AND d.source = 'meeting'
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id AND a.source = 'meeting'
    """
    params: list = []
    wheres: list = []
    if kind_filter:
        wheres.append("m.kind = ?")
        params.append(kind_filter)
    if since:
        wheres.append("m.held_at >= ?")
        params.append(since)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " GROUP BY m.meeting_id ORDER BY m.held_at DESC, m.kind"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("（該当するレコードなし）")
        return

    print(f"{'開催日':<12} {'会議名':<25} {'決定数':>5} {'AI数':>5}  {'登録日時':<19}  meeting_id")
    print("-" * 95)
    for r in rows:
        parsed_at = (r["parsed_at"] or "")[:19]
        print(
            f"{r['held_at']:<12} {(r['kind'] or ''):<25} {r['d_count']:>5} {r['ai_count']:>5}"
            f"  {parsed_at:<19}  {r['meeting_id']}"
        )
    print(f"\n合計: {len(rows)} 件")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class MinutesIngestPlugin:
    source_name = "minutes"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--minutes-name", default=None,
            metavar="NAME",
            help="特定の会議名のみ処理（minutes ソース用、省略時は全DBを対象）",
        )
        parser.add_argument(
            "--minutes-dir", default=None,
            metavar="DIR",
            help="議事録DBのディレクトリ（minutes ソース用、デフォルト: data/minutes/）",
        )
        parser.add_argument(
            "--minutes-force", action="store_true",
            help="既存レコードを上書き（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-list", action="store_true",
            help="pm.db の転記済み会議一覧を表示して終了（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-delete", default=None,
            metavar="MEETING_ID",
            help="指定した meeting_id を pm.db から削除して終了（minutes ソース用）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        minutes_dir = (
            Path(args.minutes_dir) if getattr(args, "minutes_dir", None)
            else DEFAULT_MINUTES_DIR
        )

        if getattr(args, "minutes_list", False):
            list_pm(ctx.pm_db_path, getattr(args, "minutes_name", None), ctx.since, ctx.no_encrypt)
            return

        if getattr(args, "minutes_delete", None):
            delete_from_pm(ctx.pm_conn, args.minutes_delete, ctx.dry_run)
            return

        if not minutes_dir.exists():
            print(f"ERROR: 議事録DBディレクトリが見つかりません: {minutes_dir}", file=sys.stderr)
            sys.exit(1)

        meeting_name = getattr(args, "minutes_name", None)
        if meeting_name:
            safe = re.sub(r"[^\w\-]", "_", meeting_name)
            db_files = [minutes_dir / f"{safe}.db"]
            if not db_files[0].exists():
                print(f"ERROR: 議事録DBが見つかりません: {db_files[0]}", file=sys.stderr)
                sys.exit(1)
        else:
            db_files = sorted(minutes_dir.glob("*.db"))
            if not db_files:
                ctx.log("[INFO] 議事録DBが見つかりません。処理を終了します。")
                return

        ctx.log(f"[INFO] 議事録DB   : {minutes_dir}")
        ctx.log(f"[INFO] 対象DB数   : {len(db_files)} 件")
        if ctx.since:
            ctx.log(f"[INFO] since      : {ctx.since}")
        if ctx.dry_run:
            ctx.log("[INFO] --dry-run モード（DB保存なし）")
        force = getattr(args, "minutes_force", False)
        if force:
            ctx.log("[INFO] --force モード（既存レコードを上書き）")

        total_ok = total_skipped = 0
        for db_file in db_files:
            ctx.log(f"\n{'='*60}")
            ctx.log(f"  会議名: {db_file.stem}")
            ctx.log(f"{'='*60}")
            ok, skipped = process_minutes_db(
                db_file, ctx.pm_conn,
                since=ctx.since, force=force, dry_run=ctx.dry_run,
                no_encrypt=ctx.no_encrypt, log=ctx.log,
            )
            total_ok      += ok
            total_skipped += skipped

        ctx.log(f"\n完了: 転記={total_ok}件, スキップ={total_skipped}件")
        if ctx.dry_run:
            ctx.log("（--dry-run のため実際には保存されていません）")
