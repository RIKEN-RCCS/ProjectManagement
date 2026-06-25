# レポート系コマンド

pm.db のデータからレポートを生成する4系統のコマンドをまとめる。

| コマンド | 用途 | 出力先 | LLM |
|---|---|---|---|
| `pm_report.py` | 定型の進捗レポート（マイルストーン・期限超過・未完了AI・直近決定）| Slack Canvas + 標準出力 | 不使用 |
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

## 2. pm_insight.py — LLM インサイト

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

## 3. canvas_report.sh — Canvas 同期 + 進捗レポート

`pm_xlsx_sync.py`（Box XLSX → pm.db 同期）→ `pm_xlsx_report.py`（pm.db → Box XLSX 生成・アップロード）を順次実行する。
**XLSX 上の編集を pm.db に取り込んでから Box へバージョン更新**するため、会議中の編集を失わない。

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
bash scripts/pm_from_slack.sh -c CHANNEL_ID --since 2026-05-14

# 2. リーダー会議系の進捗レポートを Canvas に投稿
bash scripts/canvas_report.sh --db data/pm.db --canvas-id <CANVAS_ID>
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
