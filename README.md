# ProjectManagement

富岳NEXTプロジェクトのプロジェクトマネジメント支援システム。

---

## 設計思想

### 目指すプロマネの姿

このシステムが目指すのは、**「議事録係＋ToDoリスト管理」ではなく、プロジェクトのゴールへの到達を管理するプロジェクトマネジメント**である。

一般的なAI活用PMツールは、発言・議事録・Slackから決定事項やアクションアイテムを拾い上げることに終始しがちである。それは情報の整理には役立つが、「プロジェクトが今どこにいるのか」「ゴールに向けて前進しているのか」を答えることができない。

本システムは以下の2層構造でこの問題に対処する。

```
【トップダウン層】 ゴール・マイルストーン（人間が定義・承認、goals.yamlで管理）
                          ↓ 評価の軸を与える
【ボトムアップ層】 アクションアイテム・決定事項（LLMが自動抽出・マイルストーンに紐づけ）
```

**トップダウン層**: プロジェクトのゴールと主要マイルストーンは `goals.yaml` に人手で定義し、意思決定者が承認する。LLMによる自動抽出に頼らない。gitで管理することでマイルストーン変更の意思決定履歴も残る。

**ボトムアップ層**: 会議議事録・Slackの膨大な情報からアクションアイテム・決定事項をLLMが自動抽出する。各アイテムはマイルストーンに紐づけられ、「このタスクはどのゴールに向けた作業か」が明確になる。

### LLMと人間の役割分担

| 役割 | 担当 |
|------|------|
| ゴール・マイルストーンの定義・承認 | 人間（意思決定者） |
| 情報の収集・整理・抽出 | LLM |
| アイテムのマイルストーンへの紐づけ推定 | LLM |
| 誤りの修正・最終判断 | 人間（Canvas上で編集） |
| 達成状況の計算・レポート生成 | システム |

LLMは「情報処理の自動化」に使い、「何を目指すか」「どこまで達成したか」の判断は人間が行う。

### 現在の実装状況

フェーズ1〜5が実装済み。`goals.yaml` にゴール・マイルストーンを定義し、`pm_goals_import.py` でDBに同期することで、レポートに「プロジェクトの現在地」セクションが自動追加される。

---

## 概要

会議議事録（Whisper文字起こし）とSlackチャンネルの投稿から、LLM（Claude）を使って決定事項とアクションアイテムを自動抽出し、SQLiteデータベース（pm.db）に蓄積する。蓄積した情報を週次進捗レポートとしてSlack Canvasに投稿し、Canvas上で対応状況を記入することでDBを更新するワークフローを提供する。

---

## 情報の流れ

![情報の流れ](minutes.png)

---

## スクリプト構成

| スクリプト | 役割 |
|---|---|
| `pm_goals_import.py` | `goals.yaml` を pm.db に完全同期（ゴール・マイルストーン管理） |
| `slack_pipeline.py` | Slackメッセージを取得・要約してCanvas投稿、`{channel_id}.db`に保存 |
| `whisper_vad.py` | 会議録音をSlurmジョブとしてWhisperで文字起こし |
| `pm_meeting_import.py` | 文字起こし議事録をLLMで解析してpm.dbに保存（単一ファイル / 一括処理・一覧・削除） |
| `pm_extractor.py` | Slack DB内のスレッド要約からアクションアイテム・決定事項を抽出してpm.dbに保存 |
| `pm_report.py` | pm.dbから週次進捗レポートを生成してSlack Canvasに投稿 |
| `pm_sync_canvas.py` | Canvas上の編集内容（担当者・内容・期限・マイルストーン・対応状況）をpm.dbに同期 |
| `pm_relink.py` | アクションアイテムの各フィールド（担当者・期限・内容・マイルストーン・status）をCSV経由で一括編集（LLM不使用） |
| `db_utils.py` | DB接続の一元管理・平文DBの暗号化変換（SQLCipher対応） |
| `cli_utils.py` | 共通CLIユーティリティ（argparse ヘルパー・`make_logger`）。各スクリプトから内部利用 |

