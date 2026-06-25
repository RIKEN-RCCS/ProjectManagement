## 主なコマンド

### 1. Slack差分取得（slack_pipeline.py）

2026-05-18 以降は全チャンネル統合DB `data/slack.db` を共有する。`--db` を省略するとこのパスが使われる。
新規 / 更新スレッドは `channel_id` 列付きで挿入され、クエリ側はすべて `WHERE channel_id = ?` で
チャンネルを絞り込む。

```sh
# 通常運用: 差分のみ取得（統合DB data/slack.db に書き込まれる）
python3 scripts/slack_pipeline.py -c CHANNEL_ID

# 初回・過去分の取り込み（oldest をAPIに渡してページネーション全件取得）
python3 scripts/slack_pipeline.py -c CHANNEL_ID --since 2025-04-01

# DB内のスレッド一覧を表示
python3 scripts/slack_pipeline.py -c CHANNEL_ID --list
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `CHANNEL_ID` | 対象チャンネルID |
| `--db PATH` | `data/slack.db` | SQLite DBファイルパス（全チャンネル統合） |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のメッセージのみ取得（APIに oldest として渡す） |
| `-l N` | `100` | 1ページあたりの取得件数上限（最大999） |
| `--skip-fetch` | - | Slack API取得をスキップ（DBのみ使用） |
| `--list` | - | DB内のスレッド一覧を表示して終了（`--since` 併用可） |
| `--no-permalink` | - | パーマリンク取得を無効化 |
| `--dry-run` | - | Slack API取得のみ実行（DB書き込みなし） |

### 1b. Slack取得→pm.db抽出 一括実行（pm_from_slack.sh）

`slack_pipeline.py`（取得）→ `pm_ingest.py slack`（pm.db抽出）を連続実行する。

```sh
# 通常運用
bash scripts/pm_from_slack.sh -c CHANNEL_ID

# 日付フィルタ付き
bash scripts/pm_from_slack.sh -c CHANNEL_ID --since 2026-01-01

# 確認用（DB保存なし）
bash scripts/pm_from_slack.sh -c CHANNEL_ID --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `CHANNEL_ID` | 対象チャンネルID（両スクリプトに渡す） |
| `--since YYYY-MM-DD` | なし | この日付以降のみ対象（両スクリプトに渡す） |
| `--dry-run` | - | DB保存なし・確認のみ（両スクリプトに渡す） |
| `--no-encrypt` | - | 平文モード（両スクリプトに渡す） |
| `--db-slack PATH` | `data/slack.db` | Slack DBパス（全チャンネル統合） |
| `--db-pm PATH` | `data/pm.db` | pm.db パス |
| `--skip-fetch` | - | Slack API取得をスキップ（`slack_pipeline.py` のみ） |
| `--force-reextract` | - | 抽出済みスレッドも再処理（`pm_ingest.py slack` のみ） |

### 2. 会議録文字起こし（pm_from_recording.sh + whisper_vad.py）

```sh
# 推奨: --meeting-name を指定すると pm.db に直接保存し .md を削除（平文ファイルが残らない）
sbatch scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting
sbatch scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --skip 30 --meeting-name Leader_Meeting

# 日付を明示上書き（省略時はファイル名の GMT タイムスタンプを JST 変換して自動取得）
sbatch scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10

# 従来方式: .md ファイルをそのまま残す（セキュリティ警告が出る）
sbatch scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--skip SECONDS` | なし | 全ファイルの冒頭をスキップ |
| `--meeting-name NAME` | なし | 指定すると文字起こし後に pm.db へ直接インポートし .md を削除 |
| `--held-at YYYY-MM-DD` | GMT→JST変換 | `--meeting-name` と併用。省略時はファイル名のGMTタイムスタンプをJSTに変換して取得 |
| `--vtt PATH` | 同名VTT自動検出 | Zoom VTT ファイルを明示指定。省略時は `{stem}.transcript.vtt` → `{stem}.vtt` の順で自動検出 |
| `--no-slide-ocr` | 有効（mp4のみ） | スライドOCRを無効化（スライドなしの動画で OCR コストを省く場合のみ使用） |
| `--scene-threshold N` | `0.25` | ffmpeg scene detect 閾値。小さくすると抽出フレーム数が増える |
| `--max-frames N` | `200` | OCR に渡すフレーム数の上限。超過時は動画全編から時系列に均等間引き（先頭 N 枚だけを拾って後半を捨てることはしない）|
| `--ocr-workers N` | `8` | OCR 並列ワーカー数 |

処理フロー: **スライドOCR（mp4のみ、scene detect + マルチモーダルLLM）** → ffmpeg → WAV変換（16kHz, mono） → DeepFilterNetノイズ除去 → SileroVAD → pyannote話者分離 → Whisper large-v3 文字起こし（スライドから抽出した固有名詞を initial_prompt に追加）

**VTT 話者情報の活用**: Zoom の自動文字起こし VTT ファイルが存在する場合、VTT の正確な話者名を Whisper の高品質日本語文字起こしと統合する。議事録 Stage 3（決定事項・アクションアイテム抽出）で話者名をもとに担当者を推定する。VTT ファイルの検索は同名のみ（フォールバックなし）: `{stem}.transcript.vtt` → `{stem}.vtt` の順で検索し、先に見つかった方を使用する。

**スライドOCRの活用**: mp4 には発表スライドが写っていることが多く、スライド上の固有名詞・技術用語・数値を OCR で抽出することで Whisper の誤変換を補える。`scripts/recording/slide_ocr.py` が ffmpeg の scene detect でスライド切り替わりのフレームを抽出し、マルチモーダルLLM（`OPENAI_API_BASE`）で Markdown に変換する。得られた結果は (1) 固有名詞リストを Whisper の `initial_prompt` に追加、(2) スライド文脈を `generate_minutes_local.py` の Stage 1/2/3 プロンプトに同梱、の 2 系統で議事録品質に反映される。スライドなしの会議（frames=0）や mp4 以外の拡張子、OPENAI_API_BASE 未設定時はスキップされ既存動作にフォールバックする。VTT × Slides × Whisper の 3 系統はそれぞれ独立して有効/無効化でき、共存する。

`--meeting-name` 指定時の追加フロー: 文字起こし完了 → `generate_minutes_local.py`（VTTあれば `--vtt` 付き）で議事録生成 → `pm_minutes_import.py` で議事録DBに保存 → `pm_ingest.py minutes` で pm.db に転記 → .md 削除

### 3. 会議議事録 → 詳細議事録DB（pm_minutes_import.py）

詳細な議事内容・決定の背景・AIの発生経緯を `data/minutes/{meeting_name}.db` に保存する。会議名ごとに独立したDBファイルを作成する。

