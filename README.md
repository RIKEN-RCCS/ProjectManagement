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

### 1. 情報の自動収集 — 「手作業で情報を集めなくて済む」

週次会議・Slackの投稿から、決定事項・アクションアイテム・担当者・期限をLLMが自動抽出して pm.db に蓄積する。

**会議録音 → 議事録 → pm.db**（録音を置くだけで完結）

```sh
# sbatch で実行（SLURM）
sbatch scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting

# スラッシュコマンドでも可能（pm_qa_server 起動中）
/argus-transcribe 2026-04-20_Leader_Meeting.m4a
```

処理フロー: 音声 → Whisper 文字起こし（VTT 話者情報・スライドOCR と統合） → ローカルLLMで議事録生成 → 議事録DB保存 → pm.db転記（平文ファイルは自動削除）

**Slack → pm.db**

```sh
bash scripts/pm_from_slack.sh -c CHANNEL_ID
```

処理フロー: Slackメッセージ差分取得（統合 `data/slack.db`） → LLM抽出 → pm.db保存

### 2. ゴール管理 — 「今どこにいるかが分かる」

マイルストーンを定義し、全アクションアイテムを紐づけることで、プロジェクトの現在地を定量的に把握する。

```sh
# goals.yaml を編集後に同期
python3 scripts/ingest/pm_ingest.py goals

# 達成状況を確認
python3 scripts/ingest/pm_ingest.py goals --goals-list
```

`goals.yaml` はgit管理。マイルストーンの変更理由・経緯がコミット履歴として残る。

### 3. 進捗の可視化とレビュー — 「会議で使えるレポートが自動で出る」

pm.db から週次進捗レポートを自動生成し、Slack Canvas に投稿する。

```sh
source ~/.secrets/slack_tokens.sh
bash scripts/canvas_report.sh
```

会議中にCanvas上で各列（担当者・内容・期限・マイルストーン・状況）を直接編集でき、`pm_sync_canvas.py` でDBに反映される。

### 4. Argus AI — 「問題に気づくのが遅れない」

Slack スラッシュコマンドでプロジェクトデータを即座に分析する。全コマンドは `pm_qa_server.py`（Socket Mode デーモン）が処理する。

```
/argus-brief                     ← 今やるべきこと（優先アクション5件）
/argus-risk                      ← リスク一覧と予兆
/argus-investigate M3の遅延原因   ← マルチステップ調査（Agent）
/argus-today                     ← 本日の活動 + 自分宛メンション
/argus-draft agenda 次回会議      ← 文書草案生成
/argus-transcribe Recording.mp4   ← 録音から議事録生成
```

`/argus-investigate` は15種類のツール（pm.db検索・FTS5全文検索・Explorer Agent・Box/Slack/Canvas出力）をLLMが自律選択するマルチステップエージェント。

出力先フラグ（末尾に付加）:
```
/argus-investigate M3の遅延原因 --to-box      # Box に md 保存
/argus-investigate M3の遅延原因 --to-slack     # チャンネルに公開投稿
/argus-investigate M3の遅延原因 --to-canvas    # Canvas に投稿
```

**Patrol Agent（自律巡回）**: cron 30分ごとに pm.db を巡回し、完了シグナル検出・期限超過リマインダー・長期停滞検出・マイルストーン健全性チェックを自動実行する。

### 5. 過去の議論を検索 — 「あの話どこで決まったっけ？」

FTS5（SudachiPy 形態素解析 + trigram） + bge-m3 embedding のハイブリッド検索で、議事録・Slack生メッセージ・BOXドキュメント・Web記事を横断検索する。

```
/argus-investigate GPU性能の評価方針について
/argus-investigate Benchparkの資料はどこ？
/argus-investigate 設計方針に関する決定事項は？
```

### 6. Box ドキュメント本文の索引化

BOX フォルダ内のドキュメント（pptx/docx/pdf/xlsx/md）を Markdown 化し、relevance 判定（core/related/noise）を経て FTS5 インデックスに組み込む。

```sh
bash scripts/pm_box_update.sh                           # 走査・変換・FTS5一括更新
python3 scripts/pm_box_relevance.py --judge --stats      # relevance 判定・分布確認
```

### 7. マルチエージェント MCP Server（pm-multi-agent）

`pm_mcp_server.py` は FastMCP サーバーとして動作し、Claude Code（Orchestrator）から全ツールを呼び出せる。`/argus-investigate` と全く同一のツール群を提供する。

```sh
source ~/.secrets/slack_tokens.sh
source ~/.secrets/rivault_tokens.sh
PYTHONPATH=scripts ~/.venv_aarch64/bin/python3 scripts/pm_mcp_server.py
```

Claude Code の `.claude/settings.json` に MCP サーバーとして登録することで、分析結果を Box/Slack/Canvas に直接出力できる。

### 8. データの編集と修正 — 「LLMの誤りを人間が正せる」

**Web UI**（ブラウザで編集）:
```sh
bash scripts/pm_daemon.sh start web
# → http://localhost:8501
```

**CLI一括編集**（CSV経由）:
```sh
python3 scripts/pm_relink.py --export          # CSVにエクスポート
python3 scripts/pm_relink.py --import relink.csv  # DBに反映
```

---

## 情報の流れ（3パスアーキテクチャ）

