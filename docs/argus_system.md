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
        ├─ Slack 生メッセージ収集    ← argus_config.yaml の channels
        ├─ 議事録本文収集            ← data/minutes/{kind}.db
        ├─ pm.db 統計収集            ← マイルストーン・期限超過AI・担当者負荷
        ├─ call_argus_llm()         ← gemma4 優先、未起動なら RiVault にフォールバック
        └─ ephemeral 返信            ← 本人のみ見える回答
```

**情報源**:
- Slack 生メッセージ（`messages` + `replies` テーブル、発言者・タイムスタンプ付き）
- 議事録本文（`data/minutes/{kind}.db` の `minutes_content` テーブル）
- pm.db 統計（マイルストーン進捗・期限超過AI・担当者別負荷・未確認決定事項）
- FTS5 検索インデックス（議事録・Slack生メッセージ・ドキュメント・Web記事）

**LLM 優先順位**:
1. gemma4（`localhost:8000`）— ヘルスチェック OK なら優先使用（128K context）
2. RiVault（`zai-org/GLM-4.7-Flash`）— gemma4 未起動時のフォールバック（200K context）

---

## ファイル構成

```
scripts/
├── pm_argus.py          # データ収集・プロンプト構築・コマンドハンドラ
├── pm_argus_agent.py    # Investigation Agent（LLM駆動マルチステップ調査）
├── pm_argus_patrol.py   # Patrol Agent メインループ・CLI
├── patrol_state.py      # Patrol 冪等性DB（patrol_state.db）
├── patrol_detect.py     # Patrol 検出ルール（決定論的・LLM不使用）
├── patrol_actions.py    # Patrol アクション実行（Slack投稿・DB書き込み）
├── patrol_confirm.py    # Patrol Block Kit ボタンハンドラ
├── patrol_users.py      # 担当者名 → Slack user_id 解決
├── pm_embed.py          # FTS5 インデックス構築（argus_config.yaml に従い各DBに書き込む）
├── pm_qa_server.py      # Slack Socket Mode デーモン（全 /argus-* コマンドを統合処理）
├── pm_qa_start.sh       # デーモン起動スクリプト（nohup + PIDファイル管理）
└── pm_qa_stop.sh        # デーモン停止スクリプト

data/
├── argus_config.yaml    # 統合設定（インデックス定義・チャンネルマッピング・pm.dbパス）
├── secretary_canvas_id.txt  # --brief-to-canvas の投稿先 Canvas ID（F0AT4N36TFF）
├── patrol_config.yaml   # Patrol Agent 設定（検出器の有効/無効・閾値・通知チャンネル）
├── patrol_state.db      # Patrol Agent 冪等性DB（自動作成、平文sqlite3）
├── qa_pm.db             # FTS5 インデックス: リーダー会議など汎用（平文sqlite3）
├── qa_pm-hpc.db         # FTS5 インデックス: HPCアプリWG
├── qa_pm-bmt.db         # FTS5 インデックス: ベンチマークWG
└── qa_pm-pmo.db         # FTS5 インデックス: PMO

