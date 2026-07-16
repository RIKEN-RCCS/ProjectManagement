## DBスキーマ

### data/slack.db（Slackデータ統合）

2026-05-18 にチャンネル別 `{channel_id}.db` を廃止し、全チャンネルを `data/slack.db` に統合した（旧DBは `data/*.db.bak` として保管）。`messages` / `replies` / `summaries` の各テーブルは
すべて `channel_id` 列を含み、PK は `(thread_ts, channel_id)` / `(msg_ts, channel_id)` で
チャンネルをまたいだ衝突を防ぐ。テーブルスキーマ自体は旧 `{channel_id}.db` から変更なし。
クエリ側はすべて `WHERE channel_id = ?` を付与する（`scripts/argus/pm_argus.py: fetch_raw_messages`、
`scripts/pm_slack_box_links.py: collect_box_messages`、`scripts/argus/patrol/users.py: _mine_slack_dbs` ほか）。

#### messages（親メッセージ）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK）。スレッドなし投稿は `msg_id` と同値 |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `user_id` | TEXT | 投稿者のユーザーID |
| `user_name` | TEXT | 投稿者の表示名 |
| `text` | TEXT | メッセージ本文 |
| `timestamp` | TEXT | 投稿日時（JST、例: `2026-01-20 19:43:23`） |
| `permalink` | TEXT | Slack上の投稿URL |
| `fetched_at` | TEXT | DBへの保存日時（ISO8601） |

#### replies（返信メッセージ）

| カラム | 型 | 説明 |
|---|---|---|
| `msg_ts` | TEXT | 返信のタイムスタンプ（PK） |
| `thread_ts` | TEXT | 親スレッドの `thread_ts`（messages への FK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `user_id` | TEXT | 投稿者のユーザーID |
| `user_name` | TEXT | 投稿者の表示名 |
| `text` | TEXT | メッセージ本文 |
| `timestamp` | TEXT | 投稿日時（JST） |
| `permalink` | TEXT | Slack上の投稿URL |
| `fetched_at` | TEXT | DBへの保存日時（ISO8601） |

#### 差分判定ロジック

```
Slack API の latest_reply  vs  MAX(replies.msg_ts)
  新規（thread_ts が messages に存在しない） → 取得
  更新（latest_reply > MAX(msg_ts)）         → 返信再取得
  変化なし                                   → スキップ（API呼び出しなし）
```

### pm.db（PM統合データ）

`action_items` / `decisions` / `meetings` / `goals` / `milestones` の唯一の正本。
2026-05-17 に pm-hpc.db / pm-pmo.db / pm-personal.db への分割運用を廃止し、すべてのチャンネル・会議のインジェスト先を pm.db に統一した（旧DBは `data/*.db.bak` として保管）。FTS5 検索インデックスも 2026-05-18 に `data/qa_index.db` に統合し、論理 index は `chunk_indexes(chunk_id, index_name)` の junction で表現する（領域フィルタとしての分離は維持しつつ、本体 chunks の重複は排除）。

#### meetings

| カラム | 型 | 説明 |
|---|---|---|
| `meeting_id` | TEXT | ファイル名ベースのID（PK） |
| `held_at` | TEXT | 開催日 |
| `kind` | TEXT | 会議種別（全体会議/技術WG等） |
| `file_path` | TEXT | 議事録ファイルパス |
| `summary` | TEXT | LLMによる要約 |
| `parsed_at` | TEXT | 解析日時 |