```sh
# 単一ファイル
python3 scripts/pm_minutes_import.py meetings/2026-03-10_Leader_Meeting.md \
    --meeting-name Leader_Meeting --held-at 2026-03-10

# 一括処理
python3 scripts/pm_minutes_import.py --bulk
python3 scripts/pm_minutes_import.py --bulk --since 2026-01-01 --force

# 議事録DB内容を一覧表示（全会議名）
python3 scripts/pm_minutes_import.py --list

# 特定会議名の一覧
python3 scripts/pm_minutes_import.py --list --meeting-name Leader_Meeting

# 詳細表示（Slack 投稿済み状況も含む）
python3 scripts/pm_minutes_import.py --show 2026-03-10_Leader_Meeting

# DB内容を修正用Markdownにエクスポート（MEETING_ID で一意に特定できるため --meeting-name 不要）
python3 scripts/pm_minutes_import.py --export 2026-03-10_Leader_Meeting --output corrected.md

# 人間が修正したMarkdownをLLM不使用でインポート（既存レコードを上書き）
python3 scripts/pm_minutes_import.py corrected.md \
    --meeting-name Leader_Meeting --held-at 2026-03-10 --no-llm --force

# 確認のみ（DB保存なし）
python3 scripts/pm_minutes_import.py corrected.md \
    --meeting-name Leader_Meeting --held-at 2026-03-10 --no-llm --force --dry-run

# 議事録DBから削除
python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting
python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting --meeting-name Leader_Meeting

# Slack にアップロード（Files タブに表示）
python3 scripts/pm_minutes_import.py \
    --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 -c CHANNEL_ID

# 特定スレッドにアップロード（スレッドに集約、Files タブには表示されない）
python3 scripts/pm_minutes_import.py \
    --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \
    -c CHANNEL_ID --thread-ts 1741234567.123456
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `input_file` | - | 文字起こしファイル（.txt / .md）（単一ファイルモード） |
| `--meeting-name NAME` | ファイル名から推定 | 会議種別名（DBファイル名に使用） |
| `--held-at YYYY-MM-DD` | ファイル名から推定 | 開催日 |
| `--bulk` | - | 一括処理モード |
| `--meetings-dir DIR` | `meetings/` | 議事録 .md ファイルの検索ディレクトリ |
| `--minutes-dir DIR` | `data/minutes/` | 議事録DBの保存ディレクトリ |
| `--since YYYY-MM-DD` | - | `--bulk` / `--list` 時のフィルタ |
| `--model MODEL` | CLI デフォルト | 使用する Claude モデル |
| `--force` | - | 既存レコードを上書き（`--post-to-slack` 時は再アップロードを許可） |
| `--dry-run` | - | DB保存・Slack API呼び出しなし・結果を標準出力のみ |
| `--output PATH` | - | 出力をファイルにも保存（単一ファイルモードのみ） |
| `--no-encrypt` | - | 平文モード |
| `--list` | - | 議事録DBの内容を表示して終了 |
| `--show MEETING_ID` | - | 指定した meeting_id の詳細（Slack投稿状況含む）を表示して終了 |
| `--export MEETING_ID` | - | DB内容を構造化Markdownでエクスポート（人間による修正の叩き台）。`--output` で保存先を指定しない場合は標準出力 |
| `--no-llm` | - | LLMを呼ばず入力ファイルを構造化Markdownとして直接解析してDBに保存。`--force` と組み合わせて修正版の上書きインポートに使用 |
| `--delete MEETING_ID` | - | 指定した meeting_id を議事録DBから削除して終了（`--meeting-name` で対象DB絞り込み可） |
| `--post-to-slack` | - | 議事録ファイルを Slack チャンネルにアップロード |
| `-c / --channel ID` | - | アップロード先チャンネルID（`--post-to-slack` 時に必須） |
| `--thread-ts TS` | - | 投稿先スレッドTS（省略: チャンネル直接投稿で Files タブに表示 / 指定: スレッド集約） |

**Slack トークン**: `SLACK_USER_TOKEN`（xoxp-）を使用。

**格納内容**:
- `minutes_content`: 議題ごとの詳細議事内容（Markdown形式）
- `decisions`: 決定事項
- `action_items`: アクションアイテム + `assignee`（担当者）+ `due_date`（期限）

### 3a. 議事録 Box アップロード・Canvas 目録生成（pm_minutes_catalog.py）

議事録DBから Markdown を Box にアップロードし、Slack Canvas に目録（Box 共有リンク一覧）を生成する。設定は `data/argus_config.yaml` の `meetings:` に会議種別ごとに `box_folder_id`・`catalog_canvas_id` を定義する。

同名ファイルが既に Box フォルダに存在する場合は **バージョン更新**（`box files:versions:upload`）で上書きする。

```sh
# 未アップロード分を Box にアップロード
python3 scripts/pm_minutes_catalog.py --upload

# 目録 Canvas を更新
python3 scripts/pm_minutes_catalog.py --catalog

# 両方実行
python3 scripts/pm_minutes_catalog.py --upload --catalog

# フィルタ付き
python3 scripts/pm_minutes_catalog.py --upload --meeting-name Leader_Meeting --since 2026-04-01

# アップロード状態一覧
python3 scripts/pm_minutes_catalog.py --list

# 確認のみ（Box・Canvas 書き込みなし）
python3 scripts/pm_minutes_catalog.py --upload --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--upload` | — | 未アップロードの議事録を Box にアップロード |
| `--catalog` | — | 目録 Canvas を更新 |
| `--list` | — | アップロード状態を一覧表示 |
| `--meeting-name NAME` | 全種別 | 特定の会議種別のみ対象 |
| `--since YYYY-MM-DD` | なし | この日付以降のみ対象 |
| `--force` | — | アップロード済みも再アップロード（Box はバージョン更新） |
| `--config PATH` | `data/argus_config.yaml` | 設定ファイル |
| `--dry-run` | — | Box・Canvas 書き込みなし |
| `--no-encrypt` | — | 平文モード |
| `--output PATH` | — | ログをファイルにも保存 |

**前提**: Box CLI (`box` コマンド) がログイン済みで、アップロード先フォルダへの書き込み権限があること。Canvas 投稿に `SLACK_USER_TOKEN`（xoxp-）が必要。

**アップロード管理**: 各議事録DBに `upload_log` テーブル（`meeting_id` PK、`box_folder_id`・`box_file_id`・`box_shared_url`・`uploaded_at`）を作成。再実行時は未アップロード分のみ処理（`--force` で既存ファイルもバージョン更新）。

**argus_config.yaml の meetings 構造**:
```yaml
meetings:
  Leader_Meeting:
    pm_db: pm.db                      # pm_from_recording_auto.sh 用（2026-05-17 以降は全会議が pm.db）
    box_folder_id: "123456789"        # 議事録 MD の Box アップロード先
    catalog_canvas_id: <CANVAS_ID>      # 目録 Canvas ID
  Co-design_Review_Meeting:
    pm_db: pm.db                      # box_folder_id 未設定なら目録対象外
```

### 3b, 4, 8. pm.db 統合インジェスト（pm_ingest.py）

**Pass 1（データ取り込み）は `pm_ingest.py` に一本化**されている。Slack・議事録・goals.yaml の3ソースを統一コマンドで処理する。
プラグインアーキテクチャの詳細は `docs/ingest_plugin.md`、全体像は `docs/architecture.md` 参照。

```sh
# ソース一覧
python3 scripts/ingest/pm_ingest.py --list

