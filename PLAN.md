# Argus — AI Project Intelligence System 実装計画

## Context

現在のPMシステムは「情報収集・整理・レポート生成」を自動化しているが、「次に何をすべきか」の判断と実行は依然として人間に委ねられている。Slack・議事録・マイルストーンのデータは揃っているのに、プロジェクトマネージャー自身が毎朝データを見てアクションを考える必要がある。

**目標**: AIが自律的に状況を分析し「今日やるべきこと」を提案し、コミュニケーション草案を生成する「AI秘書」を実装する。

**方針**:
- インターフェース: Slack スラッシュコマンド（既存 pm_qa_server.py に統合）
- Phase 1 コマンド: `/pm-brief`, `/pm-draft`, `/pm-risk`（提案・草案のみ、自動実行なし）
- Phase 2（後日）: `/pm-do`（自動実行。LLMのJSON出力精度が安定してから）
- 期待する役割: デイリーブリーフィング・コミュニケーション草案生成・予兆検知

### 入力データ戦略：生メッセージ + RiVault

**要約ではなく生メッセージを使う理由**: Slack DBの `summaries` は「誰が誰に」「ニュアンス」「経緯」が省略される。AI秘書は文脈を正確に把握するため、`messages` + `replies` テーブルの生テキストを使用する。

**LLM**: RiVault（`zai-org/GLM-4.7-Flash`）を主力LLMとして使用する。
- コンテキスト上限: **200k トークン**（1ヶ月分の生メッセージも余裕で収まる）
- ローカルの gemma4（max-model-len=32768）ではブリーフィング等の大量コンテキスト処理に不十分
- 環境変数: `~/.secrets/rivault_tokens.sh` に `RIVAULT_URL` と `RIVAULT_TOKEN` が設定済み
- gemma4は軽量・短文タスク（re-rank等）にのみ残す（現行スクリプトとの後方互換を維持）

### 対象チャンネル

AI秘書がデータ収集する Slack チャンネルは `data/secretary_channels.txt` で指定する（1行1チャンネルID）。デフォルトは以下:

```
C08SXA4M7JT
C08M0249GRL
```

議事録は `data/minutes/` 配下の全 `{kind}.db` を対象とする。

---

## アーキテクチャ

```
Slack (/pm-brief, /pm-draft, /pm-risk)
        |
        | Socket Mode（既存 pm_qa_server.py に統合）
        v
  +-----------------------------------------+
  |  入力データ収集層                        |
  |  ・messages + replies（生メッセージ）    |
  |    SQLCipher暗号化DB: open_db(encrypt=True)|
  |  ・data/minutes/{kind}.db（議事録本文） |
  |  ・pm.db（AI・決定事項・マイルストーン）|
  +-----------------------------------------+
        |
        v
  +-----------------------------------------+
  |  LLM 層                                  |
  |  RiVault: GLM-4.7-Flash（200k context） |
  |  <- cli_utils.call_rivault() 新規追加    |
  |  gemma4: 軽量タスクのみ（後方互換）     |
  +-----------------------------------------+
        |
        v
  ephemeral 返答（Slack）
  Canvas投稿（AI秘書専用 Canvas を新規作成）
```

---

## 実装前の前提確認

### Step 0: RiVault API 互換性テスト

実装に入る前に、以下を手動で確認する:

```bash
source ~/.secrets/rivault_tokens.sh
# RIVAULT_URL には末尾の /v1 が含まれているため、そのまま /chat/completions を付加する
curl -s "$RIVAULT_URL/chat/completions" \
  -H "Authorization: Bearer $RIVAULT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zai-org/GLM-4.7-Flash",
    "messages": [{"role": "user", "content": "1+1は？"}],
    "max_tokens": 32
  }'
```

確認事項:
- RIVAULT_URL は末尾に `/v1` を含む形式（例: `https://rivault.example/v1`）
- レスポンスが `choices[0].message.content` 形式か
- 認証が `Authorization: Bearer {token}` か
- ストリーミング対応の有無

結果に応じて `call_rivault()` の実装を調整する。

---

## 実装ステップ

### Step 1: Slack アプリへのコマンド登録（手動・前提条件）

既存の /ask アプリ（pm_qa_server.py が使用中）に以下を追加:
- `/pm-brief` — 今日の状況サマリーと推奨アクション
- `/pm-draft` — コミュニケーション草案を生成（引数: 用途 件名）
- `/pm-risk` — リスク一覧と対応提案

既存の Bot Token Scopes（`commands`, `chat:write`）で動作する。新しい App は不要。

