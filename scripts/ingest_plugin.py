#!/usr/bin/env python3
"""
ingest_plugin.py

pm.db インジェストプラグインの共通インターフェース定義。

新しいデータソースを追加するには:
1. このモジュールの IngestPlugin Protocol を実装したクラスを ingest_*.py に作成する
2. pm_ingest.py の PLUGINS 辞書に1行追加する
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@dataclass
class IngestContext:
    """プラグインが run() で受け取る共有状態。"""
    pm_conn: sqlite3.Connection
    pm_db_path: Path
    dry_run: bool
    no_encrypt: bool
    since: str | None          # YYYY-MM-DD または None
    log: Callable[[str], None]
    repo_root: Path


@runtime_checkable
class IngestPlugin(Protocol):
    """
    pm.db へのデータ投入プラグインが満たすべきインターフェース。

    source_name: "--list" 表示やログに使う短い識別子（例: "slack", "minutes"）
    add_args():  プラグイン固有の argparse 引数を登録する
                 共通フラグ (--db, --dry-run, --no-encrypt, --since) は登録不要
    run():       IngestContext を受け取り投入を実行する
                 ctx.dry_run が True の場合は DB 書き込みを行わない
    """

    source_name: str

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        ...

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        ...