# Slack 生メッセージ → 決定事項・アクションアイテム抽出
python3 scripts/ingest/pm_ingest.py slack --slack-channel CHANNEL_ID
python3 scripts/ingest/pm_ingest.py slack --slack-channel CHANNEL_ID --since 2026-01-01
python3 scripts/ingest/pm_ingest.py slack --slack-channel CHANNEL_ID --slack-force-reextract
python3 scripts/ingest/pm_ingest.py slack --slack-channel CHANNEL_ID --slack-list
python3 scripts/ingest/pm_ingest.py slack --dry-run --output result.txt

# 議事録DB → pm.db 転記（LLM不使用、担当者・期限を直接コピー）
python3 scripts/ingest/pm_ingest.py minutes
python3 scripts/ingest/pm_ingest.py minutes --minutes-name Leader_Meeting
python3 scripts/ingest/pm_ingest.py minutes --since 2026-01-01
python3 scripts/ingest/pm_ingest.py minutes --minutes-name Leader_Meeting --minutes-force
python3 scripts/ingest/pm_ingest.py minutes --minutes-delete 2026-03-10_Leader_Meeting
python3 scripts/ingest/pm_ingest.py minutes --minutes-list

# goals.yaml → goals/milestones テーブル完全同期
python3 scripts/ingest/pm_ingest.py goals
python3 scripts/ingest/pm_ingest.py goals --dry-run
python3 scripts/ingest/pm_ingest.py goals --goals-list
```

**共通オプション**（全ソース共通）:

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし | この日付以降のデータのみ対象 |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 出力をファイルにも保存 |
| `--no-encrypt` | - | 平文モード |

**slack ソース固有オプション**:

| オプション | デフォルト | 説明 |
|---|---|---|
| `--slack-channel CHANNEL_ID` | `CHANNEL_ID` | 対象チャンネルID |
| `--slack-db PATH` | `data/slack.db` | Slack DBのパス（全チャンネル統合） |
| `--slack-force-reextract` | - | 抽出済みスレッドも再処理 |
| `--slack-list` | - | 抽出済みスレッド一覧を表示して終了 |

**minutes ソース固有オプション**:

| オプション | デフォルト | 説明 |
|---|---|---|
| `--minutes-name NAME` | 全DBを対象 | 特定の会議名のみ処理 |
| `--minutes-dir DIR` | `data/minutes/` | 議事録DBのディレクトリ |
| `--minutes-force` | - | 既存レコードを上書き |
| `--minutes-list` | - | 転記済み会議の一覧表示 |
| `--minutes-delete MEETING_ID` | - | 指定 meeting_id を pm.db から削除 |

**goals ソース固有オプション**:

| オプション | デフォルト | 説明 |
|---|---|---|
| `--goals-file PATH` | `goals.yaml` | goals.yaml のパス |
| `--goals-list` | - | 登録済みゴール・マイルストーン一覧と達成状況を表示 |

**minutes 転記の注意**: 担当者・期限は議事録DBから直接コピーされる。`milestone_id` のみ Canvas または `pm_relink.py` で補完する。

### 5. PMレポート生成・Canvas投稿（pm_report.py）

レポート構成: **サマリー → 直近の決定事項 → 要注意事項 → 未完了アクションアイテム（表形式）**

未完了アクションアイテム表には ID・担当者・期限・マイルストーン・状況・内容・出典・対応状況 の列があり（pm_relink.py --export と列・順序を統一）、会議中にCanvas上で直接記入できる。

```sh
# 週次進捗レポートを生成してCanvas投稿
python3 scripts/pm_report.py

# 直近1ヶ月のデータのみ対象にしてレポート生成
python3 scripts/pm_report.py --since 2026-02-01

# 確認用（Canvas投稿なし）
python3 scripts/pm_report.py --dry-run --output report.md
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--canvas-id ID` | `<CANVAS_ID>` | 投稿先 Canvas ID |
| `--since YYYY-MM-DD` | なし（全期間） | この日付以降のデータのみ対象 |
| `--skip-canvas` | - | Canvas 投稿をスキップ |
| `--show-acknowledged` | - | 確認済み決定事項も表示する（デフォルトは非表示） |
| `--show-workload` | - | 担当者別負荷セクションを出力する（デフォルトは非表示） |
| `--dry-run` | - | Canvas 投稿なし・結果を標準出力のみ |
| `--output PATH` | - | 出力をファイルにも保存 |

**直近の決定事項の確認済み管理**:
- Canvas 上の決定事項セクションにチェックボックスが表示される
- チェックを入れると `pm_sync_canvas.py` 実行時に `acknowledged_at` が記録される
- デフォルトでは確認済み（チェック済み）の決定事項はレポートに表示されない
- `--show-acknowledged` を指定すると確認済みも含めて表示（未確認→確認済みの順）

### 6. Canvas対応状況 → pm.db 同期（pm_sync_canvas.py）

会議中にCanvas上の各列に記入された内容をpm.dbに反映する。

**運用フロー**:
1. `pm_report.py` でアクションアイテム表をCanvas投稿（対応状況列は空）
2. 会議中にメンバーがCanvas上の各列を記入・編集
3. 決定事項を確認したらCanvas上のチェックボックスにチェック
4. 会議後に本スクリプトを実行してpm.dbを更新

```sh
# 通常運用
python3 scripts/pm_sync_canvas.py

# 確認用（DB更新なし）
python3 scripts/pm_sync_canvas.py --dry-run

# 結果をファイルにも保存
python3 scripts/pm_sync_canvas.py --output sync_result.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--canvas-id ID` | `<CANVAS_ID>` | 対象 Canvas ID |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 結果をファイルにも保存 |

**完了判定**: `状況` 列のみで判断（`対応状況` 列はclose判定に使わない）

`状況` 列の完了キーワード（`status='closed'` に更新）: `完了` `done` `済` `対応済` `解決` `close` `closed` `finish` `finished` `[x]`（大文字小文字を区別しない）。Canvas上のチェックボックスにチェックを入れた場合も `[x]` として検出され完了扱いになる。

`対応状況` 列は内容をそのまま `note` 列に保存（`status` には影響しない）

Canvas上で変更可能な列: **担当者・期限・マイルストーン・状況・内容・対応状況**（非空かつDB値と異なる場合のみ更新）

**決定事項の確認（acknowledgement）同期**:
- Canvas の決定事項セクションのチェックボックス状態を読み取り、`decisions.acknowledged_at` を更新する
- チェックあり → `acknowledged_at` に現在日時を記録
- チェックなし（外した場合） → `acknowledged_at` を NULL にリセット（確認取り消し）
- **注意**: チェック直後は Slack の `url_private` 更新に遅延があるため、チェック後数分待ってから実行すること
- Canvas 同期が拾えなかった場合は `--acknowledge ID...` で直接指定できる
- `--debug-canvas` オプションでCanvas生データをダンプしてトラブルシュートできる

### 7. アクションアイテム・決定事項の一括編集（pm_relink.py）

アクションアイテムと決定事項をCSV経由で一括編集する。LLMは使用しない。1ファイルに2セクション（アクションアイテム / 決定事項）を出力・入力する。

**アクションアイテムの編集可能フィールド**:

| フィールド | 空欄の扱い |
|---|---|
| `assignee` | NULL（担当者なし） |
| `due_date` | NULL（期限なし） |
| `milestone_id` | NULL（紐づけ解除） |
| `content` | スキップ（変更なし） |
| `status` | スキップ（変更なし）。`open` / `closed` を推奨 |

**決定事項の編集可能フィールド**:

| フィールド | 空欄の扱い |
|---|---|
| `content` | スキップ（変更なし） |
| `decided_at` | スキップ（変更なし） |

```sh
# milestone_id が未設定のアイテム + 全決定事項をCSVにエクスポート（デフォルト: relink.csv）
python3 scripts/pm_relink.py --export

