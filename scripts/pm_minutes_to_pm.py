#!/usr/bin/env python3
"""
pm_minutes_to_pm.py

議事録DB（data/minutes/{kind}.db）の内容を LLM 不使用で pm.db に転記する。

pm_minutes_import.py で構造化済みの担当者・期限情報をそのまま pm.db の
action_items テーブルに書き込む。milestone_id は空のまま転記し、
Canvas または pm_relink.py で後から補完する。

Usage:
    # 全会議名を転記
    python3 scripts/pm_minutes_to_pm.py

    # 特定会議名のみ転記
    python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting

    # 日付フィルタ
    python3 scripts/pm_minutes_to_pm.py --since 2026-01-01

    # 確認用（DB保存なし）
    python3 scripts/pm_minutes_to_pm.py --dry-run

Options:
    --meeting-name NAME     特定の会議名のみ処理（省略時は全DBを対象）
    --minutes-dir DIR       議事録DBのディレクトリ（デフォルト: data/minutes/）
    --db PATH               pm.db のパス（必須: data/pm.db / data/pm-hpc.db / data/pm-bmt.db）
    --since YYYY-MM-DD      この日付以降の会議のみ転記
    --force                 既存レコードを上書き
    --dry-run               DB保存なし・転記内容を表示のみ
    --no-encrypt            平文モード
    --delete MEETING_ID     指定した meeting_id を pm.db から削除して終了
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db, init_pm_db as init_db, normalize_assignee
from cli_utils import add_dry_run_arg, add_no_encrypt_arg, add_since_arg
from pm_minutes_import import db_path_for_kind, init_minutes_db


# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"
DEFAULT_DB = REPO_ROOT / "data" / "pm.db"


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

    # minutes_content の先頭500文字をsummaryに使用
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
            (meeting_id, d["content"], held_at, source_ref, d["source_context"], now),
        )

    for a in action_items:
        pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at)"
            " VALUES (?, ?, ?, ?, 'open', 'meeting', ?, ?)",
            (meeting_id, a["content"], normalize_assignee(a["assignee"]), a["due_date"],
             source_ref, now),
        )

    pm_conn.commit()
    return "ok"


# --------------------------------------------------------------------------- #
# 1つのminutes DBを処理
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

        # minutes_connは都度開く（コミット後に最新データを読む）
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
    """指定 meeting_id を pm.db から削除する"""
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
def list_pm(db_path: Path, kind_filter: str | None,
            since: str | None, no_encrypt: bool) -> None:
    """pm.db の meetings テーブルを一覧表示する"""
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
# メイン
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="議事録DB（data/minutes/）→ pm.db への転記（LLM不使用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 全会議を転記
  python3 scripts/pm_minutes_to_pm.py

  # 特定会議名のみ
  python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting

  # dry-run で確認
  python3 scripts/pm_minutes_to_pm.py --dry-run

  # 日付フィルタ
  python3 scripts/pm_minutes_to_pm.py --since 2026-01-01 --force

  # pm.db の転記済み一覧を確認
  python3 scripts/pm_minutes_to_pm.py --list
  python3 scripts/pm_minutes_to_pm.py --list --meeting-name Leader_Meeting
  python3 scripts/pm_minutes_to_pm.py --list --since 2026-03-01

  # pm.db から削除
  python3 scripts/pm_minutes_to_pm.py --delete 2026-03-10_Leader_Meeting
""",
    )
    parser.add_argument("--meeting-name", default=None,
                        help="特定の会議名のみ処理（省略時は全DBを対象）")
    parser.add_argument("--minutes-dir", default=None,
                        help="議事録DBのディレクトリ（デフォルト: data/minutes/）")
    parser.add_argument("--db", default=None, help="pm.db のパス（必須: data/pm.db / data/pm-hpc.db / data/pm-bmt.db）")
    add_since_arg(parser)
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true",
                        help="pm.db の転記済み会議一覧を表示して終了")
    parser.add_argument("--delete", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id を pm.db から削除して終了")
    args = parser.parse_args()

    minutes_dir = Path(args.minutes_dir) if args.minutes_dir else DEFAULT_MINUTES_DIR
    if not args.db:
        print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
        print("  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db", file=sys.stderr)
        sys.exit(1)
    db_path = Path(args.db)

    # --- list ---
    if args.list:
        list_pm(db_path, args.meeting_name, args.since, args.no_encrypt)
        return

    # --- delete ---
    if args.delete:
        pm_conn = init_db(db_path, no_encrypt=args.no_encrypt)
        delete_from_pm(pm_conn, args.delete, args.dry_run)
        pm_conn.close()
        return

    if not minutes_dir.exists():
        print(f"ERROR: 議事録DBディレクトリが見つかりません: {minutes_dir}", file=sys.stderr)
        sys.exit(1)

    # 対象DBファイルを収集
    if args.meeting_name:
        safe = re.sub(r"[^\w\-]", "_", args.meeting_name)
        db_files = [minutes_dir / f"{safe}.db"]
        if not db_files[0].exists():
            print(f"ERROR: 議事録DBが見つかりません: {db_files[0]}", file=sys.stderr)
            sys.exit(1)
    else:
        db_files = sorted(minutes_dir.glob("*.db"))
        if not db_files:
            print("[INFO] 議事録DBが見つかりません。処理を終了します。")
            return

    print(f"[INFO] 議事録DB   : {minutes_dir}")
    print(f"[INFO] pm.db      : {db_path}")
    print(f"[INFO] 対象DB数   : {len(db_files)} 件")
    if args.since:
        print(f"[INFO] since      : {args.since}")
    if args.dry_run:
        print("[INFO] --dry-run モード（DB保存なし）")
    if args.force:
        print("[INFO] --force モード（既存レコードを上書き）")

    pm_conn = init_db(db_path, no_encrypt=args.no_encrypt)

    total_ok = total_skipped = 0
    for db_file in db_files:
        print(f"\n{'='*60}")
        print(f"  会議名: {db_file.stem}")
        print(f"{'='*60}")
        ok, skipped = process_minutes_db(
            db_file, pm_conn,
            since=args.since, force=args.force, dry_run=args.dry_run,
            no_encrypt=args.no_encrypt,
        )
        total_ok      += ok
        total_skipped += skipped

    pm_conn.close()

    print(f"\n完了: 転記={total_ok}件, スキップ={total_skipped}件")
    if args.dry_run:
        print("（--dry-run のため実際には保存されていません）")


if __name__ == "__main__":
    main()
