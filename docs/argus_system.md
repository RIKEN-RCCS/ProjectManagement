# Argus AI Project Intelligence System

Slack・議事録・pm.db のデータを統合分析し、プロジェクトマネージャーの
日常的な状況把握・リスク検知・文書草案生成を支援するシステム。

---

## 概要

```
ユーザー: /argus-brief 60 @西澤（チャンネル: C08SXA4M7JT）
         ↓ Slack Socket Mode
pm_qa_server.py（常駐デーモン）
  ├─ ack()                         ← 3秒以内に即時応答
  ├─ "Argus 分析中..." を表示
  └─ executor.submit(_run_brief)   ← バックグラウンドスレッドへ
        ├─ Slack 生メッセージ収集    ← qa_config.yaml の channels
        ├─ 議事録本文収集            ← data/minutes/{kind}.db
        ├─ pm.db 統計収集            ← マイルストーン・期限超過AI・担当者負荷
        ├─ call_argus_llm()         ← gemma4 優先、未起動なら RiVault にフォールバック
        └─ ephemeral 返信            ← 本人のみ見える回答
```

**情報源**:
- Slack 生メッセージ（`messages` + `replies` テーブル、発言者・タイムスタンプ付き）
- 議事録本文（`data/minutes/{kind}.db` の `minutes_content` テーブル）
- pm.db 統計（マイルストーン進捗・期限超過AI・担当者別負荷・未確認決定事項）

**LLM 優先順位**:
1. gemma4（`localhost:8000`）— ヘルスチェック OK なら優先使用（128K context）
2. RiVault（`zai-org/GLM-4.7-Flash`）— gemma4 未起動時のフォールバック（200K context）

---

## ファイル構成

```
scripts/
├── pm_argus.py          # データ収集・プロンプト構築・コマンドハンドラ
└── pm_qa_server.py      # Slack Socket Mode デーモン（/argus-ask・/argus-* を統合）
                         # ← pm_qa_start.sh で起動

data/
├── qa_config.yaml           # チャンネル→インデックスマッピング（/argus-ask・Argus 共通）
└── secretary_canvas_id.txt  # --brief-to-canvas の投稿先 Canvas ID（F0AT4N36TFF）

logs/
├── pm_qa_server.log     # サーバーログ（/argus-ask・/argus-* 両方）
├── pm_qa_server.pid     # PIDファイル（起動中のみ存在）
└── pm_argus_cron.log    # cron 自動実行ログ
```

---

## 起動・停止

Argus のスラッシュコマンド（`/argus-brief` 等）は `pm_qa_server.py` に統合されているため、
**`pm_qa_start.sh` を起動するだけで `/argus-ask` と `/argus-*` の両方が有効になる**。
専用の別デーモンは不要。

```bash
# 起動（起動中なら何もしない。cron の自動再起動にも使用）
bash scripts/pm_qa_start.sh

# 停止
bash scripts/pm_qa_stop.sh

# 状態確認
cat logs/pm_qa_server.pid | xargs kill -0 && echo 起動中 || echo 停止中

# ログ確認
tail -f logs/pm_qa_server.log
```

`pm_qa_start.sh` は起動時に以下を自動で行う:
- `~/.secrets/slack_tokens.sh`（`SLACK_BOT_TOKEN`・`SLACK_APP_TOKEN`・`SLACK_USER_TOKEN`）の読み込み
- `~/.secrets/rivault_tokens.sh`（`RIVAULT_URL`・`RIVAULT_TOKEN`）の読み込み
- `OPENAI_API_BASE=http://localhost:8000/v1`（gemma4）のデフォルト設定

---

## スラッシュコマンド

すべてのコマンドは **ephemeral**（自分にだけ見える）で返答する。

### `/argus-brief [引数]` — デイリーブリーフィング

今日やるべきことを優先度順に最大5件提示する。pm.db 統計・Slack 生メッセージ・議事録を総合分析。

