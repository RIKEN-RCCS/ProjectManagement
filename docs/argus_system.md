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
        ├─ Slack 生メッセージ収集    ← secretary_channels.txt の全チャンネル
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
└── pm_qa_server.py      # Slack Socket Mode デーモン（/ask・/argus-* を統合）
                         # ← pm_qa_start.sh で起動

data/
├── secretary_channels.txt    # Argus が生メッセージを収集するチャンネルID（1行1ID）
└── secretary_canvas_id.txt   # --brief-to-canvas の投稿先 Canvas ID（F0AT4N36TFF）

logs/
├── pm_qa_server.log     # サーバーログ（/ask・/argus-* 両方）
├── pm_qa_server.pid     # PIDファイル（起動中のみ存在）
└── pm_argus_cron.log    # cron 自動実行ログ
```

---

## 起動・停止

Argus のスラッシュコマンド（`/argus-brief` 等）は `pm_qa_server.py` に統合されているため、
**`pm_qa_start.sh` を起動するだけで `/ask` と `/argus-*` の両方が有効になる**。
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

### `/ask <質問>` — 議事録・Slack 要約の QA（既存機能）

実行チャンネルに対応するインデックスDB（FTS5）を検索して回答する。
Argus とは独立した機能で、同じデーモンで動作する。

```
/ask 設計方針に関する最近の議論は？
/ask Benchparkハッカソンの内容を教えて
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

## CLI モード（pm_argus.py 直接実行）

デーモン不要で手動実行できる。`source ~/.secrets/slack_tokens.sh` が必要。

```bash
# ブリーフィング生成 → 標準出力のみ（Canvas 投稿なし）
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas --dry-run

# ブリーフィング生成 → Canvas に投稿
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas

# リスク分析 → 標準出力のみ
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --risk --dry-run

# リスク分析 → Canvas にも投稿
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --risk --canvas-id F0AT4N36TFF

# 引数指定（スラッシュコマンドと同等）
~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas --dry-run \
    --days 60 --assignee 西澤 --topic Benchpark
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--brief-to-canvas` | — | ブリーフィング生成・Canvas 投稿 |
| `--risk` | — | リスク分析生成 |
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

### `data/secretary_channels.txt`

Argus が生メッセージを収集する Slack チャンネルID（1行1ID、`#` 始まりはコメント）。

```
# Argus が生メッセージを収集する Slack チャンネルID
C08SXA4M7JT    # 20_1_リーダ会議メンバ
C08M0249GRL    # 20_アプリケーション開発エリア
C08LSJP4R6K    # 21_hpcアプリケーションwg
C093DQFSCRH    # 21_1_hpcアプリケーションwg_ブロック1
C093LP1J15G    # 21_2_hpcアプリケーションwg_ブロック2
C08MJ0NF5UZ    # 22_ベンチマークwg
C096ER1A0LU    # 23_benchmark_framework
C0A6AC59AHM    # 24_ai-hpc-application
```

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
| `OPENAI_MODEL` | 任意 | `google/gemma-4-26B-A4B-it` | gemma4 モデル名 |
| `RIVAULT_URL` | 任意 | — | RiVault エンドポイント（gemma4 未起動時のフォールバック） |
| `RIVAULT_TOKEN` | 任意 | — | RiVault API トークン |

`pm_qa_start.sh` が `~/.secrets/slack_tokens.sh` と `~/.secrets/rivault_tokens.sh` を自動 source するため、デーモン起動時は手動設定不要。CLI 直接実行時は `source ~/.secrets/slack_tokens.sh` が必要。

---

## セットアップ（初回のみ）

### 1. Slack アプリへのコマンド登録

[api.slack.com/apps](https://api.slack.com/apps) で `/ask` を登録済みのアプリに以下を追加:

- `/argus-brief` — Short Description: 今日の状況サマリーと優先アクション
- `/argus-draft` — Short Description: 草案生成（`agenda`/`report`/`request`）
- `/argus-risk` — Short Description: リスク一覧と対応提案

Request URL は Socket Mode では無視されるため任意の HTTPS URL でよい。

### 2. QA インデックスの構築（`/ask` 用。Argus には不要）

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