---

### Step 2: cli_utils.py に call_rivault() を追加

`_call_openai_compat()` と同じパターンで、RiVault 専用の呼び出し関数を追加する。

```python
def call_rivault(prompt: str, *, model: str = "zai-org/GLM-4.7-Flash",
                 timeout: int = 300, max_tokens: int = 8192,
                 system: str = "") -> str:
    """
    RiVault (GLM-4.7-Flash, 200k context) を呼び出す。
    環境変数:
        RIVAULT_URL   -- エンドポイント URL
        RIVAULT_TOKEN -- API トークン
    Step 0 の結果に応じて no_stream を調整する。
    """
    base_url = os.environ.get("RIVAULT_URL")
    if not base_url:
        raise RuntimeError("RIVAULT_URL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください")
    api_key = os.environ.get("RIVAULT_TOKEN", "dummy")
    return call_local_llm(
        prompt,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_tokens=max_tokens,
        system=system,
        no_stream=True,  # Step 0 の結果で変更する可能性あり
    )
```

既存の `call_claude()` / `_call_openai_compat()` は変更しない（後方互換維持）。

**変更対象**: `scripts/cli_utils.py`（関数追加のみ）

---

### Step 3: 生メッセージ・議事録収集関数を作成

`scripts/pm_argus.py` を新規作成し、データ収集ロジックをまとめる。
（Slackハンドラとデータ収集ロジックを分離する。テスト容易性のため。）

```python
# pm_argus.py — AI秘書のデータ収集・プロンプト構築ロジック

def fetch_raw_messages(channel_id: str, since_date: str, *,
                       data_dir: Path, no_encrypt: bool = False) -> str:
    """
    Slack DB ({channel_id}.db) から messages + replies を取得し、
    "[YYYY-MM-DD HH:MM] user_name: text" 形式で整形して返す。
    DB は SQLCipher 暗号化済み → open_db(encrypt=not no_encrypt) で接続する。
    """

def fetch_recent_minutes(since_date: str, *,
                         minutes_dir: Path, no_encrypt: bool = False) -> str:
    """
    data/minutes/{kind}.db の meetings + minutes_content テーブルから
    held_at >= since_date の議事録本文を取得して返す。
    """

def fetch_pm_stats(conn) -> dict:
    """
    pm.db から統計データを収集する。
    db_utils: fetch_milestone_progress(), fetch_assignee_workload()
    pm_insight: fetch_overdue_items(), fetch_unacknowledged_decisions(),
                fetch_unlinked_items_count(), fetch_no_assignee_count(),
                fetch_weekly_trends()
    """

def build_brief_prompt(messages: str, minutes: str, stats: dict,
                       context: str, today: str) -> str:
    """ブリーフィング生成用プロンプトを構築する"""

def build_draft_prompt(purpose: str, subject: str,
                       messages: str, stats: dict, context: str) -> str:
    """草案生成用プロンプトを構築する"""

def build_risk_prompt(messages: str, minutes: str, stats: dict,
                      context: str, today: str) -> str:
    """リスク分析用プロンプトを構築する"""
```

`pm_insight.py` の `fetch_overdue_items()` 等はモジュールレベル関数なので
`from pm_insight import fetch_overdue_items, ...` で import する。
（pm_insight.py は `if __name__ == "__main__"` ガードがあるため、
import 時の argparse 副作用は発生しない。）

**新規作成**: `scripts/pm_argus.py`

---

### Step 4: pm_qa_server.py にコマンドハンドラを追加

既存の `build_app()` 内に `/pm-brief`, `/pm-draft`, `/pm-risk` のハンドラを追加する。
pm_qa_server.py は既に Socket Mode + ThreadPoolExecutor + ack/respond パターンが確立しているため、
新しいデーモンを立てずに統合する。

```python
# build_app() 内に追加:

@app.command("/pm-brief")
def handle_pm_brief(ack, respond, command):
    ack()
    respond(text=":hourglass_flowing_sand: 分析中...", response_type="ephemeral")
    executor.submit(_run_brief, respond, command)

@app.command("/pm-draft")
def handle_pm_draft(ack, respond, command):
    ack()
    text = (command.get("text") or "").strip()
    if not text:
        respond(
            text="用途と件名を指定してください。\n"
                 "例: `/pm-draft agenda 次回リーダー会議`\n"
                 "用途: `agenda`(会議アジェンダ), `report`(進捗報告), `request`(確認依頼)",
            response_type="ephemeral",
        )
        return
    respond(text=":hourglass_flowing_sand: 草案生成中...", response_type="ephemeral")
    executor.submit(_run_draft, respond, command)

@app.command("/pm-risk")
def handle_pm_risk(ack, respond, command):
    ack()
    respond(text=":hourglass_flowing_sand: リスク分析中...", response_type="ephemeral")
    executor.submit(_run_risk, respond, command)
```

