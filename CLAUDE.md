# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
@docs/project.md

---

## システム概要

情報の流れは以下の2系統を統合する。

```
[Slack] ─── slack_pipeline.py ───→ {channel_id}.db
                                          ↓
[会議議事録]                        pm.db ←─ pm_extractor.py
  meetings/*.md        │          (決定事項・               ↑
                       │        アクションアイテム)   {channel_id}.db
                       │                  ↓
                       │            pm_report.py
                       │                  ↓
                       │           Slack Canvas / レポート
                       │
                       └─ pm_minutes_import.py ──→ data/minutes/{kind}.db
                                    ↓          （詳細議事録・担当者・期限）
                         pm_minutes_to_pm.py ──→ pm.db
                              （LLM不使用）      （担当者・期限を直接転記）
```

`pm_from_recording.sh --meeting-name` は `generate_minutes_local.py`（ローカルLLMで高品質議事録生成）→ `pm_minutes_import.py --no-llm`（DB保存）→ `pm_minutes_to_pm.py`（pm.db転記）の順で呼び出す。

**各DBの役割分担**:
- `{channel_id}.db` — Slackデータ専用。チャンネルごとに独立。
- `pm.db` — PM情報専用。複数チャンネル・複数会議を横断して統合。
- `data/minutes/{kind}.db` — 議事録詳細専用。会議名ごとに独立。決定・AIの背景を含む。

---

## ファイル構成

```
slack/
├── meetings/                        # 議事録の一次着地点（Markdown形式）
│   ├── YYYY-MM-DD_会議名.md
├── scripts/                         # スクリプト一式
│   ├── slack_pipeline.py            # Slack取得・要約・Canvas投稿（統合版）。--canvas-id でCanvas投稿、--skip-fetch でDB既存要約から全体要約のみ生成、--skip-llm でLLMスキップ、--list でスレッド一覧表示
│   ├── pm_minutes_import.py         # 議事録 → data/minutes/{kind}.db（詳細議事録・担当者・期限を構造化保存）。--export でDB内容をMarkdownにエクスポート、--no-llm で人間修正済みMarkdownをLLM不使用で再インポート。--post-to-slack でSlack投稿。--delete で削除
│   ├── pm_minutes_to_pm.py          # data/minutes/{kind}.db → pm.db 転記（LLM不使用。--delete で削除）
│   ├── pm_extractor.py              # Slack DB → 決定事項・アクションアイテム抽出 → pm.db（--list で抽出済み一覧）
│   ├── pm_report.py                 # pm.db → 進捗レポート生成・Canvas投稿（SlackリンクはクリッカブルURL形式）。--show-workload で担当者別負荷セクションを追加出力
│   ├── pm_sync_canvas.py            # Canvas → pm.db 同期（担当者・期限・マイルストーン・状況・内容・対応状況・決定事項内容）。open/close判定は「状況」列のみ
│   ├── pm_relink.py                 # アクションアイテム・決定事項の各フィールドをCSV経由で一括編集（LLM不使用）。deleted(0/1)でアイテムの有効化/削除も可能。--include-deleted で削除済みアイテムを対象に含める
│   ├── pm_insight.py                # pm.db → LLMによるプロジェクト健全性評価・リスク特定・改善提案を生成・Canvas投稿
│   ├── pm_goals_import.py           # goals.yaml → pm.db 完全同期
│   ├── pm_web.py                    # pm.db ローカル編集 Web UI（NiceGUI + AG Grid）。ブラウザでアクションアイテム・決定事項を編集。source列からSlackリンク・議事録ポップアップ表示。楽観的排他制御付き。起動: bash scripts/pm_web_start.sh（port 8501）
│   ├── pm_web_start.sh              # pm_web.py をバックグラウンドで起動（nohup + PIDファイル管理）
│   ├── pm_web_stop.sh               # pm_web.py を停止（PIDファイルでプロセス管理）
│   ├── canvas_utils.py              # Slack Canvas 操作の共通ユーティリティ（sanitize_for_canvas・post_to_canvas・セクション削除ロジック）
│   ├── db_utils.py                  # DB接続の一元管理・平文DB暗号化変換（SQLCipher対応）。open_pm_db・fetch_milestone_progress・fetch_assignee_workload も提供
│   ├── cli_utils.py                 # 共通CLIユーティリティ（argparse ヘルパー・make_logger・load_claude_md・call_claude）。OPENAI_API_BASE が設定されている場合はローカルLLMを使用（call_local_llm 経由）
│   ├── generate_minutes_local.py    # ローカルLLMを使って文字起こしから高品質議事録を生成。マルチステージ処理・CoT除去・ストリーミング対応。../Minutes/scripts/ からコピー
│   ├── pm_from_recording_auto.sh    # data/*.m4a を検出して pm_from_recording.sh を自動投入。-c CHANNEL_ID でSlack投稿も自動化
│   ├── pm_from_recording.sh         # 会議録音をローカルで処理するスクリプト。文字起こし後 generate_minutes_local.py → pm_minutes_import.py --no-llm → pm_minutes_to_pm.py を自動実行
│   ├── pm_from_slack.sh             # Slack取得・要約 → pm.db抽出を連続実行（slack_pipeline.py + pm_extractor.py）
│   ├── canvas_report.sh             # Canvas同期 → PMレポート生成・Canvas投稿（pm_sync_canvas.py + pm_report.py）
│   ├── slack_post_minutes.sh        # 議事録DBの内容をSlackチャンネルに投稿（pm_minutes_import.py --post-to-slack）
│   └── whisper_vad.py               # VAD+DeepFilterNet+Whisperによる話者分離・文字起こし
└── data/                            # DBと出力ファイル
    ├── {channel_id}.db              # Slackデータ（例: C0A9KG036CS.db）
    ├── pm.db                        # PM統合データ
    ├── minutes/                     # 詳細議事録DB（会議名ごとに独立）
    │   └── {kind}.db                # 例: Leader_Meeting.db
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
export OPENAI_MODEL="google/gemma-4-26B-A4B-it"     # サーバに読み込まれているモデル名
export OPENAI_MAX_TOKENS="8192"                      # Slack 要約用（pm_extractor・slack_pipeline）
```

`OPENAI_API_BASE` が設定されている場合、`call_claude()` は Claude CLI の代わりにローカルLLMを呼び出す（`cli_utils.py` の `call_local_llm()` 経由）。会議録音処理（`pm_from_recording.sh`）はこれらの変数をスクリプト内でハードコードしているため、別途設定は不要。

`OPENAI_MAX_TOKENS` は Slack 要約・抽出（`pm_extractor.py`・`slack_pipeline.py`）の最大出力トークン数。会議録音処理では `generate_minutes_local.py` の `--max-tokens 16384` が使われるため本変数は影響しない。

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

@docs/commands.md

---

@docs/schema.md

---

@docs/canvas_api.md

---

@docs/roadmap.md

---

@docs/qa_system.md