```
/argus-brief
/argus-brief 60                   ← 直近60日分を分析（デフォルト: 30日）
/argus-brief @西澤                 ← 西澤さん担当事項にフォーカス
/argus-brief Benchpark             ← Benchpark 話題にフォーカス
/argus-brief 60 @西澤 GPU性能      ← 全オプション組み合わせ
```

**引数のパース規則**:
| トークン | 判定 | 例 |
|---|---|---|
| 数字のみ | 直近日数 | `60` → 過去60日分 |
| `@` 始まり | 担当者フォーカス | `@西澤` → 西澤さんの担当事項を重点分析 |
| その他テキスト | 話題フォーカス | `Benchpark` → Benchpark 関連を重点分析 |

---

### `/argus-draft <用途> <件名>` — 文書草案生成

会議アジェンダ・進捗報告・確認依頼メッセージの草案を生成する。

```
/argus-draft agenda 次回リーダー会議
/argus-draft report 4月進捗報告
/argus-draft request NVIDIAへの性能確認
```

| 用途 | 説明 | 主な情報源 |
|---|---|---|
| `agenda` | 会議アジェンダ草案 | 未確認決定事項・期限超過AI・直近Slack |
| `report` | 進捗報告草案 | マイルストーン進捗・直近2週完了AI・担当者負荷 |
| `request` | 確認依頼メッセージ草案 | 担当者別負荷・期限超過AI・直近Slack |

---

### `/argus-risk [引数]` — リスク分析

顕在化しているリスクと放置すると問題になりうる予兆を優先度付きで列挙する。

```
/argus-risk
/argus-risk 60                    ← 直近60日分を分析
/argus-risk @小林                  ← 小林さん担当事項のリスクにフォーカス
/argus-risk Benchpark             ← Benchpark 関連リスクにフォーカス
```

引数のパース規則は `/argus-brief` と同じ。

---

### `/argus-transcribe <ファイル名>` — 会議録音の文字起こし・議事録生成

Slack チャンネルにアップロードされた音声・動画ファイルをダウンロードし、
Whisper による文字起こし → LLM による議事録生成 を実行してスレッドに投稿する。

```
/argus-transcribe GMT20260302-032528_Recording.mp4
/argus-transcribe 2026-04-20_Leader_Meeting.m4a
```

**処理フロー**:
1. ファイルをチャンネルから検索・ダウンロード
2. Singularity コンテナ内で FFmpeg 変換（16kHz mono WAV） + Whisper large-v3 文字起こし
3. `generate_minutes_local.py` で LLM 議事録生成（マルチステージ: チャンク抽出 → 統合 → 決定事項・AI 抽出）
4. 完成した議事録ファイルをスレッドにアップロード

**進捗通知**:
- 処理開始・ダウンロード完了・文字起こし完了・Stage 1/2/3 進捗をスレッドに随時投稿（チャンネル全員に可視）
- 最終完了・エラーは実行者のみに ephemeral で通知

**排他制御**: 同時実行は 1 ジョブのみ。処理中に再実行すると現在のジョブ情報を表示してエラーを返す。

**前提条件**:
- `../Minutes` リポジトリが同一ホストに存在し、Singularity コンテナ（`whisper.sif`）が利用可能であること
- `VLLM_API_BASE` が設定され、ローカル LLM サーバーが起動していること
- `HUGGING_FACE_TOKEN` が `~/.secrets/hf_tokens.sh` または環境変数に設定されていること
- `AUDIO_SAVE_DIR` に十分なディスク空き容量があること（デフォルト: `/tmp/whisper_audio`）

---

### `/argus-investigate <質問>` — マルチステップ調査（Agent）

LLM が自律的にツール（DB検索・全文検索・Slackメッセージ取得）を選択・呼び出して
段階的にデータを収集・分析する。単発ブリーフィングでは不可能な因果分析・クロスソース相関に対応。

