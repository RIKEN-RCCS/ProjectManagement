# アーキテクチャ

本システムはプロジェクトマネジメント支援を目的とし、**4つの層**と **3パスのデータ処理** + **3系統の出力先** で構成される。

---

## データの流れ全体図

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 入力元                                                                   │
│  Slack / 会議録音 / Zoom VTT / goals.yaml / Box / Web記事              │
└──────────────────────┬──────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Pass 1: 収集・抽出                                                        │
│  slack_pipeline.py  → data/slack.db                                     │
│  pm_minutes_import.py → data/minutes/{kind}.db                          │
│  pm_from_recording.sh (Whisper→generate_minutes_local→import)           │
│  pm_box_crawl.py  → data/box_docs.db                                    │
│  pm_slack_box_links.py → data/docs_*.db                                 │
│  pm_web_fetch.py  → data/web_articles.db                                │
│  pm_ingest.py (slack/minutes/goals の pm.db への転記)                    │
│                                                                         │
│  ★ ここで pm.db が「正本」になる                                        │
│     action_items / decisions / meetings / goals / milestones             │
└──────────────────────┬──────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Pass 2: エンリッチメント・索引化                                            │
│  enrich_items.py: pm.db に判断者・根拠・関連IDを補完                      │
│  pm_embed.py:                                                           │
│     FTS5 (trigram + 形態素) → qa_index.db の chunks + fts              │
│     embedding (bge-m3)  → qa_index.db の chunk_embeddings  ← 新規      │
│  pm_box_distill.py: knowledge.db へのナレッジ蒸留 (Stage 1+2)            │
└──────────────────────┬──────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Pass 3: 検索・分析・生成（Argus AI — マルチエージェント）                   │
│                                                                         │
│  qa_server.py (Socket Mode) → ルーティング                                │
│                                                                         │
│  /argus-brief                                                           │
│    ├─ [並列Worker] PMデータ / Slack会話 / 議事録                          │
│    └─ [Orchestrator] 3視点を統合 → 5件のアクション                        │
│                                                                         │
│  /argus-risk                                                            │
│    ├─ [並列Worker] PMデータ / Slack会話 / 議事録 / ナレッジ               │
│    └─ [Orchestrator] 4視点を統合 → リスク一覧                            │
│                                                                         │
│  /argus-investigate                                                     │
│    ├─ マルチステップ agent ループ                                        │
│    ├─ 各ステップ内の tool_call を並列実行 (ThreadPool)                     │
│    ├─ ツール: search_text / search_knowledge / search_decisions /       │
│    │          get_slack_messages / get_milestone_progress / ...          │
│    └─ ハイブリッド検索 (FTS5 RRF + embedding cosine similarity)          │
│                                                                         │
│  /argus-today / /argus-draft / /argus-transcribe / /argus-knowledge     │
│                                                                         │
│  pm_argus_patrol.py: 自律巡回 (30分間隔、矛盾検知・期限超過検出)          │
└──────────────────────┬──────────────────────────────────────────────────┘
                       │
          ┌────────────┼────────────┬────────────┐
          ▼            ▼            ▼            ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │ Slack    │ │ Canvas   │ │ Box      │ │ Argus    │
   │ (ephem)  │ │ (永続)   │ │ XLSX/MD  │ │ Console  │
   │          │ │          │ │ (版管理) │ │ (Web UI) │
   └──────────┘ └──────────┘ └──────────┘ └──────────┘

```

---

## 4層構造

```
┌──────────────────────────────────────────────────────────────────┐
│ トップダウン層: ゴール・マイルストーン（goals.yaml、人間が承認）     │
│   → 評価の軸を与える                                                  │
├──────────────────────────────────────────────────────────────────┤
│ ボトムアップ層: アクションアイテム・決定事項                         │
│   Slack・議事録から LLM が自動抽出 → pm.db                           │
├──────────────────────────────────────────────────────────────────┤
│ エンリッチメント層: 過去ナレッジによる文脈補完                       │
│   FTS5 検索 + embedding 類似度 + 構造化データで                      │
│   判断者・根拠・関連IDを付与                                          │
├──────────────────────────────────────────────────────────────────┤
│ ナレッジ層: BOX 本文・議事録・決定事項を意思決定単位に蒸留           │
│   → knowledge.db (プロジェクト全体共通)                              │
│     brief/risk のプロンプトに常時同梱できる短文サマリと、            │
│     investigate でフル展開できる根拠・代替案・制約を保持             │
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