**各コマンドの内部処理**:

#### `/pm-brief` (_run_brief)
1. `data/secretary_channels.txt` から対象チャンネルIDリストを読み込む
2. 各チャンネルの `{channel_id}.db` を `open_db(encrypt=True)` で開き、直近30日分の messages + replies を取得・整形
3. `data/minutes/` 配下の全 `{kind}.db` から直近30日分の議事録本文を取得
4. pm.db を `open_pm_db()` で開き、統計データを収集（`fetch_milestone_progress()`, `fetch_overdue_items()` 等）
5. `call_rivault()` でブリーフィング生成
   - プロンプト: 「今日 {date} 時点の状況を踏まえ、プロジェクトマネージャーが優先的に対応すべきことを5件以内でリストアップせよ。各項目に具体的な次の一手を添えよ。」
6. 結果を ephemeral で返す

#### `/pm-draft <purpose> <subject>` (_run_draft)
purpose を明示的に指定させる（LLMによる自動判定はしない）:
- `agenda` → 会議アジェンダ: 直近Slack生メッセージ + 未確認決定事項 + 未完了AI
- `report` → 進捗報告: マイルストーン進捗 + 直近2週間の完了AI + 期限超過AI
- `request` → 確認依頼: 担当者別負荷 + 期限超過AI + 対象者の関連メッセージ

`call_rivault()` で草案生成し、ephemeral で返す（コピペして使う）。

#### `/pm-risk` (_run_risk)
1. 直近30日の生メッセージ + 議事録 + pm.db 統計を収集
2. `call_rivault()` でリスク分析: 「定量データと会話の文脈から、顕在化しているリスクだけでなく、放置すると問題になりうる予兆も含めて列挙せよ。各リスクに優先度（高/中/低）と推奨対応を付けよ。」
3. 結果を ephemeral で返す
4. AI秘書専用 Canvas にリスク分析結果を投稿（オプション: `--canvas-id` 指定時のみ）

**変更対象**: `scripts/pm_qa_server.py`

---

### Step 5: AI秘書専用 Canvas の作成

既存の Project Management Canvas (`F0ALP1XQJHL`) を上書きすると pm_report.py のレポートが消えるため、
AI秘書専用の Canvas を新規作成する。

手順:
1. Slack API (`canvases.create`) で新規スタンドアロン Canvas を作成
2. Canvas ID を `data/secretary_canvas_id.txt` に保存
3. `/pm-brief --to-canvas` 実行時にこの Canvas に投稿（`post_to_canvas()` で全体上書きOK）

Canvas の内容構成:
```markdown
# AI秘書レポート（2026-04-16）

## 今日の優先事項
- 井上さん: M3マイルストーン担当者未割当5件の確認（期限超過リスク）
- 西澤さんへの対応状況確認（期限7日超過: アイテムXX）
- 未確認決定事項3件を次回会議アジェンダに追加

## リスク・予兆
- [高] ...
- [中] ...

---
_生成: 2026-04-16 09:00 JST_
```

---

### Step 6: pm_qa_start.sh を更新

既存の `pm_qa_start.sh` に RiVault トークンの読み込みと対象チャンネル設定を追加する。

```bash
# 追加行:
if [[ -f "$HOME/.secrets/rivault_tokens.sh" ]]; then
    source "$HOME/.secrets/rivault_tokens.sh"
fi
```

pm_argus_start.sh / pm_argus_stop.sh は不要（pm_qa_server.py に統合するため）。

**変更対象**: `scripts/pm_qa_start.sh`

---

### Step 7: デイリー自動実行（cron 設定）

毎朝 8:57 JST にブリーフィングを自動生成し Canvas に投稿:

```bash
# crontab に追加
57 8 * * 1-5  cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement && \
  source ~/.secrets/slack_tokens.sh && source ~/.secrets/rivault_tokens.sh && \
  ~/.venv_aarch64/bin/python3 scripts/pm_argus.py --brief-to-canvas
```

`pm_argus.py --brief-to-canvas` モード: Socket Mode を使わず、ブリーフィング生成 → Canvas 投稿のみを実行するCLIモード。

---

## 変更対象ファイル