---

## 日常の運用フロー

### 定期実行（Slack差分取得・要約）

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db
```

### ゴール・マイルストーンの更新

`goals.yaml` を編集・承認したら pm.db に同期する。

```sh
# 変更内容の確認（DB操作なし）
python3 scripts/pm_goals_import.py --dry-run

# 同期実行（追加・更新・削除を完全同期）
python3 scripts/pm_goals_import.py

# 登録済み一覧・達成状況の確認
python3 scripts/pm_goals_import.py --list
```

### 会議前: 週次レポートをCanvasに投稿

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/pm_report.py --canvas-id F0AAD2494VB
```

レポート構成:
1. プロジェクトの現在地（マイルストーン達成率・状況、DBから直接計算）
2. サマリー（LLM生成）
3. 直近の決定事項
4. 要注意事項
5. 未完了アクションアイテム（表形式: ID・担当者・内容・期限・ソース・対応状況）

### 会議中: Canvas上でアクションアイテムの「対応状況」列に記入

完了判定キーワード（DBの`status`を`closed`に更新）:
`完了`, `done`, `済`, `対応済`, `解決`, `closed`, `finish`, `finished`

上記以外の記入はメモとして`note`列に保存（statusはopenのまま）。

### 会議後: Canvas上の記入内容をDBに反映

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/pm_sync_canvas.py --canvas-id F0AAD2494VB
```

Canvas上で変更可能な列: **担当者・内容・期限・マイルストーン（M1〜M5）・対応状況**

---

### アクションアイテムの一括編集（LLM不使用）

アクションアイテムの各フィールドをCSVを介して手動で編集する。`assignee`・`due_date`・`milestone_id`・`content`・`status` を一括変更できる。

```sh
# milestone_id が未設定のアイテムをCSVにエクスポート
python3 scripts/pm_relink.py --export

# 全件エクスポート
python3 scripts/pm_relink.py --export --all --output relink_all.csv

# 日付フィルタ付きエクスポート
python3 scripts/pm_relink.py --export --since 2026-02-01

# 変更内容を確認（DB更新なし）
python3 scripts/pm_relink.py --import relink.csv --dry-run

# DBに反映（確認プロンプトあり）
python3 scripts/pm_relink.py --import relink.csv

# アクションアイテムをターミナルに一覧表示
python3 scripts/pm_relink.py --list
python3 scripts/pm_relink.py --list --all --since 2026-02-01
```

`assignee`・`due_date`・`milestone_id` は空欄 → NULL（解除）。`content`・`status` は空欄の場合スキップ（変更なし）。

---

## 会議議事録の処理フロー

### 1. 録音を文字起こし → pm.db へ直接インポート（推奨）

`--meeting-name` を指定すると文字起こし後に pm.db へ直接インポートし、.md ファイルを削除する（平文ファイルがディスクに残らない）。

```sh
# 推奨: pm.db に直接保存（.md は削除）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting

# 日付を明示する場合（省略時はファイル名のGMTタイムスタンプをJSTに自動変換）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10

# 冒頭スキップ
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 --skip 30 --meeting-name Leader_Meeting
```

### 1a. 従来方式: .md ファイルを経由する場合

`--meeting-name` を省略すると従来通り .md ファイルを出力する（セキュリティ警告が表示される）。

```sh
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4
# → GMT20260302-032528_Recording.md が生成される
```

.md ファイルを後から pm.db に一括登録する場合:

```sh
python3 scripts/pm_meeting_import.py --bulk

# 特定日付以降のみ
python3 scripts/pm_meeting_import.py --bulk --since 2026-01-01
```

### 3. Slack要約からアクションアイテムを抽出

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/pm_extractor.py -c C08SXA4M7JT

# 抽出済みスレッドの一覧確認
python3 scripts/pm_extractor.py -c C08SXA4M7JT --list
```

---

## データベース構成

### `{channel_id}.db` - Slackデータ

