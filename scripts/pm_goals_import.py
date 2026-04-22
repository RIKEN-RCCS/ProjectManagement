#!/usr/bin/env python3
"""
pm_goals_import.py — 後方互換 CLI ラッパー

goals.yaml を pm.db の goals/milestones テーブルに完全同期する。
実装は ingest_goals.py に集約済み。新規利用は pm_ingest.py goals を推奨。

Usage:
    python3 scripts/pm_goals_import.py
    python3 scripts/pm_goals_import.py --goals-file goals.yaml
    python3 scripts/pm_goals_import.py --dry-run
    python3 scripts/pm_goals_import.py --list

Options:
    --goals-file PATH   goals.yaml のパス（デフォルト: goals.yaml）
    --db PATH           pm.db のパス（必須）
    --dry-run           DB保存なし・内容を表示のみ
    --list              pm.db に登録済みのゴール・マイルストーン一覧を表示
    --no-encrypt        DBを暗号化しない（平文モード）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import init_pm_db as _init_pm_db
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, make_logger
from ingest_goals import (
    GoalsIngestPlugin,
    list_registered,
    ensure_goals_schema,
    DEFAULT_GOALS_FILE,
)
from ingest_plugin import IngestContext


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="goals.yaml を pm.db に読み込む")
    parser.add_argument("--goals-file", default=None, help="goals.yaml のパス")
    parser.add_argument("--db", default=None, help="pm.db のパス（必須）")
    add_dry_run_arg(parser)
    parser.add_argument("--list", action="store_true", help="登録済み一覧を表示して終了")
    add_no_encrypt_arg(parser)
    add_output_arg(parser)
    args = parser.parse_args()

    if not args.db:
        print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
        print("  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db)
    log, close_log = make_logger(args.output)

    if args.list:
        list_registered(db_path, args.no_encrypt, log=log)
        close_log()
        return

    pm_conn = _init_pm_db(db_path, no_encrypt=args.no_encrypt)
    ensure_goals_schema(pm_conn)

    # 従来の --goals-file フラグをプラグインの --goals-* 相当の属性として設定する
    args.goals_file = args.goals_file
    args.goals_list = False

    ctx = IngestContext(
        pm_conn=pm_conn,
        pm_db_path=db_path,
        dry_run=args.dry_run,
        no_encrypt=args.no_encrypt,
        since=None,
        log=log,
        repo_root=REPO_ROOT,
    )

    plugin = GoalsIngestPlugin()
    try:
        plugin.run(args, ctx)
    finally:
        pm_conn.close()
        close_log()


if __name__ == "__main__":
    main()