| ファイル | 変更種別 | 内容 |
|---|---|---|
| `scripts/cli_utils.py` | **変更** | `call_rivault()` 関数を追加 |
| `scripts/pm_argus.py` | **新規作成** | データ収集・プロンプト構築ロジック + `--brief-to-canvas` CLIモード |
| `scripts/pm_qa_server.py` | **変更** | `/pm-brief`, `/pm-draft`, `/pm-risk` ハンドラを追加 |
| `scripts/pm_qa_start.sh` | **変更** | `source ~/.secrets/rivault_tokens.sh` を追加 |
| `data/secretary_channels.txt` | **新規作成** | 対象チャンネルIDリスト |

---

## 再利用する既存コンポーネント

| コンポーネント | ファイル | 用途 |
|---|---|---|
| `call_local_llm()` | `scripts/cli_utils.py` | `call_rivault()` が内部で使用（変更なし） |
| `load_claude_md_context()` | `scripts/cli_utils.py` | プロジェクト文脈をプロンプトに埋め込み |
| `open_db(encrypt=True)` | `scripts/db_utils.py` | Slack DB（SQLCipher暗号化）の接続 |
| `open_pm_db()` | `scripts/db_utils.py` | pm.db 接続 |
| `fetch_milestone_progress()` | `scripts/db_utils.py` | マイルストーン進捗取得 |
| `fetch_assignee_workload()` | `scripts/db_utils.py` | 担当者別負荷取得 |
| `fetch_overdue_items()` | `scripts/pm_insight.py` | 期限超過アイテム取得（import して使用） |
| `fetch_unacknowledged_decisions()` | `scripts/pm_insight.py` | 未確認決定事項取得（import して使用） |
| `fetch_unlinked_items_count()` | `scripts/pm_insight.py` | 未紐づけ件数取得（import して使用） |
| `fetch_no_assignee_count()` | `scripts/pm_insight.py` | 担当者なし件数取得（import して使用） |
| `fetch_weekly_trends()` | `scripts/pm_insight.py` | 週次トレンド取得（import して使用） |
| `post_to_canvas()` | `scripts/canvas_utils.py` | AI秘書専用Canvas への投稿 |
| `sanitize_for_canvas()` | `scripts/canvas_utils.py` | Markdown 整形 |
| Socket Mode + ThreadPoolExecutor | `scripts/pm_qa_server.py` | 既存基盤にハンドラを追加 |

---

## 検証方法

1. **Step 0**: `curl` で RiVault API の互換性を確認。レスポンス形式を記録
2. **Step 2**: `call_rivault("1+1は？")` を Python REPL から呼び出してレスポンスを確認
3. **Step 3**: `pm_argus.py --brief-to-canvas --dry-run` で Canvas 投稿なしにブリーフィング生成を確認
4. **Step 4**: pm_qa_server.py を再起動し、Slack で `/pm-brief` を実行して ephemeral メッセージが返ることを確認
5. `/pm-draft agenda 次回リーダー会議` でアジェンダ草案が返ることを確認
6. `/pm-risk` でリスク分析結果が返ることを確認

---

## Phase 2（後日）: /pm-do 自動実行

Phase 1 で GLM-4.7-Flash の出力品質（特にJSON構造化）が安定したことを確認してから着手する。

設計方針:
- `/pm-brief` がアクション提案に `action_id` を付与
- 提案内容を `secretary_proposals` テーブルに保存
- `/pm-do a1` で対応する提案を pm.db に反映（assign_item, close_item 等）
- 実行前に対象のaction_item IDをユーザーに確認表示する安全策を入れる

---

## 注意事項

- **RiVault**: `source ~/.secrets/rivault_tokens.sh` で `RIVAULT_URL` / `RIVAULT_TOKEN` を設定。pm_qa_start.sh でこの source を自動実行する
- **gemma4との使い分け**: `call_rivault()` は大量コンテキスト処理（ブリーフィング・草案生成）に使用。既存の /ask（QA検索）は gemma4 を引き続き使用（変更なし）
- **Slack DB の暗号化**: 生メッセージ取得時は `db_utils.open_db(encrypt=True)` で接続する。鍵は `~/.secrets/pm_db_key.txt` から自動読み込み
- **Canvas の分離**: AI秘書は専用Canvas を使用し、既存の pm_report.py Canvas を上書きしない
- **pm_insight.py からの import**: `fetch_overdue_items` 等はモジュールレベル関数。`if __name__ == "__main__"` ガードがあるため import 時の副作用なし
