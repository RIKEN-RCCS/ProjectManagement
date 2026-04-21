## 主なコマンド

### 1. Slack差分取得（slack_pipeline.py）

```sh
# 通常運用: 差分のみ取得
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db

# 初回・過去分の取り込み（oldest をAPIに渡してページネーション全件取得）
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db \
    --since 2025-04-01

# DB内のスレッド一覧を表示
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --list
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db PATH` | `data/{channel_id}.db` | SQLite DBファイルパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のメッセージのみ取得（APIに oldest として渡す） |
| `-l N` | `100` | 1ページあたりの取得件数上限（最大999） |
| `--skip-fetch` | - | Slack API取得をスキップ（DBのみ使用） |
| `--list` | - | DB内のスレッド一覧を表示して終了（`--since` 併用可） |
| `--no-permalink` | - | パーマリンク取得を無効化 |
| `--dry-run` | - | Slack API取得のみ実行（DB書き込みなし） |

### 1b. Slack取得→pm.db抽出 一括実行（pm_from_slack.sh）

`slack_pipeline.py`（取得）→ `pm_extractor.py`（pm.db抽出）を連続実行する。

```sh
# 通常運用
bash scripts/pm_from_slack.sh -c C08SXA4M7JT

# 日付フィルタ付き
bash scripts/pm_from_slack.sh -c C08SXA4M7JT --since 2026-01-01

# 確認用（DB保存なし）
bash scripts/pm_from_slack.sh -c C08SXA4M7JT --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID（両スクリプトに渡す） |
| `--since YYYY-MM-DD` | なし | この日付以降のみ対象（両スクリプトに渡す） |
| `--dry-run` | - | DB保存なし・確認のみ（両スクリプトに渡す） |
| `--no-encrypt` | - | 平文モード（両スクリプトに渡す） |
| `--db-slack PATH` | `data/{channel_id}.db` | Slack DBパス |
| `--db-pm PATH` | `data/pm.db` | pm.db パス |
| `--skip-fetch` | - | Slack API取得をスキップ（`slack_pipeline.py` のみ） |
| `--force-reextract` | - | 抽出済みスレッドも再処理（`pm_extractor.py` のみ） |

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

処理フロー: ffmpeg → WAV変換（16kHz, mono） → DeepFilterNetノイズ除去 → SileroVAD → pyannote話者分離 → Whisper large-v3 文字起こし

`--meeting-name` 指定時の追加フロー: 文字起こし完了 → `pm_minutes_import.py` で議事録DBに保存 → `pm_minutes_to_pm.py` で pm.db に転記 → .md 削除

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
    --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 -c C08SXA4M7JT

# 特定スレッドにアップロード（スレッドに集約、Files タブには表示されない）
python3 scripts/pm_minutes_import.py \
    --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \
    -c C08SXA4M7JT --thread-ts 1741234567.123456
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

### 3b. 議事録DB → pm.db 転記（pm_minutes_to_pm.py）

`pm_minutes_import.py` で作成した議事録DBの内容を **LLM不使用** で pm.db に転記する。
担当者・期限は議事録DBから直接コピーされる。milestone_id のみ Canvas または `pm_relink.py` で補完する。

```sh
# 全会議名の議事録DBを pm.db に転記
python3 scripts/pm_minutes_to_pm.py

# 特定会議名のみ転記
python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting

# 日付フィルタを付けて転記
python3 scripts/pm_minutes_to_pm.py --since 2026-01-01

# 確認用（DB保存なし）
python3 scripts/pm_minutes_to_pm.py --dry-run

# 既存レコードを上書き
python3 scripts/pm_minutes_to_pm.py --meeting-name Leader_Meeting --force

# pm.db から削除
python3 scripts/pm_minutes_to_pm.py --delete 2026-03-10_Leader_Meeting
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--meeting-name NAME` | 全DBを対象 | 特定の会議名のみ処理 |
| `--minutes-dir DIR` | `data/minutes/` | 議事録DBのディレクトリ |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし | この日付以降の会議のみ転記 |
| `--force` | - | 既存レコードを上書き |
| `--dry-run` | - | DB保存なし・転記内容を表示のみ |
| `--no-encrypt` | - | 平文モード |
| `--delete MEETING_ID` | - | 指定した meeting_id を pm.db から削除して終了 |

### 4. Slack生メッセージ → pm.db（pm_extractor.py）

```sh
# 通常運用: 未処理スレッドのみ抽出
python3 scripts/pm_extractor.py -c C08SXA4M7JT

# 確認用（DB保存なし）
python3 scripts/pm_extractor.py -c C08SXA4M7JT --dry-run --output result.txt

# 抽出済みスレッドの一覧表示
python3 scripts/pm_extractor.py -c C08SXA4M7JT --list
python3 scripts/pm_extractor.py -c C08SXA4M7JT --list --since 2026-02-01
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db-slack PATH` | `data/{channel_id}.db` | Slack DBのパス |
| `--db-pm PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のスレッドのみ対象 |
| `--force-reextract` | - | 抽出済みスレッドも再処理 |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 標準出力の内容をファイルにも保存 |
| `--list` | - | 抽出済みスレッドの一覧を表示して終了（`--since` 併用可） |

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
| `--canvas-id ID` | `F0AAD2494VB` | 投稿先 Canvas ID |
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
| `--canvas-id ID` | `F0AAD2494VB` | 対象 Canvas ID |
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

### 8. ゴール・マイルストーン同期（pm_goals_import.py）

`goals.yaml` を読み込み `goals` / `milestones` テーブルに完全同期する。