# 全件エクスポート
python3 scripts/pm_relink.py --export --all

# 日付フィルタを付けてエクスポート
python3 scripts/pm_relink.py --export --since 2026-02-01

# 変更内容を確認（DB更新なし）
python3 scripts/pm_relink.py --import relink.csv --dry-run

# DBに反映（確認プロンプトあり）
python3 scripts/pm_relink.py --import relink.csv

# アクションアイテムと決定事項を一覧表示
python3 scripts/pm_relink.py --list
python3 scripts/pm_relink.py --list --all
python3 scripts/pm_relink.py --list --since 2026-02-01
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--export` | - | アクションアイテム + 決定事項をCSVにエクスポート |
| `--import PATH` | - | CSVを読み込んでDBを更新 |
| `--list` | - | アクションアイテムと決定事項を一覧表示して終了 |
| `--all` | - | `--export` / `--list` 時に全件対象（デフォルトは `milestone_id IS NULL` のみ） |
| `--since YYYY-MM-DD` | - | `--export` / `--list` 時のフィルタ |
| `--output PATH` | `relink.csv` | `--export` 時の出力ファイルパス |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--no-encrypt` | - | 平文モード |
| `--dry-run` | - | DB更新なし・変更内容を表示のみ |

### 8. ゴール・マイルストーン同期

`pm_ingest.py goals` に統合されている。上記「3b, 4, 8. pm.db 統合インジェスト」参照。

### 9. DBユーティリティ（db_utils.py）

#### 暗号化・鍵管理

```sh
# 鍵を生成（初回のみ）
python3 scripts/db_utils.py --gen-key
# → ~/.secrets/pm_db_key.txt に 64文字のランダム鍵を生成（chmod 600）

# 既存の平文DBを暗号化DBに変換（初回のみ）
python3 scripts/db_utils.py --migrate data/pm.db data/CHANNEL_ID.db data/CHANNEL_ID.db
# → 各 .bak にバックアップを作成してから変換・検証

# 変換内容を確認のみ（変換しない）
python3 scripts/db_utils.py --migrate data/pm.db --dry-run
```

**鍵ファイルを紛失すると暗号化済みDBは復元不可能。パスワードマネージャー等に必ずバックアップすること。**

### 10. LLMインサイト生成（pm_insight.py）

pm.db のデータを統計集計し、LLM でプロジェクトの健全性評価・リスク特定・改善提案を生成する。
`pm_report.py` が定型の進捗レポートを出力するのに対し、本スクリプトは「なぜ遅れているか」「どのリスクが最も重大か」「次に何をすべきか」という解釈・洞察を生成する。

```sh
# 確認用（Canvas投稿なし・標準出力のみ）
python3 scripts/pm_insight.py --db data/pm.db --dry-run

# ファイルに保存して内容確認
python3 scripts/pm_insight.py --db data/pm.db --dry-run --output insight.md

# Canvas に投稿
python3 scripts/pm_insight.py --db data/pm.db --canvas-id <CANVAS_ID>

# 直近1ヶ月のデータのみ対象
python3 scripts/pm_insight.py --db data/pm.db --since 2026-02-01 --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | - | pm.db のパス（必須） |
| `--canvas-id ID` | なし | 投稿先 Canvas ID（省略時は Canvas 投稿なし） |
| `--since YYYY-MM-DD` | なし | この日付以降のデータのみ対象 |
| `--skip-canvas` | - | Canvas 投稿をスキップ |
| `--dry-run` | - | Canvas 投稿なし・結果を標準出力のみ |
| `--output PATH` | - | 結果をファイルにも保存 |
| `--no-encrypt` | - | 平文モード |
| `--model MODEL` | CLI デフォルト | 使用する Claude モデル |

**生成されるインサイトの構成**:
1. 総合評価（A/B/C/D ヘルススコア + LLM生成の日本語ナラティブ）
2. マイルストーン別評価（進捗評価・懸念事項）
3. リスク・課題（優先度 H/M/L + 推奨対応）
4. 改善提案（具体的アクション + 根拠）

**LLMに渡すデータ**:
- マイルストーン進捗（open/closed件数・期限残日数）
- 期限超過アイテム一覧（上位15件）
- 担当者別負荷（open件数・期限超過件数）
- マイルストーン未紐づけアイテム数・担当者なしアイテム数
- 週次トレンド（直近4週の作成件数 vs. 完了件数）
- 未確認決定事項一覧（上位10件）

全スクリプトに `--no-encrypt` オプションがあり、平文モードで動作させることができる。

#### 変更履歴の確認（audit_log）

Canvas同期（`pm_sync_canvas.py`）とマイルストーン紐づけ変更（`pm_relink.py`）の変更前後を記録した `audit_log` を参照する。

```sh
# 直近30件を表示
python3 scripts/db_utils.py --audit-log

# 件数を指定
python3 scripts/db_utils.py --audit-log --limit 100

# ソースで絞り込む
python3 scripts/db_utils.py --audit-log --source canvas_sync
python3 scripts/db_utils.py --audit-log --source relink

# 特定アクションアイテムの変更履歴
python3 scripts/db_utils.py --audit-log --id 98

# ファイルにも保存
python3 scripts/db_utils.py --audit-log --output audit.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--limit N` | `30` | 表示件数 |
| `--source SOURCE` | なし（全件） | `canvas_sync` または `relink` で絞り込む |
| `--id ID` | なし（全件） | アクションアイテムIDで絞り込む |
| `--no-encrypt` | - | 平文モード |
| `--output PATH` | - | 結果をファイルにも保存 |

### 11. PM DB Editor Web UI（pm_api.py）

pm.db の内容をブラウザ上で閲覧・編集できる Web UI。FastAPI + 静的フロントエンド（`scripts/static/`）を同一プロセスで配信する。アクションアイテム・決定事項の各フィールドをセル上で直接編集できる。`source` 列をクリックすると Slack リンク（Slackを新規タブで開く）または議事録ポップアップを表示する。

```sh
# 起動（バックグラウンドデーモン）
bash scripts/pm_daemon.sh start web
# → http://localhost:8501 でブラウザアクセス
# → ログ: logs/pm_web.log
# → PID: logs/pm_web.pid

# 停止
bash scripts/pm_daemon.sh stop web

# 起動状態の確認
cat logs/pm_web.pid | xargs kill -0 && echo 起動中 || echo 停止中

# ログ確認
tail -f logs/pm_web.log
```

| オプション（環境変数・引数） | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--no-encrypt` | - | 平文モード |
| `--port N` | `8501` | ポート番号 |

**機能**:
- アクションアイテム: 内容・担当者・期限・マイルストーン・状況・対応状況・削除フラグをセル編集
- 決定事項: 内容・発生日・削除フラグをセル編集
- `source` 列: `Slack` クリック → 投稿をブラウザの新規タブで開く / `minutes` クリック → 議事録をポップアップ表示
- フィルタ: status（open/closed/すべて）・マイルストーン・発生日・削除状態（非削除/削除済み/すべて）
- 楽観的排他制御: 別タブや他ユーザーが先に保存した場合はエラーを表示し上書きを防止

