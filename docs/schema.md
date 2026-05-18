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

### data/qa_index.db（FTS5 統合検索インデックス）

詳細は `docs/argus_system.md` の「統合インデックスDB スキーマ」を参照。
要点のみ:

- `chunks(id, source_type, source_db, record_id, held_at, content, tokens, source_ref, indexed_at)` — 原文チャンク本体
- `chunk_indexes(chunk_id, index_name)` — 論理 index（`pm` / `pm-hpc` / `pm-pmo` / `pm-all`）と chunk の M:N 関係
- `fts` / `fts_tokens` — 検索用 FTS5 仮想テーブル（trigram / SudachiPy 形態素）
- `index_state(source_db, index_name, last_indexed)` — `(source_db, index_name)` 単位の差分更新タイムスタンプ
