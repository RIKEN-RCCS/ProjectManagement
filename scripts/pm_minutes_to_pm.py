#!/usr/bin/env python3
"""
pm_minutes_to_pm.py — 後方互換 CLI ラッパー

議事録DB（data/minutes/{kind}.db）の内容を LLM 不使用で pm.db に転記する。
実装は ingest_minutes.py に集約済み。新規利用は pm_ingest.py minutes を推奨。

Usage:
    python3 scripts/pm_minutes_to_pm.py
    python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting
    python3 scripts/pm_minutes_to_pm.py --since 2026-01-01
    python3 scripts/pm_minutes_to_pm.py --dry-run
    python3 scripts/pm_minutes_to_pm.py --list
    python3 scripts/pm_minutes_to_pm.py --delete 2026-03-10_Leader_Meeting

Options:
    --meeting-name NAME     特定の会議名のみ処理（省略時は全DBを対象）
    --minutes-dir DIR       議事録DBのディレクトリ（デフォルト: data/minutes/）
    --db PATH               pm.db のパス（必須）
    --since YYYY-MM-DD      この日付以降の会議のみ転記
    --force                 既存レコードを上書き
    --dry-run               DB保存なし・転記内容を表示のみ
    --no-encrypt            平文モード
    --list                  pm.db の転記済み会議一覧を表示して終了
    --delete MEETING_ID     指定した meeting_id を pm.db から削除して終了
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import init_pm_db as _init_pm_db
from cli_utils import add_dry_run_arg, add_no_encrypt_arg, add_since_arg
from ingest_minutes import (
    MinutesIngestPlugin,
    list_pm,
    delete_from_pm,
    DEFAULT_MINUTES_DIR,
)
from ingest_plugin import IngestContext


REPO_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(
        description="議事録DB（data/minutes/）→ pm.db への転記（LLM不使用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python3 scripts/pm_minutes_to_pm.py
  python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting
  python3 scripts/pm_minutes_to_pm.py --dry-run
  python3 scripts/pm_minutes_to_pm.py --since 2026-01-01 --force
  python3 scripts/pm_minutes_to_pm.py --list
  python3 scripts/pm_minutes_to_pm.py --delete 2026-03-10_Leader_Meeting
""",
    )
    parser.add_argument("--meeting-name", default=None,
                        help="特定の会議名のみ処理（省略時は全DBを対象）")
    parser.add_argument("--minutes-dir", default=None,
                        help="議事録DBのディレクトリ（デフォルト: data/minutes/）")
    parser.add_argument("--db", default=None,
                        help="pm.db のパス（必須: data/pm.db / data/pm-hpc.db / data/pm-bmt.db）")
    add_since_arg(parser)
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true",
                        help="pm.db の転記済み会議一覧を表示して終了")
    parser.add_argument("--delete", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id を pm.db から削除して終了")
    args = parser.parse_args()

    if not args.db:
        print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
        print("  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db)

    if args.list:
        list_pm(db_path, args.meeting_name, args.since, args.no_encrypt)
        return

    pm_conn = _init_pm_db(db_path, no_encrypt=args.no_encrypt)

    if args.delete:
        delete_from_pm(pm_conn, args.delete, args.dry_run)
        pm_conn.close()
        return

    # 従来の --meeting-name, --minutes-dir, --force フラグを
    # プラグインの --minutes-* 相当の属性として設定する
    args.minutes_name = args.meeting_name
    args.minutes_dir = args.minutes_dir
    args.minutes_force = args.force
    args.minutes_list = False
    args.minutes_delete = None

    ctx = IngestContext(
        pm_conn=pm_conn,
        pm_db_path=db_path,
        dry_run=args.dry_run,
        no_encrypt=args.no_encrypt,
        since=args.since,
        log=print,
        repo_root=REPO_ROOT,
    )

    plugin = MinutesIngestPlugin()
    try:
        plugin.run(args, ctx)
    finally:
        pm_conn.close()


if __name__ == "__main__":
    main()