logs/
├── pm_qa_server.log     # サーバーログ（全 /argus-* コマンド）
├── pm_qa_server.pid     # PIDファイル（起動中のみ存在）
├── pm_argus_cron.log    # cron ブリーフィング自動実行ログ
└── pm_argus_patrol.log  # cron Patrol 自動実行ログ
```

---

## 起動・停止

全 Argus スラッシュコマンド（`/argus-brief`・`/argus-investigate` 等）は `pm_qa_server.py` に統合されているため、**`pm_qa_start.sh` を起動するだけで全機能が有効になる**。

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
1. ファイルをチャンネルから検索・ダウンロード（同名 VTT も自動検索: `{stem}.transcript.vtt` → `{stem}.vtt`）
2. Singularity コンテナ内で FFmpeg 変換（16kHz mono WAV） + Whisper large-v3 文字起こし
3. `generate_minutes_local.py` で LLM 議事録生成（マルチステージ: チャンク抽出 → 統合 → 決定事項・AI 抽出。VTT があれば `--vtt` 付きで話者情報を活用）
4. 完成した議事録ファイルをスレッドにアップロード

**VTT 話者情報の活用**: 音声ファイルと同名の Zoom VTT ファイル（例: `2026-04-28_Leader_Meeting.m4a` → `2026-04-28_Leader_Meeting.transcript.vtt` または `2026-04-28_Leader_Meeting.vtt`）がチャンネルにアップロードされている場合、自動的にダウンロードして議事録生成に活用する。VTT の正確な話者名を用いてアクションアイテムの担当者推定精度が向上する。

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
旧 `/argus-ask`（単発QA）の機能はこのコマンドに統合された。

```
/argus-investigate M3マイルストーンの遅延原因を調査して
/argus-investigate 先週の決定事項が実行されているか確認
/argus-investigate Benchparkハッカソンの準備状況を確認
/argus-investigate @西澤 の負荷が高い原因を分析して
/argus-investigate 設計方針に関する最近の議論は？
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

### 検出ルール詳細（`patrol_detect.py`）

#### 1. 完了シグナル検出（`detect_completion_signals`）

Slack の source='slack' AI に対し、元スレッドの返信から完了シグナルを二段構えで検出する。

| 段階 | 方式 | 検出例 |
|---|---|---|
| 第1段 | キーワードマッチ（`pm_sync_canvas.py` の `CLOSE_KEYWORDS` を共用） | 「完了」「done」「済」「対応済」「解決」等 |
| 第2段 | LLM 判定（`call_argus_llm()` で自然言語分析） | 「報告書を提出した」「対応しました」「反映済みです」等 |

- 対象: `source='slack'` かつ `source_ref` が Slack パーマリンクの open AI のみ
- 返信の検索範囲: `max_reply_age_days`（デフォルト7日）以内の返信
- cooldown: 一度確認送信したAIは再送しない（cooldown_days=9999 = 実質1回のみ）
- 検出時: 担当者に Block Kit ボタン付き DM を送信

**LLM 判定のプロンプト**: AIの内容と返信テキストを渡し、「YES: （根拠）」または「NO」の判定を求める。キーワードで拾えない表現（「検証環境にデプロイしました」等）を補完する目的。

#### 2. 期限超過リマインダー（`detect_overdue_items`）

`db_utils.fetch_overdue_items()` で期限超過の open AI を取得し、担当者にDMで通知する。

- cooldown: 7日（同一AIへの再通知間隔）
- 担当者ごとにまとめて1通で送信（最大5件表示、超過分は「…他N件」）

#### 3. 期限前警告（`detect_approaching_deadlines`）

期限まで `warn_days_before`（デフォルト3日）以内の open AI を検出して担当者に警告する。

- 対象: `due_date >= today AND due_date <= today + warn_days_before`
- cooldown: `warn_days_before` と同値（期限前に1回のみ通知）

#### 4. 未確認決定事項フォロー（`detect_unacknowledged_decisions`）

`stale_days`（デフォルト7日）以上 `acknowledged_at` が NULL の決定事項をリーダー会議チャンネルに通知する。

- 対象: `decided_at <= today - stale_days` かつ `acknowledged_at IS NULL`
- 通知先: `leader_channel`（デフォルト: `C08SXA4M7JT`）
- cooldown: 7日
- 最大10件表示

#### 5. 長期停滞検出（`detect_stale_items`）

`stale_days`（デフォルト14日）以上更新のない open AI を検出する。

- 最終更新日の判定: `audit_log` の最新 `changed_at` → なければ `extracted_at`
- 通知: 担当者にDM + リーダー会議チャンネルにサマリー
- cooldown: 14日

#### 6. マイルストーン健全性（`detect_milestone_health`）