### 12. ドキュメントレジストリ（pm_slack_box_links.py）

Slack投稿中のBOXリンクを収集し、ローカルLLMで構造化メタデータを抽出して `docs_{index_name}.db` に保存する。情報の散逸に対処するための機能。

```sh
# 全インデックス対象に抽出
python3 scripts/pm_slack_box_links.py

# 特定インデックスのみ
python3 scripts/pm_slack_box_links.py --index-name pm

# 確認のみ（DB保存なし）
python3 scripts/pm_slack_box_links.py --dry-run

# 登録済みドキュメント一覧
python3 scripts/pm_slack_box_links.py --list

# Canvas に投稿
python3 scripts/pm_slack_box_links.py --post-to-canvas --canvas-id F0XXXXXX --index-name pm
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--index-name NAME` | 全インデックス | 特定インデックスのみ処理 |
| `--config PATH` | `data/argus_config.yaml` | 設定ファイルのパス |
| `--data-dir PATH` | `data` | ソースDBのディレクトリ |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--list` | - | 登録済みドキュメント一覧を表示して終了 |
| `--post-to-canvas` | - | ドキュメント一覧を Canvas に投稿 |
| `--canvas-id ID` | - | 投稿先 Canvas ID（`--post-to-canvas` 時に必須） |

**セキュリティ注意**: ローカルLLM（`OPENAI_API_BASE`）のみを使用。外部APIには情報を送出しない。`OPENAI_API_BASE` 未設定時はエラーで停止する。

**抽出済み管理**: `extract_state` テーブルで処理済み `thread_ts` を記録し、再実行時に重複処理を防止する。

**FTS5連携**: 抽出後に `pm_embed.py` を実行すると、`docs_{index_name}.db` のドキュメントが FTS5 インデックスに組み込まれ `/argus-investigate` で検索可能になる。

### 12a. BOXドキュメント本文取込み（pm_box_crawl.py + pm_box_relevance.py + pm_box_update.sh）

§12 がBOXリンクの**メタデータ**だけを保存するのに対し、本節は **BOXフォルダから本文を取得して Markdown 化**し、ナレッジ検索の本体として `/argus-investigate` から本文を直接ヒットさせる仕組みを扱う。

#### 全体パイプライン

```
[box_sources.yaml]
   ↓ ステップA: フォルダ走査
pm_box_crawl.py --scan
   ↓ box_files テーブル（メタデータ + folder_path + index_name）
   ↓ ステップB: 本文Markdown化
pm_box_crawl.py --convert
   ↓ doc_content テーブル（content_md + content_hash）
   ↓ ステップC: relevance 判定（任意だが推奨）
pm_box_relevance.py --judge
   ↓ box_files.relevance ∈ {core, related, noise, unknown}
   ↓ ステップD: FTS5 索引化（noise は自動除外）
pm_embed.py
   ↓ data/qa_index.db に投入（chunk_indexes 経由で index_name に紐付け）
   ↓ noise の box_files は索引対象から自動除外
   → /argus-investigate から検索可能
```

`pm_box_update.sh` は **A+B+D** を一括実行するエントリポイント（ステップ1: `pm_slack_box_links.py`、ステップ2: `pm_box_crawl.py --scan --convert`、ステップ3: `pm_embed.py`）。relevance 判定（C）は別運用。

#### ステップA+B: 走査・変換（pm_box_crawl.py）

BOX CLI 経由でフォルダを再帰走査し、対応形式（pptx/docx/pdf/xlsx/md/boxnote/txt）をローカル変換チェーンで Markdown 化して `box_docs.db` に保存する。

```sh
# 走査と変換を一括実行
python3 scripts/pm_box_crawl.py --scan --convert

# 走査のみ（メタデータだけ box_files に登録）
python3 scripts/pm_box_crawl.py --scan

# 既存登録ファイルの本文だけ抽出
python3 scripts/pm_box_crawl.py --convert

# 特定ソース・形式・ファイルを限定
python3 scripts/pm_box_crawl.py --scan --source "アプリケーション開発エリア"
python3 scripts/pm_box_crawl.py --convert --type pptx
python3 scripts/pm_box_crawl.py --convert --box-file-id 123456789

# 再変換（content_hash が変わっていなくても再処理）
python3 scripts/pm_box_crawl.py --convert --force

# 変換ロジックの単体検証（DB書き込みなし）
python3 scripts/pm_box_crawl.py --debug-convert /path/to/file.pptx

