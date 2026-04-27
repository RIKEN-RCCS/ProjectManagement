#!/usr/bin/env python3
"""
pm_minutes_catalog.py

議事録DBから Markdown ファイルを一括アップロードし、
チャンネルごとの Canvas 目録（クリッカブルリンク一覧）を生成する。

Usage:
    # 未アップロード分を一括アップロード
    python3 scripts/pm_minutes_catalog.py --upload

    # 目録Canvasを更新
    python3 scripts/pm_minutes_catalog.py --catalog

    # 両方実行
    python3 scripts/pm_minutes_catalog.py --upload --catalog

    # フィルタ付き
    python3 scripts/pm_minutes_catalog.py --upload --meeting-name Leader_Meeting --since 2026-04-01

    # アップロード状態一覧
    python3 scripts/pm_minutes_catalog.py --list

    # 確認のみ
    python3 scripts/pm_minutes_catalog.py --upload --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import make_logger, add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg
from pm_minutes_import import (
    init_minutes_db, db_path_for_kind,
    reconstruct_minutes_md, upload_md_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "data" / "minutes_channels.yaml"
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"

UPLOAD_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_log (
    meeting_id   TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    permalink    TEXT,
    uploaded_at  TEXT NOT NULL,
    PRIMARY KEY (meeting_id, channel_id)
);
"""