マイルストーンの完了率が期限に対する経過割合から期待される水準を下回る場合にアラートする。

- 判定式: `completion_rate < elapsed_ratio * threshold` かつ `elapsed_ratio > 0.2`
- `threshold`: デフォルト0.7（経過割合の70%が期待完了率）
- 通知先: `leader_channel`
- cooldown: 7日

#### 7. 週次トレンド悪化（`detect_weekly_trend_alert`）

直近2週の完了件数が前2週より `decline_threshold`（デフォルト50%）以上減少した場合にアラートする。

- `db_utils.fetch_weekly_trends()` で直近4週のデータを使用
- 通知先: `leader_channel`
- cooldown: 7日

### 通知の送信先とフォールバック

```
担当者宛通知:
  user_id 解決成功 → DM（conversations.open → chat.postMessage）
  user_id 解決失敗 → leader_channel に「*担当者名* さん宛:」付きで投稿

チャンネル通知:
  leader_channel に直接投稿
```

Block Kit ボタン（完了確認）はDMにのみ送信。leader_channel フォールバック時はテキストのみ。

### 完了確認フロー（Block Kit）

```
patrol_detect.py
  └─ 完了シグナル検出
       ↓
patrol_actions.py: send_completion_confirm()
  ├─ patrol_state.db に pending_confirmations を作成（status='pending'）
  ├─ Block Kit メッセージを DM に送信
  │    ├─ 「完了にする」ボタン（action_id: patrol_approve_close, value: pending_id）
  │    └─ 「まだ完了していない」ボタン（action_id: patrol_reject_close, value: pending_id）
  └─ notifications テーブルに記録
       ↓ ボタン押下
pm_qa_server.py: app.action("patrol_approve_close" / "patrol_reject_close")
  └─ patrol_confirm.py
       ├─ handle_approve_close():
       │    ├─ pending_confirmations.status → 'approved'
       │    ├─ action_items.status → 'closed'（audit_log 付き）
       │    └─ 元メッセージをテキスト更新（ボタン削除）
       └─ handle_reject_close():
            ├─ pending_confirmations.status → 'rejected'
            └─ 元メッセージをテキスト更新（ボタン削除）
```

### 担当者名 → Slack user_id 解決（`patrol_users.py`）

pm.db の `assignee` は「西澤」のような日本語表示名だが、Slack DM には `user_id` が必要。
`UserResolver` が以下の3段階で解決する:

| 優先順位 | 方式 | 速度 | 説明 |
|---|---|---|---|
| 1 | `patrol_state.db` の `user_cache` | 即時 | 24時間キャッシュ。ヒットすれば API 呼び出しなし |
| 2 | Slack DB マイニング | 高速 | `data/C*.db` の `messages.user_name` を LIKE 検索 |
| 3 | Slack API フォールバック | 低速 | `users.list` で全メンバー取得（1回のみ、以降はメモリキャッシュ） |

解決成功時は自動的に `user_cache` に記録される。解決失敗時は `None` を返し、通知は leader_channel にフォールバックする。

### patrol_state.db スキーマ

平文 sqlite3。機密情報を含まないため暗号化不要。90日以上前のレコードは自動 prune。

#### notifications（通知スロットリング）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `event_type` | TEXT | 検出器名（`completion_confirm`・`overdue_reminder`・`deadline_warning`・`decision_followup`・`stale_alert`・`milestone_alert`・`weekly_trend_alert`） |
| `target_key` | TEXT | 対象を一意に特定するキー（`ai:{id}`・`decision:{id}`・`milestone:{id}`・`trend:{date}`） |
| `sent_at` | TEXT | 送信日時（UTC ISO8601） |
| `channel_id` | TEXT | 送信先チャンネル |
| `message_ts` | TEXT | 送信メッセージの ts |

UNIQUE(event_type, target_key)。同一キーは UPSERT で `sent_at` を更新。