#### action_items

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | アクションアイテムの内容 |
| `assignee` | TEXT | 担当者 |
| `due_date` | TEXT | 期限（なければNULL） |
| `status` | TEXT | `open` / `closed`。Canvas の `状況` 列で更新（close判定キーワードまたは直接指定） |
| `note` | TEXT | 対応状況メモ（Canvas の `対応状況` 列で記入）。`status` の変更には影響しない |
| `milestone_id` | TEXT | 紐づくマイルストーンID（M1〜M5、なければNULL） |
| `source` | TEXT | `meeting` または `slack` |
| `source_ref` | TEXT | 背景への参照（議事録パス or Slackパーマリンク） |
| `extracted_at` | TEXT | 発生日（meetingは開催日、slackは投稿日。YYYY-MM-DD） |
| `channel_id` | TEXT | 出典 Slack チャンネルID（slack 由来のレコードのみ。meeting 由来は NULL）。2026-05-18 追加 |
| `deleted` | INTEGER | 論理削除フラグ（0=有効、1=削除済み。デフォルト0）。全クエリで `COALESCE(deleted,0)=0` でフィルタ |
| `source_context` | TEXT | 根拠となった議論・発言の要約（`decisions.source_context` と同様、LLMが抽出） |
| `requested_by` | TEXT | 依頼者（`enrich_items.py` が過去ナレッジから推定・補完。`decisions.decided_by` に相当） |
| `requested_by_confidence` | TEXT | `requested_by` 推定の確信度 |
| `rationale` | TEXT | このアクションが必要になった背景・根拠（enrich が補完） |
| `related_ids` | TEXT | 関連する decision/action_item ID（JSON配列文字列、enrich が推定） |

#### decisions

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | 決定事項の内容 |
| `decided_at` | TEXT | 決定日 |
| `source` | TEXT | `meeting` または `slack` |
| `source_ref` | TEXT | 背景への参照（議事録パス or Slackパーマリンク） |
| `source_context` | TEXT | 根拠となった議論・発言の要約（`pm_ingest.py minutes` 経由のみ。LLMが抽出） |
| `extracted_at` | TEXT | 発生日（meetingは開催日、slackは投稿日。YYYY-MM-DD） |
| `channel_id` | TEXT | 出典 Slack チャンネルID（slack 由来のレコードのみ。meeting 由来は NULL）。2026-05-18 追加 |
| `deleted` | INTEGER | 論理削除フラグ（0=有効、1=削除済み。デフォルト0）。全クエリで `COALESCE(deleted,0)=0` でフィルタ |
| `decided_by` | TEXT | 判断者（`enrich_items.py` が過去ナレッジから推定・補完） |
| `decided_by_confidence` | TEXT | `decided_by` 推定の確信度（`high`/`medium`/`low` 等） |
| `rationale` | TEXT | 決定の根拠。流入時（LLM抽出、`[根拠: ...]`タグ）または enrich 時に補完されるが、**流入時の値がある場合は enrich で上書きしない**（`_save_decision_enrichment()` の `CASE WHEN rationale IS NULL OR TRIM(rationale)='' THEN ? ELSE rationale END`。モードA=即時捕捉を優先する設計） |
| `related_ids` | TEXT | 関連する decision/action_item ID（JSON配列文字列、enrich が推定） |
| `trade_off` | TEXT | 捨てた案・比較検討した代替案（`[捨てた案: ...]`タグ、Argus垂直軸 流入拡張で2026-07-01追加）。機能2の決定クラスタ集約時の矛盾検出に使う想定 |
| `reversal_condition` | TEXT | この決定を見直す条件（`[覆す条件: ...]`タグ、同上）。将来的にレビュー発火のトリガーに使う想定 |

`rationale`/`trade_off`/`reversal_condition` は議事録・Slackの抽出プロンプトが出力する
ブラケットタグ（`[出典: ...] [根拠: ...] [捨てた案: ...] [覆す条件: ...]`、順不同・全て省略可）から
`pm_minutes_import.py::_parse_decisions()` / `scripts/ingest/slack.py::save_slack_items()` が
分離して保存する。詳細は `data/FugakuNEXT_Argus_designsheet.docx` および PLAN.md「Argus 垂直軸」参照。