```
/argus-investigate M3マイルストーンの遅延原因を調査して
/argus-investigate 先週の決定事項が実行されているか確認
/argus-investigate Benchparkハッカソンの準備状況を確認
/argus-investigate @西澤 の負荷が高い原因を分析して
```

**処理フロー**:
1. シードデータ収集（プロジェクト概況・マイルストーン進捗・担当者負荷）
2. LLM がシードデータを分析し、深掘りすべきツールを `<tool_call>` タグで指定
3. ツール実行結果を LLM にフィードバック → さらなる深掘りまたは `<final_answer>` で回答完了
4. 最大5ステップ（180秒タイムアウト）

**利用可能なツール**:
| ツール | 用途 |
|---|---|
| `get_milestone_progress` | マイルストーン完了率・残日数 |
| `get_overdue_items` | 期限超過AI（担当者・MSフィルタ可） |
| `get_assignee_workload` | 担当者別負荷 |
| `get_weekly_trends` | 週次作成/完了トレンド |
| `get_unacknowledged_decisions` | 未確認決定事項 |
| `search_action_items` | AI条件検索 |
| `search_decisions` | 決定事項キーワード検索 |
| `search_text` | 議事録・Slack全文検索（FTS5 + re-ranking） |
| `get_slack_messages` | 特定チャンネルの生メッセージ |

**CLI モード**:
```bash
python3 scripts/pm_argus_agent.py --investigate "M3の遅延原因を調査" --dry-run
python3 scripts/pm_argus_agent.py --investigate "先週の決定事項の実行状況" --max-steps 5
```

---

### `/argus-ask <質問>` — 議事録・Slack 要約の QA（既存機能）

実行チャンネルに対応するインデックスDB（FTS5）を検索して回答する。
Argus とは独立した機能で、同じデーモンで動作する。

```
/argus-ask 設計方針に関する最近の議論は？
/argus-ask Benchparkハッカソンの内容を教えて
```

---

## 自動実行（cron）

平日朝 8:57 にブリーフィングを自動生成して Canvas（`F0AT4N36TFF`）に投稿する。

```
57 8 * * 1-5  cd /lvs0/.../ProjectManagement && \
  source ~/.secrets/slack_tokens.sh && \
  source ~/.secrets/rivault_tokens.sh && \
  ~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas \
  >> logs/pm_argus_cron.log 2>&1
```

ログ確認: `tail -f logs/pm_argus_cron.log`

---

## Patrol Agent（自律型PM巡回）

cron で30分ごとに実行され、プロジェクト状況を巡回する。
完了シグナル検出は LLM 判定を併用（キーワードマッチ + 自然言語分析の二段構え）。
それ以外の検出器はルールベース（LLM 不使用）。

```
cron (30分ごと、平日のみ)
  └─ pm_argus_patrol.py
       ├─ 完了シグナル検出     → 担当者にDMでBlock Kitクローズ確認
       ├─ 期限超過リマインダー  → 担当者にDMで通知（7日間隔）
       ├─ 期限前警告           → 担当者にDMで通知（期限3日前）
       ├─ 未確認決定事項フォロー → リーダー会議チャンネルに通知
       ├─ 長期停滞検出         → 担当者 + リーダー会議チャンネルに通知
       ├─ マイルストーン健全性  → リーダー会議チャンネルにアラート
       └─ 週次トレンド悪化     → リーダー会議チャンネルにアラート
```

**完了確認の流れ**:
1. Slack スレッド返信をスキャン:
   - 第1段: キーワードマッチ（「完了」「done」等）— 高速・決定論的
   - 第2段: LLM 判定 — キーワードで拾えない自然言語表現を検出（「報告書を提出した」「対応しました」等）
2. 担当者に Block Kit ボタン付き DM を送信（「完了にする」/「まだ完了していない」）
3. ボタン押下 → `pm_qa_server.py` が受信 → pm.db の status を更新（audit_log 付き）

**冪等性**: `data/patrol_state.db` で送信済み通知を記録し、cooldown 期間内の再送を防止。

### CLI