#### pending_confirmations（承認待ち管理）

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK（Block Kit の `value` に使用） |
| `action_type` | TEXT | `close_ai` |
| `target_id` | INTEGER | 対象 action_items.id |
| `proposed_by` | TEXT | `patrol`（固定） |
| `evidence` | TEXT | 検出根拠（キーワードマッチの場合はスレッド返信テキスト、LLM判定の場合は `[LLM判定] 根拠`） |
| `created_at` | TEXT | 作成日時（UTC ISO8601） |
| `status` | TEXT | `pending` → `approved` / `rejected` |
| `resolved_at` | TEXT | 承認/却下日時 |
| `resolved_by` | TEXT | 承認/却下した Slack user_id |

#### user_cache（ユーザーID キャッシュ）

| カラム | 型 | 説明 |
|---|---|---|
| `display_name` | TEXT | PK（日本語表示名） |
| `user_id` | TEXT | Slack user_id |
| `cached_at` | TEXT | キャッシュ日時（UTC ISO8601）。24時間超過でキャッシュミス扱い |

### patrol_config.yaml 全パラメータ

```yaml
patrol:
  enabled: true                      # Patrol Agent 全体の有効/無効
  leader_channel: C08SXA4M7JT        # エスカレーション・サマリー投稿先チャンネル

  completion_detection:
    enabled: true                    # 完了シグナル検出の有効/無効
    use_llm: true                    # LLM による第2段判定を使うか
    max_reply_age_days: 7            # スレッド返信の検索範囲（日数）

  overdue_reminder:
    enabled: true                    # 期限超過リマインダーの有効/無効
    cooldown_days: 7                 # 同一AIへの再通知間隔

  deadline_warning:
    enabled: true                    # 期限前警告の有効/無効
    warn_days_before: 3              # 期限何日前に警告するか

  decision_followup:
    enabled: true                    # 未確認決定事項フォローの有効/無効
    stale_days: 7                    # 何日間未確認で通知するか
    cooldown_days: 7                 # 再通知間隔

  stale_detection:
    enabled: true                    # 長期停滞検出の有効/無効
    stale_days: 14                   # 何日間変化なしで停滞とみなすか
    cooldown_days: 14                # 再通知間隔

  milestone_health:
    enabled: true                    # マイルストーン健全性チェックの有効/無効
    threshold: 0.7                   # 完了率 < (経過割合 * threshold) でアラート
    cooldown_days: 7                 # 再通知間隔

  weekly_trend:
    enabled: true                    # 週次トレンド悪化検出の有効/無効
    decline_threshold: 0.5           # 50%以上減少でアラート
```

### Patrol CLI

```bash
# 通常実行
python3 scripts/pm_argus_patrol.py

# DB・Slack 変更なし（動作確認用）
python3 scripts/pm_argus_patrol.py --dry-run

# 特定の検出器のみ実行（--only に渡せる名前は下表参照）
python3 scripts/pm_argus_patrol.py --only overdue
python3 scripts/pm_argus_patrol.py --only completion,deadline

# 承認待ち一覧
python3 scripts/pm_argus_patrol.py --list-pending
```

**`--only` に渡せる検出器名**:

| 名前 | 対応する検出関数 |
|---|---|
| `completion` | `detect_completion_signals` |
| `overdue` | `detect_overdue_items` |
| `deadline` | `detect_approaching_deadlines` |
| `decision` | `detect_unacknowledged_decisions` |
| `stale` | `detect_stale_items` |
| `milestone` | `detect_milestone_health` |
| `trend` | `detect_weekly_trend_alert` |

### Patrol cron 設定