#### achievements（実績台帳、per-app。2026-07-16 追加）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK（AUTOINCREMENT） |
| `app` | TEXT | アプリ名 |
| `title` | TEXT | 実績サマリ（短い一句） |
| `category` | TEXT | 実績の種別（移植/性能測定/公開/登録/ベンチ収録/評価等） |
| `achieved_on` | TEXT | 達成時期（`YYYY-MM` または `YYYY-MM-DD`。不明なら空文字） |
| `evidence_ref` | TEXT | 根拠の出典参照 |
| `evidence_quote` | TEXT | 根拠となった原文の抜粋 |
| `confidence` | TEXT | LLM抽出の確信度（`high` / `low`） |
| `status` | TEXT | 人間検収ゲート（`proposed` / `confirmed` / `rejected`。デフォルト `proposed`） |
| `source` | TEXT | 抽出元（デフォルト `argus_auto`） |
| `dedup_key` | TEXT | UNIQUE。`app\|正規化title` から生成し冪等 upsert のキーに使う |
| `created_at` / `updated_at` | TEXT | 作成日時・更新日時 |
| `deleted` | INTEGER | 論理削除フラグ（0=有効、1=削除済み。デフォルト0） |

差分判定: populator (`scripts/ingest/achievements.py`) は既存 title を LLM に見せる「台帳認識抽出」＋
run内 self-dedup（embedding貪欲クラスタリング）＋既存行との embedding 類似度0.85比較で重複候補を
除外したうえで、`dedup_key` への `ON CONFLICT DO UPDATE` により冪等 upsert する。`confidence=high`
は自動的に `confirmed`、それ以外は `proposed` のまま人間の検収を待つ。`status` が `confirmed` /
`rejected` の行は再実行時に本文を上書きしない（人間の確定・却下を保護）。exec summary / Box XLSX
は `confirmed` の行のみを使用する。

#### slack_extractions（抽出済みスレッド管理）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `extracted_at` | TEXT | 抽出日時（ISO8601） |

差分判定: `slack_extractions` に存在するスレッドは `--force-reextract` なしでスキップ

#### goals

| カラム | 型 | 説明 |
|---|---|---|
| `goal_id` | TEXT | ゴールID（PK、例: G1） |
| `name` | TEXT | ゴール名 |
| `description` | TEXT | 説明 |

#### milestones

| カラム | 型 | 説明 |
|---|---|---|
| `milestone_id` | TEXT | マイルストーンID（PK、例: M1） |
| `goal_id` | TEXT | 紐づくゴールID（goals への FK） |
| `name` | TEXT | マイルストーン名 |
| `due_date` | TEXT | 期限（YYYY-MM-DD） |
| `area` | TEXT | 担当エリア |
| `status` | TEXT | `active` / `achieved` |
| `success_criteria` | TEXT | 達成条件（JSON配列） |
| `imported_at` | TEXT | 最終同期日時 |

#### Argus 垂直軸: 前提・意思決定台帳（有向グラフ）

`goals`/`milestones`（上記）は `goals.yaml` から人間が定義する既存のトップダウン目標管理。
以下の `ledger_*` 4テーブルは Argus 垂直軸（前提・意思決定台帳）の骨格で、2026-07-01 追加
（PLAN.md「Argus 垂直軸」Phase 1）。役割が異なるため **`goals` と `ledger_goals` は別テーブル**。
詳細設計は `data/FugakuNEXT_Argus_designsheet.docx` を参照。台帳は決定 (`decisions`) →
目標/前提への型付き辺（`ledger_edges`）で構成される有向グラフとして、意図された方向
（`ledger_goals.weight`）と実態の方向（`decisions` からの貢献辺の重み付き合計）の Δ を
可視化する（機能2、Phase 3で実装予定）。

##### ledger_goals（目標・制約）

意図された方向を3層（最上位/識別要件/前提条件）で表現する。

| カラム | 型 | 説明 |
|---|---|---|
| `goal_id` | TEXT | PK（例: `G-NS`, `G-PHYS`, `C-SOVEREIGN`） |
| `kind` | TEXT | `goal` / `constraint` |
| `layer` | TEXT | `top` / `identifying` / `tablestakes` |
| `is_top_goal` | INTEGER | 最上位目標フラグ |
| `name` | TEXT | 目標名 |
| `identification_test` | TEXT | 識別テスト（商用クラスタも同一主張が可能か。否→識別要件／是→前提条件の判定基準） |
| `weight` | TEXT | 重み（高/中/低。機能1が上位意思から更新する想定、時変） |
| `weight_status` | TEXT | `ratified`（批准済み）/ `provisional`（要批准） |
| `source` | TEXT | 出所参照キー（本文は含めない。機密は `docs/project.md` 系に分離） |
| `source_status` | TEXT | `confirmed` / `needs_source`（出所未確定） |
| `state` | TEXT | `active` 等 |
| `created_at` / `last_reviewed_at` | TEXT | 作成日時・最終レビュー日時 |