# --------------------------------------------------------------------------- #
# YAML 読み込み
# --------------------------------------------------------------------------- #
def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        print(f"[ERROR] 設定ファイルが見つかりません: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    channels = cfg.get("channels") or {}
    if not channels:
        print(f"[ERROR] 設定ファイルに channels が定義されていません: {config_path}", file=sys.stderr)
        sys.exit(1)
    return channels


# --------------------------------------------------------------------------- #
# DB ヘルパー
# --------------------------------------------------------------------------- #
def _ensure_upload_log(conn):
    conn.executescript(UPLOAD_LOG_SCHEMA)


def _load_instances(conn, kind_filter: str | None, since: str | None):
    sql = "SELECT meeting_id, held_at, kind FROM instances WHERE 1=1"
    params: list = []
    if kind_filter:
        sql += " AND kind = ?"
        params.append(kind_filter)
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


def _is_uploaded(conn, meeting_id: str, channel_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM upload_log WHERE meeting_id = ? AND channel_id = ?",
        (meeting_id, channel_id),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# --upload
# --------------------------------------------------------------------------- #
def cmd_upload(args, channels: dict, minutes_dir: Path, log) -> None:
    client = None
    if not args.dry_run:
        from slack_sdk import WebClient
        token = os.getenv("SLACK_USER_TOKEN")
        if not token:
            print("[ERROR] SLACK_USER_TOKEN を設定してください", file=sys.stderr)
            sys.exit(1)
        client = WebClient(token=token)

    kind_set: set[str] = set()
    for ch_cfg in channels.values():
        for kind in ch_cfg.get("minutes", []):
            if args.meeting_name and kind != args.meeting_name:
                continue
            kind_set.add(kind)

    total_uploaded = 0
    total_skipped = 0

    for kind in sorted(kind_set):
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

        for channel_id, ch_cfg in channels.items():
            if kind not in ch_cfg.get("minutes", []):
                continue
            ch_name = ch_cfg.get("name", channel_id)

            for inst in instances:
                mid = inst["meeting_id"]
                held_at = inst["held_at"]

                if not args.force and _is_uploaded(conn, mid, channel_id):
                    total_skipped += 1
                    continue

                data = _load_meeting_data(conn, mid)
                md_content = reconstruct_minutes_md(held_at, kind, data)
                filename = f"{held_at}_{kind}.md"

                if args.dry_run:
                    log(f"  [DRY] {filename} → #{ch_name}")
                    total_uploaded += 1
                    continue

                permalink = upload_md_file(
                    client, channel_id, None,
                    held_at, kind, log,
                    fallback_content=md_content,
                )

                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO upload_log"
                    " (meeting_id, channel_id, permalink, uploaded_at)"
                    " VALUES (?, ?, ?, ?)",
                    (mid, channel_id, permalink, now),
                )
                conn.commit()
                log(f"  [OK] {filename} → #{ch_name} ({permalink})")
                total_uploaded += 1

        conn.close()

    log(f"\n完了: {total_uploaded} 件アップロード、{total_skipped} 件スキップ")


# --------------------------------------------------------------------------- #
# --catalog
# --------------------------------------------------------------------------- #
def cmd_catalog(args, channels: dict, minutes_dir: Path, log) -> None:
    from canvas_utils import post_to_canvas, sanitize_for_canvas

    for channel_id, ch_cfg in channels.items():
        canvas_id = ch_cfg.get("catalog_canvas_id")
        if not canvas_id:
            log(f"[SKIP] #{ch_cfg.get('name', channel_id)}: catalog_canvas_id 未設定")
            continue

        kinds = ch_cfg.get("minutes", [])
        if args.meeting_name:
            kinds = [k for k in kinds if k == args.meeting_name]
        if not kinds:
            continue

        ch_name = ch_cfg.get("name", channel_id)
        sections: list[str] = [f"# {ch_name} 議事録目録", ""]

        has_entries = False
        for kind in sorted(kinds):
            db_path = db_path_for_kind(minutes_dir, kind)
            if not db_path.exists():
                continue

            conn = init_minutes_db(db_path, no_encrypt=args.no_encrypt)
            _ensure_upload_log(conn)

            rows = conn.execute(
                "SELECT u.meeting_id, i.held_at, u.permalink"
                " FROM upload_log u"
                " JOIN instances i ON u.meeting_id = i.meeting_id"
                " WHERE u.channel_id = ?"
                " ORDER BY i.held_at DESC",
                (channel_id,),
            ).fetchall()
            conn.close()

            if not rows:
                continue

            has_entries = True
            sections.append(f"## {kind}")
            sections.append("")
            for r in rows:
                held_at = r["held_at"]
                permalink = r["permalink"] or ""
                label = f"{held_at} {kind}"
                if permalink:
                    sections.append(f"- [{label}]({permalink})")
                else:
                    sections.append(f"- {label}")
            sections.append("")

        if not has_entries:
            log(f"[SKIP] #{ch_name}: アップロード済みの議事録がありません")
            continue

        catalog_md = "\n".join(sections)

        if args.dry_run:
            log(f"--- #{ch_name} (Canvas: {canvas_id}) ---")
            log(catalog_md)
            log("")
            continue

        sanitized = sanitize_for_canvas(catalog_md)
        post_to_canvas(canvas_id, sanitized)
        log(f"[OK] #{ch_name} の目録を Canvas {canvas_id} に投稿しました")


# --------------------------------------------------------------------------- #
# --list
# --------------------------------------------------------------------------- #
def cmd_list(args, channels: dict, minutes_dir: Path, log) -> None:
    kind_set: set[str] = set()
    for ch_cfg in channels.values():
        for kind in ch_cfg.get("minutes", []):
            if args.meeting_name and kind != args.meeting_name:
                continue
            kind_set.add(kind)

    for kind in sorted(kind_set):
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

        log(f"\n=== {kind} ({len(instances)} 件) ===")
        log(f"{'meeting_id':<40} {'held_at':<12} {'uploads'}")
        log(f"{'-'*40} {'-'*12} {'-'*30}")

        for inst in instances:
            mid = inst["meeting_id"]
            held_at = inst["held_at"]
            uploads = conn.execute(
                "SELECT channel_id, permalink FROM upload_log WHERE meeting_id = ?",
                (mid,),
            ).fetchall()
            if uploads:
                ch_list = ", ".join(
                    f"{r['channel_id']}" + (" *" if r["permalink"] else "")
                    for r in uploads
                )
            else:
                ch_list = "(未アップロード)"
            log(f"{mid:<40} {held_at:<12} {ch_list}")

        conn.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="議事録一括アップロード・Canvas目録生成",
    )
    parser.add_argument("--upload", action="store_true",
                        help="未アップロードの議事録を一括アップロード")
    parser.add_argument("--catalog", action="store_true",
                        help="目録Canvasを更新")
    parser.add_argument("--list", action="store_true", dest="show_list",
                        help="アップロード状態を一覧表示")
    parser.add_argument("--meeting-name",
                        help="特定の会議種別のみ対象")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="YAML設定ファイル（デフォルト: data/minutes_channels.yaml）")
    parser.add_argument("--minutes-dir", default=str(DEFAULT_MINUTES_DIR),
                        help="議事録DBディレクトリ（デフォルト: data/minutes/）")
    parser.add_argument("--force", action="store_true",
                        help="アップロード済みも再アップロード")
    add_since_arg(parser)
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_output_arg(parser)

    args = parser.parse_args()

    if not (args.upload or args.catalog or args.show_list):
        parser.print_help()
        sys.exit(1)

    log, log_close = make_logger(args.output if hasattr(args, "output") else None)
    channels = load_config(Path(args.config))
    minutes_dir = Path(args.minutes_dir)

    try:
        if args.show_list:
            cmd_list(args, channels, minutes_dir, log)
        if args.upload:
            cmd_upload(args, channels, minutes_dir, log)
        if args.catalog:
            cmd_catalog(args, channels, minutes_dir, log)
    finally:
        log_close()


if __name__ == "__main__":
    main()