```
*/30 * * * 1-5 cd /lvs0/.../ProjectManagement && \
  source ~/.secrets/slack_tokens.sh && \
  source ~/.secrets/rivault_tokens.sh && \
  ~/.venv_aarch64/bin/python3 scripts/pm_argus_patrol.py \
  >> logs/pm_argus_patrol.log 2>&1
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

## 検索サブシステム（FTS5 インデックス）

`/argus-investigate` の `search_text` ツールおよび内部のハイブリッド検索で使用される全文検索基盤。

### インデックスの分割方針

| インデックス | DBファイル | 想定ソース |
|---|---|---|
| `pm` | `data/qa_pm.db` | Leader_Meeting + リーダー会議チャンネル |
| `pm-hpc` | `data/qa_pm-hpc.db` | Block1/2、SubWGx + HPCアプリWGチャンネル |
| `pm-bmt` | `data/qa_pm-bmt.db` | BenchmarkWG_Meeting + ベンチマークWGチャンネル |
| `pm-pmo` | `data/qa_pm-pmo.db` | Co-design_Review等 + PMO系チャンネル |

実際のマッピングは `data/argus_config.yaml` で定義する。

### 索引対象コンテンツ

| source_type | 説明 | 採用理由 |
|---|---|---|
| `minutes_content` | 議事録本文（段落単位チャンク） | 生の議事内容を参照するため |
| `slack_raw` | Slack生メッセージ（スレッド単位でまとめたチャンク） | 要約で失われるニュアンス・文脈を保持するため |
| `document` | BOXドキュメントメタデータ（タイトル・説明・種別） | Slack上で共有された資料の発見可能性向上のため |
| `web` | 外部Web記事（RIKEN公式・HPC系ニュース・NVIDIAブログ等） | プロジェクト関連の公開情報を検索するため |

LLM抽出の `decisions`・`action_items` は**索引対象外**。
抽出精度の問題で誤情報を提示するリスクがあるため、議事録本文とSlack生メッセージのみを使用する。

### argus_config.yaml の構造

```yaml
indices:
  pm:
    db: data/qa_pm.db
    minutes: [Leader_Meeting, Co-design_Review_Meeting]
    channels: [C08M0249GRL, C08SXA4M7JT, ...]

  pm-hpc:
    db: data/qa_pm-hpc.db
    minutes: [Block1_Meeting, Block2_Meeting, SubWG3_Meeting, ...]
    channels: [C08LSJP4R6K, C093DQFSCRH, C093LP1J15G]

  pm-bmt:
    db: data/qa_pm-bmt.db
    minutes: [BenchmarkWG_Meeting]
    channels: [C08MJ0NF5UZ, C096ER1A0LU]

  pm-pmo:
    db: data/qa_pm-pmo.db
    minutes: []
    channels: [C08PE3K9N72]

# チャンネル → インデックス名のマッピング（investigate時のインデックス自動選択に使用）
channel_map: {}

# マッピングなしチャンネルのフォールバック
default_index: pm
```

### インデックスDB スキーマ（各DBで共通）

```sql
CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,   -- 'minutes_content' | 'slack_raw' | 'document' | 'web'
    source_db   TEXT NOT NULL,   -- 'minutes/Leader_Meeting.db' | 'C08SXA4M7JT.db' | 'docs_pm.db' | 'web_articles.db'
    record_id   TEXT,
    held_at     TEXT,            -- YYYY-MM-DD
    content     TEXT NOT NULL,   -- 原文チャンク（最大1000文字）
    tokens      TEXT,            -- SudachiPy形態素解析トークン（スペース区切り）
    source_ref  TEXT,            -- Slackパーマリンク or 会議ID
    indexed_at  TEXT NOT NULL
);

-- trigram検索用（content全文）
CREATE VIRTUAL TABLE fts USING fts5(
    content, content='chunks', content_rowid='id', tokenize='trigram'
);

-- 形態素解析トークン検索用（名詞・動詞・形容詞・副詞の辞書形）
CREATE VIRTUAL TABLE fts_tokens USING fts5(
    tokens, content='chunks', content_rowid='id', tokenize='unicode61'
);

CREATE TABLE index_state (
    source_db TEXT PRIMARY KEY,
    last_indexed TEXT
);
```

### 検索アルゴリズム（retrieve_chunks）

```
質問: "GPU性能の評価方針について"

