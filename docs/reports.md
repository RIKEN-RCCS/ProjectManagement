# レポート系コマンド

pm.db のデータからレポートを生成する4系統のコマンドをまとめる。

| コマンド | 用途 | 出力先 | LLM |
|---|---|---|---|
| `pm_report.py` | 定型の進捗レポート（マイルストーン・期限超過・未完了AI・直近決定）| Slack Canvas + 標準出力 | 不使用 |
| `pm_biweekly_report.py` | 隔週進捗レポート（pptx + Markdown）| pptx ファイル + Markdown | 任意（`--no-llm` で OFF）|
| `pm_insight.py` | LLM による健全性評価・リスク特定・改善提案 | Slack Canvas + 標準出力 | 必須 |
| `canvas_report.sh` | `pm_sync_canvas.py` → `pm_report.py` の連続実行 | Slack Canvas | 不使用 |

すべて `--filter` で `argus_config.yaml` の `filter_presets` を使ったチャンネル・議事録絞り込みに対応する（`pm_insight.py` は未対応）。

---

## 共通: `--filter` プリセット

`data/argus_config.yaml` の `filter_presets` に定義されたプリセット名を `--filter` に渡すと、
`action_items.channel_id` / `decisions.channel_id` / `meetings.kind` で AI・決定事項を絞り込む。
複数指定すると OR 結合される。

```sh
# リーダー会議系チャンネル + リーダー会議の議事録のみ
--filter "リーダー会議系" --filter "リーダー会議"

# HPC アプリ WG だけ
--filter "HPCアプリケーションWG系" --filter "HPCアプリケーションWG"
```

定義済みプリセット（`filter_presets.channels` / `filter_presets.meeting_kinds`）:

| カテゴリ | 主なプリセット名 |
|---|---|
| `channels` | `リーダー会議系` / `HPCアプリケーションWG系` / `ベンチマークWG系` / `アプリケーション開発エリア系` / `アーキテクチャエリア` / `システムソフトエリア` / `運用技術エリア` / `PMO/管理` |
| `meeting_kinds` | `リーダー会議` / `HPCアプリケーションWG` / `ベンチマークWG` / `アプリケーション開発エリア` |

未指定（`--filter` なし）の場合は **pm.db 全体**が対象になる（旧運用と同じ）。

---

## 1. pm_report.py — 進捗レポート

レポート構成: **プロジェクトの現在地 → 要注意事項 → 直近の決定事項 → 未完了アクションアイテム（→ 担当者別負荷）**

```sh
# 全体レポート → Canvas 投稿
python3 scripts/pm_report.py

# 確認用（Canvas 投稿なし、ファイルにも保存）
python3 scripts/pm_report.py --dry-run --output report.md

# 直近1ヶ月のみ
python3 scripts/pm_report.py --since 2026-04-21

# リーダー会議系のみに絞る
python3 scripts/pm_report.py --filter "リーダー会議系" --filter "リーダー会議"

# HPC アプリ WG 用に Canvas を分けて投稿
python3 scripts/pm_report.py \
    --canvas-id F0XXXXXX \
    --filter "HPCアプリケーションWG系" --filter "HPCアプリケーションWG"
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス（一本化されたため指定不要） |
| `--canvas-id ID` | `<CANVAS_ID>` | 投稿先 Canvas ID |
| `--since YYYY-MM-DD` | 全期間 | この日付以降のデータのみ対象 |
| `--filter PRESET` | — | `filter_presets` のプリセット名（複数指定可） |
| `--show-acknowledged` | — | 確認済み決定事項も表示 |
| `--show-workload` | — | 担当者別負荷セクションを出力 |
| `--skip-canvas` | — | Canvas 投稿をスキップ |
| `--dry-run` | — | Canvas 投稿なし・標準出力のみ |
| `--output PATH` | — | 出力をファイルにも保存 |
| `--no-encrypt` | — | 平文モード |

**未完了 AI 表の列**: ID・担当者・期限・マイルストーン・状況・内容・出典・対応状況。会議中に Canvas 上で
直接記入できる（`pm_sync_canvas.py` で pm.db に反映）。

---

## 2. pm_biweekly_report.py — 隔週レポート（pptx）

直近2週間（`--since` 〜 `--until`）の活動を pptx に整形して `reports/` に出力する。
LLM ナラティブ（`--no-llm` で無効化可）と次の14日以内に期限を迎える AI 一覧（`--lookahead-days`）を含む。

```sh
# 直近14日のレポートを reports/biweekly_<until>.pptx に生成
python3 scripts/pm_biweekly_report.py

# 期間指定
python3 scripts/pm_biweekly_report.py --since 2026-05-01 --until 2026-05-14

# Markdown だけ標準出力（pptx 生成なし）
python3 scripts/pm_biweekly_report.py --dry-run

# Markdown を別ファイルに保存
python3 scripts/pm_biweekly_report.py --markdown-only --md-output report.md

# LLM ナラティブを生成しない
python3 scripts/pm_biweekly_report.py --no-llm

