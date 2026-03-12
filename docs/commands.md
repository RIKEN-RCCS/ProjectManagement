## 主なコマンド

### 1. Slack取得・要約・Canvas投稿（slack_pipeline.py）

```sh
# 通常運用: 差分のみ取得・要約してCanvas投稿
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db

# 初回・過去分の取り込み（oldest をAPIに渡してページネーション全件取得）
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db \
    --since 2025-04-01

# Canvas投稿せず取得・要約のみ（全体要約生成もスキップ）
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db \
    --skip-canvas
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db PATH` | `data/{channel_id}.db` | SQLite DBファイルパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のメッセージのみ取得（APIに oldest として渡す） |
| `-l N` | `100` | 1ページあたりの取得件数上限（最大999） |
| `--skip-fetch` | - | Slack API取得をスキップ（DBのみ使用） |
| `--force-resummary` | - | 全スレッドを強制再要約 |
| `--skip-canvas` | - | Canvas投稿・全体要約生成をスキップ |
| `--no-permalink` | - | パーマリンク取得を無効化 |
| `--canvas-id ID` | `F0AAD2494VB` | 投稿先CanvasID |

### 2. 会議録文字起こし（trans.sh + whisper_vad.py）

```sh
# 推奨: --meeting-name を指定すると pm.db に直接保存し .md を削除（平文ファイルが残らない）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --skip 30 --meeting-name Leader_Meeting

# 日付を明示上書き（省略時はファイル名の GMT タイムスタンプを JST 変換して自動取得）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10

# 従来方式: .md ファイルをそのまま残す（セキュリティ警告が出る）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--skip SECONDS` | なし | 全ファイルの冒頭をスキップ |
| `--meeting-name NAME` | なし | 指定すると文字起こし後に pm.db へ直接インポートし .md を削除 |
| `--held-at YYYY-MM-DD` | GMT→JST変換 | `--meeting-name` と併用。省略時はファイル名のGMTタイムスタンプをJSTに変換して取得 |

処理フロー: ffmpeg → WAV変換（16kHz, mono） → DeepFilterNetノイズ除去 → SileroVAD → pyannote話者分離 → Whisper large-v3 文字起こし

`--meeting-name` 指定時の追加フロー: 文字起こし完了 → pm_meeting_import.py で pm.db にインポート → .md 削除

### 3. 会議議事録 → pm.db（pm_meeting_import.py）

単一ファイルモードと一括処理モード（`--bulk`）に対応。`_parsed.md` で終わるファイルは対象外。インポート済みのファイルは `--force` なしでスキップ（LLM呼び出しなし）。

```sh
# 単一ファイル
python3 scripts/pm_meeting_import.py meetings/GMT20260302-032528_Recording.md \
    --meeting-name "アプリ-ベンチマークリーダー会議" --held-at 2026-03-02

# 単一ファイル（出力をファイルにも保存）
python3 scripts/pm_meeting_import.py meetings/GMT20260302-032528_Recording.md \
    --meeting-name Leader_Meeting --held-at 2026-03-02 \
    --output meetings/GMT20260302-032528_Recording_parsed.txt

# 一括処理（meetings/ ディレクトリ内を全て処理）
python3 scripts/pm_meeting_import.py --bulk

# 一括処理（特定日付以降のみ）
python3 scripts/pm_meeting_import.py --bulk --since 2026-01-01

# 一括処理（既存レコードを上書き）
python3 scripts/pm_meeting_import.py --bulk --force

# インポート済み議事録の一覧表示
python3 scripts/pm_meeting_import.py --list
python3 scripts/pm_meeting_import.py --list --since 2026-02-01

# 議事録の削除
python3 scripts/pm_meeting_import.py --delete 2026-03-02_Leader_Meeting
python3 scripts/pm_meeting_import.py --delete 2026-03-02_Leader_Meeting --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `input_file` | - | 文字起こしファイル（.txt / .md）（単一ファイルモード） |
| `--meeting-name NAME` | `"不明"` | 会議種別名（「会議の種類と頻度」参照）（単一ファイルモード） |
| `--held-at YYYY-MM-DD` | ファイル名から推定 | 開催日（単一ファイルモード） |
| `--bulk` | - | 一括処理モード（meetings/ ディレクトリ内を全て処理） |
| `--meetings-dir DIR` | `meetings/` | 一括処理時の議事録ディレクトリ |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のファイルのみ対象（`--bulk` / `--list` 時） |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--force` | - | 既存レコードを上書き |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 標準出力の内容をファイルにも保存（単一ファイルモードのみ） |
| `--no-encrypt` | - | DBを暗号化しない（平文モード） |
| `--list` | - | インポート済み議事録一覧を表示して終了 |
| `--delete MEETING_ID` | - | 指定した meeting_id の議事録をDBから削除する |

### 4. Slack要約 → pm.db（pm_extractor.py）

```sh
# 通常運用: 未処理スレッドのみ抽出
python3 scripts/pm_extractor.py -c C08SXA4M7JT

# 確認用（DB保存なし）
python3 scripts/pm_extractor.py -c C08SXA4M7JT --dry-run --output result.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db-slack PATH` | `data/{channel_id}.db` | Slack DBのパス |
| `--db-pm PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降の要約のみ対象 |
| `--force-reextract` | - | 抽出済みスレッドも再処理 |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 標準出力の内容をファイルにも保存 |

### 5. PMレポート生成・Canvas投稿（pm_report.py）

レポート構成: **サマリー → 直近の決定事項 → 要注意事項 → 未完了アクションアイテム（表形式）**

未完了アクションアイテム表には ID・担当者・内容・期限・ソース・マイルストーン・対応状況 の列があり、会議中にCanvas上で直接記入できる。

```sh
# 週次進捗レポートを生成してCanvas投稿
python3 scripts/pm_report.py