Step 1: SudachiPy形態素解析
  "GPU性能の評価方針について"
  → 名詞・動詞・形容詞・副詞の辞書形を抽出
  → ["GPU", "性能", "評価", "方針"]  ← 2文字名詞も正しく抽出

Step 2: fts_tokens AND検索（段階的トークン削減）
  MATCH "GPU 性能 評価 方針"
  → ヒットなしなら先頭3語 → 先頭2語 → 先頭1語の順で再試行

Step 3: trigram FTS5 フォールバック（Step 2がヒットしない場合）
  ひらがな連続列で分割 → 3文字以上のトークンを AND 検索
  先頭3語 → 先頭2語 → 先頭1語の順で再試行

Step 4: LIKE検索フォールバック
  LIKE '%GPU%'

Step 5: 最新日付レコード（それでも0件の場合）
  日付降順で最近30件を返す
```

SudachiPy形態素解析が主検索。trigramは「性能」「評価」などの2文字名詞がヒットしない
ため補助的な位置づけ。

### LLM re-ranking

FTS検索で30件取得後、LLMが質問との関連度を判定して上位5件に絞り込む。

```
FTS検索（30件）
  ↓
re-rankプロンプト:
  各チャンクの先頭400文字を番号付きで提示
  「関連する5件の番号をスペース区切りで出力」
  max_tokens=30, timeout=30秒
  ↓
LLMが選んだ番号のチャンクのみを回答生成へ渡す

※ LLMエラー・番号が得られない場合は先頭5件で代替（フォールバック）
```

件数より**re-rankが見るプレビューの長さ（400文字）**が回答品質を支配する。
プレビューが短いと関連度判定が外れ、的外れなチャンクが回答生成に渡される。

### チャンネル→インデックスの解決

```
/argus-investigate 実行 (channel_id=C08MJ0NF5UZ)
  ↓
channel_map.get("C08MJ0NF5UZ") → なければ default_index="pm"
  ↓
index_db_map["pm"] → Path("data/qa_pm.db")
  ↓
qa_pm.db に対して FTS5検索
```

### 出典のチャンネル名表示

Slack要約の出典はチャンネルIDではなく人が読みやすい名称で表示する（`pm_qa_server.py` の `_CHANNEL_NAMES` で管理）:

| チャンネルID | 表示名 |
|---|---|
| C08M0249GRL | 20_アプリケーション開発エリア |
| C08SXA4M7JT | 20_1_リーダ会議メンバ |
| C08LSJP4R6K | 21_hpcアプリケーションwg |
| C093DQFSCRH | 21_1_hpcアプリケーションwg_ブロック1 |
| C093LP1J15G | 21_2_hpcアプリケーションwg_ブロック2 |
| C08MJ0NF5UZ | 22_ベンチマークwg |
| C096ER1A0LU | 23_benchmark_framework |
| C0A6AC59AHM | 24_ai-hpc-application |
| C08PE3K9N72 | pmo |

### ドキュメントレジストリの索引化

`pm_embed.py` は各インデックスに対応する `docs_{index_name}.db` が存在する場合、ドキュメントレジストリをFTS5チャンクとして索引化する。1ドキュメント = 1チャンク（タイトル・種別・説明・共有者・トピック・URLを連結）。

```
pm_document_extract.py → docs_pm.db（ドキュメントメタデータ）
pm_embed.py           → qa_pm.db（FTS5チャンクとして索引化）
/argus-investigate    → qa_pm.db を search_text ツール経由で検索
```

### Slack Bolt + Socket Modeの3秒タイムアウト対策

```
/argus-investigate を実行
  ├─ ack()                            ← 即時（3秒以内）
  ├─ respond("Argus 調査中...")       ← 即時表示
  └─ executor.submit(_run_investigate) ← バックグラウンドスレッドへ
         ├─ シードデータ収集
         ├─ LLM → tool_call → 実行 → LLM（最大5ステップ）
         └─ respond(回答, replace_original=True)
