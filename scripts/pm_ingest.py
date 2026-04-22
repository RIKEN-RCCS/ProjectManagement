#!/usr/bin/env python3
"""
pm_ingest.py — pm.db 統合インジェストランナー

データソースを指定して pm.db へデータを投入する。
新しいソースは ingest_*.py を作成して PLUGINS に1行追加するだけで追加できる。

Usage:
    python3 scripts/pm_ingest.py slack  --slack-channel C08SXA4M7JT
    python3 scripts/pm_ingest.py slack  --slack-channel C08SXA4M7JT --dry-run
    python3 scripts/pm_ingest.py minutes --minutes-name Leader_Meeting
    python3 scripts/pm_ingest.py minutes --since 2026-01-01
    python3 scripts/pm_ingest.py goals  --goals-file goals.yaml
    python3 scripts/pm_ingest.py --list

共通オプション:
    --db PATH           pm.db のパス（デフォルト: data/pm.db）
    --dry-run           DB保存なし・確認のみ
    --no-encrypt        平文モード
    --since YYYY-MM-DD  この日付以降のデータのみ対象
    --output PATH       ログをファイルにも保存
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_utils import init_pm_db
from cli_utils import add_dry_run_arg, add_no_encrypt_arg, add_since_arg, add_output_arg, make_logger
from ingest_plugin import IngestContext

# --------------------------------------------------------------------------- #
# プラグイン登録（新ソース追加はここに1行追加するだけ）
# --------------------------------------------------------------------------- #
from ingest_slack   import SlackIngestPlugin
from ingest_minutes import MinutesIngestPlugin
from ingest_goals   import GoalsIngestPlugin

PLUGINS: dict[str, object] = {
    "slack":   SlackIngestPlugin(),
    "minutes": MinutesIngestPlugin(),
    "goals":   GoalsIngestPlugin(),
    # 将来の例:
    # "jira":    JiraIngestPlugin(),
    # "gcal":    GoogleCalendarIngestPlugin(),
}
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="pm.db 統合インジェストランナー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ソース一覧:
  slack    Slack {channel_id}.db → 決定事項・アクションアイテム抽出
  minutes  議事録DB (data/minutes/) → pm.db 転記
  goals    goals.yaml → goals/milestones テーブル同期

使用例:
  python3 scripts/pm_ingest.py slack --slack-channel C08SXA4M7JT
  python3 scripts/pm_ingest.py minutes --since 2026-01-01 --db data/pm.db
  python3 scripts/pm_ingest.py goals --dry-run
  python3 scripts/pm_ingest.py --list
""",
    )

    parser.add_argument(
        "source", nargs="?", choices=list(PLUGINS),
        help="データソース名",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="利用可能なソース一覧を表示して終了",
    )
    parser.add_argument("--db", default=None, metavar="PATH", help="pm.db のパス")
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_since_arg(parser)
    add_output_arg(parser)

    # 各プラグインの固有引数を登録
    for plugin in PLUGINS.values():
        plugin.add_args(parser)

    args = parser.parse_args()

    if args.list:
        print("利用可能なソース:")
        for name, plugin in PLUGINS.items():
            print(f"  {name}")
        return

    if not args.source:
        parser.print_help()
        sys.exit(1)

    plugin = PLUGINS[args.source]
    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
    log, close_log = make_logger(getattr(args, "output", None))

    pm_conn = init_pm_db(db_path, no_encrypt=args.no_encrypt)

    ctx = IngestContext(
        pm_conn=pm_conn,
        pm_db_path=db_path,
        dry_run=args.dry_run,
        no_encrypt=args.no_encrypt,
        since=args.since,
        log=log,
        repo_root=REPO_ROOT,
    )

    try:
        plugin.run(args, ctx)
    finally:
        pm_conn.close()
        close_log()


if __name__ == "__main__":
    main()
