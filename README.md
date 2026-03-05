# ProjectManagement

富岳NEXTプロジェクトのプロジェクトマネジメント支援システム。Slackの日常的なやり取りと会議議事録を統合し、決定事項・アクションアイテムの一元管理と定期レポート生成を行う。

---

## 概要

会議議事録（Whisper文字起こし）とSlackチャンネルの投稿から、LLM（Claude）を使って決定事項とアクションアイテムを自動抽出し、SQLiteデータベース（pm.db）に蓄積する。蓄積した情報を週次進捗レポートとしてSlack Canvasに投稿し、Canvas上で対応状況を記入することでDBを更新するワークフローを提供する。

---

## 情報の流れ

```
[会議録音ファイル]
      |
      | trans.sh (Slurm + whisper_vad.py)
      v
[文字起こし .md]
      |
      | meeting_parser.py
      v
[pm.db] <------- pm_extractor.py <--- [{channel_id}.db]
   |                                          ^
   |                              slack_pipeline.py
   |                                          |
   |                                    [Slack API]
   |
   | pm_report.py
   v
[Slack Canvas] ---> (会議中に「対応状況」を記入)
                          |
                          | pm_sync_canvas.py
                          v
                        [pm.db 更新]
```

---

## スクリプト構成

| スクリプト | 役割 |
|---|---|
| `slack_pipeline.py` | Slackメッセージを取得・要約してCanvas投稿、`{channel_id}.db`に保存 |
| `trans.sh` + `whisper_vad.py` | 会議録音をSlurmジョブとしてWhisperで文字起こし |
| `meeting_parser.py` | 文字起こし議事録をLLMで解析してpm.dbに保存 |
| `pm_extractor.py` | Slack DB内のスレッド要約からアクションアイテム・決定事項を抽出してpm.dbに保存 |
| `pm_report.py` | pm.dbから週次進捗レポートを生成してSlack Canvasに投稿 |
| `pm_sync_canvas.py` | Canvas上の「対応状況」列を読み取りpm.dbを更新 |

---

## 日常の運用フロー

### 定期実行（Slack差分取得・要約）

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db
```

### 会議前: 週次レポートをCanvasに投稿

```sh
source ~/.secrets/slack_tokens.sh
python3 scripts/pm_report.py --canvas-id F0AAD2494VB
```

レポート構成:
1. サマリー（LLM生成）
2. 直近の決定事項
3. 要注意事項
4. 未完了アクションアイテム（表形式: ID・担当者・内容・期限・ソース・対応状況）

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

### 2. 文字起こしをpm.dbに保存

```sh
python3 scripts/meeting_parser.py meetings/GMT20260302-032528_Recording.md \
    --meeting-name "Leader_Meeting" --held-at 2026-03-02
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

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可。`meeting_parser.py`, `pm_extractor.py`, `pm_report.py` は**Claude Codeの外のターミナル**から実行すること。
- Slack Canvasは表示できない文字（特殊Unicode）を含むとAPIエラーになる。スクリプト内で`sanitize_for_canvas()`による除去処理を実施済み。
- pm_report.pyは上書き投稿のみ対応。初回実行前にCanvasの内容を手動で削除しておくこと。
- Slack DBはチャンネルごとに独立（`data/{channel_id}.db`）。pm.dbは複数チャンネル・複数会議を横断して統合する。