- `messages`: 親メッセージ
- `replies`: 返信メッセージ
- `summaries`: LLMによるスレッド要約

### `pm.db` - PM統合データ

- `meetings`: 会議情報（開催日・種別・要約）
- `action_items`: アクションアイテム（担当者・期限・status・note・milestone_id）
- `decisions`: 決定事項
- `slack_extractions`: 抽出済みスレッド管理（差分処理用）
- `goals` / `milestones`: goals.yaml から同期したゴール・マイルストーン
- `audit_log`: action_items の変更履歴（Canvas同期・relink 実行時に記録）

変更履歴の確認:
```sh
python3 scripts/db_utils.py --audit-log
python3 scripts/db_utils.py --audit-log --source canvas_sync --limit 50
python3 scripts/db_utils.py --audit-log --id 98        # 特定アイテムの履歴
python3 scripts/db_utils.py --audit-log --output audit.txt  # ファイルにも保存
```

---

## 環境セットアップ

### Python環境

```sh
# uv仮想環境を使用
~/.venv_x86_64/bin/python3 scripts/pm_report.py ...
# または
source ~/.venv_x86_64/bin/activate
python3 scripts/pm_report.py ...
```

### 必要パッケージ

```
slack-bolt
slack-sdk
```

### Slack Bot Token の取得

以下のスクリプトの実行には Slack Bot Token（`xoxb-...`）が必要。

1. Slack API サイト（api.slack.com/apps）でアプリを作成する
2. 「OAuth & Permissions」で以下のBot Token Scopesを付与する:
   - `channels:history` - メッセージ取得
   - `channels:read` - チャンネル情報取得
   - `users:read` - ユーザー名取得
   - `files:read` - Canvas（ファイル）取得
   - `canvases:read` - Canvas内容読み取り
   - `canvases:write` - Canvas編集
3. アプリをワークスペースにインストールし、「Bot User OAuth Token」をコピーする
4. アプリを対象チャンネルに招待する（`/invite @アプリ名`）

### トークン設定（安全な方法）

```sh
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_BOT_TOKEN="xoxb-..."
EOF
chmod 600 ~/.secrets/slack_tokens.sh
```

---

## セキュリティ

### DBの暗号化（SQLCipher AES-256）

`pm.db`（決定事項・アクションアイテム・会議情報）および `{channel_id}.db`（Slackメッセージ・要約）の全DBに SQLCipher による AES-256 暗号化を採用している。ファイルが漏洩しても鍵なしでは内容を読めない。

暗号化鍵は `~/.secrets/pm_db_key.txt`（`chmod 600`）または環境変数 `PM_DB_KEY` から読み込む。すべてのスクリプトで DB 接続を `scripts/db_utils.py` に一元管理することで、暗号化を透過的に適用している。

#### 初回セットアップ

```sh
# 鍵を生成
python3 scripts/db_utils.py --gen-key

# 既存の平文DBを暗号化DBに変換（バックアップを自動作成）
python3 scripts/db_utils.py --migrate data/pm.db data/C08SXA4M7JT.db data/C0A9KG036CS.db

# 変換内容を確認のみ（変換しない）
python3 scripts/db_utils.py --migrate data/pm.db --dry-run

# バックアップなしで変換
python3 scripts/db_utils.py --migrate data/pm.db --no-backup
```

生成した鍵はパスワードマネージャー等に必ずバックアップすること。**鍵を紛失すると暗号化済みDBは復元不可能。**

---

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可。`pm_meeting_import.py`, `pm_extractor.py`, `pm_report.py` は**Claude Codeの外のターミナル**から実行すること。
- Slack Canvasは表示できない文字（特殊Unicode）を含むとAPIエラーになる。スクリプト内で`sanitize_for_canvas()`による除去処理を実施済み。
- pm_report.pyは上書き投稿のみ対応。初回実行前にCanvasの内容を手動で削除しておくこと。
- Slack DBはチャンネルごとに独立（`data/{channel_id}.db`）。pm.dbは複数チャンネル・複数会議を横断して統合する。