# 一覧・ファイル削除
python3 scripts/pm_box_crawl.py --list
python3 scripts/pm_box_crawl.py --remove --box-file-id 123456789
python3 scripts/pm_box_crawl.py --remove --folder-pattern "アーカイブ/*"
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--scan` | — | `box_sources.yaml` の全ソースを走査して `box_files` に登録 |
| `--convert` | — | `box_files` の未変換ファイルをダウンロード・Markdown化 |
| `--list` | — | 登録ファイル一覧を表示 |
| `--show BOX_FILE_ID` | — | 指定ファイルの本文を表示 |
| `--remove` | — | ファイルを `box_files` + `doc_content` から削除（`--box-file-id` または `--folder-pattern` と併用） |
| `--source NAME` | 全ソース | `box_sources.yaml` の `name` で絞り込み |
| `--box-file-id ID` | — | 特定ファイルのみ |
| `--folder-pattern PAT` | — | `--remove` 用 fnmatch パターン（例: `アーカイブ/*`）|
| `--type EXT` | 全形式 | `pptx` / `docx` / `pdf` / `xlsx` / `md` / `boxnote` / `txt` のみ変換 |
| `--force` | — | 変換済みも再変換 |
| `--workers N` | `2` | `--convert` の並列数 |
| `--db PATH` | `data/box_docs.db` | DBファイル |
| `--config PATH` | `data/box_sources.yaml` | ソース定義ファイル |
| `--debug-convert PATH` | — | 変換のみ単体実行（DB書き込みなし、ロジック検証用） |
| `--dry-run` / `--no-encrypt` / `--output` | — | 共通 |

**変換チェーン（形式別フォールバック）**:

| 形式 | 主経路 | フォールバック |
|---|---|---|
| md / txt | そのまま読み込み | — |
| docx | LibreOffice → HTML → Markdown | — |
| xlsx | LibreOffice → HTML → Markdown（テーブル整形） | — |
| pptx | LibreOffice → HTML → Markdown | gemma4 マルチモーダル OCR（`OPENAI_API_BASE`）|
| pdf | `pdftotext` でテキスト抽出 | テキスト無しのスキャンPDFはマルチモーダルOCR |
| boxnote | JSON 抽出（`_extract_boxnote_text`）| — |

**マルチモーダル変換**: 文字情報を持たないPPTXやスキャンPDFは ffmpeg 等で画像化したうえで `OPENAI_API_BASE` のマルチモーダルLLMに投げて Markdown 化する。`OPENAI_API_BASE` 未設定の場合はテキスト抽出のみで進む。

#### ステップC: relevance 判定（pm_box_relevance.py）

本文の冒頭をローカルLLMで読み取り、ナレッジとしての価値を 4 段階に分類する。判定結果は `box_files.relevance` に保存される。

| relevance | 用途 |
|---|---|
| `core`    | 富岳NEXTプロジェクトの本質的ナレッジ（設計資料・公式報告書・意思決定資料）|
| `related` | 関連するが本質ではない（補助資料・参考情報・過去事例）|
| `noise`   | プロジェクト外・索引化するとノイズになる（雑談添付・個人メモ）|
| `unknown` | 判定不能（情報不足）|

```sh
# 未判定のみLLM判定
python3 scripts/pm_box_relevance.py --judge

# 全件再判定 / 特定 index_name のみ
python3 scripts/pm_box_relevance.py --judge --force
python3 scripts/pm_box_relevance.py --judge --index-name pm

# CSV にエクスポート（noise を先頭に並べ、人手で final_relevance を上書き可能）
python3 scripts/pm_box_relevance.py --export --output screen.csv

# 精査後のCSVをDBに反映
python3 scripts/pm_box_relevance.py --import screen.csv

# relevance分布の集計
python3 scripts/pm_box_relevance.py --stats
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--judge` | — | 未判定 (`relevance` が空) のファイルをLLM判定 |
| `--export` | — | CSV にエクスポート（`noise` 優先表示） |
| `--import PATH` | — | CSV をインポートして `relevance` を上書き |
| `--stats` | — | relevance 分布を集計表示 |
| `--index-name NAME` | 全インデックス | 特定インデックスのみ |
| `--force` | — | `--judge` で判定済みも再判定 |
| `--output PATH` | `docs_screen.csv` | `--export` の出力先 |
| `--dry-run` / `--no-encrypt` | — | 共通 |

**FTS5 連携**: `pm_embed.py` は `box_files.relevance = 'noise'` のファイルを索引対象から除外する（`COALESCE(bf.relevance, '') != 'noise'`）。NULL/空文字のファイル（未判定）は索引対象に含まれる。

#### ステップD: 一括実行シェル（pm_box_update.sh）

ステップ1（Slackリンク）+ ステップ2（本文取込み）+ ステップ3（FTS5）を順に実行する。

```sh
# 全ソース・全インデックス
bash scripts/pm_box_update.sh

# 特定インデックスのみ
bash scripts/pm_box_update.sh --index-name pm

# 走査・変換は飛ばしてFTS5だけ更新
bash scripts/pm_box_update.sh --skip-box-content

# 本文取込みは走らせるが FTS5 は飛ばす
bash scripts/pm_box_update.sh --skip-embed

# 確認のみ
bash scripts/pm_box_update.sh --dry-run
```

| オプション | 説明 |
|---|---|
| `--index-name NAME` | 特定インデックス（pm / pm-hpc / pm-pmo） |
| `-c CHANNEL_ID` | ステップ1（pm_slack_box_links.py）のチャンネルを限定 |
| `--since YYYY-MM-DD` | ステップ1 の対象日付 |
| `--force` | 抽出済み・変換済みも再処理 |
| `--full-rebuild` | ステップ3 で FTS5 を全件再構築 |
| `--skip-box-content` | ステップ2（pm_box_crawl.py）をスキップ |
| `--skip-embed` | ステップ3（pm_embed.py）をスキップ |
| `--dry-run` | 全ステップで DB保存なし |

**ログ**: `logs/pm_box_update.log` に追記。

#### box_sources.yaml の構造

```yaml
sources:
  - name: "20_アプリケーション開発ユニット"
    folder_id: "321131927032"        # BOX Web UI URL の末尾から取得
    index_names: [pm, pm-all]        # 投入先の論理 index 名（qa_index.db.chunk_indexes に登録）
    recursive: true
    extensions: [pptx, docx, pdf, md]
    max_file_size_mb: 50
    enabled: true
    exclude_folders: ["*_議事", "*/old*", "*Application_analysis*"]
    exclude_patterns: ["*_draft*", "~$*", "会議案内/*"]
```

| フィールド | 必須 | 説明 |
|---|---|---|
| `name` | ✓ | 表示名・`--source` でのフィルタキー |
| `folder_id` | ✓ | BOX フォルダ ID（数値文字列）|
| `index_names` | ✓ | 投入先インデックス名のリスト（複数指定可）|
| `recursive` | — | サブフォルダも走査するか（デフォルト `true`） |
| `extensions` | — | 対象拡張子リスト（未指定なら全 `SUPPORTED_EXTENSIONS`）|
| `max_file_size_mb` | — | 上限サイズ（超過ファイルはスキップ）|
| `enabled` | — | `false` なら走査対象外 |
| `exclude_folders` | — | フォルダパスの fnmatch パターン（マッチしたフォルダ配下を除外）|
| `exclude_patterns` | — | ファイル名の fnmatch パターン |

#### box_docs.db のスキーマ

詳細は `docs/schema.md` の「data/box_docs.db」セクションを参照。要点:

- `box_files`: メタデータ（`box_file_id` UNIQUE、`folder_path`・`index_name`・`source_name` 等）+ `pm_box_relevance.py` が埋める `relevance` / `relevance_reason` / `relevance_judged_at`
- `doc_content`: 本文 Markdown（`box_file_id` UNIQUE、`content_md`・`content_hash`・`convert_method` 等）
- `box_files` と `doc_content` は `box_file_id` で 1:1。`--remove` 時は両テーブルから削除される

#### 運用フロー（推奨）

```
1. box_sources.yaml にフォルダ追加
2. bash scripts/pm_box_update.sh                      # 走査・変換・FTS5更新
3. python3 scripts/pm_box_relevance.py --judge        # 関連度判定（未判定のみ）
4. python3 scripts/pm_box_relevance.py --stats        # noise が多すぎないか確認
5. python3 scripts/pm_box_relevance.py --export       # 不安なら CSV で精査
6. （必要なら）CSV を編集 → --import で反映
7. bash scripts/pm_box_update.sh --skip-box-content   # FTS5 のみ再構築（noise 除外を反映）
```

定期運用は `pm_box_update.sh` を crontab に登録、relevance 判定は新規追加時のみ実行する形が現実的。

### 13. 外部Web情報取得（pm_web_fetch.py）

RIKEN公式サイト・HPCニュースサイト・NVIDIAブログなどの外部公開情報を取得し `data/web_articles.db` に保存する。
取得対象・キーワードフィルタ・対象インデックスは `data/web_sources.yaml` で定義する。
FTS5インデックスへの組み込みは `pm_box_update.sh`（`pm_embed.py`）が自動的に行う。

```sh
# 全ソースの差分取得（新規URLのみ保存）
python3 scripts/pm_web_fetch.py

# 特定ソースのみ
python3 scripts/pm_web_fetch.py --source "Top500"

# 保存せず件数確認
python3 scripts/pm_web_fetch.py --dry-run

# 全件再取得（既存URLも上書き）
python3 scripts/pm_web_fetch.py --full-refetch

