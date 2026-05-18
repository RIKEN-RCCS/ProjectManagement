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

### data/knowledge.db（蒸留ナレッジレイヤ）

`box_docs.db` の本文・`data/minutes/{kind}.db` の議事録・`pm.db` の `decisions` を入力として、LLM が
「**1 レコード = 1 つの意思決定 / 制約 / 不変条件**」の粒度で抽出した蒸留ナレッジを保持する。
プロンプトに毎回詰める負荷を最小化する目的のため、各レコードは **要点のみで数百字以内**を狙う。

**設計原則**:
- **プロジェクト全体共通**: `index_name` 等のチャンネル別分割は持たない。`/argus-investigate` も `/argus-brief` `/argus-risk` も同じレコード集合を参照する
- **意思決定単位**: 1 BOX ファイル ≠ 1 レコード。1 ファイルから 0〜複数件、1 レコードが複数ファイルを根拠にすることもある（`knowledge_sources` で N:M 結合）
- **追跡可能**: 全レコードに必ず `source_box_file_ids` / `source_decision_ids` / `source_meeting_ids` のいずれかが紐づく。LLM 推論のみの主張は許容しない
- **時系列の劣化を扱う**: `last_validated_at` を持ち、古いレコードは brief/risk 側で減衰させる（または `confidence` を下げる）。新しい意思決定で上書きされた場合は `superseded_by` で連鎖する
- **暗号化**: 設計判断・組織情報を含むため SQLCipher で暗号化（pm.db と同様）

#### knowledge（ナレッジ本体）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | TEXT | PK。`KN-{4桁ゼロ埋め連番}` 形式（例: `KN-0042`）。再採番しない |
| `kind` | TEXT | レコード種別。`decision`（意思決定）/ `constraint`（制約・不変条件）/ `position`（ステークホルダーの方針）/ `glossary`（プロジェクト固有用語の確定定義）|
| `topic` | TEXT | 1 行サマリ（〜30字）。例: `Scale-upドメインはNVL4で合意` |
| `current_state` | TEXT | 現在の状態・採用案・確定事項（〜80字）。最終的にプロンプトに同梱される本体 |
| `rationale` | TEXT | 根拠・採用理由（〜200字）。investigate 用 |
| `alternatives_rejected` | TEXT | 却下された代替案（〜200字、JSON 配列推奨）。investigate 用 |
| `constraints_invariants` | TEXT | 紐づく制約条件・前提（〜200字、JSON 配列推奨）|
| `tags` | TEXT | 検索・フィルタ用タグ（JSON 配列。例: `["architecture","scale-up","co-design"]`）|
| `owners` | TEXT | 主たる責任者の表示名リスト（JSON 配列。`pm.db.assignee` と同じ命名規則）|
| `decided_at` | TEXT | 意思決定日（YYYY-MM-DD。会議名・Slack 投稿などから推定）|
| `last_validated_at` | TEXT | このレコードの内容を最後に裏取り・更新した日（YYYY-MM-DD）|
| `confidence` | TEXT | LLM/人手による確度。`high` / `medium` / `low` |
| `superseded_by` | TEXT | 後続レコードに上書きされた場合の `knowledge.id`（NULL なら現役） |
| `deleted` | INTEGER | 論理削除フラグ（0=有効、1=削除済み）。全クエリで `COALESCE(deleted,0)=0` でフィルタ |
| `created_at` | TEXT | 初回登録日時（UTC ISO8601） |
| `updated_at` | TEXT | 最終更新日時（UTC ISO8601） |

**プロンプト同梱の最小単位は `id` + `topic` + `current_state` + `last_validated_at`** 程度を想定。
investigate ツールがフル展開（`rationale` / `alternatives_rejected`）を引く際に他のフィールドが効く。

#### knowledge_sources（ソース文書との N:M 結合）

| カラム | 型 | 説明 |
|---|---|---|
| `knowledge_id` | TEXT | `knowledge.id` への参照（PK の一部） |
| `source_type` | TEXT | `box_file` / `minutes` / `decision` / `slack` / `web` のいずれか（PK の一部） |
| `source_ref` | TEXT | source_type に応じた識別子（PK の一部）。`box_file` なら `box_file_id`、`minutes` なら `meeting_id`、`decision` なら `decisions.id`、`slack` ならパーマリンク、`web` なら URL |
| `weight` | TEXT | このソースが当該レコードの根拠としてどの程度寄与するか。`primary`（主たる根拠）/ `supporting`（補強）/ `historical`（過去経緯）|
| `excerpt` | TEXT | 該当箇所の抜粋（〜200字）。蒸留の再現性とトレース用。元 BOX ファイルの該当節をコピー |
| `added_at` | TEXT | 紐付け日時（UTC ISO8601） |