# リーダー会議系だけのレポート
python3 scripts/pm_biweekly_report.py \
    --filter "リーダー会議系" --filter "リーダー会議"
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--since YYYY-MM-DD` | 14日前 | 期間開始 |
| `--until YYYY-MM-DD` | 今日 | 期間終了 |
| `--filter PRESET` | — | `filter_presets` のプリセット名（複数指定可） |
| `--index-name NAME` | `pm` | `argus_config.yaml` の index 名（参考情報の取得対象） |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--knowledge-db PATH` | `data/knowledge.db` | knowledge.db のパス |
| `--box-db PATH` | `data/box_docs.db` | box_docs.db のパス |
| `--slack-db PATH` | `data/slack.db` | slack.db のパス |
| `--config PATH` | `data/argus_config.yaml` | 設定ファイル |
| `--output PATH` | `reports/biweekly_<until>.pptx` | pptx 出力先 |
| `--markdown-only` | — | pptx を生成せず Markdown のみ出力 |
| `--md-output PATH` | stdout | Markdown 出力先 |
| `--no-llm` | — | LLM ナラティブを生成しない |
| `--lookahead-days N` | `14` | 次 N 日以内に期限の AI を集める |
| `--dry-run` | — | ファイル出力せず標準出力に Markdown |
| `--no-encrypt` | — | 平文モード |

---

## 3. pm_insight.py — LLM インサイト

pm.db を統計集計し、LLM で **総合評価（A/B/C/D）→ マイルストーン別評価 → リスク・課題 → 改善提案** を生成する。
`pm_report.py` が定型レポートなのに対し、本コマンドは「なぜ遅れているか」「次に何をすべきか」を生成する。

```sh
# 確認用（Canvas 投稿なし）
python3 scripts/pm_insight.py --db data/pm.db --dry-run

# Canvas 投稿
python3 scripts/pm_insight.py --db data/pm.db --canvas-id <CANVAS_ID>

# 直近1ヶ月のみ
python3 scripts/pm_insight.py --db data/pm.db --since 2026-04-21 --dry-run --output insight.md
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | — | pm.db のパス（必須） |
| `--canvas-id ID` | — | 投稿先 Canvas ID（省略時は Canvas 投稿なし） |
| `--since YYYY-MM-DD` | — | この日付以降のデータのみ対象 |
| `--skip-canvas` | — | Canvas 投稿をスキップ |
| `--model MODEL` | CLI デフォルト | 使用するモデル |
| `--dry-run` | — | Canvas 投稿なし・標準出力のみ |
| `--output PATH` | — | 結果をファイルにも保存 |
| `--no-encrypt` | — | 平文モード |

**LLM に渡すデータ**: マイルストーン進捗・期限超過 AI 上位15件・担当者別負荷・未紐づけ件数・
週次トレンド（直近4週）・未確認決定事項上位10件。

`--filter` は未対応（pm.db 全体での評価を意図しているため）。範囲を絞りたい場合は `--since` を使う。

---

## 4. canvas_report.sh — Canvas 同期 + 進捗レポート

`pm_sync_canvas.py`（Canvas → pm.db 同期）→ `pm_report.py`（pm.db → Canvas 投稿）を順次実行する。
**Canvas 上の編集を pm.db に取り込んでから上書き投稿**するため、会議中の編集を失わない。

```sh
# 通常運用
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID>

# 期間指定
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID> --since 2026-04-01

# Canvas 同期だけスキップ（pm_report.py のみ）
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID> --skip-sync

# 確認のみ
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID> --dry-run
```

| オプション | 必須 | 説明 |
|---|---|---|
| `--db PATH` | ✓ | pm.db のパス |
| `--canvas-id ID` | ✓ | Canvas ID |
| `--since YYYY-MM-DD` | — | レポートの対象期間（`pm_report.py` にのみ渡す） |
| `--dry-run` | — | 両スクリプトを `--dry-run` で実行 |
| `--no-encrypt` | — | 平文モード |
| `--output PATH` | — | レポートをファイルにも保存 |
| `--show-acknowledged` | — | 確認済み決定事項も表示 |
| `--skip-sync` | — | Canvas 同期をスキップして `pm_report.py` のみ実行 |
| `--skip-canvas` | — | Canvas 投稿をスキップ |

`--filter` には未対応（必要なら `pm_report.py` を直接呼ぶ）。

---

## 運用例

### 月曜朝の週次運用

```sh
# 1. Slack 取り込み + pm.db 反映
bash scripts/pm_from_slack.sh -c <CHANNEL_ID> --since 2026-05-14

# 2. リーダー会議系の進捗レポートを Canvas に投稿
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID>
```

### 隔週の進捗報告

```sh
# 1. 隔週レポートを pptx で生成
python3 scripts/pm_biweekly_report.py \
    --filter "リーダー会議系" --filter "リーダー会議" \
    --output reports/biweekly_2026-05-21.pptx
```

### エリア別レポート（フィルタ活用）

```sh
# HPC アプリ WG 専用
python3 scripts/pm_report.py \
    --canvas-id F0HPC_CANVAS_ID \
    --filter "HPCアプリケーションWG系" --filter "HPCアプリケーションWG"

# ベンチマーク WG 専用
python3 scripts/pm_report.py \
    --canvas-id F0BENCH_CANVAS_ID \
    --filter "ベンチマークWG系" --filter "ベンチマークWG"
```

---

## 関連ドキュメント

- `docs/commands.md` — 全コマンドリファレンス
- `docs/architecture.md` — Pass 1（取り込み）/ Pass 2（エンリッチメント）
- `docs/canvas_api.md` — Slack Canvas API 仕様
- `docs/schema.md` — pm.db スキーマ（`action_items.channel_id` / `decisions.channel_id` / `meetings.kind`）