```bash
# 通常実行
python3 scripts/pm_argus_patrol.py

# DB・Slack 変更なし（動作確認用）
python3 scripts/pm_argus_patrol.py --dry-run

# 特定の検出器のみ実行
python3 scripts/pm_argus_patrol.py --only overdue
python3 scripts/pm_argus_patrol.py --only completion,deadline

# 承認待ち一覧
python3 scripts/pm_argus_patrol.py --list-pending
```

### cron 設定

```
*/30 * * * 1-5 cd /lvs0/.../ProjectManagement && \
  source ~/.secrets/slack_tokens.sh && \
  source ~/.secrets/rivault_tokens.sh && \
  ~/.venv_aarch64/bin/python3 scripts/pm_argus_patrol.py \
  >> logs/pm_argus_patrol.log 2>&1
```

### 設定ファイル: `data/patrol_config.yaml`

各検出器の有効/無効・cooldown 間隔・閾値を設定する。

### ファイル構成

```
scripts/
├── pm_argus_patrol.py   # メインループ・CLI
├── patrol_state.py      # 冪等性DB（patrol_state.db）
├── patrol_detect.py     # 7つの検出ルール
├── patrol_actions.py    # Slack投稿・DB書き込み
├── patrol_confirm.py    # Block Kit ボタンハンドラ
└── patrol_users.py      # 担当者名 → Slack user_id 解決

data/
├── patrol_config.yaml   # 設定
└── patrol_state.db      # 冪等性DB（自動作成、平文sqlite3）
```

---

## CLI モード（pm_argus.py 直接実行）

デーモン不要で手動実行できる。`source ~/.secrets/slack_tokens.sh` が必要。

```bash
# ブリーフィング生成 → 標準出力のみ（Canvas 投稿なし）
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas --dry-run

# ブリーフィング生成 → Canvas に投稿
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas

# リスク分析 → 標準出力のみ（Canvas 投稿なし）
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --risk --dry-run

# リスク分析 → Canvas に投稿
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --risk

# 引数指定（スラッシュコマンドと同等）
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas --dry-run \
    --days 60 --assignee 西澤 --topic Benchpark
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--brief-to-canvas` | — | ブリーフィング生成・Canvas 投稿 |
| `--risk` | — | リスク分析生成・Canvas 投稿 |
| `--canvas-id ID` | `secretary_canvas_id.txt` の値 | 投稿先 Canvas ID |
| `--dry-run` | — | Canvas 投稿なし・標準出力のみ |
| `--since YYYY-MM-DD` | 30日前 | データ収集開始日（`--days` より優先） |
| `--days N` | `30` | 直近何日分を対象にするか |
| `--assignee NAME` | — | 担当者フォーカス（例: `--assignee 西澤`） |
| `--topic TEXT` | — | 話題フォーカス（例: `--topic Benchpark`） |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--no-encrypt` | — | 平文モード |

---

## 設定ファイル

### `data/qa_config.yaml`

Argus・`/argus-ask`・`/argus-investigate` が参照するチャンネル・インデックスDBの設定。
`indices.{name}.channels` が Argus の生メッセージ収集対象チャンネルを定義する。
詳細は `docs/qa_system.md` を参照。

### `data/secretary_canvas_id.txt`

`--brief-to-canvas` / cron 自動実行の投稿先 Canvas ID を1行で記載。
現在: `F0AT4N36TFF`

---

## 環境変数

| 変数 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | 必須 | — | Bot Token（`xoxb-`）— `/argus-*` の返信に使用 |
| `SLACK_APP_TOKEN` | 必須 | — | App-Level Token（`xapp-`）— Socket Mode 接続 |
| `SLACK_USER_TOKEN` | 必須 | — | User Token（`xoxp-`）— Canvas 投稿に使用 |
| `OPENAI_API_BASE` | 推奨 | `http://localhost:8000/v1` | gemma4 vLLM エンドポイント |
| `OPENAI_API_KEY` | 任意 | `"dummy"` | gemma4 API キー |
| （モデル名） | — | 自動取得 | vLLM `/v1/models` から自動検出 |
| `RIVAULT_URL` | 任意 | — | RiVault エンドポイント（gemma4 未起動時のフォールバック） |
| `RIVAULT_TOKEN` | 任意 | — | RiVault API トークン |