## Pass 1: 収集・抽出（Ingest）

一次情報源から構造化データを作る。

```
[Slack]
  └── slack_pipeline.py ──→ data/slack.db (全チャンネル統合)
                                  │
[会議録音]                             │
  └── pm_from_recording.sh             │
       ├── whisper_vad.py  (文字起こし)  │
       ├── generate_minutes_local.py    │
       │    (LLM議事録生成)              │
       └── pm_minutes_import.py ──→ data/minutes/{kind}.db
                                        │
[Zoom VTT]                               │
  └── (文字起こし補助) ──────────────→ data/minutes/{kind}.db
                                        │
[goals.yaml]                             │
  └─────────────────────────────────→   │
                                        ▼
                                  pm_ingest.py
                                  ├── slack.py  (slack.db → pm.db)
                                  ├── minutes.py (minutes/*.db → pm.db)
                                  └── goals.py  (goals.yaml → pm.db)
                                        │
                                        ▼
                                  ┌─────────┐
                                  │ pm.db   │ ← 正本
                                  │ actions  │
                                  │ decisions│
                                  │ meetings │
                                  │ goals    │
                                  │ milestones│
                                  └─────────┘
```

### 補助的な収集

```
[BOX ドキュメント]
  └── pm_box_crawl.py ──→ data/box_docs.db (本文 Markdown)

[BOX リンク (Slack上)]
  └── pm_slack_box_links.py ──→ data/docs_*.db (メタデータ)

[Web 記事]
  └── pm_web_fetch.py ──→ data/web_articles.db

[Excel 編集の反映]
  └── pm_xlsx_sync.py ──→ pm.db (Box XLSX → pm.db の逆方向同期)
```

### 出力

```
pm.db → Slack Canvas (pm_sync_canvas.py: Canvas → pm.db の逆同期)
pm.db → Box XLSX (pm_xlsx_report.py: pm.db → Box へのエクスポート)
```

---

## Pass 2: エンリッチメント・索引化

Pass 1 で作られたデータを補完・索引化する。

### エンリッチメント (enrich_items.py)

pm.db の新規レコードに対し、過去ナレッジから判断者・根拠・関連IDを補完する。

```
pm.db (新規レコード)
  │
  ├─ knowledge_context.py でナレッジ取得
  │    ├─ pm.db 構造化データ（直近の decisions/action_items）
  │    ├─ FTS5 全文検索（議事録・Slack・ドキュメント）
  │    └─ 参加者パターン（誰がよく発言しているか）
  │
  └─ enrich_items.py (LLM 補完)
       ├─ decided_by / requested_by（判断者）
       ├─ rationale（根拠）
       ├─ source_context（背景）
       └─ related_ids（関連レコード）
  │
  ▼
pm.db (UPDATE)
```

### FTS5 + embedding 索引 (pm_embed.py)

全ての検索可能コンテンツを qa_index.db に統合する。

```
入力元:
  data/minutes/{kind}.db (議事録)
  data/slack.db          (Slack メッセージ)
  data/box_docs.db       (BOX ドキュメント)
  data/web_articles.db   (Web 記事)
  │
  ├── split_into_chunks (1000 chars, 100 overlap)
  ├── SudachiPy 形態素解析 → fts_tokens
  │
  ▼
qa_index.db
├── chunks          (本文)
├── chunk_indexes   (index_name による論理分割)
├── fts             (trigram FTS5)
├── fts_tokens      (形態素 FTS5)
├── chunk_embeddings (bge-m3 ベクトル)  ← NEW
└── index_state     (差分更新管理)
```

### ナレッジ蒸留 (pm_box_distill.py)

```
入力: box_docs.db / minutes/*.db / pm.db.decisions
  │
  ├─ Stage 1: gemma4 で意思決定単位に抽出
  │  採否ポリシーでノイズ除外
  │
  └─ Stage 2: bge-m3 embedding → 既存ナレッジと類似度比較
       ≥ 0.92 → 自動 merge
       ≥ 0.85 → LLM 審査
       < 0.85 → LLM keep/drop 判定
  │
  ▼
knowledge.db
├── knowledge
├── knowledge_sources
├── knowledge_relations
├── knowledge_audit
├── distill_state
└── knowledge_embeddings
```

