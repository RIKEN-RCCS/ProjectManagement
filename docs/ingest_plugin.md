# pm.db インジェストプラグイン仕様

## 概要

pm.db へのデータ投入を「プラグイン」として統一インターフェースで管理する仕組み。
新しいデータソースの追加は **ファイル1本 + 1行** で完結する。

---

## アーキテクチャ

```
pm_ingest.py          ← 統合ランナー（エントリポイント）
  │
  ├─ PLUGINS 辞書にプラグインを登録
  │    "slack"   → SlackIngestPlugin   (ingest_slack.py)
  │    "minutes" → MinutesIngestPlugin (ingest_minutes.py)
  │    "goals"   → GoalsIngestPlugin   (ingest_goals.py)
  │
  ├─ IngestContext を構築して plugin.run() に渡す
  │
  └─ ingest_plugin.py  ← IngestContext / IngestPlugin の定義

既存スクリプト（後方互換 CLI ラッパー）:
  pm_extractor.py      → ingest_slack.py を呼び出す
  pm_minutes_to_pm.py  → ingest_minutes.py を呼び出す
  pm_goals_import.py   → ingest_goals.py を呼び出す
```

---

## インターフェース定義（`ingest_plugin.py`）

### `IngestContext` — プラグインが受け取る共有状態

```python
@dataclass
class IngestContext:
    pm_conn:     sqlite3.Connection   # 開済みの pm.db 接続（読み書き可）
    pm_db_path:  Path                 # pm.db のファイルパス（エラー表示用）
    dry_run:     bool                 # True なら DB 書き込み禁止
    no_encrypt:  bool                 # True なら平文モード
    since:       str | None           # YYYY-MM-DD。この日付以降のみ対象（None = 全期間）
    log:         Callable[[str], None]  # ログ出力関数（stdout + ファイル）
    repo_root:   Path                 # リポジトリルート（パス解決用）
```

**注意事項**:
- `pm_conn` は `init_pm_db()` 済みで基本スキーマ・マイグレーション適用済み。
  プラグイン独自のテーブルが必要な場合は `run()` 内で `CREATE TABLE IF NOT EXISTS` する。
- `pm_conn.commit()` はプラグイン側で呼ぶ（ランナーは commit しない）。
- `dry_run=True` のときは `pm_conn.commit()` を呼んではならない。

### `IngestPlugin` — プラグインが実装する Protocol

```python
@runtime_checkable
class IngestPlugin(Protocol):
    source_name: str  # 識別子。英小文字・ハイフン推奨（例: "slack", "my-api"）

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        """プラグイン固有の argparse 引数を登録する。"""
        ...

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        """データを取得して pm.db に投入する。"""
        ...
```

---

## 実装ルール

### 1. ファイル名

`scripts/ingest_{source_name}.py` に作成する。

### 2. `source_name`

英小文字とハイフンのみ使用（例: `"slack"`, `"minutes"`, `"my-api"`）。  
`pm_ingest.py --list` の表示と `python3 pm_ingest.py <source_name>` のコマンド名になる。

### 3. `add_args()` — 引数の命名規則

全プラグインの引数が **1つのフラットな `argparse.Namespace`** を共有するため、  
固有引数の先頭には `--{source_name}-` プレフィックスを付ける。

```python
# 良い例（slack プラグイン）
parser.add_argument("--slack-channel", ...)
parser.add_argument("--slack-force-reextract", ...)

# 悪い例（他のプラグインと衝突する可能性）
parser.add_argument("--channel", ...)
parser.add_argument("--force", ...)
```

以下の共通フラグは **登録しない**（ランナーが追加済み）:

| フラグ | `args` 属性名 |
|---|---|
| `--db PATH` | `args.db` |
| `--dry-run` | `args.dry_run` |
| `--no-encrypt` | `args.no_encrypt` |
| `--since YYYY-MM-DD` | `args.since` |
| `--output PATH` | `args.output` |

### 4. `run()` — 実装パターン

