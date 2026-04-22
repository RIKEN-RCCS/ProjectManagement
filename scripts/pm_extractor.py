#!/usr/bin/env python3
"""
pm_extractor.py — 後方互換 CLI ラッパー

Slack {channel_id}.db の生メッセージから決定事項・アクションアイテムを抽出して pm.db に保存する。
実装は ingest_slack.py に集約済み。新規利用は pm_ingest.py slack を推奨。

Usage:
    python3 scripts/pm_extractor.py
    python3 scripts/pm_extractor.py -c C08SXA4M7JT
    python3 scripts/pm_extractor.py -c C08SXA4M7JT --since 2026-01-01
    python3 scripts/pm_extractor.py --force-reextract
    python3 scripts/pm_extractor.py --dry-run
    python3 scripts/pm_extractor.py --list

Options:
    -c / --channel ID   対象チャンネルID（デフォルト: C0A9KG036CS）
    --db-slack PATH     {channel_id}.db のパス（省略時は data/{channel_id}.db）
    --db-pm PATH        pm.db のパス（デフォルト: data/pm.db）
    --since YYYY-MM-DD  この日付以降のスレッドのみ対象
    --force-reextract   抽出済みスレッドも再抽出
    --dry-run           DB保存なし・結果を標準出力のみ
    --output PATH       標準出力の内容をファイルにも保存
    --list              抽出済みスレッド一覧を表示して終了
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import init_pm_db
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg, make_logger
from ingest_slack import (
    SlackIngestPlugin,
    open_slack_db,
    cmd_list_extractions,
    ensure_slack_extractions,
    DEFAULT_CHANNEL,
)
from ingest_plugin import IngestContext


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slack生メッセージ → pm.db への決定事項・アクションアイテム抽出"
    )
    parser.add_argument("-c", "--channel", default=DEFAULT_CHANNEL, help="対象チャンネルID")
    parser.add_argument("--db-slack", default=None, help="{channel_id}.db のパス")
    parser.add_argument("--db-pm", default=None, help="pm.db のパス")
    add_since_arg(parser, "（スレッドのみ対象）")
    parser.add_argument("--force-reextract", action="store_true", help="抽出済みスレッドも再処理")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true", help="抽出済みスレッドの一覧を表示して終了")
    args = parser.parse_args()

    channel_id = args.channel
    slack_db_path = Path(args.db_slack) if args.db_slack else REPO_ROOT / "data" / f"{channel_id}.db"
    pm_db_path = Path(args.db_pm) if args.db_pm else DEFAULT_PM_DB

    log, close_log = make_logger(args.output)

    pm_conn = init_pm_db(pm_db_path, no_encrypt=args.no_encrypt)

    if args.list:
        slack_conn = open_slack_db(slack_db_path, no_encrypt=args.no_encrypt)
        ensure_slack_extractions(pm_conn)
        cmd_list_extractions(slack_conn, pm_conn, channel_id, args.since, log=log)
        slack_conn.close()
        pm_conn.close()
        close_log()
        return

    # 従来の -c/--channel, --db-slack, --force-reextract フラグを
    # プラグインの --slack-* 相当の属性として設定する
    args.slack_channel = channel_id
    args.slack_db = args.db_slack
    args.slack_force_reextract = args.force_reextract
    args.slack_list = False

    ctx = IngestContext(
        pm_conn=pm_conn,
        pm_db_path=pm_db_path,
        dry_run=args.dry_run,
        no_encrypt=args.no_encrypt,
        since=args.since,
        log=log,
        repo_root=REPO_ROOT,
    )

    plugin = SlackIngestPlugin()
    try:
        plugin.run(args, ctx)
    finally:
        pm_conn.close()
        close_log()


if __name__ == "__main__":
    main()
