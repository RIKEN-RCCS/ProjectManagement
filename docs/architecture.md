# アーキテクチャ

本システムはプロジェクトマネジメント支援を目的とし、**3つの層**と **2パスのデータ処理**で構成される。

---

## 3層構造

```
┌──────────────────────────────────────────────────────────────────┐
│ トップダウン層: ゴール・マイルストーン（goals.yaml、人間が承認）     │
│   → 評価の軸を与える                                                  │
├──────────────────────────────────────────────────────────────────┤
│ ボトムアップ層: アクションアイテム・決定事項                         │
│   Slack・議事録から LLM が自動抽出 → pm.db                           │
├──────────────────────────────────────────────────────────────────┤
│ エンリッチメント層: 過去ナレッジによる文脈補完                       │
│   FTS5 検索 + 構造化データで判断者・根拠・関連IDを付与               │
└──────────────────────────────────────────────────────────────────┘
```

**LLM と人間の役割分担**:

| 活動 | 担当 |
|---|---|
| ゴール・マイルストーンの定義 | 人間（意思決定者）|
| 情報の収集・整理・抽出 | LLM |
| エンリッチメント（判断者・根拠の補完）| LLM |
| 誤りの修正・最終判断 | 人間（Canvas / Web UI）|
| 達成状況の計算・レポート生成 | システム（LLM不使用）|

---

## 2パスのデータ処理

### Pass 1: 抽出（Ingest）

生データ（Slack・議事録・goals.yaml）から構造化データ（`decisions` / `action_items` / `milestones`）を作る。

```
[Slack] ──→ slack_pipeline.py ──→ {channel_id}.db ──┐
                                                     ├──→ pm_ingest.py ──→ pm.db
[議事録] ──→ pm_minutes_import.py ──→ minutes/*.db  ┤
                                                     │
[goals.yaml] ──────────────────────────────────────┘
```

**ファイル**: `scripts/ingest/` パッケージ配下の `slack.py` / `minutes.py` / `goals.py` + 統合ランナー `pm_ingest.py`
**詳細**: `docs/ingest_plugin.md`

### Pass 2: エンリッチメント（Enrich）

Pass 1 で作られた `decisions` / `action_items` に対し、過去ナレッジを参照して **判断者・根拠・関連ID** を補完する。

```
pm.db の新規 decisions/action_items
  │
  ├─ knowledge_context.py でナレッジ取得
  │    ├─ pm.db 構造化データ（直近の decisions/action_items）
  │    ├─ FTS5 全文検索（議事録・Slack・ドキュメント・Web記事）
  │    └─ 参加者パターン（誰がよく発言しているか）
  │
  ├─ enrich_items.py で LLM に補完させる
  │    ├─ decided_by / requested_by（判断者・依頼者、名前正規化あり）
  │    ├─ rationale（根拠）
  │    ├─ source_context（背景となった議論の要約）
  │    └─ related_ids（過去の関連 decisions/action_items ID）
  │
  └─ pm.db に UPDATE（confidence スコア付き）
```

**ファイル**: `scripts/enrich/enrich_items.py`（CLI）+ `scripts/enrich/knowledge_context.py`（共通ライブラリ）

**なぜ2パスなのか**:
- Pass 1 は「単一のスレッド・議事録」だけを見て抽出する（LLM の context に収まる範囲）
- Pass 2 は「過去のナレッジ全体」から関連情報を引いて紐付ける（FTS5 で事前絞り込みが必要）
- 分離することで Pass 1 の失敗と Pass 2 の失敗を独立に扱える

---

## 周辺機能

### データ品質管理

| スクリプト | 役割 |
|---|---|
| `pm_screen.py` | pm.db の重複・類似・曖昧アイテムを検出（`pm_relink.py` 互換CSVで出力） |
| `pm_relink.py` | CSV経由で一括編集（LLM不使用、削除・マイルストーン紐付けを含む）|
| `pm_sync_canvas.py` | Canvas 編集を pm.db に反映（会議中の修正フロー）|

### レポート・可視化

| スクリプト | 役割 |
|---|---|
| `pm_report.py` | 定型進捗レポート（マイルストーン進捗・期限超過・担当者負荷）|
| `pm_insight.py` | LLM 洞察（健全性評価・リスク特定）|
| `pm_api.py` | Web UI（`pm.db` 閲覧・編集、FastAPI）|

### Argus AI（問い合わせ・巡回）

| スクリプト | 役割 |
|---|---|
| `pm_qa_server.py` | Slack Socket Mode デーモン（全 `/argus-*` コマンド）|
| `pm_argus.py` | ブリーフィング・リスク分析・草案生成 |
| `pm_argus_agent.py` | マルチステップ調査エージェント（`/argus-investigate`）|
| `pm_argus_patrol.py` | 自律型PM巡回（cron 30分間隔）|
| `pm_embed.py` | FTS5 インデックス構築（Pass 2 と共通の検索基盤）|

**詳細**: `docs/argus_system.md`

### 入力データ取り込み

| スクリプト | 役割 |
|---|---|
| `pm_minutes_import.py` | 議事録 Markdown → `data/minutes/{kind}.db` |
| `pm_minutes_catalog.py` | 議事録を Slack 投稿 + Canvas 目録生成 |
| `pm_document_extract.py` | Slack投稿中のBOXリンク → `docs_*.db`（メタデータ）|
| `pm_web_fetch.py` | 外部Webサイト → `web_articles.db` |
| `generate_minutes_local.py` | 文字起こし → 議事録（ローカルLLM。`--vtt`・`--slide-context` 対応）|
| `transcribe_pipeline.py` / `whisper_vad.py` | 音声/動画ファイル → 文字起こし（`--initial-prompt-extra` で固有名詞注入）|
| `slide_ocr.py` | mp4 → スライドフレーム抽出 → マルチモーダルOCR（slide_context / terminology 生成）|

