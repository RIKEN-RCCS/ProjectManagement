# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 機密ファイルの取り扱い（最重要）

以下のファイルは Claude（Claude Code / Claude API）から**絶対に読み込まない**。
スクリプトはファイルシステムから直接読むため、Claude が読まなくても運用に支障はない。

- **`docs/project.md`** — ステークホルダー氏名・メールアドレス・Slack user_id・組織体制（理研 / 富士通 / NVIDIA の役職と意思決定権限）等
  - ローカル LLM（vLLM gemma4 / Whisper 議事録生成）のみが `cli_utils.py: load_claude_md_context()` 経由で直接読む
- **`data/argus_config.yaml`** — Slack channel_id / Canvas ID / Box folder_id 等
  - スクリプトは `yaml.safe_load()` で直接読む（cli_utils / pm_minutes_catalog / pm_api 等）
  - キー構造のみ必要な場合は `pm-argus-config-schema` Skill を参照する
- **`goals.yaml`** — プロジェクトの戦略目標・マイルストーン名・期限・成功基準
  - スクリプトは `yaml.safe_load()` で直接読む（`scripts/ingest/goals.py` ほか）
  - キー構造のみ必要な場合は `pm-goals-schema` Skill を参照する
- **`~/.secrets/` 配下** — Slack トークン等

共通ルール:
- `Read` ツールで本文を開かない（`.claude/settings.local.json` の `permissions.deny` で機械的にブロック済み）
- `grep -c` / `wc -l` 等でメタ情報（行数・存在）に触れる程度は可、本文の context 展開は不可
- 値が必要な場合は**ユーザーに該当行の貼り付けを依頼**する

---

## プロジェクト文脈

このリポジトリは**プロジェクトマネージメント支援システム**である。

### 設計思想：目指すプロマネの姿

このシステムが目指すのは「議事録係＋ToDoリスト管理」ではなく、**プロジェクトのゴールへの到達を管理するプロジェクトマネジメント**である。

LLMを使ったPMツールは、発言・議事録・Slackから決定事項やアクションアイテムを拾い上げることに終始しがちである。それは情報の整理には役立つが、「プロジェクトが今どこにいるのか」「ゴールに向けて前進しているのか」を答えることができない。本システムは以下の2層構造でこの問題に対処する。

```
【トップダウン層】 ゴール・マイルストーン
                  └─ goals.yaml に人間（意思決定者）が定義・承認、gitで変更履歴管理
                          ↓ 評価の軸を与える
【ボトムアップ層】 アクションアイテム・決定事項
                  └─ 会議議事録・Slackから LLM が自動抽出・マイルストーンに紐づけ
```

**LLMと人間の役割分担**:
- 「何を目指すか」「マイルストーンの定義・承認」→ 人間（意思決定者）
- 「情報の収集・整理・抽出」「マイルストーンへの紐づけ推定」→ LLM
- 「誤りの修正・最終判断」→ 人間（Slack Canvas上で編集、または `pm_minutes_import.py --export` → 修正 → `--no-llm --force` で再インポート）
- 「達成状況の計算・レポート生成」→ システム

Slackの日常的なやり取りと会議議事録を統合し、決定事項・アクションアイテムの一元管理と定期レポート生成を目的とする。

<!-- プロジェクトの内容を docs/project.md に記載する、機密性の高い内容のため github へ登録しない -->

---

## システム概要

情報の流れは **2パス構造**（抽出 → エンリッチメント）で構成される。
全体像は `docs/architecture.md` を参照。

```
           [Pass 1: 抽出]                         [Pass 2: エンリッチメント]

[Slack] ─→ slack_pipeline.py ─→ data/slack.db ───┐
                                                   │
[議事録]                                           ├─→ pm_ingest.py ─→ pm.db ─┐
 meetings/*.md ─→ pm_minutes_import.py ─→ minutes/*.db                        │
                                                   │                          │
[goals.yaml] ────────────────────────────────────┘                          │
                                                                              │
  enrich_items.py (LLM + knowledge_context.py + FTS5) ←────────────────────┘
      ↓ 判断者・根拠・関連IDを付与
   pm.db (強化版)
      ↓
   pm_report.py / pm_argus.py / pm_api.py → Canvas / Slack / Web UI
```

- Pass 1 は単一スレッド・議事録から構造化データを作る
- Pass 2 は過去ナレッジ全体から関連情報を引いて紐付ける
- データ品質管理は `pm_screen.py`（重複検出）→ `pm_relink.py`（一括削除・編集）で行う

