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

```
[goals.yaml] ---> pm_goals_import.py ---> [pm.db: goals/milestones]
 (人間が定義)                                       |
                                                    | 評価の軸を与える
[会議録音ファイル]                                   |
      |                                             v
      | trans.sh (Slurm + whisper_vad.py)    [pm.db: action_items]
      v                                        (milestone_id 付き)
[文字起こし .md]                                     |
      |                                             |
      | pm_meeting_import.py                        |
      v                                             |
[pm.db] <------- pm_extractor.py <--- [{channel_id}.db]
   |                                          ^
   |                              slack_pipeline.py
   |                                          |
   |                                    [Slack API]
   |
   | pm_report.py
   v
[Slack Canvas]
  - プロジェクトの現在地（マイルストーン達成率）
  - サマリー・決定事項・要注意事項
  - 未完了アクションアイテム表
       |
       | (会議中に担当者・内容・期限・対応状況を直接編集)
       v
  pm_sync_canvas.py ---> [pm.db 更新]
```

---

## スクリプト構成

| スクリプト | 役割 |
|---|---|
| `pm_goals_import.py` | `goals.yaml` を pm.db に完全同期（ゴール・マイルストーン管理） |
| `slack_pipeline.py` | Slackメッセージを取得・要約してCanvas投稿、`{channel_id}.db`に保存 |
| `trans.sh` + `whisper_vad.py` | 会議録音をSlurmジョブとしてWhisperで文字起こし |
| `pm_meeting_bulk_import.py` | `meetings/` の議事録ファイルを一括でpm.dbに登録・削除・一覧表示 |
| `pm_meeting_import.py` | 文字起こし議事録をLLMで解析してpm.dbに保存（1ファイル単位） |
| `pm_extractor.py` | Slack DB内のスレッド要約からアクションアイテム・決定事項を抽出してpm.dbに保存 |
| `pm_report.py` | pm.dbから週次進捗レポートを生成してSlack Canvasに投稿 |
| `pm_sync_canvas.py` | Canvas上の編集内容（担当者・内容・期限・対応状況）をpm.dbに同期 |
| `db_utils.py` | DB接続の一元管理（SQLCipher暗号化対応） |
| `db_migrate.py` | 既存の平文DBを暗号化DBに変換 |

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

---

## 会議議事録の処理フロー

### 1. 録音を文字起こし（Slurmジョブ）

```sh
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4
```

出力: `GMT20260302-032528_Recording.md`（タイムスタンプ・話者ラベル付き）

### 2. 議事録を一括でpm.dbに登録

`meetings/` ディレクトリ内の `YYYY-MM-DD_{会議名}.md` ファイルをまとめて登録する。

```sh
python3 scripts/pm_meeting_bulk_import.py

# 特定日付以降のみ
python3 scripts/pm_meeting_bulk_import.py --since 2026-01-01
```

### 3. Slack要約からアクションアイテムを抽出

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/pm_extractor.py -c C08SXA4M7JT
```

---

## データベース構成

### `{channel_id}.db` - Slackデータ

- `messages`: 親メッセージ
- `replies`: 返信メッセージ
- `summaries`: LLMによるスレッド要約

### `pm.db` - PM統合データ

- `meetings`: 会議情報（開催日・種別・要約）
- `action_items`: アクションアイテム（担当者・期限・status・note）
- `decisions`: 決定事項
- `slack_extractions`: 抽出済みスレッド管理（差分処理用）

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
python3 scripts/db_migrate.py data/pm.db data/C08SXA4M7JT.db data/C0A9KG036CS.db
```

生成した鍵はパスワードマネージャー等に必ずバックアップすること。**鍵を紛失すると暗号化済みDBは復元不可能。**

---

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可。`pm_meeting_import.py`, `pm_extractor.py`, `pm_report.py` は**Claude Codeの外のターミナル**から実行すること。
- Slack Canvasは表示できない文字（特殊Unicode）を含むとAPIエラーになる。スクリプト内で`sanitize_for_canvas()`による除去処理を実施済み。
- pm_report.pyは上書き投稿のみ対応。初回実行前にCanvasの内容を手動で削除しておくこと。
- Slack DBはチャンネルごとに独立（`data/{channel_id}.db`）。pm.dbは複数チャンネル・複数会議を横断して統合する。