##### ledger_assumptions（前提）

確信度・根拠と、機能1（外界取り込み）の監視対象を保持する。投入経路は
`scripts/ingest/ledger.py::upsert_assumptions()`（`content` の完全一致で重複判定、
自然キーが無いため）。読み取り側は Patrol `detect_external_signals`
（`docs/argus_system.md`「検出ルール詳細 8」参照）。

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | 前提の内容 |
| `confidence` | TEXT | 確信度（過剰反応の抑制に使用） |
| `evidence` | TEXT | 根拠 |
| `monitor_target` | TEXT | どの外部シグナルが肯定/否定するか（機能1の取り込み口、`ledger_edges` の `monitors` 辺と対応） |
| `source` | TEXT | 出所 |
| `state` | TEXT | `active` / `superseded` / `review` |
| `created_at` / `last_reviewed_at` | TEXT | 作成日時・最終レビュー日時 |

##### ledger_issues（論点）

未解決事項。責任者・期限を持ち、`ledger_edges` の `blocks` 辺で決定をブロックする。

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `issue_id` | TEXT | UNIQUE（例: `Q-FP64`） |
| `content` | TEXT | 論点の内容 |
| `owner` | TEXT | 責任者 |
| `due_date` | TEXT | 期限 |
| `state` | TEXT | `open` 等 |
| `created_at` | TEXT | 作成日時 |

##### ledger_edges（型付き辺）

台帳グラフ本体。方向に関する情報は辺（from→to）が保持する。

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `edge_type` | TEXT | `contributes`（貢献）/ `depends_on`（依拠）/ `monitors`（監視）/ `blocks`（ブロック） |
| `from_kind` / `from_id` | TEXT | 起点（例: `decision`/`42`, `issue`/`Q-FP64`） |
| `to_kind` / `to_id` | TEXT | 終点（例: `goal`/`G-PHYS`, `assumption`/`7`） |
| `weight` | REAL | 貢献の度合い（機能2の実態投入量の重み付き合計に使用） |
| `source` | TEXT | 出所 |
| `rationale` | TEXT | この辺を張った根拠 |
| `state` | TEXT | `active` 等 |
| `created_at` | TEXT | 作成日時 |

`UNIQUE(edge_type, from_kind, from_id, to_kind, to_id)` により同一の辺の重複投入は
`INSERT OR REPLACE` で冪等に扱える。Phase 1時点で生成されるのは `decisions` → `ledger_goals`
の `contributes` 辺のみ（`enrich_items.py::enrich_decision()` が生成）。`depends_on`（前提への依拠）
は `ledger_assumptions` が空のため未生成。

#### audit_log（変更履歴）

Canvas同期（`pm_sync_canvas.py`）およびマイルストーン紐づけ変更（`pm_relink.py`）の際に、上書き前の値を記録する。

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `table_name` | TEXT | 変更対象テーブル名（現在は `action_items` のみ） |
| `record_id` | TEXT | 変更対象レコードのID |
| `field` | TEXT | 変更されたカラム名（`assignee`・`status`・`milestone_id` 等） |
| `old_value` | TEXT | 変更前の値（NULL の場合は NULL） |
| `new_value` | TEXT | 変更後の値 |
| `changed_at` | TEXT | 変更日時（UTC ISO8601） |
| `source` | TEXT | 変更元（`canvas_sync` または `relink`） |

### data/box_docs.db（BOX 本文ナレッジ）

`pm_box_crawl.py` が `box_sources.yaml` のフォルダを走査して BOX のファイル一覧と本文を Markdown 化して保存する。`pm_box_relevance.py` がローカル LLM で relevance（core/related/noise/unknown）を判定して `box_files.relevance` 列を埋める。`pm_embed.py` は `relevance = 'noise'` のファイルを索引対象から自動除外する（NULL/空文字は索引対象に含まれる）。