`pm_from_recording.sh --meeting-name` は `generate_minutes_local.py`（ローカルLLMで高品質議事録生成）→ `pm_minutes_import.py --no-llm`（DB保存）→ `pm_ingest.py minutes`（pm.db転記）の順で呼び出す。Zoom VTT ファイルが同名で存在する場合は自動検出し、話者情報を議事録生成に活用する（`--vtt` オプション、解像度サフィックス `_3840x2160` 等を剥がすフォールバックあり）。mp4 の場合は `slide_ocr.py` を冒頭で自動実行し、スライド文脈と固有名詞リストを Stage 1/2/3 プロンプト・Whisper initial_prompt の両方に同梱して ASR/LLM 両段階で品質を向上させる（`--no-slide-ocr` で無効化可能）。

**各DBの役割分担**:
- `data/slack.db` — Slackデータ専用（**全チャンネル横断**。`messages` / `replies` の `channel_id` 列で絞り込む）。2026-05-18 にチャンネル別 `{channel_id}.db` の分割を廃止し統合。旧DBは `data/*.db.bak` として保管。
- `pm.db` — PM情報専用（**action_items / decisions / meetings / goals / milestones の唯一の正本**）。2026-05-17 に pm-hpc.db / pm-pmo.db / pm-personal.db への分割を廃止し pm.db に一本化。`action_items.channel_id` / `decisions.channel_id` で出典チャンネルを保持。
- `data/minutes/{kind}.db` — 議事録詳細専用。会議名ごとに独立。決定・AIの背景を含む。
- `data/box_docs.db` — Box フォルダから取得したドキュメント本文（Markdown化）+ relevance（core/related/noise/unknown）。`pm_box_crawl.py` が登録、`pm_box_relevance.py` が判定。
- `data/knowledge.db` — **蒸留ナレッジレイヤ**（プロジェクト全体共通、`index_name` 等の分割なし）。BOX 本文・議事録・決定事項を「意思決定 / 制約 / 立場 / 用語」の粒度に LLM 蒸留し、brief/risk のプロンプトに常時同梱できる短文サマリと、investigate でフル展開できる根拠・代替案・制約を保持。詳細は `docs/schema.md` 参照。蒸留スクリプトは未実装。
- `data/qa_index.db` — FTS5 検索インデックスの統合DB。`chunks` + `chunk_indexes(chunk_id, index_name)` の junction で論理 index（`pm` / `pm-hpc` / `pm-pmo` / `pm-all`）を表現。2026-05-18 に `qa_pm*.db` への DB 分割を廃止し統合。

---

## ファイル構成

