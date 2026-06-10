# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## ドキュメント運用ルール（最重要）

このリポジトリでは、Claude が参照・更新する 3 つのドキュメントを **時間軸で分離** して運用する。
これは Claude Code 標準の「CLAUDE.md に何でも書く」運用とは異なるので、必ず守ること。

| ファイル | 時間軸 | 役割 | 更新タイミング |
|---|---|---|---|
| **`CLAUDE.md`**（本ファイル） | 現在 | 規約・禁止事項・ポインタ。安定情報のみ | 規約そのものが変わるとき（稀） |
| **`PLAN.md`** | 未来 | 進行中・保留中の実装計画 | 計画着手時に追記、完了時に削除して LOG.md へ移す |
| **`LOG.md`** | 過去 | 完了した変更・方針転換・破棄された案の journal | 計画完了時、設計の方針転換時、ボツ案を残したいとき |

**運用上の鉄則**:
1. **CLAUDE.md に履歴を書かない** — 「2026-05-XX に〜を変更した」という記述は LOG.md へ。CLAUDE.md は常に「現状を表す」スナップショット。古い記述は上書きで置換する。
2. **PLAN.md のエントリは完了時に消す** — 完了した計画は LOG.md に 3-5 行で圧縮して移す。PLAN.md には in-flight な項目だけが残るようにする。
3. **LOG.md は「なぜ」を残す** — 「何をしたか」は git log で追えるので書かない。**判断の経緯・他案を捨てた理由・破棄された実験** を 1 エントリ 3-5 行で残す。新しいエントリを上に追加する。
4. **詳細設計は `docs/` 側に切り出す** — CLAUDE.md / PLAN.md / LOG.md からはリンクを張る形にし、コンテキストを汚さない。
5. **CLAUDE.md は毎会話のコンテキストに自動展開される** — 肥大化させない。詳細リファレンスは `docs/` + Skill 経由で必要時に読む。

---

## 機密ファイルの取り扱い（最重要）

以下のファイルは Claude（Claude Code / Claude API）から**絶対に読み込まない**。
スクリプトはファイルシステムから直接読むため、Claude が読まなくても運用に支障はない。

- **`docs/project.md`** — ステークホルダー氏名・メールアドレス・Slack user_id・組織体制（理研 / 富士通 / NVIDIA の役職と意思決定権限）等
  - ローカル LLM（vLLM gemma4 / Whisper 議事録生成）のみが `cli_utils.py: load_claude_md_context()` 経由で直接読む
- **`data/argus_config.yaml`** — Slack channel_id / Canvas ID / Box folder_id 等
  - スクリプトは `yaml.safe_load()` で直接読む（cli_utils / pm_minutes_catalog / pm_api 等）
  - キー構造のみ必要な場合は `pm-argus-config-schema` Skill を参照する
- **`goals.yaml`** — プロジェクトの戦略目標・マイルストーン名・期限・成功基準
  - スクリプトは `yaml.safe_load()` で直接読む（`scripts/ingest/goals.py` ほか）
  - キー構造のみ必要な場合は `pm-goals-schema` Skill を参照する
- **`~/.secrets/` 配下** — Slack トークン等

共通ルール:
- `Read` ツールで本文を開かない（`.claude/settings.local.json` の `permissions.deny` で機械的にブロック済み）
- `grep -c` / `wc -l` 等でメタ情報（行数・存在）に触れる程度は可、本文の context 展開は不可
- 値が必要な場合は**ユーザーに該当行の貼り付けを依頼**する

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

---

## システム概要（要約）

情報の流れは **2パス + ナレッジ蒸留** で構成される。詳細図とスクリプト一覧は `docs/architecture.md` を参照。

- **Pass 1 抽出** — Slack / 議事録 / goals.yaml → `pm.db`（`scripts/ingest/`）
- **Pass 2 エンリッチ** — 過去ナレッジから判断者・根拠・関連IDを補完（`scripts/enrich/`）
- **Pass 3 蒸留** — BOX 本文・議事録・決定事項を意思決定単位に蒸留 → `data/knowledge.db`（`pm_box_distill.py`）
- データ品質管理は `pm_screen.py`（重複検出）→ `pm_relink.py`（CSV 一括編集）

**主要 DB の役割**（スキーマ詳細は `pm-schema` Skill）:
- `data/slack.db` — Slack 全チャンネル統合（`channel_id` 列で絞り込む）
- `data/pm.db` — action_items / decisions / meetings / goals / milestones の唯一の正本
- `data/minutes/{kind}.db` — 議事録詳細（会議名ごとに独立）
- `data/box_docs.db` — BOX 本文 Markdown + relevance 判定
- `data/knowledge.db` — 蒸留ナレッジレイヤ（プロジェクト全体共通）
- `data/qa_index.db` — FTS5 統合インデックス（`chunk_indexes` で論理 index 分離）

会議録音処理 `pm_from_recording.sh --meeting-name` は VTT 自動検出 + mp4 ならスライド OCR 自動実行。詳細は `pm-commands` Skill。

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
export OPENAI_MAX_TOKENS="8192"                      # Slack 抽出用（pm_ingest.py slack / ingest_slack.py）
```

`OPENAI_API_BASE` が設定されている場合、`call_claude()` は Claude CLI の代わりにローカルLLMを呼び出す（`cli_utils.py` の `call_local_llm()` 経由）。モデル名は vLLM の `/v1/models` API から自動取得するため、`OPENAI_MODEL` の設定は不要。

`OPENAI_MAX_TOKENS` は Slack 抽出（`pm_ingest.py slack` 経由の `ingest_slack.py`）の最大出力トークン数。会議録音処理では `generate_minutes_local.py` の `--max-tokens 16384` が使われるため本変数は影響しない。

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

@docs/architecture.md

---

## 関連スキル（必要時に Skill ツールで読み込む）

- `pm-commands` — `docs/commands.md`。スクリプト・CLI のオプション一覧を引くとき
- `pm-schema` — `docs/schema.md`。DB テーブル定義・列・差分判定ロジックを引くとき
- `argus-system` — `docs/argus_system.md`。Argus / Patrol / FTS5 索引まわりを触るとき
- `slack-canvas-api` — `docs/canvas_api.md`。Canvas を投稿・編集・再作成するとき
- `pm-roadmap` — `docs/roadmap.md`。実装済みフェーズ・未実装課題を確認するとき
- `pm-ingest-plugin` — `docs/ingest_plugin.md`。`scripts/ingest/` に新ソースを追加するとき
- `pm-distill-policy` — `docs/distill_policy.md`。ナレッジ蒸留 (knowledge.db) の採否基準を引くとき
- `pm-argus-commands` — `docs/argus_outcomes.md`。Argus 5 コマンドの使い方・引数・内部ツール仕様を触るとき
- `pm-reports` — `docs/reports.md`。pm_report / pm_biweekly_report / pm_insight / canvas_report.sh のオプションを引くとき
- `docs/minutes_consensus.md` — Self-Consistency 議事録生成（`--consensus N`）のアルゴリズム・CLI・環境変数分離を触るとき
- `pm-argus-config-schema` — `data/argus_config.yaml` のキー構造（実値非掲載）。索引・会議目録・フィルタ設定を触るとき
- `pm-goals-schema` — `goals.yaml` のキー構造（実値非掲載）。`pm_ingest.py goals` / マイルストーン同期周りを触るとき
