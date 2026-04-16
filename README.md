# ProjectManagement

富岳NEXT アプリケーション開発エリアのプロジェクトマネジメント支援システム。

---

## このシステムが解決する問題

大規模プロジェクトでは、会議・Slack・資料に**情報が分散**し、以下の問題が起きる。

| PM課題 | 放置するとどうなるか |
|--------|----------------------|
| 会議の決定事項が記憶頼みで流れる | 同じ議論が繰り返され、合意が形骸化する |
| アクションアイテムの担当・期限が曖昧 | 「誰がやるのか」が不明なまま期限を超過する |
| ゴールとタスクが紐づいていない | 忙しいが前進していない状態に陥る |
| 状況把握に毎回手作業が必要 | PMが情報収集に追われ、判断に集中できない |
| 機密情報を外部サービスに送れない | 市販PMツールが使えず、手運用に逆戻りする |

本システムは、**ローカルLLM**で情報を自動収集・構造化し、人間がゴールの定義と最終判断に集中できる環境を提供する。

---

## 設計思想：2層構造

```
【トップダウン層】 ゴール・マイルストーン
                  └─ goals.yaml に人間が定義・承認（gitで変更履歴管理）
                          ↓ 評価の軸を与える
【ボトムアップ層】 アクションアイテム・決定事項
                  └─ 会議議事録・Slackから LLM が自動抽出・マイルストーンに紐づけ
```

| 役割 | 担当 |
|------|------|
| ゴール・マイルストーンの定義・承認 | 人間（意思決定者） |
| 情報の収集・整理・抽出・紐づけ推定 | LLM |
| 誤りの修正・最終判断 | 人間（Canvas / Web UI で編集） |
| 達成状況の計算・レポート・リスク検知 | システム |

---

## 機能マップ

本システムの機能を、解決するPM課題ごとに整理する。

### 1. 情報の自動収集 — 「手作業で情報を集めなくて済む」

週次会議・Slackの投稿から、決定事項・アクションアイテム・担当者・期限をLLMが自動抽出して pm.db に蓄積する。

**会議録音 → 議事録 → pm.db**（録音を置くだけで完結）

```sh
# data/ に .m4a を置いて実行するだけ
bash scripts/pm_from_recording_auto.sh

# Slack投稿も自動化
bash scripts/pm_from_recording_auto.sh -c C08SXA4M7JT
```

処理フロー: 音声 → Whisper文字起こし → ローカルLLMで議事録生成 → 議事録DB保存 → pm.db転記（平文ファイルはディスクに残らない）

**Slack → 要約 → pm.db**

```sh
bash scripts/pm_from_slack.sh -c C08SXA4M7JT
```

処理フロー: Slackメッセージ差分取得 → スレッド単位で要約 → 決定事項・アクションアイテム抽出 → pm.db保存

### 2. ゴール管理 — 「今どこにいるかが分かる」

マイルストーンを定義し、全アクションアイテムを紐づけることで、プロジェクトの現在地を定量的に把握する。

```sh
# goals.yaml を編集後に同期
python3 scripts/pm_goals_import.py

# 達成状況を確認
python3 scripts/pm_goals_import.py --list
```

`goals.yaml` はgit管理。マイルストーンの変更理由・経緯がコミット履歴として残る。

### 3. 進捗の可視化とレビュー — 「会議で使えるレポートが自動で出る」

pm.db から週次進捗レポートを自動生成し、Slack Canvas に投稿する。会議中にCanvas上で直接編集でき、変更はDBに同期される。

**会議前: レポート投稿**

```sh
source ~/.secrets/slack_tokens.sh
bash scripts/canvas_report.sh --db data/pm.db --canvas-id F0ALP1XQJHL
```

レポート構成:
1. **プロジェクトの現在地** — マイルストーン達成率・残日数（DBから自動計算）
2. **サマリー** — LLMによる全体概況
3. **直近の決定事項** — 未確認はチェックボックスで管理
4. **要注意事項** — 期限超過・担当者不明のアイテム
5. **未完了アクションアイテム** — 表形式（Canvas上で各列を直接編集可能）

**会議中: Canvas上で編集**

Canvas上で編集可能な列: **担当者・内容・期限・マイルストーン・状況・対応状況**

- **状況** 列にチェックを入れる or 「完了」「done」等を記入 → 完了扱い
- **決定事項** のチェックボックスにチェック → 確認済みとして次回レポートから非表示

**会議後: DB同期**（次回レポート投稿時に自動実行。単独実行も可能）

```sh
python3 scripts/pm_sync_canvas.py --db data/pm.db --canvas-id F0ALP1XQJHL
```

