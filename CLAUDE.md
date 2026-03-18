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
- 「誤りの修正・最終判断」→ 人間（Slack Canvas上で編集）
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

`recording_to_pm.sh --meeting-name` は `pm_minutes_import.py` → `pm_minutes_to_pm.py` の順で呼び出す。

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
│   ├── slack_pipeline.py            # Slack取得・要約・Canvas投稿（統合版）。--skip-llm でLLMスキップ、--list でスレッド一覧表示
│   ├── pm_minutes_import.py         # 議事録 → data/minutes/{kind}.db（詳細議事録・担当者・期限を構造化保存。--post-to-slack でSlack投稿。--delete で削除）
│   ├── pm_minutes_to_pm.py          # data/minutes/{kind}.db → pm.db 転記（LLM不使用。--delete で削除）
│   ├── pm_extractor.py              # Slack DB → 決定事項・アクションアイテム抽出 → pm.db（--list で抽出済み一覧）
│   ├── pm_report.py                 # pm.db → 進捗レポート生成・Canvas投稿（SlackリンクはクリッカブルURL形式）
│   ├── pm_sync_canvas.py            # Canvas → pm.db 同期（担当者・期限・マイルストーン・状況・内容・対応状況）。open/close判定は「状況」列のみ
│   ├── pm_relink.py                 # アクションアイテムの各フィールド（担当者・期限・内容・マイルストーン等）をCSV経由で一括編集（LLM不使用）。note列は参照用として出力
│   ├── pm_goals_import.py           # goals.yaml → pm.db 完全同期
│   ├── db_utils.py                  # DB接続の一元管理・平文DB暗号化変換（SQLCipher対応）
│   ├── cli_utils.py                 # 共通CLIユーティリティ（argparse ヘルパー・make_logger・load_claude_md）
│   ├── recording_to_pm.sh                     # 会議録音をテキスト化するSlurmジョブスクリプト。文字起こし後 pm_minutes_import.py → pm_minutes_to_pm.py を自動実行
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
export SLACK_MCP_XOXB_TOKEN="xoxp-..."   # 全スクリプト共通（xoxp- / xoxb- どちらでも可）
export SLACK_USER_TOKEN="xoxp-..."        # pm_minutes_import.py --post-to-slack 用（ユーザーとして投稿・本人が削除可能）
EOF
chmod 600 ~/.secrets/slack_tokens.sh

# 2. 実行前に読み込む（毎回）
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py ...
```

ローカルLLM（OpenAI互換API）を使う場合:
```sh
export OPENAI_API_BASE="http://..."
export OPENAI_API_KEY="..."
export OPENAI_MODEL="..."
```

---

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可（ネストセッション制限）。各スクリプトはClaude Codeの外のターミナルから実行すること。
- `call_claude()` 内で `CLAUDECODE` 環境変数を子プロセスから除外する処理を実装済み。
- `slack-mcp-server` バイナリが必要。PATH、`~/bin/`、`~/.local/bin/` の順で探索する。
- Python仮想環境は `~/.venv_x86_64` を使用。`~/.venv_x86_64/bin/python3 scripts/xxx.py` で実行する。

---

@docs/commands.md

---

@docs/schema.md

---

@docs/roadmap.md