`pm_qa_start.sh` が `~/.secrets/slack_tokens.sh` と `~/.secrets/rivault_tokens.sh` を自動 source するため、デーモン起動時は手動設定不要。CLI 直接実行時は `source ~/.secrets/slack_tokens.sh` が必要。

---

## セットアップ（初回のみ）

### 1. Slack アプリへのコマンド登録

[api.slack.com/apps](https://api.slack.com/apps) で `/argus-ask` を登録済みのアプリに以下を追加:

- `/argus-brief` — Short Description: 今日の状況サマリーと優先アクション
- `/argus-draft` — Short Description: 草案生成（`agenda`/`report`/`request`）
- `/argus-risk` — Short Description: リスク一覧と対応提案
- `/argus-transcribe` — Short Description: 会議録音の文字起こし・議事録生成
- `/argus-investigate` — Short Description: マルチステップ調査（Agent）

Request URL は Socket Mode では無視されるため任意の HTTPS URL でよい。

**Patrol Agent の Block Kit ボタン**: Socket Mode を使用している場合、`app.action()` ハンドラは追加設定なしで動作する（Interactivity は Socket Mode で自動有効）。Bot Token に `chat:write`、`im:write`、`users:read` スコープが必要。

### 2. QA インデックスの構築（`/argus-ask` 用。Argus には不要）

```bash
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --full-rebuild
```

### 3. cron の設定確認

```bash
crontab -l | grep argus
```

現在の設定: 平日朝 8:57 に `--brief-to-canvas` を自動実行。

---

## トラブルシューティング

**`/argus-brief` を実行しても何も返ってこない:**
1. `tail -50 logs/pm_qa_server.log` でエラーを確認
2. `cat logs/pm_qa_server.pid | xargs kill -0 && echo 起動中 || echo 停止中` でデーモン確認
3. `bash scripts/pm_qa_start.sh` で再起動

**「LLMエラー」が返ってくる:**
- `curl http://localhost:8000/v1/models` で gemma4 の起動確認
- gemma4 が落ちていれば RiVault にフォールバックするはずなので `RIVAULT_URL` が設定されているか確認: `echo $RIVAULT_URL`
- ログで `ローカル LLM に接続できません。RiVault にフォールバック` が出ているか確認

**プロンプトが大きすぎてエラー:**
- `_MAX_CHARS_PER_CHANNEL = 20000`（`pm_argus.py`）でチャンネルあたりの上限を調整
- `--days` を短くする（例: `30` → `14`）

**cron のブリーフィングが Canvas に反映されない:**
- `tail -20 logs/pm_argus_cron.log` でエラー確認
- `SLACK_USER_TOKEN` が有効か確認（Canvas 投稿は User Token を使用）

**`/argus-draft` で「用途を指定してください」と返ってくる:**
- 第1引数に `agenda` / `report` / `request` のいずれかを指定すること
- 例: `/argus-draft agenda 次回リーダー会議`

**`/argus-transcribe` で「pipeline モジュールの読み込みに失敗」と返ってくる:**
- `../Minutes/slack_bot/pipeline.py` が存在するか確認
- `../Minutes/slack_bot/config.py` の `AUDIO_SAVE_DIR`・`VLLM_API_BASE`・`VLLM_MODEL` が正しく設定されているか確認

**`/argus-transcribe` で「現在処理中のジョブがあります」と返ってくる:**
- 前のジョブが完了または失敗するまで待つ
- `tail -f logs/pm_qa_server.log` で進捗を確認する
- デーモンを再起動すればジョブロックはリセットされる（処理中のジョブは中断される）