# 保存済み記事一覧
python3 scripts/pm_web_fetch.py --list
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--source NAME` | 全ソース | `web_sources.yaml` の name 値で特定ソースのみ処理 |
| `--dry-run` | - | DB保存なし・件数確認のみ |
| `--full-refetch` | - | 全件再取得（既存URLも上書き） |
| `--list` | - | 保存済み記事一覧を表示して終了 |
| `--index-name NAME` | - | `--list` 時のインデックスフィルタ |
| `--config PATH` | `data/web_sources.yaml` | ソース定義ファイルのパス |
| `--data-dir PATH` | `data` | データディレクトリのパス |

**cron設定（毎朝03:30 JST）**:
```sh
crontab -e
# 以下を追加:
# 30 3 * * * cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement && ~/.venv_aarch64/bin/python3 scripts/pm_web_fetch.py >> logs/pm_web_cron.log 2>&1
```

**FTS5連携**: `web_articles.db` が存在すれば `pm_embed.py`（`pm_box_update.sh` 経由）実行時に自動で FTS5 インデックスに組み込まれ `/argus-investigate` で検索可能になる。

**web_sources.yaml の構造**:
```yaml
sources:
  - name: "Top500"
    url: "https://top500.org/news/feed/"
    type: rss                          # "rss" または "html_index"
    keywords: [Fugaku, RIKEN, HPC]    # いずれか1語を含む記事のみ保存
    max_articles: 50                   # 1回の実行で最大何件保存するか
    target_indices: [pm, pm-hpc]       # 組み込む論理 index 名（qa_index.db.chunk_indexes に登録）
    enabled: true
```

### 14. エンリッチメント（enrich_items.py） — Pass 2

pm.db の既存 `decisions` / `action_items` に対し、過去ナレッジを参照して **判断者・根拠・関連ID** を補完する。
2パスアーキテクチャの Pass 2 に相当する（Pass 1 は `pm_ingest.py` による抽出）。全体像は `docs/architecture.md` 参照。

```sh
# dry-run（DB更新なし・結果を標準出力）
python3 scripts/enrich/enrich_items.py --dry-run --since 2026-03-01

# 特定IDのみ（d:{decision_id} / a:{action_item_id}）
python3 scripts/enrich/enrich_items.py --id d:42 a:15 --dry-run

# 全件エンリッチ実行
python3 scripts/enrich/enrich_items.py --since 2026-03-01
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし | この日付以降のアイテムのみ対象 |
| `--id ID...` | なし | 特定アイテムのみ処理（`d:42` = decision id=42, `a:15` = action_item id=15）|
| `--dry-run` | - | DB保存なし・結果を標準出力 |
| `--output PATH` | - | 結果をファイルにも保存 |
| `--config PATH` | `data/argus_config.yaml` | FTS5インデックス設定 |
| `--no-encrypt` | - | 平文モード |

**付与されるフィールド**:
- **decisions**: `decided_by` / `decided_by_confidence` / `rationale` / `related_ids`
- **action_items**: `requested_by` / `requested_by_confidence` / `rationale` / `source_context` / `related_ids`

**ナレッジソース**（`knowledge_context.py` が取得）:
- pm.db 構造化データ（直近の decisions / action_items）
- FTS5 全文検索（議事録・Slack・ドキュメント・Web記事）
- 参加者パターン（誰がよく発言しているか）

`knowledge_context.py` は共通ライブラリ（単体実行なし、import して使用）。

### 15. データ品質スクリーニング（pm_screen.py）

pm.db のアクションアイテム・決定事項から重複・類似・曖昧アイテムを検出する。`pm_relink.py --import` 互換CSVで出力するため、`deleted=1` を立てて一括削除できる。

**検出カテゴリ**:
- `exact_dup` — 正規化後に完全一致する重複
- `near_dup` — 先頭N文字が一致し内容が微妙に異なる類似重複
- `ambiguous` — 短すぎて文脈なしでは意味が類推できないもの

```sh
# スクリーニング結果を画面表示
python3 scripts/pm_screen.py

# CSV にエクスポート（pm_relink.py --import で編集可能）
python3 scripts/pm_screen.py --export

# 出力先を指定
python3 scripts/pm_screen.py --export --output screen.csv

# 閾値調整
python3 scripts/pm_screen.py --short-threshold 25 --prefix-len 20

# 決定事項も対象に含める
python3 scripts/pm_screen.py --include-decisions
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--export` | - | CSV出力モード |
| `--output PATH` | `screen.csv` | CSV出力先 |
| `--short-threshold N` | - | ambiguous と判定する文字数閾値 |
| `--prefix-len N` | - | near_dup 判定の先頭一致文字数 |
| `--include-decisions` | - | 決定事項もスクリーニング対象に含める |
| `--no-encrypt` | - | 平文モード |

**運用フロー**:
1. `pm_screen.py --export` で重複候補をCSV出力
2. CSVを人間が確認し、削除すべき行に `deleted=1` を立てる
3. `pm_relink.py --import screen.csv` で一括削除を反映

### 16. ナレッジ蒸留（pm_box_distill.py） — Pass 3

`box_docs.db` 本文・`data/minutes/{kind}.db`・`pm.db.decisions` を入力として、
ローカル LLM (gemma4) で「**意思決定 / 制約 / 立場 / 用語**」の単位に蒸留し、
`bge-m3` 埋め込みによる類似度判定 + LLM (Kimi) の品質審査の二段ゲートを通して
`data/knowledge.db` に書き込む。設計原則・採否基準は `docs/distill_policy.md` 参照。

```sh
# 全ソース・差分のみ蒸留（distill_state で冪等管理。Stage 2 がデフォルト ON）
python3 scripts/pm_box_distill.py

# ソースを限定
python3 scripts/pm_box_distill.py --source box
python3 scripts/pm_box_distill.py --source minutes
python3 scripts/pm_box_distill.py --source decisions

# 期間指定
python3 scripts/pm_box_distill.py --since 2026-04-01

# 確認のみ（LLM 呼び出し・DB保存なし）
python3 scripts/pm_box_distill.py --dry-run

# distill_state を無視して再蒸留
python3 scripts/pm_box_distill.py --force

# Stage 2 (類似度判定 + LLM 品質審査) を無効化（デバッグ用、段階1のみ）
python3 scripts/pm_box_distill.py --no-two-stage

# 統計表示（kind / confidence / status 内訳。500 超で警告）
python3 scripts/pm_box_distill.py --stats

# 既存レコードに埋め込みを後追い計算（Stage 2 を有効化する前 / 単体運用）
python3 scripts/pm_box_distill.py --embed-backfill

# 既存レコードに対して LLM 品質審査のみ実行（後追いパージ）
#   1. プラン CSV を生成（DB変更なし）
python3 scripts/pm_box_distill.py --quality-only --quality-plan data/quality_plan.csv
#   2. CSV を人手確認（drop / merge_target が妥当か）
#   3. 適用
python3 scripts/pm_box_distill.py --quality-only --apply
#   スモークテスト（先頭 N 件のみ）
python3 scripts/pm_box_distill.py --quality-only --limit 10 --quality-plan /tmp/test.csv
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--source {box,minutes,decisions,all}` | `all` | 蒸留対象ソース |
| `--since YYYY-MM-DD` | なし | この日付以降のみ対象 |
| `--force` | — | `distill_state` を無視して再蒸留 |
| `--dry-run` | — | LLM 呼び出し・DB保存なし、件数のみ表示 |
| `--stats` | — | knowledge.db の統計を表示して終了 |
| `--no-two-stage` | — | Stage 2 (類似度判定 + LLM 品質審査) を無効化 |
| `--merge-threshold N` | `0.92` | 自動 merge のコサイン類似度しきい値 |
| `--review-threshold N` | `0.85` | LLM 審査対象のコサイン類似度しきい値 |
| `--embed-backfill` | — | 既存レコードに埋め込みを後追い計算（蒸留はしない） |
| `--quality-only` | — | 既存レコードに対して LLM 品質審査のみ実行 |
| `--quality-plan PATH` | — | `--quality-only` 時のプラン CSV 出力先 |
| `--apply` | — | `--quality-only` 時に DB へ変更を適用 |
| `--limit N` | — | `--quality-only` 時に先頭 N 件のみ処理 |
| `--no-encrypt` | — | 平文モード |
| `--output PATH` | — | ログをファイルにも保存 |