PK は `(knowledge_id, source_type, source_ref)`。1 つの knowledge は複数ソースを持ち、1 ソースも複数 knowledge に再利用される。

#### knowledge_relations（ナレッジ間の関係）

| カラム | 型 | 説明 |
|---|---|---|
| `from_id` | TEXT | `knowledge.id`（PK の一部） |
| `to_id` | TEXT | `knowledge.id`（PK の一部） |
| `relation` | TEXT | `supersedes`（旧版を上書き）/ `depends_on`（前提とする）/ `conflicts_with`（矛盾、要解決）/ `refines`（同テーマの補足）/ `related_to`（関連） |
| `note` | TEXT | 関係の補足説明（任意） |
| `created_at` | TEXT | 登録日時（UTC ISO8601） |

PK は `(from_id, to_id, relation)`。`supersedes` は `knowledge.superseded_by` と整合するように両側を更新する。

#### knowledge_audit（変更履歴）

`pm.db.audit_log` と同じ思想。LLM による自動更新・人手の修正のいずれも記録する。

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `knowledge_id` | TEXT | 対象 knowledge.id |
| `field` | TEXT | 変更カラム名（`current_state` / `rationale` / `superseded_by` 等） |
| `old_value` | TEXT | 変更前 |
| `new_value` | TEXT | 変更後 |
| `changed_at` | TEXT | 変更日時（UTC ISO8601） |
| `source` | TEXT | 変更元（`distill_llm` / `human_edit` / `relink` / `merge`） |
| `actor` | TEXT | 人手編集の場合の編集者名（任意） |

#### distill_state（蒸留処理の冪等性管理）

入力ソース（BOX ファイル等）に対して、どのバージョンまで蒸留したかを記録する。`box_files.content_hash` が変わったら再蒸留トリガになる。

| カラム | 型 | 説明 |
|---|---|---|
| `source_type` | TEXT | `box_file` / `minutes` / `decision`（PK の一部） |
| `source_ref` | TEXT | source_type に応じた識別子（PK の一部）|
| `last_input_hash` | TEXT | 蒸留時に見た入力のハッシュ（`doc_content.content_hash` 等）。これと現在値が異なれば再蒸留 |
| `last_distilled_at` | TEXT | 最終蒸留日時（UTC ISO8601）|
| `produced_knowledge_ids` | TEXT | この蒸留で作成・更新した `knowledge.id` のリスト（JSON 配列）。再蒸留時に「同じソースから派生した既存レコードのうち、新版で言及されなくなったもの」を検出する用途 |
| `status` | TEXT | `ok` / `skipped`（content 短すぎ等）/ `error`（LLM 失敗）|
| `note` | TEXT | エラー詳細・スキップ理由 |

PK は `(source_type, source_ref)`。

#### 運用上の不変条件

- `superseded_by` が埋まっている `knowledge` は brief/risk のプロンプトに **入れない**（investigate 側はオプションで履歴閲覧可能にする）
- `confidence='low'` でも `current_state` が短文であれば brief/risk に出す価値はあるが、出力時に明示する（"暫定: …"）
- `last_validated_at` が一定期間（例: 180日）以上前のレコードは「要再確認」として risk セクションに別出ししても良い
- 1 つの `topic` に対して `kind` を増やすのではなく、新たな `id` を発行して `supersedes` で繋ぐ。レコード自体の編集は人手修正・誤字訂正のみ。


詳細は `docs/argus_system.md` の「統合インデックスDB スキーマ」を参照。
要点のみ:

- `chunks(id, source_type, source_db, record_id, held_at, content, tokens, source_ref, indexed_at)` — 原文チャンク本体
- `chunk_indexes(chunk_id, index_name)` — 論理 index（`pm` / `pm-hpc` / `pm-pmo` / `pm-all`）と chunk の M:N 関係
- `fts` / `fts_tokens` — 検索用 FTS5 仮想テーブル（trigram / SudachiPy 形態素）
- `index_state(source_db, index_name, last_indexed)` — `(source_db, index_name)` 単位の差分更新タイムスタンプ