```sh
# goals.yaml を編集・承認後に実行（完全同期）
python3 scripts/pm_goals_import.py

# 変更内容を確認してから実行
python3 scripts/pm_goals_import.py --dry-run

# 登録済み一覧・達成状況を確認
python3 scripts/pm_goals_import.py --list

# 一覧をファイルにも保存
python3 scripts/pm_goals_import.py --list --output goals_status.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--goals-file PATH` | `goals.yaml` | goals.yaml のパス |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--dry-run` | - | DB保存なし・内容を表示のみ |
| `--list` | - | 登録済みゴール・マイルストーン一覧と達成状況を表示して終了 |
| `--no-encrypt` | - | DBを暗号化しない（平文モード） |
| `--output PATH` | - | 出力をファイルにも保存（`--list` / 通常インポート時の標準出力を保存） |

### 9. DBユーティリティ（db_utils.py）

#### 暗号化・鍵管理

```sh
# 鍵を生成（初回のみ）
python3 scripts/db_utils.py --gen-key
# → ~/.secrets/pm_db_key.txt に 64文字のランダム鍵を生成（chmod 600）

# 既存の平文DBを暗号化DBに変換（初回のみ）
python3 scripts/db_utils.py --migrate data/pm.db data/C08SXA4M7JT.db data/C0A9KG036CS.db
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
python3 scripts/pm_insight.py --db data/pm.db --canvas-id F0ALP1XQJHL

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

### 11. PM DB Editor Web UI（pm_web.py）

pm.db の内容をブラウザ上で閲覧・編集できる Web UI。NiceGUI + AG Grid を使用。アクションアイテム・決定事項の各フィールドをセル上で直接編集できる。`source` 列をクリックすると Slack リンク（Slackを新規タブで開く）または議事録ポップアップを表示する。

```sh
# 起動（バックグラウンドデーモン）
bash scripts/pm_web_start.sh
# → http://localhost:8501 でブラウザアクセス
# → ログ: logs/pm_web.log
# → PID: logs/pm_web.pid

# 停止
bash scripts/pm_web_stop.sh

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

### 12. ドキュメントレジストリ（pm_document_extract.py）

Slack投稿中のBOXリンクを収集し、ローカルLLMで構造化メタデータを抽出して `docs_{index_name}.db` に保存する。情報の散逸に対処するための機能。

```sh
# 全インデックス対象に抽出
python3 scripts/pm_document_extract.py

# 特定インデックスのみ
python3 scripts/pm_document_extract.py --index-name pm

# 確認のみ（DB保存なし）
python3 scripts/pm_document_extract.py --dry-run

# 登録済みドキュメント一覧
python3 scripts/pm_document_extract.py --list
python3 scripts/pm_document_extract.py --list --index-name pm-bmt

# Canvas に投稿
python3 scripts/pm_document_extract.py --post-to-canvas --canvas-id F0XXXXXX --index-name pm
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--index-name NAME` | 全インデックス | 特定インデックスのみ処理 |
| `--config PATH` | `data/qa_config.yaml` | 設定ファイルのパス |
| `--data-dir PATH` | `data` | ソースDBのディレクトリ |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--list` | - | 登録済みドキュメント一覧を表示して終了 |
| `--post-to-canvas` | - | ドキュメント一覧を Canvas に投稿 |
| `--canvas-id ID` | - | 投稿先 Canvas ID（`--post-to-canvas` 時に必須） |

**セキュリティ注意**: ローカルLLM（`OPENAI_API_BASE`）のみを使用。外部APIには情報を送出しない。`OPENAI_API_BASE` 未設定時はエラーで停止する。

**抽出済み管理**: `extract_state` テーブルで処理済み `thread_ts` を記録し、再実行時に重複処理を防止する。

**FTS5連携**: 抽出後に `pm_embed.py` を実行すると、`docs_{index_name}.db` のドキュメントが FTS5 インデックスに組み込まれ `/argus-ask` で検索可能になる。

### 13. ハイブリッド検索テスト（pm_qa_server.py --test-hybrid）

`/argus-ask` のハイブリッド検索をSlackデーモン不要でCLIテストする。

```sh
# 構造化クエリ
python3 scripts/pm_qa_server.py --test-hybrid "西澤さんの担当タスクは？"
python3 scripts/pm_qa_server.py --test-hybrid "M1マイルストーンの進捗は？"
python3 scripts/pm_qa_server.py --test-hybrid "期限超過アイテムは？"

# テキスト検索（既存動作）
python3 scripts/pm_qa_server.py --test-hybrid "設計方針について"

# ハイブリッド検索
python3 scripts/pm_qa_server.py --test-hybrid "GPU性能に関する決定事項は？"
```

出力: Intent分類結果 → 構造化クエリ結果 → FTS検索結果 → LLM回答 を順に表示する。

### 14. 外部Web情報取得（pm_web_fetch.py）

RIKEN公式サイト・HPCニュースサイト・NVIDIAブログなどの外部公開情報を取得し `data/web_articles.db` に保存する。
取得対象・キーワードフィルタ・対象インデックスは `data/web_sources.yaml` で定義する。
FTS5インデックスへの組み込みは `pm_document_update.sh`（`pm_embed.py`）が自動的に行う。

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

**FTS5連携**: `web_articles.db` が存在すれば `pm_embed.py`（`pm_document_update.sh` 経由）実行時に自動で FTS5 インデックスに組み込まれ `/argus-ask` で検索可能になる。

**web_sources.yaml の構造**:
```yaml
sources:
  - name: "Top500"
    url: "https://top500.org/news/feed/"
    type: rss                          # "rss" または "html_index"
    keywords: [Fugaku, RIKEN, HPC]    # いずれか1語を含む記事のみ保存
    max_articles: 50                   # 1回の実行で最大何件保存するか
    target_indices: [pm, pm-hpc]       # 組み込む qa_pm*.db インデックス名
    enabled: true
```
