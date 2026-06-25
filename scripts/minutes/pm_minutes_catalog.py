#!/usr/bin/env python3
"""
pm_minutes_catalog.py

議事録DBから Markdown ファイルを Box にアップロードし、
Slack Canvas 上に目録（Box 共有リンク一覧）を生成する。

設定ソース: data/argus_config.yaml の meetings: セクション
    meetings:
      Leader_Meeting:
        pm_db: pm.db
        box_folder_id: "123456789"     # 議事録 MD のアップロード先 Box フォルダ
        catalog_canvas_id: <CANVAS_ID>   # 目録を書き込む Slack Canvas ID

Usage:
    python3 scripts/pm_minutes_catalog.py --upload
    python3 scripts/pm_minutes_catalog.py --catalog
    python3 scripts/pm_minutes_catalog.py --upload --catalog
    python3 scripts/pm_minutes_catalog.py --upload --meeting-name Leader_Meeting --since 2026-04-01
    python3 scripts/pm_minutes_catalog.py --list
    python3 scripts/pm_minutes_catalog.py --upload --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_utils import (
    add_dry_run_arg, add_no_encrypt_arg, add_output_arg, add_since_arg,
    make_logger,
)
from pm_minutes_import import (
    db_path_for_kind, init_minutes_db, reconstruct_minutes_md,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = REPO_ROOT / "data" / "argus_config.yaml"
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"

UPLOAD_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_log (
    meeting_id     TEXT PRIMARY KEY,
    box_folder_id  TEXT NOT NULL,
    box_file_id    TEXT NOT NULL,
    box_shared_url TEXT,
    uploaded_at    TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# 設定読み込み
# --------------------------------------------------------------------------- #
def load_meetings_config(config_path: Path) -> dict[str, dict]:
    """argus_config.yaml の meetings: セクションを読む。"""
    if not config_path.exists():
        print(f"[ERROR] 設定ファイルが見つかりません: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    meetings = cfg.get("meetings") or {}
    if not meetings:
        print(f"[ERROR] meetings: が定義されていません: {config_path}", file=sys.stderr)
        sys.exit(1)
    return meetings


from box_cli import (
    box_upload_or_version,
    box_get_or_create_shared_link,
)


# --------------------------------------------------------------------------- #
# DB ヘルパ
# --------------------------------------------------------------------------- #
def _ensure_upload_log(conn):
    # 旧スキーマ（channel_id・permalink）が残っている場合は作り直す
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='upload_log'"
    ).fetchone()
    if row:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_log)").fetchall()}
        if "box_file_id" not in cols:
            conn.execute("DROP TABLE upload_log")
    conn.executescript(UPLOAD_LOG_SCHEMA)


def _load_instances(conn, kind: str, since: str | None):
    sql = "SELECT meeting_id, held_at, kind FROM instances WHERE kind = ?"
    params: list = [kind]
    if since:
        sql += " AND held_at >= ?"
        params.append(since)
    sql += " ORDER BY held_at DESC"
    return conn.execute(sql, params).fetchall()


def _load_meeting_data(conn, meeting_id: str) -> dict:
    mc_row = conn.execute(
        "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1",
        (meeting_id,),
    ).fetchone()
    return {
        "decisions": [dict(r) for r in conn.execute(
            "SELECT content, source_context FROM decisions WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()],
        "action_items": [dict(r) for r in conn.execute(
            "SELECT content, assignee, due_date FROM action_items WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()],
        "minutes_content": mc_row["content"] if mc_row else None,
    }


# --------------------------------------------------------------------------- #
# --upload
# --------------------------------------------------------------------------- #
def cmd_upload(args, meetings: dict[str, dict], minutes_dir: Path, log) -> None:
    total_uploaded = 0
    total_skipped = 0
    total_failed = 0

    for kind, mcfg in sorted(meetings.items()):
        if args.meeting_name and kind != args.meeting_name:
            continue
        folder_id = mcfg.get("box_folder_id")
        if not folder_id:
            continue

        db_path = db_path_for_kind(minutes_dir, kind)
        if not db_path.exists():
            log(f"[WARN] DB が見つかりません: {db_path}")
            continue

        conn = init_minutes_db(db_path, no_encrypt=args.no_encrypt)
        _ensure_upload_log(conn)

        instances = _load_instances(conn, kind, args.since)
        if not instances:
            conn.close()
            continue

        for inst in instances:
            mid = inst["meeting_id"]
            held_at = inst["held_at"]
            filename = f"{held_at}_{kind}.md"

            existing = conn.execute(
                "SELECT box_file_id, box_shared_url FROM upload_log WHERE meeting_id = ?",
                (mid,),
            ).fetchone()
            if existing and not args.force:
                total_skipped += 1
                continue

            if args.dry_run:
                log(f"  [DRY] {filename} → Box folder {folder_id}")
                total_uploaded += 1
                continue

            data = _load_meeting_data(conn, mid)
            md_content = reconstruct_minutes_md(held_at, kind, data)

            try:
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td) / filename
                    tmp.write_text(md_content, encoding="utf-8")
                    file_id = box_upload_or_version(tmp, folder_id, filename, log)
                    shared_url = box_get_or_create_shared_link(file_id, log)
            except RuntimeError as e:
                log(f"  [ERROR] {filename}: {e}")
                total_failed += 1
                continue

            conn.execute(
                "INSERT OR REPLACE INTO upload_log"
                " (meeting_id, box_folder_id, box_file_id, box_shared_url, uploaded_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (mid, folder_id, file_id, shared_url, datetime.now().isoformat()),
            )
            conn.commit()
            log(f"  [OK] {filename} (file_id={file_id}) → {shared_url}")
            total_uploaded += 1

        conn.close()

    log(f"\n完了: {total_uploaded} 件アップロード、"
        f"{total_skipped} 件スキップ、{total_failed} 件失敗")


# --------------------------------------------------------------------------- #
# --catalog
# --------------------------------------------------------------------------- #
def cmd_catalog(args, meetings: dict[str, dict], minutes_dir: Path, log) -> None:
    from canvas_utils import post_to_canvas, sanitize_for_canvas

    # catalog_canvas_id ごとに会議種別をまとめる
    by_canvas: dict[str, list[str]] = {}
    for kind, mcfg in meetings.items():
        if args.meeting_name and kind != args.meeting_name:
            continue
        canvas_id = mcfg.get("catalog_canvas_id")
        if not canvas_id:
            continue
        by_canvas.setdefault(canvas_id, []).append(kind)

    if not by_canvas:
        log("[INFO] catalog_canvas_id が設定された会議がありません")
        return

    for canvas_id, kinds in by_canvas.items():
        sections: list[str] = ["# 議事録目録", ""]
        has_entries = False

        for kind in sorted(kinds):
            db_path = db_path_for_kind(minutes_dir, kind)
            if not db_path.exists():
                continue

            conn = init_minutes_db(db_path, no_encrypt=args.no_encrypt)
            _ensure_upload_log(conn)
            rows = conn.execute(
                "SELECT u.meeting_id, i.held_at, u.box_shared_url"
                " FROM upload_log u"
                " JOIN instances i ON u.meeting_id = i.meeting_id"
                " WHERE i.kind = ?"
                " ORDER BY i.held_at DESC",
                (kind,),
            ).fetchall()
            conn.close()
            if not rows:
                continue

            has_entries = True
            sections.append(f"## {kind}")
            sections.append("")
            for r in rows:
                held_at = r["held_at"]
                url = r["box_shared_url"] or ""
                label = f"{held_at} {kind}"
                if url:
                    sections.append(f"- [{label}]({url})")
                else:
                    sections.append(f"- {label}")
            sections.append("")

        if not has_entries:
            log(f"[SKIP] Canvas {canvas_id}: アップロード済みの議事録がありません")
            continue

        catalog_md = "\n".join(sections)

        if args.dry_run:
            log(f"--- Canvas {canvas_id} ---")
            log(catalog_md)
            log("")
            continue

        post_to_canvas(canvas_id, sanitize_for_canvas(catalog_md))
        log(f"[OK] Canvas {canvas_id} に目録を投稿しました ({len(kinds)} 種別)")


# --------------------------------------------------------------------------- #
# --list
# --------------------------------------------------------------------------- #
def cmd_list(args, meetings: dict[str, dict], minutes_dir: Path, log) -> None:
    for kind, mcfg in sorted(meetings.items()):
        if args.meeting_name and kind != args.meeting_name:
            continue
        if not mcfg.get("box_folder_id"):
            continue

        db_path = db_path_for_kind(minutes_dir, kind)
        if not db_path.exists():
            continue

        conn = init_minutes_db(db_path, no_encrypt=args.no_encrypt)
        _ensure_upload_log(conn)

        instances = _load_instances(conn, kind, args.since)
        if not instances:
            conn.close()
            continue

        log(f"\n=== {kind} ({len(instances)} 件, Box folder {mcfg['box_folder_id']}) ===")
        log(f"{'meeting_id':<40} {'held_at':<12} {'box_file_id':<14} url")
        log(f"{'-'*40} {'-'*12} {'-'*14} {'-'*30}")

        for inst in instances:
            mid = inst["meeting_id"]
            row = conn.execute(
                "SELECT box_file_id, box_shared_url FROM upload_log WHERE meeting_id = ?",
                (mid,),
            ).fetchone()
            file_id = row["box_file_id"] if row else "-"
            url = (row["box_shared_url"] if row else "") or "(未アップロード)"
            log(f"{mid:<40} {inst['held_at']:<12} {file_id:<14} {url}")

        conn.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="議事録 Box 一括アップロード・Canvas 目録生成",
    )
    parser.add_argument("--upload", action="store_true",
                        help="未アップロードの議事録を Box に一括アップロード")
    parser.add_argument("--catalog", action="store_true",
                        help="目録 Canvas を更新")
    parser.add_argument("--list", action="store_true", dest="show_list",
                        help="アップロード状態を一覧表示")
    parser.add_argument("--meeting-name",
                        help="特定の会議種別のみ対象")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="設定ファイル（デフォルト: data/argus_config.yaml）")
    parser.add_argument("--minutes-dir", default=str(DEFAULT_MINUTES_DIR),
                        help="議事録DBディレクトリ（デフォルト: data/minutes/）")
    parser.add_argument("--force", action="store_true",
                        help="アップロード済みも再アップロード（Box はバージョン更新）")
    add_since_arg(parser)
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_output_arg(parser)

    args = parser.parse_args()

    if not (args.upload or args.catalog or args.show_list):
        parser.print_help()
        sys.exit(1)

    log, log_close = make_logger(args.output if hasattr(args, "output") else None)
    meetings = load_meetings_config(Path(args.config))
    minutes_dir = Path(args.minutes_dir)

    try:
        if args.show_list:
            cmd_list(args, meetings, minutes_dir, log)
        if args.upload:
            cmd_upload(args, meetings, minutes_dir, log)
        if args.catalog:
            cmd_catalog(args, meetings, minutes_dir, log)
    finally:
        log_close()


if __name__ == "__main__":
    main()
