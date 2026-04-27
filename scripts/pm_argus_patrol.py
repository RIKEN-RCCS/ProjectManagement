#!/usr/bin/env python3
"""
pm_argus_patrol.py — Argus Patrol Agent メインループ

決定論的ルールエンジンでプロジェクト状況を巡回し、
リマインダー・完了確認・エスカレーション等を自律的に実行する。

Usage:
    # 通常実行（cron から呼ばれる）
    python3 scripts/pm_argus_patrol.py

    # DB・Slack 変更なし（動作確認用）
    python3 scripts/pm_argus_patrol.py --dry-run

    # 特定の検出器のみ実行
    python3 scripts/pm_argus_patrol.py --only overdue
    python3 scripts/pm_argus_patrol.py --only completion,deadline

    # 承認待ち一覧
    python3 scripts/pm_argus_patrol.py --list-pending

cron エントリ例:
    */30 * * * 1-5 cd /lvs0/.../ProjectManagement && \\
      source ~/.secrets/slack_tokens.sh && \\
      source ~/.secrets/rivault_tokens.sh && \\
      ~/.venv_aarch64/bin/python3 scripts/pm_argus_patrol.py \\
      >> logs/pm_argus_patrol.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import open_pm_db
from patrol_state import PatrolState
from patrol_users import UserResolver
from patrol_detect import (
    detect_completion_signals,
    detect_overdue_items,
    detect_approaching_deadlines,
    detect_unacknowledged_decisions,
    detect_stale_items,
    detect_milestone_health,
    detect_weekly_trend_alert,
)

logger = logging.getLogger("argus_patrol")

_DATA_DIR = _REPO_ROOT / "data"
_PM_DB = _DATA_DIR / "pm.db"
_STATE_DB = _DATA_DIR / "patrol_state.db"
_CONFIG_FILE = _DATA_DIR / "patrol_config.yaml"


# --------------------------------------------------------------------------- #
# PatrolContext
# --------------------------------------------------------------------------- #
@dataclass
class PatrolContext:
    conn: Any                    # pm.db sqlite3.Connection
    state: PatrolState
    slack: Any                   # WebClient | None
    user_resolver: UserResolver
    dry_run: bool
    today: str
    config: dict
    data_dir: Path = field(default=_DATA_DIR)


# --------------------------------------------------------------------------- #
# 検出器レジストリ
# --------------------------------------------------------------------------- #
DETECTORS: dict[str, Any] = {
    "completion": detect_completion_signals,
    "overdue": detect_overdue_items,
    "deadline": detect_approaching_deadlines,
    "decisions": detect_unacknowledged_decisions,
    "stale": detect_stale_items,
    "milestone": detect_milestone_health,
    "trend": detect_weekly_trend_alert,
}


# --------------------------------------------------------------------------- #
# メインループ
# --------------------------------------------------------------------------- #
def run_patrol(
    *,
    dry_run: bool = False,
    no_encrypt: bool = False,
    only: list[str] | None = None,
) -> None:
    """全検出器を順に実行する。"""
    config = _load_config()
    if not config.get("patrol", {}).get("enabled", True):
        logger.info("Patrol は無効化されています (patrol.enabled=false)")
        return

    conn = open_pm_db(_PM_DB, no_encrypt=no_encrypt)
    state = PatrolState(_STATE_DB)

    slack = None
    if not dry_run:
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        if bot_token:
            try:
                from slack_sdk import WebClient
                slack = WebClient(token=bot_token)
            except ImportError:
                logger.warning("slack_sdk が利用不可。Slack 投稿をスキップします。")
        else:
            logger.warning("SLACK_BOT_TOKEN 未設定。Slack 投稿をスキップします。")

    user_resolver = UserResolver(state, slack, _DATA_DIR)
    today = date.today().isoformat()

    ctx = PatrolContext(
        conn=conn,
        state=state,
        slack=slack,
        user_resolver=user_resolver,
        dry_run=dry_run,
        today=today,
        config=config,
        data_dir=_DATA_DIR,
    )

    detectors_to_run = DETECTORS
    if only:
        detectors_to_run = {
            k: v for k, v in DETECTORS.items() if k in only
        }
        if not detectors_to_run:
            logger.error(
                "指定された検出器が見つかりません: %s (有効: %s)",
                only,
                list(DETECTORS.keys()),
            )
            conn.close()
            state.close()
            return

    total = 0
    for name, detector_fn in detectors_to_run.items():
        try:
            count = detector_fn(ctx)
            if count:
                logger.info("[%s] %d 件検出", name, count)
            total += count
        except Exception as e:
            logger.exception("[%s] エラー: %s", name, e)

    if not dry_run and total > 0:
        conn.commit()

    conn.close()
    state.close()

    logger.info("Patrol 完了: %d 件のアクション (%s)", total, "dry-run" if dry_run else "実行")


def list_pending() -> None:
    """承認待ちエントリを一覧表示する。"""
    state = PatrolState(_STATE_DB)
    pending = state.list_pending()
    state.close()

    if not pending:
        print("承認待ちはありません。")
        return

    print(f"承認待ち: {len(pending)} 件\n")
    for p in pending:
        evidence_preview = (p.get("evidence") or "")[:80]
        print(
            f"  ID={p['id']}  type={p['action_type']}  "
            f"target=AI#{p['target_id']}  "
            f"created={p['created_at'][:16]}"
        )
        if evidence_preview:
            print(f"    根拠: {evidence_preview}")


# --------------------------------------------------------------------------- #
# 設定ファイル
# --------------------------------------------------------------------------- #
def _load_config() -> dict:
    """patrol_config.yaml をロードする。"""
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    logger.warning("patrol_config.yaml が見つかりません: %s", _CONFIG_FILE)
    return {}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Argus Patrol Agent — 自律型プロジェクト管理巡回",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB・Slack 変更なし（動作確認用）",
    )
    parser.add_argument(
        "--no-encrypt",
        action="store_true",
        help="pm.db を平文モードで開く",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help=(
            "実行する検出器をカンマ区切りで指定 "
            f"(選択肢: {','.join(DETECTORS.keys())})"
        ),
    )
    parser.add_argument(
        "--list-pending",
        action="store_true",
        help="承認待ちエントリを一覧表示",
    )
    args = parser.parse_args()

    if args.list_pending:
        list_pending()
        return

    only = args.only.split(",") if args.only else None
    run_patrol(dry_run=args.dry_run, no_encrypt=args.no_encrypt, only=only)


if __name__ == "__main__":
    main()