**採否ポリシー**:
- `confidence='low'` のレコードは書き込まない
- Stage 2 で `verdict=drop` と判定された候補は `distill_state.status='quality_dropped'` を記録
- 類似度 ≥ `--merge-threshold` の候補は新規追加せず、既存 `KN-XXXX` の `knowledge_sources` に
  当該ソースを追加するだけ（補強情報として `weight='supporting'`）
- LLM 判定が失敗 / 未応答の候補は保守的に drop
- 物理削除はしない（人手介入時も `deleted=1` のみ）

**環境変数**:
```sh
EMBED_API_BASE=$RIVAULT_URL    # 省略時は RIVAULT_URL を流用
EMBED_API_KEY=$RIVAULT_TOKEN   # 省略時は RIVAULT_TOKEN
EMBED_MODEL=bge-m3:567m        # 省略時のデフォルト（RiVault が提供）
```

### 16a. ナレッジ重複・多重抽出の診断（pm_knowledge_inspect.py）

knowledge.db の件数が想定を超えたとき (例: 500 件超) に最初に走らせる読み取り専用ツール。
蒸留プロンプトの粒度を見直す判断材料を出す。

```sh
# 全 5 検査を実行
python3 scripts/pm_knowledge_inspect.py

# 上位件数を変更
python3 scripts/pm_knowledge_inspect.py --top 50

# 一部だけ
python3 scripts/pm_knowledge_inspect.py --skip-source --skip-near
```

検査内容:
1. **1 source_ref から派生したナレッジ数**（多い順）— BOX ファイル名・フォルダパスも併記
2. **topic 完全一致**の重複
3. **topic 先頭 N 文字一致**の重複
4. **current_state 正規化一致**（空白除去・小文字化後一致）
5. **同じ source_ref 内で current_state 先頭一致**（1 ファイル多重抽出のうち特に冗長な組）

| オプション | デフォルト | 説明 |
|---|---|---|
| `--top N` | `30` | (1) で表示する上位件数 |
| `--topic-prefix N` | `20` | (3) の topic 先頭一致文字数 |
| `--cs-prefix N` | `30` | (5) の current_state 先頭一致文字数 |
| `--skip-source` / `--skip-topic` / `--skip-cs` / `--skip-near` | — | 個別検査をスキップ |

### 16b. ナレッジ後追い dedupe（pm_knowledge_dedupe.py）

`current_state` 完全一致でグループ化し、各グループから keeper を 1 件残して他を
`superseded_by` で連鎖させる。`Stage 2` の embedding ベース dedupe を導入する前の
ベースライン整理に使う（蒸留時の重複は今後 Stage 2 が防ぐので、このスクリプトは
段階3導入前の既存データに対してのみ使う想定）。

keeper 選定: confidence(high優先) → last_validated_at 降順 → created_at 昇順 → KN-XXXX 番号小さい順。

```sh
# まずプラン CSV を出して目で確認（DB変更なし）
python3 scripts/pm_knowledge_dedupe.py --plan dedupe_plan.csv

# CSV を確認後、そのまま適用（プラン未指定時は内部生成プランを使う）
python3 scripts/pm_knowledge_dedupe.py --apply

# CSV を編集して keeper を変えたい場合
python3 scripts/pm_knowledge_dedupe.py --apply --plan dedupe_plan.csv --actor "井上"

# 一致モード切り替え（topic + current_state ペア一致）
python3 scripts/pm_knowledge_dedupe.py --plan plan.csv --mode topic
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--mode {current_state,topic}` | `current_state` | 重複検出のキー |
| `--plan PATH` | — | プラン CSV のパス（書き出し / 読み込み） |
| `--apply` | — | DB へ変更を適用 |
| `--actor NAME` | `dedupe` | audit の actor 列 |

挙動:
- 各 loser の `knowledge_sources` を keeper にコピー（`weight='supporting'` 化）
- loser に `superseded_by = keeper` を立て、`knowledge_relations.supersedes` に
  `note='auto-dedupe'` 付きで登録
- `knowledge_audit` に `source='merge'`・`actor=<指定値>` で記録
- 物理削除はしない

### 17. ナレッジ人手編集（pm_knowledge_edit.py）

普段使わない前提の人手介入 CLI。`/argus-investigate` などで間違いに気付いたときに最小手数で
修正できるよう、即時操作と CSV 一括編集の両方を提供する。詳細は `docs/distill_policy.md` 参照。

```sh
# 一覧（非削除のみ） / 削除済みも含める
python3 scripts/pm_knowledge_edit.py --list
python3 scripts/pm_knowledge_edit.py --list --include-deleted

# 1件詳細（sources / relations / audit_log を含む）
python3 scripts/pm_knowledge_edit.py --show KN-0042

# 即時操作
python3 scripts/pm_knowledge_edit.py --invalidate KN-0042             # deleted=1
python3 scripts/pm_knowledge_edit.py --restore KN-0042                # deleted=0
python3 scripts/pm_knowledge_edit.py --supersede KN-0042 KN-0099      # 上書き連鎖
python3 scripts/pm_knowledge_edit.py --confidence KN-0042 low

# CSV 一括編集
python3 scripts/pm_knowledge_edit.py --export                          # knowledge_edit.csv
python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv --dry-run
python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv --actor "井上"
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--list` | — | レコード一覧 |
| `--show KN-XXXX` | — | 詳細表示 |
| `--invalidate KN-XXXX` | — | `deleted=1`（論理削除） |
| `--restore KN-XXXX` | — | `deleted=0`（復活） |
| `--supersede OLD NEW` | — | `OLD.superseded_by = NEW` を立てて `knowledge_relations.supersedes` も登録 |
| `--confidence ID LEVEL` | — | `confidence` を `high` / `medium` / `low` に変更 |
| `--export` | — | CSV 一括編集用にエクスポート |
| `--import PATH` | — | CSV を読み込んで `knowledge.db` に反映 |
| `--include-deleted` | — | `--list` / `--export` で削除済みも含める |
| `--actor NAME` | — | 変更者名（`knowledge_audit.actor` に記録） |
| `--dry-run` | — | `--import` 時に DB 更新せず変更内容のみ表示 |
| `--no-encrypt` | — | 平文モード |

全変更は `knowledge_audit` に `source='human_edit'` で記録される。`--supersede` は
`knowledge_relations` の `supersedes` 行も自動で作る。物理削除はサポートしない。
