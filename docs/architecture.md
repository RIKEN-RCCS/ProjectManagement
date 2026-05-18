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

## 2パスのデータ処理

### Pass 1: 抽出（Ingest）

生データ（Slack・議事録・goals.yaml）から構造化データ（`decisions` / `action_items` / `milestones`）を作る。

```
[Slack] ──→ slack_pipeline.py ──→ data/slack.db ─────┐
                                  (全チャンネル統合)  ├──→ pm_ingest.py ──→ pm.db
[議事録] ──→ pm_minutes_import.py ──→ minutes/*.db  ┤    (channel_id 列で絞り込み)
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

### Pass 3: ナレッジ蒸留（Distill）— 設計中

`box_docs.db` の本文・議事録・`pm.db.decisions` を入力として、LLM が **意思決定 / 制約 / 立場 / 用語**
の単位に蒸留し、`data/knowledge.db` に格納する（スキーマは `docs/schema.md` 参照）。
本文を毎回プロンプトに詰める方式は brief/risk の S/N を下げるため、**蒸留済みの短文要約のみを常時参照**
する形にする。

```
box_docs.db (本文 Markdown)
data/minutes/{kind}.db                   ┐
pm.db.decisions                          ├─→ pm_box_distill.py（仮、未実装）
                                          │     └─ LLM で蒸留 → 1レコード = 1意思決定
                                          ▼
                                    knowledge.db
                                    （プロジェクト全体共通、index 分割なし）
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
        ▼                                 ▼                                 ▼
  /argus-brief                       /argus-risk                       /argus-investigate
  プロンプトに current_state を     不整合・古いレコード・矛盾を       get_knowledge ツールで
  常時同梱                          シグナルとして抽出                rationale / alternatives を引く
```

**ナレッジ層の設計原則**（詳細は `docs/schema.md`）:
- **プロジェクト全体共通**: `argus_config.yaml` の `index_name` 等によるチャンネル別分割は持たない。
  ナレッジは富岳NEXTプロジェクト全体に渡って共有される
- **意思決定単位**: 1 BOX ファイル ≠ 1 レコード。`knowledge_sources` で N:M 結合し、根拠を追跡可能にする
- **時系列の劣化**: `last_validated_at` と `superseded_by` で版管理。古いレコードは brief で減衰させる
- **冪等な蒸留**: `distill_state` で入力ハッシュを記録し、変化のあった BOX ファイルのみ再蒸留

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
| `pm_minutes_catalog.py` | 議事録を Box にアップロード + Canvas 目録生成 |
| `pm_slack_box_links.py` | Slack投稿中のBOXリンク → `docs_*.db`（メタデータ）|
| `pm_box_crawl.py` | BOXフォルダを走査 → 本文を Markdown 化 → `box_docs.db`（pptx/docx/pdf/xlsx/boxnote）|
| `pm_box_relevance.py` | `box_docs.db` を LLM で relevance 判定（core/related/noise/unknown）|
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
│   ├── pm_minutes_catalog.py          議事録 Box アップロード・Canvas 目録
│   ├── pm_slack_box_links.py         Slack上のBOXリンク → docs_*.db (メタデータ)
│   ├── pm_box_crawl.py                BOX本文 → box_docs.db (Markdown化)
│   ├── pm_box_relevance.py            box_docs.db の relevance 判定
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
    ├── pm_box_update.sh
    └── pm_daemon.sh  (start/stop/status × qa: Argus, web: pm_api の統合管理)
```

---

## DB 構成

| DB | 役割 | 暗号化 |
|---|---|---|
| `data/slack.db` | Slack 生データ（**全チャンネル統合**。`channel_id` 列で絞り込む。2026-05-18 に `{channel_id}.db` 分割を廃止）| ✅ |
| `data/minutes/{kind}.db` | 議事録詳細（会議名単位）| ✅ |
| `data/pm.db` | PM 統合データ（**action_items / decisions / meetings / goals / milestones の唯一の正本**。2026-05-17 に pm-hpc.db / pm-pmo.db / pm-personal.db への分割を廃止し pm.db に一本化。`action_items.channel_id` / `decisions.channel_id` で出典チャンネルを保持）| ✅ |
| `data/docs_*.db` | BOXドキュメントメタデータ（Slackリンク経由）| ✅ |
| `data/box_docs.db` | BOXドキュメント本文（Markdown化）+ relevance（core/related/noise/unknown）| ✅ |
| `data/knowledge.db` | **蒸留ナレッジレイヤ**（プロジェクト全体共通。意思決定 / 制約 / 立場 / 用語の正規化済みレコード）| ✅ |
| `data/web_articles.db` | 外部Web記事 | ❌（公開情報）|
| `data/qa_index.db` | FTS5 統合検索インデックス（`chunks` + `chunk_indexes(chunk_id, index_name)` で論理 index を分離。2026-05-18 に `qa_pm*.db` 分割を廃止）| ❌（導出データ）|
| `data/patrol_state.db` | Patrol 冪等性・承認待ち | ❌（機密なし）|

**詳細**: `docs/schema.md`

---

## データの流れ全体図

```
       ┌─── Slack ───→ data/slack.db ─────────┐
       │              (全チャンネル統合)        │
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
                          qa_index.db         enrich_items.py
                       (FTS5 統合 + index_name)  ＋ナレッジ文脈
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