#### box_files（メタデータ + relevance）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `box_file_id` | TEXT | BOX ファイル ID（UNIQUE） |
| `box_folder_id` | TEXT | 親フォルダの BOX ID |
| `name` | TEXT | ファイル名 |
| `file_format` | TEXT | 拡張子（`pptx` / `docx` / `pdf` / `xlsx` / `md` / `boxnote` / `txt`） |
| `size_bytes` | INTEGER | ファイルサイズ |
| `modified_at` | TEXT | BOX 上の最終更新日時（JST 表記、`YYYY-MM-DD HH:MM`） |
| `folder_path` | TEXT | ルート（`box_sources.yaml.name`）からの相対フォルダパス |
| `index_name` | TEXT | 投入先の論理 index 名（`box_sources.yaml.index_names` の 1 つ。複数所属の場合は別行で重複登録） |
| `source_name` | TEXT | `box_sources.yaml.name`（フィルタキー） |
| `registered_at` | TEXT | DB への登録日時（UTC ISO8601） |
| `relevance` | TEXT | LLM/人手による関連度判定。`core` / `related` / `noise` / `unknown`。NULL は未判定 |
| `relevance_reason` | TEXT | LLM が出した判定根拠（1 行） |
| `relevance_judged_at` | TEXT | 判定日時（UTC ISO8601） |

#### doc_content（本文 Markdown）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `box_file_id` | TEXT | `box_files.box_file_id` への参照（UNIQUE。1:1） |
| `content_md` | TEXT | Markdown 化された本文 |
| `content_hash` | TEXT | `content_md` のハッシュ（差分判定用。同一ハッシュなら再変換スキップ） |
| `page_count` | INTEGER | ページ数（pptx のスライド数 / pdf のページ数等。形式に依存） |
| `char_count` | INTEGER | `content_md` の文字数 |
| `convert_method` | TEXT | 変換経路（`libreoffice_html` / `pdftotext` / `gemma4_multimodal` / `boxnote_json` / `text` 等） |
| `extracted_at` | TEXT | 変換日時（UTC ISO8601） |

`box_files` と `doc_content` は `box_file_id` で 1:1。`pm_box_crawl.py --remove` は両テーブルから削除する。

### data/knowledge.db（蒸留ナレッジレイヤ、2026-06-16 廃止）

旧・意思決定/制約/不変条件を蒸留して保持する専用DBだったが、`decisions` との二重管理・
低い実消費（brief/risk 同梱と investigate の search_knowledge のみ）を理由に全廃した。
背景知識は現在 `pm.db.decisions`（rationale 付き）で代替する。生成側スクリプト
（`pm_box_distill.py` / `pm_knowledge_*.py`）は `scripts/archive/` ごと削除済み
（削除経緯・データ規模は LOG.md「2026-06-16 knowledge.db 全廃」参照）。

### data/qa_index.db（FTS5統合インデックス + embedding）

Slack / 議事録 / BOX 本文 / Web記事を横断する全文検索・意味検索の統合インデックス。
`pm_embed.py` が構築する。詳細は `docs/argus_system.md` の「統合インデックスDB スキーマ」を参照。

| テーブル | 説明 |
|---|---|
| `chunks(id, source_type, source_db, record_id, held_at, content, tokens, source_ref, indexed_at)` | 原文チャンク本体（1000字・100字オーバーラップで分割） |
| `chunk_indexes(chunk_id, index_name)` | 論理 index（`pm` / `pm-hpc` / `pm-pmo` / `pm-all` 等）と chunk の M:N 結合 |
| `fts` | trigram tokenize の FTS5 仮想テーブル（`content='chunks'`） |
| `fts_tokens` | SudachiPy 形態素解析済みトークンの FTS5 仮想テーブル（`unicode61` tokenize） |
| `chunk_embeddings(chunk_id, model, dim, vector, embedded_at)` | bge-m3 embedding ベクトル（BLOB）。investigate のハイブリッド検索（FTS5 RRF + cosine similarity）に使用 |
| `index_state(source_db, index_name, last_indexed)` | `(source_db, index_name)` 単位の差分更新タイムスタンプ |
