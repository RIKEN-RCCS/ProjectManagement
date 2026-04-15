## DBスキーマ

### {channel_id}.db（Slackデータ）

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

#### summaries（スレッド要約）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `summary` | TEXT | Claude CLIが生成した要約テキスト |
| `summarized_at` | TEXT | 要約生成日時（ISO8601） |
| `last_reply_ts` | TEXT | 要約時点での最新返信の `msg_ts`（返信なしは NULL） |

#### 差分判定ロジック

```
Slack から取得した最新返信 msg_ts  vs  summaries.last_reply_ts
  新規（thread_ts が DB に存在しない） → 取得・要約
  更新（最新 msg_ts > last_reply_ts）  → 返信再取得・再要約
  変化なし                             → スキップ（API・LLM呼び出しなし）
```

### pm.db（PM統合データ）

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
| `deleted` | INTEGER | 論理削除フラグ（0=有効、1=削除済み。デフォルト0）。全クエリで `COALESCE(deleted,0)=0` でフィルタ |

#### decisions

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | 決定事項の内容 |
| `decided_at` | TEXT | 決定日 |
| `source` | TEXT | `meeting` または `slack` |
| `source_ref` | TEXT | 背景への参照（議事録パス or Slackパーマリンク） |
| `source_context` | TEXT | 根拠となった議論・発言の要約（`pm_minutes_to_pm.py` 経由のみ。LLMが抽出） |
| `extracted_at` | TEXT | 発生日（meetingは開催日、slackは投稿日。YYYY-MM-DD） |
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