```
slack/
├── meetings/                        # 議事録の一次着地点（Markdown形式）
│   ├── YYYY-MM-DD_会議名.md
├── scripts/                         # スクリプト一式
│   ├── slack_pipeline.py            # Slack差分取得（SDK経由）。新規・更新スレッドのみ取得してDBに保存。--skip-fetch でAPI取得スキップ、--list でスレッド一覧表示
│   ├── pm_minutes_import.py         # 議事録 → data/minutes/{kind}.db（詳細議事録・担当者・期限を構造化保存）。--export でDB内容をMarkdownにエクスポート、--no-llm で人間修正済みMarkdownをLLM不使用で再インポート。--post-to-slack でSlack投稿。--delete で削除
│   ├── pm_minutes_catalog.py        # 議事録を Box にアップロードし、Canvas 目録を更新（argus_config.yaml の meetings: に box_folder_id・catalog_canvas_id を定義）
│   ├── ingest/                      # [Pass 1] データ取り込みプラグイン一式
│   │   ├── pm_ingest.py             #   統合ランナー（`pm_ingest.py --list` で一覧）
│   │   ├── slack.py                 #   Slack DB生メッセージ → 決定事項・アクションアイテム抽出
│   │   ├── minutes.py               #   data/minutes/{kind}.db → pm.db 転記（LLM不使用）
│   │   ├── goals.py                 #   goals.yaml → goals/milestones 完全同期
│   │   └── ingest_plugin.py         #   IngestContext / IngestPlugin の定義（docs/ingest_plugin.md 参照）
│   ├── pm_report.py                 # pm.db → 進捗レポート生成・Canvas投稿（SlackリンクはクリッカブルURL形式）。--show-workload で担当者別負荷セクションを追加出力
│   ├── pm_sync_canvas.py            # Canvas → pm.db 同期（担当者・期限・マイルストーン・状況・内容・対応状況・決定事項内容）。open/close判定は「状況」列のみ
│   ├── pm_relink.py                 # アクションアイテム・決定事項の各フィールドをCSV経由で一括編集（LLM不使用）。deleted(0/1)でアイテムの有効化/削除も可能。--include-deleted で削除済みアイテムを対象に含める
│   ├── pm_screen.py                 # pm.db の重複・類似・曖昧アイテムを検出（exact_dup/near_dup/ambiguous）。pm_relink.py 互換CSVで出力して一括削除可能
│   ├── enrich/                      # [Pass 2] エンリッチメント
│   │   ├── enrich_items.py          #   pm.db の decisions/action_items に判断者・根拠・related_ids を補完（LLM + FTS5）
│   │   └── knowledge_context.py     #   共通ライブラリ（SudachiPy・FTS5検索・参加者パターン取得）
│   ├── pm_insight.py                # pm.db → LLMによるプロジェクト健全性評価・リスク特定・改善提案を生成・Canvas投稿
│   ├── argus/                       # Argus AI パッケージ（問い合わせ・巡回）
│   │   ├── pm_argus.py              #   Slack・議事録・pm.db統合分析（ブリーフィング・リスク分析・草案生成）
│   │   ├── pm_argus_agent.py        #   Investigation Agent — LLM駆動マルチステップ調査（/argus-investigate）
│   │   ├── pm_argus_patrol.py       #   Patrol Agent — 自律型PM巡回（cron 30分間隔）
│   │   ├── pm_qa_server.py          #   Slack Socket Modeデーモン（全 /argus-* コマンド・ハイブリッド検索）
│   │   └── patrol/                  #   Patrol サブモジュール
│   │       ├── state.py             #     冪等性DB（patrol_state.db）・スロットリング・承認待ち管理
│   │       ├── detect.py            #     検出ルール（完了シグナル・期限超過・停滞・健全性）
│   │       ├── actions.py           #     Slack投稿・Block Kit・DB書き込み・audit_log
│   │       ├── confirm.py           #     Block Kit ボタンハンドラ（承認/却下）
│   │       └── users.py             #     担当者名 → Slack user_id 解決
│   ├── pm_api.py                    # FastAPI REST API + 静的フロントエンド配信。pm.db のアクションアイテム・決定事項・議事録・ファイル一覧を提供。web_utils.py を使用
│   ├── pm_daemon.sh                 # デーモン統合管理: `start/stop/status` × `qa`（Argus）/`web`（pm_api）
│   ├── canvas_utils.py              # Slack Canvas 操作の共通ユーティリティ（sanitize_for_canvas・post_to_canvas・セクション削除ロジック）
│   ├── db_utils.py                  # DB接続の一元管理・統計クエリ・平文DB暗号化変換（SQLCipher対応）。open_pm_db・fetch_milestone_progress・fetch_assignee_workload・fetch_overdue_items 等も提供
│   ├── cli_utils.py                 # 共通CLIユーティリティ（argparse ヘルパー・make_logger・load_claude_md・call_claude・call_local_llm・strip_think_blocks・VTTパース・話者マッピング）。OPENAI_API_BASE が設定されている場合はローカルLLMを使用
│   ├── format_utils.py              # Markdownテーブル整形の共通ユーティリティ（マイルストーン進捗・期限超過・担当者負荷・週次トレンド・決定事項）
│   ├── web_utils.py                 # pm_api.py 用のDB読み書き・楽観的排他制御（scan_pm_dbs・get_conn・load_action_items・do_save_action_items 等）
│   ├── pm_slack_box_links.py       # Slack上のBOXリンクを収集・LLMで構造化 → docs_{index_name}.db に保存（メタデータのみ）。Canvas投稿・FTS5連携対応
│   ├── pm_box_crawl.py             # BOXフォルダを走査して本文を Markdown 化（pptx/docx/pdf/xlsx/boxnote）→ box_docs.db に保存。box_sources.yaml で対象フォルダ定義
│   ├── pm_box_relevance.py         # box_docs.db の各ファイルをローカルLLMで relevance 判定（core/related/noise/unknown）。noise は pm_embed.py が索引除外
│   ├── pm_box_distill.py           # [Pass 3] box_docs.db / minutes / pm.db.decisions → knowledge.db への蒸留。Stage 1 (LLM 抽出) → Stage 2 (bge-m3 類似度 ≥0.92 で auto-merge / ≥0.85 で LLM 審査 / それ以外は LLM keep/drop 判定) の二段ゲート。--embed-backfill / --quality-only で既存レコードへの後追い処理も可能
│   ├── pm_knowledge_edit.py        # knowledge.db の人手編集CLI。--invalidate / --supersede / --confidence / CSV一括編集。全変更は knowledge_audit に記録
│   ├── pm_knowledge_inspect.py     # knowledge.db の重複・多重抽出を診断（読み取り専用）。1 source_ref からの派生数・topic/current_state 重複を集計
│   ├── pm_knowledge_dedupe.py      # current_state 完全一致による後追い dedupe。keeper 選定 → 他を superseded_by で連鎖（Stage 2 導入前の既存データ整理用）
│   ├── embed_utils.py              # OpenAI 互換 /v1/embeddings 呼び出し + コサイン類似度（RiVault の bge-m3:567m がデフォルト）
│   ├── pm_box_update.sh        # ステップ1: pm_slack_box_links.py → ステップ2: pm_box_crawl.py → ステップ3: pm_embed.py を連続実行
│   ├── pm_web_fetch.py              # 外部WebサイトのRSS/HTMLを取得 → web_articles.db に保存（web_sources.yaml で定義）。cron毎朝03:30で自動実行
│   ├── pm_embed.py                  # QAインデックス構築（argus_config.yaml に従いSudachiPy形態素解析+FTS5インデックスを各DBに書き込む。docs_*.db・web_articles.db も索引化）
│   ├── pm_argus_daily.sh            # cron用: argus/pm_argus.py --brief-to-canvas / --risk を平日朝7:47に実行
│   ├── recording/                   # 会議録音処理パッケージ
│   │   ├── generate_minutes_local.py #  ローカルLLMでの高品質議事録生成（マルチステージ・--vtt・--slide-context 対応）
│   │   ├── transcribe_pipeline.py   #   /argus-transcribe 用パイプライン（Slack DL → スライドOCR → Whisper → 議事録生成）
│   │   ├── whisper_vad.py           #   VAD+DeepFilterNet+Whisper による話者分離・文字起こし（--initial-prompt-extra 対応）
│   │   └── slide_ocr.py             #   ffmpeg scene detect + マルチモーダルLLM で動画からスライド文脈・固有名詞を抽出
│   ├── pm_from_recording_auto.sh    # data/*.m4a および data/*.mp4 を検出して pm_from_recording.sh を自動投入。同名VTTも自動移動。-c CHANNEL_ID でSlack投稿も自動化
│   ├── pm_from_recording.sh         # 会議録音をローカルで処理するスクリプト。mp4 はスライドOCR自動有効化、同名VTT自動検出（--vtt で明示指定も可）。recording/slide_ocr.py → recording/generate_minutes_local.py → pm_minutes_import.py --no-llm → ingest/pm_ingest.py minutes を自動実行
│   ├── pm_argus_daily_summary.sh    # cron用: 平日17:00に --today-only で当日分サマリーを Canvas に投稿
│   ├── pm_from_slack.sh             # Slack取得 → pm.db抽出を連続実行（slack_pipeline.py + ingest/pm_ingest.py slack）
│   ├── canvas_report.sh             # Canvas同期 → PMレポート生成・Canvas投稿（pm_sync_canvas.py + pm_report.py）
│   └── slack_post_minutes.sh        # 議事録DBの内容をSlackチャンネルに投稿（pm_minutes_import.py --post-to-slack）
└── data/                            # DBと出力ファイル
    ├── slack.db                     # Slackデータ（全チャンネル統合、channel_id列で絞り込み）
    ├── pm.db                        # PM統合データ
    ├── minutes/                     # 詳細議事録DB（会議名ごとに独立）
    │   └── {kind}.db                # 例: Leader_Meeting.db
    ├── docs_*.db                    # ドキュメントレジストリDB（Slack上のBOXリンクのメタデータ、暗号化）
    ├── box_docs.db                  # BOX本文DB（Markdown化したpptx/docx/pdf/xlsx/boxnote本文 + relevance、暗号化）
    ├── knowledge.db                 # 蒸留ナレッジDB（意思決定/制約/立場/用語、プロジェクト全体共通、暗号化）
    ├── box_sources.yaml             # BOXソース定義（folder_id・index_names・除外パターン等）
    ├── web_articles.db              # 外部Web記事DB（平文sqlite3、公開情報なので暗号化不要）
    ├── web_sources.yaml             # 外部Webソース定義（URL・キーワードフィルタ・対象インデックス）
    ├── argus_config.yaml            # Argus 統合設定（インデックス定義・チャンネルマッピング・pm.dbパス・会議ごとの Box/Canvas ID）
    ├── qa_index.db                  # FTS5 統合インデックスDB（chunks + chunk_indexes junction で論理 index_name を分離）
    ├── secretary_canvas_id.txt      # Argus の Canvas 投稿先ID
    ├── patrol_config.yaml           # Patrol Agent 設定（検出器の有効/無効・閾値・通知チャンネル）
    ├── patrol_state.db              # Patrol Agent 冪等性DB（通知履歴・承認待ち・ユーザーキャッシュ、平文sqlite3）
    └── slack_summarize_*.md         # 全体要約（デバッグ・履歴用）
```