---

## スクリプト分類一覧（見通し用）

```
scripts/
├── 取り込み (Pass 1)
│   ├── slack_pipeline.py              Slack差分取得
│   ├── ingest/                        [Pass 1] 取り込みパッケージ
│   │   ├── pm_ingest.py               統合ランナー
│   │   ├── slack.py                   Slack → pm.db プラグイン
│   │   ├── minutes.py                 議事録DB → pm.db プラグイン
│   │   ├── goals.py                   goals.yaml → pm.db プラグイン
│   │   └── ingest_plugin.py           プラグインインタフェース
│   ├── pm_minutes_import.py           議事録MD → 議事録DB
│   ├── pm_minutes_catalog.py          議事録 Slack 投稿
│   ├── pm_document_extract.py         BOXリンク → docs_*.db
│   └── pm_web_fetch.py                外部Web → web_articles.db
│
├── enrich/ (Pass 2)                   エンリッチメントパッケージ
│   ├── enrich_items.py                CLI
│   └── knowledge_context.py           共通ライブラリ
│
├── データ品質
│   ├── pm_screen.py                   重複・曖昧検出
│   ├── pm_relink.py                   CSV一括編集
│   └── pm_sync_canvas.py              Canvas → pm.db 同期
│
├── レポート・可視化
│   ├── pm_report.py                   進捗レポート
│   ├── pm_insight.py                  LLM洞察
│   └── pm_api.py                      Web UI (FastAPI)
│
├── argus/                             Argus AI パッケージ
│   ├── pm_qa_server.py                Socket Mode デーモン
│   ├── pm_argus.py                    ブリーフィング
│   ├── pm_argus_agent.py              Investigation Agent
│   ├── pm_argus_patrol.py             Patrol Agent
│   └── patrol/                        Patrol サブパッケージ（state/detect/actions/confirm/users）
│   pm_embed.py                        FTS5構築（argus/ 外、他スクリプトからも使用）
│
├── recording/                         会議録音処理パッケージ
│   ├── generate_minutes_local.py      議事録生成（VTT・スライドOCR文脈対応）
│   ├── transcribe_pipeline.py         /argus-transcribe パイプライン（スライドOCR自動実行）
│   ├── whisper_vad.py                 Whisper ASR（initial_prompt に固有名詞追加可）
│   └── slide_ocr.py                   mp4 → ffmpeg scene detect + マルチモーダルOCR
│
├── 共通ユーティリティ
│   ├── db_utils.py                    DB接続・統計クエリ
│   ├── cli_utils.py                   LLM呼び出し・argparseヘルパ
│   ├── format_utils.py                Markdownテーブル整形
│   ├── canvas_utils.py                Canvas操作
│   └── web_utils.py                   Web UI 共通
│
└── シェルスクリプト（エントリポイント）
    ├── pm_from_slack.sh
    ├── pm_from_recording.sh
    ├── pm_from_recording_auto.sh
    ├── canvas_report.sh
    ├── slack_post_minutes.sh
    ├── pm_document_update.sh
    └── pm_daemon.sh  (start/stop/status × qa: Argus, web: pm_api の統合管理)
```

---

## DB 構成

| DB | 役割 | 暗号化 |
|---|---|---|
| `{channel_id}.db` | Slack 生データ（チャンネル単位）| ✅ |
| `data/minutes/{kind}.db` | 議事録詳細（会議名単位）| ✅ |
| `data/pm.db` | PM 統合データ（横断）| ✅ |
| `data/docs_*.db` | BOXドキュメントメタデータ | ✅ |
| `data/web_articles.db` | 外部Web記事 | ❌（公開情報）|
| `data/qa_pm*.db` | FTS5検索インデックス | ❌（導出データ）|
| `data/patrol_state.db` | Patrol 冪等性・承認待ち | ❌（機密なし）|

**詳細**: `docs/schema.md`

---

## データの流れ全体図

```
       ┌─── Slack ───→ {channel_id}.db ──────┐
       │                                       │
[一次情報]──── 音声 ──→ 文字起こし ──→ 議事録MD ─┤
       │                                       │
       └─── goals.yaml ────────────────────┐  │
                                             ▼  ▼
                                        ┌─────────┐   Pass 1: 抽出
                                        │ pm.db   │
                                        │  (基礎) │
                                        └────┬────┘
                                             │
                                             ▼              Pass 2: エンリッチ
                                 ┌─── pm_embed.py ───┐
                                 │                    │
                                 ▼                    ▼
                           qa_pm*.db          enrich_items.py
                           (FTS5)              ＋ナレッジ文脈
                                 │                    │
                                 └────────┬───────────┘
                                          ▼
                                     ┌────────┐
                                     │ pm.db  │
                                     │(強化版)│
                                     └───┬────┘
                                         │
                  ┌──────────────────────┼──────────────────────┐
                  ▼                      ▼                      ▼
              pm_report               pm_argus              pm_api
              pm_insight           pm_argus_agent         (Web UI)
              (Canvas)             pm_argus_patrol
                                    (Slack)
```