---

## Pass 3: 検索・分析・生成（Argus AI）

Slack Bot (Socket Mode) として動作し、全 `/argus-*` コマンドを処理する。

### アーキテクチャ

```
pm_qa_server.py (Socket Mode デーモン)
  │
  ├── ThreadPoolExecutor(max_workers=4)
  │
  ├── /argus-ask        ──→ 意図分類 (structured/text/hybrid) → 検索 → 回答
  │
  ├── /argus-brief      ──→ Orchestrator-Worker パターン
  │   ├─ Worker A: PMデータ → アクション候補
  │   ├─ Worker B: Slack会話 → アクション候補
  │   ├─ Worker C: 議事録   → アクション候補
  │   └─ Orchestrator: 統合 → 5件に絞り込み
  │
  ├── /argus-risk       ──→ Orchestrator-Worker パターン
  │   ├─ Worker A: PMデータ
  │   ├─ Worker B: Slack会話
  │   ├─ Worker C: 議事録
  │   ├─ Worker D: ナレッジ一貫性
  │   └─ Orchestrator: 統合 → リスク報告
  │
  ├── /argus-investigate ──→ マルチステップ Agent
  │   ├─ ツール並列実行 (ThreadPoolExecutor)
  │   ├─ ハイブリッド検索 (FTS5 + embedding RRF)
  │   ├─ max 5 steps / 480s timeout
  │   └─ コンテキスト圧縮機構
  │
  ├── /argus-today      ──→ 日次サマリー（メンション抽出付き）
  ├── /argus-draft      ──→ アジェンダ/レポート草案
  ├── /argus-transcribe ──→ 録音 → 議事録パイプライン
  ├── /argus-knowledge  ──→ ナレッジ編集
  └── /argus-narrate    ──→ TTS ナレーション (PPTX/PDF → mp4)
```

### 検索パイプライン (investigate / ask)

```
質問
  │
  ├── extract_search_keywords() (LLM: メタ語除去)
  ├── expand_query_hyde()       (LLM: 日英別表現生成)
  │
  ├── [並列] FTS5 検索 (形態素 → trigram → LIKE)
  ├── [並列] embedding ベクトル検索 (bge-m3)
  │
  ├── RRF 融合 (Reciprocal Rank Fusion)
  ├── 鮮度スコアリング (指数減衰, half-life 180日)
  ├── 重複排除 + マージ
  │
  └── LLM re-rank → top-5 → generate_answer
```

---

## 3系統の出力先

### 1. Slack (ephemeral / チャンネル投稿)

| コマンド | 出力先 | 形式 |
|---|---|---|
| `/argus-brief` | 実行者のみ (ephemeral) | Block Kit + mrkdwn |
| `/argus-risk` | 実行者のみ (ephemeral) | Block Kit + mrkdwn |
| `/argus-investigate` | 実行者のみ (ephemeral) | Block Kit + mrkdwn |
| `/argus-ask` | 実行者のみ (ephemeral) | Block Kit + mrkdwn |
| Patrol Agent | リーダー会議チャンネル | Block Kit + 承認ボタン |

### 2. Slack Canvas (永続)

| 内容 | 更新スクリプト | CRON |
|---|---|---|
| ブリーフィング | `pm_argus.py --brief-to-canvas` | 毎朝 6:57 JST |
| アクションアイテム一覧 | `pm_report.py` | 手動/Web UI |
| 議事録目録 | `pm_minutes_catalog.py --catalog` | 手動/Web UI |

### 3. Box (版管理)

| 内容 | 更新スクリプト | トリガー |
|---|---|---|
| pm_report.xlsx | `pm_xlsx_report.py` | Web UI から手動、または CRON |
| pm_report.xlsx | `pm_minutes_publish.py --xlsx-only` | Actions/Decisions 保存時に自動 |
| 議事録 Markdown | `pm_minutes_catalog.py --upload` | 手動/Web UI |
| 議事録 Markdown | `pm_minutes_publish.py` (Stage 2) | Minutes 編集保存時に自動 |

### 4. Argus Console (Web UI, FastAPI + Vue 3)