---

## 環境変数

**トークンは `.bashrc` に絶対に直書きしないこと。** 全プロセスに漏洩する危険がある。

```sh
# 1. トークンファイルを作成（初回のみ）
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_USER_TOKEN="xoxp-..."   # 全スクリプト共通（xoxp- ユーザートークン）
EOF
chmod 600 ~/.secrets/slack_tokens.sh

# 2. 実行前に読み込む（毎回）
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py ...
```

ローカルLLM（OpenAI互換API）を使う場合:
```sh
export OPENAI_API_BASE="http://localhost:8000/v1"   # vLLM エンドポイント
export OPENAI_API_KEY="dummy"                        # 認証不要のローカルサーバは "dummy" で可
export OPENAI_MAX_TOKENS="8192"                      # Slack 抽出用（pm_ingest.py slack / ingest_slack.py）
```

`OPENAI_API_BASE` が設定されている場合、`call_claude()` は Claude CLI の代わりにローカルLLMを呼び出す（`cli_utils.py` の `call_local_llm()` 経由）。モデル名は vLLM の `/v1/models` API から自動取得するため、`OPENAI_MODEL` の設定は不要。

`OPENAI_MAX_TOKENS` は Slack 抽出（`pm_ingest.py slack` 経由の `ingest_slack.py`）の最大出力トークン数。会議録音処理では `generate_minutes_local.py` の `--max-tokens 16384` が使われるため本変数は影響しない。

