#!/usr/bin/env python3
"""
pm_minutes_publish.py — Publish minutes edits to pm.db and Box XLSX.

Called as a subprocess by AdminJobQueue when a user edits minutes in the Web UI.

Pipeline:
  1. Sync minutes.db -> pm.db  (transfer_meeting with force=True)
  2. Reconstruct minutes Markdown -> upload to Box (version update)
  3. Rebuild XLSX from pm.db  (build_workbook)
  4. Upload XLSX to Box       (box_upload_or_version with optimistic locking)

Usage (by AdminJobQueue):
    python3 scripts/pm_minutes_publish.py \\
        --meeting-id MEETING_ID --kind KIND --held-at YYYY-MM-DD
        [--file-path PATH] [--no-encrypt]
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from box_cli import box_find_file, box_json, box_upload_or_version
from db_utils import fetch_milestone_progress, open_pm_db
from ingest.minutes import transfer_meeting
from pm_minutes_import import init_minutes_db, reconstruct_minutes_md
from pm_report import (
    detect_risk_items,
    fetch_open_action_items,
    fetch_recent_decisions,
)
from pm_xlsx_report import (
    _build_meeting_url_map,
    build_workbook,
    load_report_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_CONFIG = REPO_ROOT / "data" / "argus_config.yaml"
DEFAULT_FILENAME = "pm_report.xlsx"


# --------------------------------------------------------------------------- #
# Box helpers
# --------------------------------------------------------------------------- #

def get_file_modified_at(file_id: str) -> str | None:
    """Box ファイルの modified_at を取得する。"""
    try:
        info = box_json(
            ["box", "files:get", file_id, "--json", "--fields", "modified_at"],
            timeout=60,
        )
        if isinstance(info, list):
            info = info[0] if info else {}
        return info.get("modified_at")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Stage 2: Update minutes Markdown on Box
# --------------------------------------------------------------------------- #

def _update_minutes_md_on_box(
    minutes_conn, meeting_id: str, held_at: str, kind: str, log,
) -> None:
    """minutes.db の内容から Markdown を再構築し、Box 上のファイルをバージョン更新する。"""
    # Read current data from minutes.db
    mc_row = minutes_conn.execute(
        "SELECT content FROM minutes_content WHERE meeting_id=? ORDER BY id DESC LIMIT 1",
        (meeting_id,),
    ).fetchone()
    decisions = minutes_conn.execute(
        "SELECT content, source_context FROM decisions WHERE meeting_id=? ORDER BY id",
        (meeting_id,),
    ).fetchall()
    action_items = minutes_conn.execute(
        "SELECT content, assignee, due_date FROM action_items WHERE meeting_id=? ORDER BY id",
        (meeting_id,),
    ).fetchall()

    # Reconstruct Markdown
    data = {
        "decisions": [dict(d) for d in decisions] if decisions else [],
        "action_items": [dict(a) for a in action_items] if action_items else [],
        "minutes_content": mc_row["content"] if mc_row else "",
    }
    md = reconstruct_minutes_md(held_at, kind, data)

    # Look up box_file_id from upload_log
    row = minutes_conn.execute(
        "SELECT box_file_id FROM upload_log WHERE meeting_id=?",
        (meeting_id,),
    ).fetchone()
    box_file_id = row["box_file_id"] if row else None

    if not box_file_id:
        log("  [SKIP] No box_file_id in upload_log — minutes Markdown not on Box yet")
        return

    # Write to temp file and upload
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="minutes_", delete=False, encoding="utf-8",
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(md)
        tmp.close()

        box_json(
            ["box", "files:versions:upload", box_file_id, str(tmp_path), "--json"],
            timeout=120,
        )
        log(f"  Box minutes Markdown updated (file_id={box_file_id})")
    except Exception as e:
        log(f"  WARNING: Box upload failed for minutes Markdown: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Publish minutes edits to pm.db and Box XLSX",
    )
    parser.add_argument("--meeting-id", default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--held-at", default=None)
    parser.add_argument("--file-path", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-encrypt", action="store_true")
    parser.add_argument("--xlsx-only", action="store_true",
                        help="XLSX 更新のみ（minutes.db 同期スキップ）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
    minutes_dir = db_path.parent / "minutes"
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG

    def log(msg: str) -> None:
        print(msg)

    if args.xlsx_only:
        log("[xlsx-only] XLSX 更新のみ実行")
        pm_conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    else:
        if not args.meeting_id or not args.kind or not args.held_at:
            log("  ERROR: --xlsx-only 未指定時は --meeting-id, --kind, --held-at が必須です")
            sys.exit(1)

        minutes_db_file = minutes_dir / f"{args.kind}.db"
        if not minutes_db_file.exists():
            log(f"  ERROR: minutes DB not found: {minutes_db_file}")
            sys.exit(1)

        # ---- Stage 1: Sync minutes.db -> pm.db ---- #
        log(f"[1/4] Syncing minutes.db -> pm.db: {args.meeting_id}")

        pm_conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
        minutes_conn = init_minutes_db(minutes_db_file, no_encrypt=args.no_encrypt)

        result = transfer_meeting(
            pm_conn, minutes_conn,
            args.meeting_id, args.held_at, args.kind, args.file_path,
            force=True, dry_run=False, log=log,
        )
        log(f"  Sync result: {result}")

        # ---- Stage 2: Update minutes Markdown on Box ---- #
        log("[2/4] Updating minutes Markdown on Box")
        _update_minutes_md_on_box(minutes_conn, args.meeting_id, args.held_at, args.kind, log)

        minutes_conn.close()

    # ---- Stage 3: Rebuild XLSX from pm.db ---- #
    log("[3/4] Rebuilding XLSX from pm.db")

    today = date.today().isoformat()
    action_items = fetch_open_action_items(pm_conn, since=None)
    decisions = fetch_recent_decisions(pm_conn, since=None, show_acknowledged=True)
    risk_items = detect_risk_items(action_items)
    milestones = fetch_milestone_progress(pm_conn)
    url_map = _build_meeting_url_map(action_items + decisions, minutes_dir)

    wb = build_workbook(
        action_items, decisions, risk_items, milestones,
        url_map, today, since=None,
    )

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(
        suffix=".xlsx", prefix="pm_report_", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        wb.save(tmp_path)
        tmp.close()
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # ---- Stage 4: Upload XLSX to Box with optimistic locking ---- #
    log("[4/4] Uploading XLSX to Box")

    report_cfg = load_report_config(config_path)
    folder_id = report_cfg.get("box_folder_id")
    filename = report_cfg.get("filename") or DEFAULT_FILENAME

    if folder_id:
        file_id = box_find_file(folder_id, filename)

        if file_id:
            ts_before = get_file_modified_at(file_id)
            log(f"  Box file modified_at at job start: {ts_before}")

            box_upload_or_version(tmp_path, folder_id, filename, log)

            ts_after = get_file_modified_at(file_id)
            if ts_before and ts_after and ts_before != ts_after:
                log("  WARNING: Concurrent modification detected!")
                log(f"    Before: {ts_before}")
                log(f"    After:  {ts_after}")
                log("    Skipping — Box file was modified by another job.")
            else:
                log(f"  Box upload complete (file_id={file_id})")
        else:
            log("  File not found on Box, uploading new...")
            box_upload_or_version(tmp_path, folder_id, filename, log)
    else:
        log("  [WARN] box_folder_id not configured — skipping Box upload")

    # Cleanup
    pm_conn.close()
    tmp_path.unlink(missing_ok=True)

    log("Done.")


if __name__ == "__main__":
    main()