```python
def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
    # 1. ソース固有の引数を取得
    channel_id = args.slack_channel

    # 2. ソース接続（ソース側のDB/API/ファイル）
    source_conn = open_source_db(...)

    # 3. dry_run チェックはデータ取得・表示の後、書き込み直前に行う
    if ctx.dry_run:
        ctx.log("  [INFO] --dry-run のため DB保存をスキップしました")
        return

    # 4. pm.db に書き込む
    ctx.pm_conn.execute("INSERT INTO action_items ...")

    # 5. commit はプラグイン側で呼ぶ
    ctx.pm_conn.commit()

    # 6. ソース接続を閉じる（pm_conn は閉じない。ランナーが閉じる）
    source_conn.close()
```

### 5. `ctx.log()` の使い方

```python
ctx.log(f"[INFO] 処理件数: {n} 件")   # 通常ログ
ctx.log(f"[WARN] スキップ: {reason}")  # 警告
ctx.log(f"[ERROR] 失敗: {e}")          # エラー（その後 sys.exit(1) または continue）
```

`print()` は使わない（`--output` でファイルにも書く仕組みが `ctx.log` に組み込まれている）。

---

## 登録方法（`pm_ingest.py`）

```python
# scripts/pm_ingest.py の PLUGINS 辞書に1行追加する
from ingest_my_api import MyApiIngestPlugin

PLUGINS: dict[str, object] = {
    "slack":   SlackIngestPlugin(),
    "minutes": MinutesIngestPlugin(),
    "goals":   GoalsIngestPlugin(),
    "my-api":  MyApiIngestPlugin(),   # ← ここに追加するだけ
}
```

---

## 新プラグインの最小実装テンプレート

```python
#!/usr/bin/env python3
"""
ingest_my_api.py — MyAPI → pm.db インジェストプラグイン
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import normalize_assignee
from ingest_plugin import IngestContext


class MyApiIngestPlugin:
    source_name = "my-api"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--my-api-endpoint", default="https://example.com/api",
            help="取得先エンドポイント（my-api ソース用）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        endpoint = args.my_api_endpoint

        # データ取得
        items = fetch_from_api(endpoint, since=ctx.since)
        ctx.log(f"[INFO] 取得件数: {len(items)} 件")

        if ctx.dry_run:
            for item in items:
                ctx.log(f"  [DRY] {item['content']}")
            return

        count = 0
        for item in items:
            ctx.pm_conn.execute(
                "INSERT INTO action_items"
                " (content, assignee, due_date, status, source, source_ref, extracted_at)"
                " VALUES (?, ?, ?, 'open', 'my-api', ?, ?)",
                (item["content"], normalize_assignee(item.get("assignee")),
                 item.get("due_date"), item.get("url", ""), item["date"]),
            )
            count += 1

        ctx.pm_conn.commit()
        ctx.log(f"完了: {count} 件を pm.db に保存しました")
```

---

## 実行方法

```sh
# 統合ランナー経由（推奨）
python3 scripts/pm_ingest.py my-api --my-api-endpoint https://...
python3 scripts/pm_ingest.py my-api --dry-run
python3 scripts/pm_ingest.py my-api --since 2026-01-01 --db data/pm.db

# ソース一覧を確認
python3 scripts/pm_ingest.py --list
```

後方互換のため、既存スクリプト（`pm_extractor.py` 等）の直接呼び出しも引き続き機能する。

---

## 既存プラグイン一覧

| source_name | クラス | ファイル | 書き込みテーブル | 重複管理 |
|---|---|---|---|---|
| `slack` | `SlackIngestPlugin` | `ingest_slack.py` | `decisions`, `action_items` | `slack_extractions`（thread_ts + channel_id） |
| `minutes` | `MinutesIngestPlugin` | `ingest_minutes.py` | `meetings`, `decisions`, `action_items` | `meetings.held_at + kind`（`--force` で上書き） |
| `goals` | `GoalsIngestPlugin` | `ingest_goals.py` | `goals`, `milestones` | `INSERT OR REPLACE`（完全同期） |