---

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可（ネストセッション制限）。各スクリプトはClaude Codeの外のターミナルから実行すること。ローカルLLM（`OPENAI_API_BASE` 設定時）はこの制限を受けない。
- `call_claude()` 内で `CLAUDECODE` 環境変数を子プロセスから除外する処理を実装済み。
- `slack-mcp-server` は不要（`slack_pipeline.py` が Slack SDK に移行済み）。
- Python仮想環境はアーキテクチャに応じて `~/.venv_aarch64`（aarch64）または `~/.venv_x86_64`（x86_64）を使用。`uname -m` で確認し適切なパスを使うこと。

---

## 作業環境セットアップ

並列作業が必要な場合は以下のtmuxレイアウトを使用すること：

- Pane 0 (左): メイン作業
- Pane 1 (右上): ログ監視 `tail -f`
- Pane 2 (右下): ジョブ状態監視

---

@docs/architecture.md

---

## 関連スキル（必要時に Skill ツールで読み込む）

- `pm-commands` — `docs/commands.md`。スクリプト・CLI のオプション一覧を引くとき
- `pm-schema` — `docs/schema.md`。DB テーブル定義・列・差分判定ロジックを引くとき
- `argus-system` — `docs/argus_system.md`。Argus / Patrol / FTS5 索引まわりを触るとき
- `slack-canvas-api` — `docs/canvas_api.md`。Canvas を投稿・編集・再作成するとき
- `pm-roadmap` — `docs/roadmap.md`。実装済みフェーズ・未実装課題を確認するとき
- `pm-ingest-plugin` — `docs/ingest_plugin.md`。`scripts/ingest/` に新ソースを追加するとき
- `pm-distill-policy` — `docs/distill_policy.md`。ナレッジ蒸留 (knowledge.db) の採否基準を引くとき
- `docs/minutes_consensus.md` — Self-Consistency 議事録生成（`--consensus N`）のアルゴリズム・CLI・環境変数分離を触るとき
- `pm-argus-config-schema` — `data/argus_config.yaml` のキー構造（実値非掲載）。索引・会議目録・フィルタ設定を触るとき
- `pm-goals-schema` — `goals.yaml` のキー構造（実値非掲載）。`pm_ingest.py goals` / マイルストーン同期周りを触るとき