```
入力元: Slack / 会議録音 / Zoom VTT / goals.yaml / Box / Web記事

Pass 1: 収集・抽出
  slack_pipeline.py       → data/slack.db（全チャンネル統合）
  pm_minutes_import.py    → data/minutes/{kind}.db
  pm_ingest.py            → data/pm.db（正本: actions/decisions/meetings/goals/milestones）
  pm_box_crawl.py         → data/box_docs.db（本文Markdown）
  pm_web_fetch.py         → data/web_articles.db

Pass 2: エンリッチメント・索引化
  enrich_items.py         → pm.db に判断者・根拠・関連IDを補完
  pm_embed.py             → qa_index.db（FTS5 + bge-m3 embedding）

Pass 3: 検索・分析・生成（Argus AI / pm-multi-agent）
  pm_qa_server.py（Slack Socket Mode）→ /argus-* コマンド
  pm_mcp_server.py（FastMCP）         → Claude Code からのツール呼び出し
  pm_argus_patrol.py                  → 自律巡回
```

詳細なアーキテクチャ図・データの流れは `docs/architecture.md` を参照。

---

## データベース構成

| DB | 役割 | 暗号化 |
|----|------|--------|
| `data/slack.db` | Slack 生メッセージ（全チャンネル統合） | ✅ |
| `data/minutes/{kind}.db` | 議事録詳細（議事内容・決定事項・AI） | ✅ |
| `data/pm.db` | **正本**: アクションアイテム・決定事項・会議・ゴール・マイルストーン | ✅ |
| `data/box_docs.db` | BOX ドキュメント本文 + relevance 判定 | ✅ |
| `data/web_articles.db` | 外部Web記事 | ✅ |
| `data/qa_index.db` | FTS5 統合インデックス + bge-m3 embedding | ✅ |
| `data/docs_*.db` | ドキュメントレジストリ（BOXリンクメタデータ） | ✅ |
| `data/patrol_state.db` | Patrol Agent 冪等性管理 | - |

### pm.db の主要テーブル

| テーブル | 内容 |
|---------|------|
| `action_items` | アクションアイテム（担当者・期限・status・milestone_id） |
| `decisions` | 決定事項（rationale 付き、確認済み管理） |
| `goals` / `milestones` | goals.yaml から同期 |
| `meetings` | 会議情報 |
| `audit_log` | 全変更履歴 |

---

## セキュリティ

- **LLM処理は全てローカル**: 議事録・Slackメッセージ等の機密情報は外部サービスに送出しない。ローカル vLLM（gemma4）または RiVault で処理する
- **DB暗号化**: 全DBに SQLCipher AES-256 暗号化を適用
- **トークン管理**: `~/.secrets/` 配下（`chmod 600`）。`.bashrc` への直書き禁止

### 初回セットアップ

```sh
# 鍵生成
python3 scripts/db_utils.py --gen-key

# 平文→暗号化変換
python3 scripts/db_utils.py --migrate data/pm.db

# FTS5 インデックス構築
python3 scripts/pm_embed.py --full-rebuild

# 環境変数
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."
export OPENAI_API_BASE="http://localhost:8000/v1"
export OPENAI_API_KEY="dummy"
EOF
chmod 600 ~/.secrets/slack_tokens.sh
```

### デーモン起動

```sh
bash scripts/pm_daemon.sh start qa    # Slack スラッシュコマンド有効化
bash scripts/pm_daemon.sh start web   # Web UI（port 8501）
```

---

## スクリプト構成

```
scripts/
├── ingest/              Pass 1: pm_ingest.py プラグイン
│   ├── pm_ingest.py    統合ランナー
│   ├── slack.py / minutes.py / goals.py
│   └── ingest_plugin.py
├── data-pipeline/       Pass 1: 一次情報収集
│   ├── slack_pipeline.py / pm_embed.py
│   ├── pm_box_crawl.py / pm_box_relevance.py
│   └── pm_web_fetch.py / pm_slack_box_links.py
├── minutes/             議事録パイプライン
│   ├── pm_minutes_import.py / pm_minutes_catalog.py
│   └── pm_minutes_publish.py
├── enrich/              Pass 2: エンリッチメント
│   ├── enrich_items.py / knowledge_context.py
│   └── pm_link_milestones.py
├── argus/               Pass 3: 検索・分析・生成
│   ├── pm_qa_server.py     Slack Socket Mode デーモン
│   ├── pm_argus_agent.py   Investigation Agent + CLI（--to-box 等対応）
│   ├── pm_argus.py         ブリーフィング/リスク/草案生成
│   ├── pm_argus_patrol.py  自律巡回 Patrol Agent
│   ├── agent_tools.py      ToolDef レジストリ（mcp_tools 委譲）
│   ├── mcp_tools.py        MCP 全ツール実装本体（pm-multi-agent と共有）
│   ├── output_tools.py     Box/Slack/Canvas 出力実装
│   ├── mcp_explorer.py     Explorer Agent
│   └── retrieval.py        FTS5 + embedding 検索
├── pm_mcp_server.py      FastMCP Server "pm-multi-agent"
├── reporting/           レポート・エクスポート
│   ├── pm_report.py / pm_insight.py
│   └── pm_xlsx_report.py / pm_xlsx_sync.py
├── recording/           会議録音処理
│   └── generate_minutes_local.py / whisper_vad.py / slide_ocr.py
├── web/                 Web UI（FastAPI + Vue 3）
│   ├── pm_api.py / web_admin.py / web_utils.py
│   └── static/          SPA フロントエンド
├── bin/                 シェルスクリプト
│   └── pm_daemon.sh / canvas_report.sh / pm_box_update.sh 等17本
└── utils/               共通ユーティリティ
    ├── db_utils.py / cli_utils.py / format_utils.py
    ├── box_cli.py / canvas_utils.py / slack_post.py
    └── embed_utils.py / transcript.py / voice_uploads.py
```

各スクリプトの詳細なオプションは `pm-commands` スキルまたは `docs/commands.md` を参照。