```

### 検索パラメータ

| パラメータ | 値 | 説明 |
|---|---|---|
| `TOP_K_RETRIEVE` | 30 | FTS検索で取得する件数 |
| `TOP_K_RERANK` | 5 | re-rank後に回答生成へ渡す件数 |
| `rerank preview` | 400文字 | re-rankプロンプトでの各チャンク提示長 |
| `MAX_TOKENS` | 1024 | 回答生成の最大トークン数 |
| `LLM_TIMEOUT` | 120秒 | 回答生成のタイムアウト |
| `RERANK_TIMEOUT` | 30秒 | re-rankのタイムアウト |
| `CHUNK_MAX_CHARS` | 1000 | チャンク最大文字数 |
| `CHUNK_OVERLAP_CHARS` | 100 | チャンクオーバーラップ文字数 |

### pm_embed.py のオプション

| オプション | デフォルト | 説明 |
|---|---|---|
| `--full-rebuild` | なし | 既存インデックスを全削除して再構築 |
| `--index-name NAME` | 全インデックス | 特定インデックスのみ処理 |
| `--config PATH` | `data/argus_config.yaml` | 設定ファイルのパス |
| `--data-dir PATH` | `data` | ソースDBの検索ディレクトリ |
| `--dry-run` | なし | 書き込みなしでチャンク数のみ表示 |

### 既存ワークフローへの統合

データ取り込み後に差分インデックスを自動更新する場合、各スクリプトの末尾に追加する:

```bash
# pm_from_slack.sh の末尾
"$PYTHON3" "$SCRIPT_DIR/pm_embed.py"

# pm_from_recording.sh の末尾
"$PYTHON3" "$SCRIPT_DIR/pm_embed.py" --index-name pm  # 該当インデックスのみ
```

---

## 設定ファイル

### `data/argus_config.yaml`

全 Argus コマンドが参照するチャンネル・インデックスDBの統合設定。
`indices.{name}.channels` が生メッセージ収集対象チャンネルと FTS5 索引対象を定義する。

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
| `ARGUS_CONFIG` | 任意 | `data/argus_config.yaml` | 設定ファイルパス |

`pm_qa_start.sh` が `~/.secrets/slack_tokens.sh` と `~/.secrets/rivault_tokens.sh` を自動 source するため、デーモン起動時は手動設定不要。CLI 直接実行時は `source ~/.secrets/slack_tokens.sh` が必要。

---

## セットアップ（初回のみ）

### 1. 依存パッケージのインストール

```bash
~/.venv_aarch64/bin/pip install sudachipy sudachidict_core
```

### 2. Slack アプリへのコマンド登録

[api.slack.com/apps](https://api.slack.com/apps) でアプリに以下を登録:

- `/argus-brief` — Short Description: 今日の状況サマリーと優先アクション
- `/argus-draft` — Short Description: 草案生成（`agenda`/`report`/`request`）
- `/argus-risk` — Short Description: リスク一覧と対応提案
- `/argus-transcribe` — Short Description: 会議録音の文字起こし・議事録生成
- `/argus-investigate` — Short Description: マルチステップ調査（Agent）

Request URL は Socket Mode では無視されるため任意の HTTPS URL でよい。

**Patrol Agent の Block Kit ボタン**: Socket Mode を使用している場合、`app.action()` ハンドラは追加設定なしで動作する（Interactivity は Socket Mode で自動有効）。Bot Token に `chat:write`、`im:write`、`users:read` スコープが必要。

### 3. FTS5 インデックスの構築

```bash
cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement

# 件数確認（書き込みなし）
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --dry-run

# 全インデックス構築
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --full-rebuild

