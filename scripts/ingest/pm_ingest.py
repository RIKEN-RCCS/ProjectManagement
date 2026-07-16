#!/usr/bin/env python3
"""
pm_ingest.py — pm.db 統合インジェストランナー

データソースを指定して pm.db へデータを投入する。
新しいソースは ingest_*.py を作成して PLUGINS に1行追加するだけで追加できる。

Usage:
    python3 scripts/pm_ingest.py slack  --slack-channel CHANNEL_ID
    python3 scripts/pm_ingest.py slack  --slack-channel CHANNEL_ID --dry-run
    python3 scripts/pm_ingest.py minutes --minutes-name Leader_Meeting
    python3 scripts/pm_ingest.py minutes --since 2026-01-01
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_utils import (
    add_dry_run_arg,
    add_no_encrypt_arg,
    add_output_arg,
    add_since_arg,
    load_claude_md_context,
    make_logger,
)
from db_utils import init_pm_db

from ingest.achievements import AchievementsIngestPlugin
from ingest.ingest_plugin import IngestContext
from ingest.ledger import LedgerIngestPlugin
from ingest.minutes import MinutesIngestPlugin

# --------------------------------------------------------------------------- #
# プラグイン登録（新ソース追加はここに1行追加するだけ）
# --------------------------------------------------------------------------- #
from ingest.slack import SlackIngestPlugin

PLUGINS: dict[str, object] = {
    "slack":        SlackIngestPlugin(),
    "minutes":      MinutesIngestPlugin(),
    "ledger":       LedgerIngestPlugin(),
    "achievements": AchievementsIngestPlugin(),
}
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="pm.db 統合インジェストランナー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ソース一覧:
  slack        Slack {channel_id}.db → 決定事項・アクションアイテム抽出
  minutes      議事録DB (data/minutes/) → pm.db 転記
  ledger       台帳シード JSON (data/ledger_seed.json) → 前提・意思決定台帳（有向グラフ）投入
  achievements アプリ別の完了実績を LLM 抽出 → pm.db achievements テーブルへ投入

使用例:
  python3 scripts/pm_ingest.py slack --slack-channel CHANNEL_ID
  python3 scripts/pm_ingest.py minutes --since 2026-01-01 --db data/pm.db
  python3 scripts/pm_ingest.py ledger --dry-run
  python3 scripts/pm_ingest.py achievements --achievements-apps GENESIS,SALMON --dry-run
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
    parser.add_argument(
        "--no-auto-enrich", action="store_true",
        help="Pass 1 投入後の自動エンリッチメント（Pass 2）をスキップ",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="既存レコードの上書きを許可",
    )
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
        for name in PLUGINS:
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
        force=getattr(args, "force", False),
        log=log,
        repo_root=REPO_ROOT,
    )

    def _max_ids() -> tuple[int, int]:
        d = pm_conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0]
        a = pm_conn.execute("SELECT COALESCE(MAX(id), 0) FROM action_items").fetchone()[0]
        return d, a

    try:
        pre_d, pre_a = _max_ids()
        plugin.run(args, ctx)

        # Pass 2: 自動エンリッチメント
        if args.dry_run or args.no_auto_enrich:
            pass
        else:
            post_d, post_a = _max_ids()
            new_d = [{"id": i} for i in range(pre_d + 1, post_d + 1)]
            new_a = [{"id": i} for i in range(pre_a + 1, post_a + 1)]
            if new_d or new_a:
                log(f"\n[INFO] 自動エンリッチ対象: decisions={len(new_d)}件, action_items={len(new_a)}件")
                try:
                    from enrich.enrich_items import _fetch_target_items, enrich_batch
                    ids = [f"d:{d['id']}" for d in new_d] + [f"a:{a['id']}" for a in new_a]
                    decisions, action_items = _fetch_target_items(pm_conn, item_ids=ids)
                    project_context = load_claude_md_context()
                    enrich_batch(
                        pm_conn, decisions, action_items,
                        project_context=project_context,
                        dry_run=False, log=log,
                    )
                except Exception as e:
                    log(f"[WARN] 自動エンリッチ失敗（Pass 1 は成功扱い）: {e}")
    finally:
        pm_conn.close()
        close_log()


if __name__ == "__main__":
    main()