### 4. リスク検知とインサイト — 「問題に気づくのが遅れない」

**Argus AI秘書** — 毎朝のブリーフィングを自動生成（cron 平日8:57）

Slack生メッセージ・議事録・pm.db統計を統合分析し、今日やるべきことを優先度順に提示する。Slackスラッシュコマンドで誰でも利用可能。

```
/argus-brief                 ← 今日の状況サマリーと優先アクション（最大5件）
/argus-brief @西澤            ← 特定担当者にフォーカス
/argus-draft agenda 次回リーダー会議  ← 会議アジェンダ草案
/argus-risk                  ← リスク一覧と予兆の検知
```

**LLMインサイト** — プロジェクト健全性のA/B/C/D評価

```sh
python3 scripts/pm_insight.py --db data/pm.db --dry-run
```

期限超過・担当者負荷・完了速度の推移等を統計集計し、LLMが「なぜ遅れているか」「次に何をすべきか」を解釈・提案する。

### 5. 過去の議論を検索 — 「あの話どこで決まったっけ？」

`/ask` コマンドで議事録本文・Slack生メッセージを自然言語検索できる。SudachiPy形態素解析 + FTS5 + LLM re-ranking による日本語検索。

```
/ask GPU性能の評価方針について
/ask Benchparkハッカソンの内容を教えて
```

### 6. データの編集と修正 — 「LLMの誤りを人間が正せる」

LLMの抽出は完璧ではない。誤った担当者・期限・マイルストーン紐づけを人間が修正できる手段を複数提供する。

**Web UI**（ブラウザで編集）

```sh
python3 scripts/pm_api.py --port 8501 --db data/pm.db
# → http://localhost:8501
```

**CLI一括編集**（CSV経由）

```sh
python3 scripts/pm_relink.py --export          # CSVにエクスポート
# CSVを編集...
python3 scripts/pm_relink.py --import relink.csv  # DBに反映
```

**議事録の修正と再インポート**

```sh
python3 scripts/pm_minutes_import.py --export 2026-03-10_Leader_Meeting -o corrected.md
# corrected.md を修正...
python3 scripts/pm_minutes_import.py corrected.md --meeting-name Leader_Meeting \
    --held-at 2026-03-10 --no-llm --force
```

---

## 情報の流れ

```
[Slack] ─── slack_pipeline.py ───→ {channel_id}.db
                                          ↓
[会議録音]                          pm.db ←─ pm_extractor.py
  data/*.m4a           │          (決定事項・               ↑
                       │        アクションアイテム)   {channel_id}.db
                       │                  ↓
                       │            pm_report.py → Slack Canvas
                       │            pm_insight.py → 健全性評価
                       │            pm_argus.py → ブリーフィング
                       │
                       └─ pm_minutes_import.py ──→ data/minutes/{kind}.db
                                    ↓          （詳細議事録・担当者・期限）
                         pm_minutes_to_pm.py ──→ pm.db
```

![情報の流れ](minutes.png)

---

## データベース構成

| DB | 役割 | 単位 |
|----|------|------|
| `data/{channel_id}.db` | Slackメッセージ・スレッド要約 | チャンネルごとに独立 |
| `data/minutes/{kind}.db` | 議事録詳細（議事内容・決定事項・AI） | 会議名ごとに独立 |
| `data/pm.db` | PM統合データ（全チャンネル・全会議を横断） | 1ファイル |
| `data/qa_pm*.db` | QA検索インデックス（FTS5） | インデックスごとに独立 |

### pm.db のテーブル

| テーブル | 内容 |
|---------|------|
| `action_items` | アクションアイテム（担当者・期限・status・note・milestone_id） |
| `decisions` | 決定事項（確認済み管理付き） |
| `goals` / `milestones` | goals.yaml から同期したゴール・マイルストーン |
| `meetings` | 会議情報（開催日・種別・要約） |
| `slack_extractions` | 抽出済みスレッド管理（差分処理用） |
| `audit_log` | 全変更履歴（Canvas同期・relink・Web UI操作を記録） |

変更履歴の確認:
```sh
python3 scripts/db_utils.py --audit-log
python3 scripts/db_utils.py --audit-log --source canvas_sync --limit 50
```

---

## セキュリティ

### 機密情報の保護方針