| 画面 | 機能 | データソース |
|---|---|---|
| Dashboard | 統計サマリー、サービス状態、最近の議事録 | pm.db / admin_jobs / minutes/*.db |
| Actions (ag-Grid) | アクションアイテム一覧・編集・保存 | pm.db → (保存時) Box XLSX 自動更新 |
| Decisions (ag-Grid) | 決定事項一覧・編集・保存 | pm.db → (保存時) Box XLSX 自動更新 |
| Recording | 録音ファイルアップロード → 議事録パイプライン | processing/ → minutes/*.db |
| Ingest | Slack/minutes/goals 取り込み実行 | AdminJobQueue → pm_ingest.py |
| Knowledge | Distill / Embed 実行 | AdminJobQueue → pm_box_distill.py |
| Reports | 週次レポート / 洞察 / XLSX 生成 | AdminJobQueue → pm_report.py |
| Minutes | 議事録一覧・編集・削除 | minutes/*.db → (保存時) pm.db + Box 自動更新 |
| Services | デーモン状態確認・起動停止・ログ閲覧 | pm_daemon.sh |

---

## DB 構成

| DB | 役割 | 書き込み元 | 読み取り元 | 暗号化 |
|---|---|---|---|---|
| `data/slack.db` | Slack 生データ（全チャンネル統合）| `slack_pipeline.py` | `pm_ingest.py`, Argus | ✅ |
| `data/minutes/{kind}.db` | 議事録詳細 | `pm_minutes_import.py` | `pm_ingest.py`, Argus, Web UI | ✅ |
| `data/pm.db` | **正本**: action_items/decisions/meetings/goals/milestones | `pm_ingest.py`, `enrich_items.py`, Web UI, `pm_sync_canvas.py`, `pm_xlsx_sync.py` | 全スクリプト | ✅ |
| `data/docs_*.db` | BOX ドキュメントメタデータ | `pm_slack_box_links.py` | `pm_box_crawl.py` | ✅ |
| `data/box_docs.db` | BOX ドキュメント本文 (Markdown) | `pm_box_crawl.py` | `pm_box_distill.py`, `pm_embed.py` | ✅ |
| `data/knowledge.db` | 蒸留ナレッジ | `pm_box_distill.py` | Argus (brief/risk/investigate) | ✅ |
| `data/web_articles.db` | 外部 Web 記事 | `pm_web_fetch.py` | `pm_embed.py` | ✅ |
| `data/qa_index.db` | FTS5 全文検索 + embedding | `pm_embed.py` | Argus (investigate/ask) | ✅ |
| `data/patrol_state.db` | Patrol 冪等性・承認待ち | `pm_argus_patrol.py` | `pm_argus_patrol.py` | ✅ |
| `data/voice_uploads.db` | TTS 音声ファイル投稿履歴 | `pm_tts.py` | `pm_tts.py` | ✅ |
| `data/admin_jobs.db` | 非同期ジョブ管理 | `web_admin.py` | `pm_api.py` | - |

---

## CRON 定期実行

| 時刻 | スクリプト | 内容 |
|---|---|---|
| 02:00 毎日 | `pm_box_update.sh` | BOX収集 + FTS5 索引更新 |
| 03:00 毎日 | `pm_web_update.sh` | Web 記事収集 |
| 04:00 毎日 | `pm_box_distill.py --source all --since 昨日` | ナレッジ蒸留 |
| 06:57 月〜金 | `pm_argus_daily.sh` | ブリーフィング生成 → Canvas |
| 07:00 月〜金 | `canvas_report.sh` | 各種レポート Canvas 投稿 |
| 16:50 月〜金 | `pm_from_slack_daily.sh` | Slack 走査 → argus-today |

---

## データ更新のカスケード（変更波及パス）

各編集操作がどの出力先に波及するか:

```
Minutes Management で編集
  → minutes/{kind}.db
  → [非同期ジョブ] pm_minutes_publish.py
       ├── pm.db 同期 (transfer_meeting)
       ├── Box: 議事録 Markdown バージョン更新
       └── Box: pm_report.xlsx バージョン更新

Actions/Decisions で編集
  → pm.db (直接)
  → [非同期ジョブ] pm_minutes_publish.py --xlsx-only
       └── Box: pm_report.xlsx バージョン更新

Canvas で編集 (アクションアイテム)
  → pm_sync_canvas.py (手動実行)
       └── pm.db 同期

Box XLSX で編集
  → pm_xlsx_sync.py (手動実行)
       └── pm.db 同期
```

---

## データ品質管理

| スクリプト | 役割 |
|---|---|
| `pm_screen.py` | pm.db の重複・類似・曖昧アイテムを検出（CSV出力）|
| `pm_relink.py` | CSV 経由で一括編集（LLM 不使用）|
| `pm_sync_canvas.py` | Canvas 編集を pm.db に反映 |
| `pm_knowledge_edit.py` | knowledge.db の人手編集 |

---

## スクリプト分類一覧

```
scripts/
├── 一次情報収集 (Pass 1)
│   ├── slack_pipeline.py              Slack差分取得
│   ├── ingest/                        pm_ingest.py プラグイン
│   │   ├── pm_ingest.py               統合ランナー
│   │   ├── slack.py                   Slack → pm.db
│   │   ├── minutes.py                 議事録DB → pm.db
│   │   ├── goals.py                   goals.yaml → pm.db
│   │   └── ingest_plugin.py           プラグインインタフェース
│   ├── pm_minutes_import.py           議事録MD → 議事録DB
│   ├── pm_minutes_catalog.py          議事録 Box アップロード + Canvas 目録
│   ├── pm_minutes_publish.py          minutes編集 → pm.db + Box XLSX/MD  ← NEW
│   ├── pm_slack_box_links.py          Slack上のBOXリンク収集
│   ├── pm_box_crawl.py                BOX本文取得 → box_docs.db
│   ├── pm_box_relevance.py            box_docs.db relevance 判定
│   └── pm_web_fetch.py                外部Web → web_articles.db
│
├── エンリッチ・索引 (Pass 2)
│   ├── enrich/
│   │   ├── enrich_items.py            pm.db 補完
│   │   └── knowledge_context.py       ナレッジ文脈
│   ├── pm_embed.py                    FTS5 + embedding 索引構築  ← 更新
│   ├── pm_box_distill.py              ナレッジ蒸留 → knowledge.db
│   └── pm_knowledge_edit.py           knowledge.db 編集CLI
│
├── データ品質
│   ├── pm_screen.py                   重複検出
│   ├── pm_relink.py                   CSV一括編集
│   └── pm_sync_canvas.py              Canvas → pm.db 同期
│
├── レポート・エクスポート
│   ├── pm_report.py                   進捗レポート → Canvas
│   ├── pm_xlsx_report.py              pm.db → Box XLSX
│   ├── pm_xlsx_sync.py                Box XLSX → pm.db
│   ├── pm_insight.py                  LLM 洞察
│   └── canvas_report.sh               CRON レポート
│
├── argus/ (Pass 3: 検索・分析・生成)
│   ├── pm_qa_server.py                Socket Mode デーモン
│   ├── pm_argus.py                    ブリーフィング/リスク/草案
│   ├── pm_argus_agent.py              Investigation Agent
│   ├── pm_argus_patrol.py             Patrol Agent
│   └── patrol/                        サブパッケージ
│
├── recording/                         会議録音処理
│   ├── generate_minutes_local.py      LLM 議事録生成
│   ├── transcribe_pipeline.py         /argus-transcribe
│   ├── whisper_vad.py                 Whisper ASR
│   └── slide_ocr.py                   スライドOCR
│
├── Web UI
│   ├── pm_api.py                      FastAPI サーバー
│   ├── web_admin.py                   AdminJobQueue + Minutes CRUD  ← NEW
│   ├── web_utils.py                   pm.db 保存ロジック
│   └── static/                        Vue 3 SPA + ag-Grid
│
├── 共通
│   ├── db_utils.py                    DB 接続
│   ├── cli_utils.py                   LLM 呼び出し
│   ├── format_utils.py                Markdown 整形
│   ├── canvas_utils.py                Canvas 操作
│   ├── embed_utils.py                 bge-m3 embedding  ← NEW
│   └── web_utils.py                   Web UI 共通
│
└── シェルスクリプト
    ├── pm_daemon.sh                   統合管理 (qa/web)
    ├── pm_box_update.sh               夜間BOX更新
    ├── pm_web_update.sh               夜間Web記事更新
    ├── pm_argus_daily.sh              CRON ブリーフィング
    ├── pm_from_recording.sh           録音 → 議事録
    └── pm_from_slack_daily.sh         CRON Slack走査
```