# 特定インデックスのみ
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --index-name pm-bmt --full-rebuild
```

### 4. cron の設定確認

```bash
crontab -l | grep argus
```

現在の設定:
- 平日朝 8:57: `--brief-to-canvas` を自動実行
- 平日30分ごと: `pm_argus_patrol.py` を自動実行

### 5. 自動再起動の設定（任意）

```cron
*/5 * * * * bash /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_qa_start.sh >> /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_qa_cron.log 2>&1
```

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

**「記録が見つかりません」と返ってくる（`/argus-investigate` の search_text）:**
- 対象チャンネルに対応するインデックスに当該会議・チャンネルが含まれているか `argus_config.yaml` を確認
- `pm_embed.py --full-rebuild --index-name <name>` で再構築

**re-rankエラーがログに出る:**
- `re-rankエラー: ...` と出た場合、LLMのコンテキスト長超過の可能性がある
- `TOP_K_RETRIEVE` を減らすか、`rerank preview` の文字数（`pm_qa_server.py` の400）を下げる

**SudachiPyが利用不可とログに出る:**
- `~/.venv_aarch64/bin/pip install sudachipy sudachidict_core` を実行
- インストール後 `pm_embed.py --full-rebuild` でインデックスを再構築する

---

## 検索品質の改善候補（優先度順）

### 現状の制約

| 項目 | 現状 | 備考 |
|---|---|---|
| 検索方式 | SudachiPy形態素解析 + FTS5 キーワードマッチ | 意味的な近さは考慮しない |
| インデックス更新 | 手動または既存スクリプトへの追記 | crontabで定期自動更新が望ましい |
| 出典リンク | Slack投稿済み議事録のみリンクあり | `pm_minutes_import.py --post-to-slack` で増やせる |

### P1: セマンティック検索の導入（最大効果）

現在のFTS5は「単語の一致」のみ。Embeddingモデルで「意味の近さ」による検索を追加する。

- `sqlite-vec` または `faiss` にチャンクのベクトルを保存
- 質問もベクトル化して近傍探索
- FTS5（キーワード）とのハイブリッド検索（BM25 + コサイン類似度）が最も精度が高い
- **前提**: Embeddingモデル用のGPUメモリが確保できること
- **候補モデル**: `BAAI/bge-m3`（570M、多言語）、`intfloat/multilingual-e5-large`（560M）、`cl-nagoya/sup-simcse-ja-large`（330M、日本語特化）
- gemma4（チャットモデル）はEmbeddingに使用不可。別ポートで専用モデルを起動する必要がある

```bash
# 例: 別ポートでEmbeddingモデルを起動
vllm serve BAAI/bge-m3 --task embed --port 8001
```

### P2: チャンク設計の改善

- **見出しの引き継ぎ**: 各チャンク先頭に所属する議題名を付与（例: `【2026-01-14 リーダー会議 / GPU性能評価】`）することでLLMの文脈理解が向上
- **親子チャンク**: 小チャンク（200文字）でFTS検索 → ヒットしたら親チャンク（1000文字）を回答生成に渡す
- **議題単位の分割**: 現在の段落分割から `##` 見出し単位の分割に変更

### P3: Re-rankingの改善

- **スコアリング方式**: 現在の「番号選択」から「各チャンクに0〜10点のスコアをつけよ」に変更することで判定精度が向上
- **Cross-Encoderモデル**: ms-marco等の小型モデルで質問とチャンクのペアを直接スコアリング（LLM呼び出し不要）

### P4: 回答生成プロンプトの改善

- **インライン引用の強制**: 回答中に `（2026-01-14 リーダー会議）` のようにインライン引用させることで根拠なき回答を抑制
- **時系列フィルタリング**: 「最近の」「直近の」などの表現を検出して検索対象を直近N件に絞る
- **Chain-of-Thought**: 回答前に取得情報と質問の関連性を考えさせてから回答させる

### P5: クエリ拡張

- 質問をそのまま検索するのではなく、LLMに「別の言い方で3パターン生成」させて複数クエリで検索しマージする（HyDE: Hypothetical Document Embeddings）