- **LLM処理は全てローカル**: 議事録・Slackメッセージ等の機密情報は外部サービスに送出しない。組織内で稼働するローカルLLM（vLLMサーバ）で処理する
- **DB暗号化**: 全DBにSQLCipher AES-256暗号化を適用。ファイルが漏洩しても鍵なしでは内容を読めない
- **議事録の平文残存防止**: `--meeting-name` 指定時は処理完了後に .md ファイルを自動削除
- **トークン管理**: `~/.secrets/` 配下にファイルとして保管（`chmod 600`）。`.bashrc` への直書き禁止

### DB暗号化の初回セットアップ

```sh
python3 scripts/db_utils.py --gen-key                    # 鍵生成
python3 scripts/db_utils.py --migrate data/pm.db data/C*.db  # 平文→暗号化変換
```

**鍵を紛失すると暗号化済みDBは復元不可能。** パスワードマネージャー等に必ずバックアップすること。

---

## 環境セットアップ

### 動作要件

- Python 3.10 以上
- **ローカルLLM（推奨）**: OpenAI互換APIサーバ（vLLM等）を起動し、環境変数で接続先を設定
- 文字起こし機能を使用する場合: GPU環境（NVIDIA L40S / GH200 等）

### トークン設定

```sh
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_USER_TOKEN="xoxp-..."
export OPENAI_API_BASE="http://localhost:8000/v1"
export OPENAI_API_KEY="dummy"
export OPENAI_MODEL="google/gemma-4-26B-A4B-it"
EOF
chmod 600 ~/.secrets/slack_tokens.sh
```

### Slack User Token のスコープ

`channels:history`, `channels:read`, `users:read`, `files:read`, `files:write`, `canvases:read`, `canvases:write`

### QAサーバー・Argus の起動

```sh
bash scripts/pm_qa_start.sh    # /ask・/argus-* が有効になる
bash scripts/pm_qa_stop.sh     # 停止
```

---

## スクリプト一覧

詳細なオプションは `CLAUDE.md` の[コマンドリファレンス](docs/commands.md)を参照。

### 日常運用（シェルスクリプト）

| スクリプト | 用途 |
|-----------|------|
| `pm_from_recording_auto.sh` | 録音ファイルの自動検出・文字起こし・議事録生成・pm.db登録 |
| `pm_from_recording.sh` | 録音ファイルを指定して文字起こし・議事録生成 |
| `pm_from_slack.sh` | Slack取得・要約・pm.db抽出を一括実行 |
| `canvas_report.sh` | Canvas同期 → レポート生成・Canvas投稿 |
| `pm_qa_start.sh` / `pm_qa_stop.sh` | QA・Argusデーモンの起動・停止 |

### 情報収集・抽出

| スクリプト | 用途 |
|-----------|------|
| `slack_pipeline.py` | Slack差分取得・スレッド要約・Canvas投稿 |
| `pm_extractor.py` | Slack要約からアクションアイテム・決定事項を抽出 |
| `pm_minutes_import.py` | 議事録をLLM解析して議事録DBに保存 |
| `pm_minutes_to_pm.py` | 議事録DBからpm.dbに転記（LLM不使用） |
| `generate_minutes_local.py` | ローカルLLMで高品質議事録を生成 |
| `whisper_vad.py` | VAD+Whisperによる話者分離・文字起こし |

### レポート・分析

| スクリプト | 用途 |
|-----------|------|
| `pm_report.py` | 週次進捗レポート生成・Canvas投稿 |
| `pm_insight.py` | プロジェクト健全性評価・リスク特定・改善提案 |
| `pm_argus.py` | Argus AI秘書（ブリーフィング・草案・リスク分析） |

### データ編集・同期

| スクリプト | 用途 |
|-----------|------|
| `pm_sync_canvas.py` | Canvas上の編集内容をpm.dbに同期 |
| `pm_relink.py` | CSV経由でアクションアイテム・決定事項を一括編集 |
| `pm_goals_import.py` | goals.yaml → pm.db 完全同期 |
| `pm_api.py` | Web UI（FastAPI REST API + フロントエンド） |

### QA・検索

| スクリプト | 用途 |
|-----------|------|
| `pm_qa_server.py` | Slack Socket Modeデーモン（/ask・/argus-*を統合処理） |
| `pm_embed.py` | QAインデックス構築（SudachiPy+FTS5） |

### 共通ライブラリ

| モジュール | 用途 |
|-----------|------|
| `db_utils.py` | DB接続・統計クエリ・暗号化（全スクリプト共通） |
| `cli_utils.py` | LLM呼び出し・ログ・argparse（全スクリプト共通） |
| `web_utils.py` | Web UI用DB読み書き・楽観的排他制御 |
| `format_utils.py` | Markdownテーブル整形 |
| `canvas_utils.py` | Slack Canvas操作 |