# 直近1ヶ月のデータのみ対象にしてレポート生成
python3 scripts/pm_report.py --since 2026-02-01

# 確認用（Canvas投稿なし）
python3 scripts/pm_report.py --dry-run --output report.md
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--canvas-id ID` | `F0AAD2494VB` | 投稿先 Canvas ID |
| `--since YYYY-MM-DD` | なし（全期間） | この日付以降のデータのみ対象 |
| `--skip-canvas` | - | Canvas 投稿をスキップ |
| `--dry-run` | - | Canvas 投稿なし・結果を標準出力のみ |
| `--output PATH` | - | 出力をファイルにも保存 |

### 6. Canvas対応状況 → pm.db 同期（pm_sync_canvas.py）

会議中にCanvas上の各列に記入された内容をpm.dbに反映する。

**運用フロー**:
1. `pm_report.py` でアクションアイテム表をCanvas投稿（対応状況列は空）
2. 会議中にメンバーがCanvas上の各列を記入・編集
3. 会議後に本スクリプトを実行してpm.dbを更新

```sh
# 通常運用
python3 scripts/pm_sync_canvas.py

# 確認用（DB更新なし）
python3 scripts/pm_sync_canvas.py --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--canvas-id ID` | `F0AAD2494VB` | 対象 Canvas ID |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |

**完了判定キーワード**（`status='closed'` に更新）: `完了` `done` `済` `対応済` `解決` `closed` `finish` `finished`

それ以外の記入内容は `note` 列に保存（`status` は `open` のまま）

Canvas上で変更可能な列: **担当者・内容・期限・マイルストーン（M1〜M5）・対応状況**（非空かつDB値と異なる場合のみ更新）

### 7. マイルストーン紐づけの一括編集（pm_relink.py）

インポート済みのアクションアイテムとマイルストーンの紐づけをCSV経由で一括編集する。LLMは使用しない。

**仕様**:
- `milestone_id` 列に `M1`〜`M5` を記入 → その値で上書き
- `milestone_id` 列を空欄 → `NULL` で上書き（紐づけ解除）

```sh
# milestone_id が未設定のアイテムをCSVにエクスポート（デフォルト: relink.csv）
python3 scripts/pm_relink.py --export

# 全件エクスポート
python3 scripts/pm_relink.py --export --all

# 変更内容を確認（DB更新なし）
python3 scripts/pm_relink.py --import relink.csv --dry-run

# DBに反映（確認プロンプトあり）
python3 scripts/pm_relink.py --import relink.csv
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--export` | - | アクションアイテムをCSVにエクスポート |
| `--import PATH` | - | CSVを読み込んでDBを更新 |
| `--all` | - | `--export` 時に全件対象（デフォルトは `milestone_id IS NULL` のみ） |
| `--output PATH` | `relink.csv` | `--export` 時の出力ファイルパス |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--no-encrypt` | - | 平文モード |
| `--dry-run` | - | DB更新なし・変更内容を表示のみ |

### 8. ゴール・マイルストーン同期（pm_goals_import.py）

`goals.yaml` を読み込み `goals` / `milestones` テーブルに完全同期する。

```sh
# goals.yaml を編集・承認後に実行（完全同期）
python3 scripts/pm_goals_import.py

# 変更内容を確認してから実行
python3 scripts/pm_goals_import.py --dry-run

# 登録済み一覧・達成状況を確認
python3 scripts/pm_goals_import.py --list
```

### 9. DBユーティリティ（db_utils.py）

#### 暗号化・鍵管理

```sh
# 鍵を生成（初回のみ）
python3 scripts/db_utils.py --gen-key
# → ~/.secrets/pm_db_key.txt に 64文字のランダム鍵を生成（chmod 600）

# 既存の平文DBを暗号化DBに変換（初回のみ）
python3 scripts/db_utils.py --migrate data/pm.db data/C08SXA4M7JT.db data/C0A9KG036CS.db
# → 各 .bak にバックアップを作成してから変換・検証

# 変換内容を確認のみ（変換しない）
python3 scripts/db_utils.py --migrate data/pm.db --dry-run
```

**鍵ファイルを紛失すると暗号化済みDBは復元不可能。パスワードマネージャー等に必ずバックアップすること。**

全スクリプトに `--no-encrypt` オプションがあり、平文モードで動作させることができる。

#### 変更履歴の確認（audit_log）

Canvas同期（`pm_sync_canvas.py`）とマイルストーン紐づけ変更（`pm_relink.py`）の変更前後を記録した `audit_log` を参照する。

```sh
# 直近30件を表示
python3 scripts/db_utils.py --audit-log

# 件数を指定
python3 scripts/db_utils.py --audit-log --limit 100

# ソースで絞り込む
python3 scripts/db_utils.py --audit-log --source canvas_sync
python3 scripts/db_utils.py --audit-log --source relink

# 特定アクションアイテムの変更履歴
python3 scripts/db_utils.py --audit-log --id 98
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--limit N` | `30` | 表示件数 |
| `--source SOURCE` | なし（全件） | `canvas_sync` または `relink` で絞り込む |
| `--id ID` | なし（全件） | アクションアイテムIDで絞り込む |
| `--no-encrypt` | - | 平文モード |
