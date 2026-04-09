# Slack QAシステム（`/ask`）

会議議事録本文・Slack要約をRAG的に活用し、Slackのスラッシュコマンドで
自然言語QAを行うシステム。

---

## 概要

```
ユーザー: /ask 設計方針について（チャンネル: C08SXA4M7JT）
         ↓ Slack Socket Mode
pm_qa_server.py（常駐デーモン）
  ├─ ack()                          ← 3秒以内に即時応答
  ├─ "検索中..." を表示
  ├─ チャンネルID → インデックスDB  ← qa_config.yaml のマッピングで解決
  ├─ SudachiPy形態素解析 + FTS5検索 ← 関連チャンクを最大30件取得
  ├─ LLM re-ranking                 ← 30件 → 上位5件に絞り込み
  ├─ call_local_llm()               ← vLLM (localhost:8000) で回答生成
  └─ ephemeral返信                  ← 本人のみ見える回答（出典付き）
```

特徴:
- **UIなし** — Slackのスラッシュコマンドのみ
- **チャンネル連動** — 実行チャンネルに応じて検索インデックスを自動切り替え
- **ベクトルDBなし** — SudachiPy形態素解析 + SQLite FTS5 による検索（追加インフラ不要）
- **議事録本文とSlack要約のみ** — LLM抽出の決定事項・AIは索引対象外
- **LLM re-ranking** — FTS5で広く取得後、LLMで関連度上位5件に絞り込み

---

## ファイル構成

```
scripts/
├── pm_embed.py          # インデックス構築（qa_config.yaml に従い各DBに書き込む）
├── pm_qa_server.py      # QAサーバー本体（Slack Socket Modeデーモン）
├── pm_qa_start.sh       # デーモン起動スクリプト（nohup + PIDファイル管理）
└── pm_qa_stop.sh        # デーモン停止スクリプト

data/
├── qa_config.yaml       # インデックス定義・チャンネルマッピング（ユーザーが編集）
├── qa_pm.db             # リーダー会議など汎用インデックス（平文sqlite3）
├── qa_pm-hpc.db         # HPCアプリWGインデックス
├── qa_pm-bmt.db         # ベンチマークWGインデックス
└── qa_pm-pmo.db         # PMOインデックス

logs/
├── pm_qa_server.log     # サーバーログ
└── pm_qa_server.pid     # PIDファイル（起動中のみ存在）
```

---

## 設計

### インデックスの分割方針

| インデックス | DBファイル | 想定ソース |
|---|---|---|
| `pm` | `data/qa_pm.db` | Leader_Meeting + リーダー会議チャンネル |
| `pm-hpc` | `data/qa_pm-hpc.db` | Block1/2、SubWGx + HPCアプリWGチャンネル |
| `pm-bmt` | `data/qa_pm-bmt.db` | BenchmarkWG_Meeting + ベンチマークWGチャンネル |
| `pm-pmo` | `data/qa_pm-pmo.db` | Co-design_Review等 + PMO系チャンネル |

実際のマッピングは `data/qa_config.yaml` で定義する。

### 索引対象コンテンツ

| source_type | 説明 | 採用理由 |
|---|---|---|
| `minutes_content` | 議事録本文（段落単位チャンク） | 生の議事内容を参照するため |
| `slack_summary` | Slackスレッド要約 | 日常的な議論・連絡を参照するため |

LLM抽出の `decisions`・`action_items` は**索引対象外**。
抽出精度の問題で誤情報を提示するリスクがあるため、原文に近い議事録本文とSlack要約のみを使用する。

### qa_config.yaml の構造

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

# /ask 実行チャンネル → インデックス名
channel_map: {}

# マッピングなしチャンネルのフォールバック
default_index: pm
```

### インデックスDB スキーマ（各DBで共通）

```sql
CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,   -- 'minutes_content' | 'slack_summary'
    source_db   TEXT NOT NULL,   -- 'minutes/Leader_Meeting.db' | 'C08SXA4M7JT.db'
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
/ask 実行 (channel_id=C08MJ0NF5UZ)
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

### Slack Bolt + Socket Modeの3秒タイムアウト対策

```
/ask を実行
  ├─ ack()                    ← 即時（3秒以内）
  ├─ respond("検索中...")     ← 即時表示
  └─ executor.submit(_run_qa) ← バックグラウンドスレッドへ
         ├─ retrieve_chunks() ← FTS5検索（~数ms）
         ├─ rerank_chunks()   ← LLM re-ranking（数秒）
         ├─ generate_answer() ← LLM生成（30〜120秒）
         └─ respond(回答, replace_original=True)
```

### 主要パラメータ

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

---

## セットアップ手順

### 1. 依存パッケージのインストール

```bash
~/.venv_aarch64/bin/pip install sudachipy sudachidict_core
```

### 2. Slackアプリの作成（初回のみ・管理者操作）

[api.slack.com/apps](https://api.slack.com/apps) で新規アプリを作成する。

- **Socket Mode**: "Settings > Socket Mode" を有効化し、`connections:write` スコープの App-Level Token（`xapp-`）を生成
- **Bot Token Scopes**: `commands`、`chat:write` を追加
- **Slash Command**: `/ask`（Request URLは任意のHTTPS URL、Socket Modeでは無視される）
- **Install to Workspace** 後、Bot User OAuth Token（`xoxb-`）をコピー

### 3. トークンの保存

```bash
# ~/.secrets/slack_tokens.sh に追記
export SLACK_BOT_TOKEN="xoxb-..."   # Bot Token（新規）
export SLACK_APP_TOKEN="xapp-..."   # App-Level Token（新規）
# SLACK_USER_TOKEN は既存のまま変更なし
```

### 4. qa_config.yaml の編集

`data/qa_config.yaml` を開き、各インデックスに対して `minutes` と `channels` を設定する。

利用可能な会議種別（`data/minutes/` 以下のファイル名から `.db` を除いたもの）:
```
BenchmarkWG_Meeting  Block1_Meeting  Block2_Meeting  Co-design_Review_Meeting
Leader_Meeting  SubWG1_Meeting  SubWG3_Meeting  SubWG4_Meeting
SubWG6_Meeting  SubWG7_Meeting  SubWG8_Meeting
```

利用可能なチャンネルID（`data/C*.db` のファイル名から `.db` を除いたもの）:
```
C08LSJP4R6K  C08M0249GRL  C08MJ0NF5UZ  C08PE3K9N72  C08SXA4M7JT
C093DQFSCRH  C093LP1J15G  C096ER1A0LU  C0A6AC59AHM  C0A9KG036CS
```

### 5. インデックスの構築

```bash
cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement

# 件数確認（書き込みなし）
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --dry-run

# 全インデックス構築
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --full-rebuild

# 特定インデックスのみ
~/.venv_aarch64/bin/python3 scripts/pm_embed.py --index-name pm-bmt --full-rebuild
```

### 6. サーバーの起動

```bash
bash scripts/pm_qa_start.sh
# → PID が logs/pm_qa_server.pid に保存される
# → ログは logs/pm_qa_server.log に記録される
```

### 7. 自動再起動の設定（任意）

```cron
*/5 * * * * bash /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_qa_start.sh >> /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_qa_cron.log 2>&1
```

---

## 使い方

### 基本的なQA

各チャンネルで `/ask` を実行すると、そのチャンネルに対応したインデックスを検索する:

```
（C08SXA4M7JT リーダー会議チャンネルで）
/ask 設計方針に関する最近の議論は？
/ask Benchparkハッカソンの内容を教えて

（C08MJ0NF5UZ ベンチマークWGチャンネルで）
/ask FFBのベンチマーク手法について
/ask 直近の会議で決まった測定方針は？
```

回答はephemeral（自分だけに見える）で返ってくる。回答末尾に使用したインデックス名が表示される。

### pm_embed.py のオプション

| オプション | デフォルト | 説明 |
|---|---|---|
| `--full-rebuild` | なし | 既存インデックスを全削除して再構築 |
| `--index-name NAME` | 全インデックス | 特定インデックスのみ処理 |
| `--config PATH` | `data/qa_config.yaml` | 設定ファイルのパス |
| `--data-dir PATH` | `data` | ソースDBの検索ディレクトリ |
| `--dry-run` | なし | 書き込みなしでチャンク数のみ表示 |

### デーモン管理

```bash
bash scripts/pm_qa_start.sh          # 起動（起動中なら何もしない）
bash scripts/pm_qa_stop.sh           # 停止
tail -f logs/pm_qa_server.log        # ログ確認
cat logs/pm_qa_server.pid | xargs kill -0 && echo 起動中 || echo 停止中
```

### 既存ワークフローへの統合

データ取り込み後に差分インデックスを自動更新する場合、各スクリプトの末尾に追加する:

```bash
# pm_from_slack.sh の末尾
"$PYTHON3" "$SCRIPT_DIR/pm_embed.py"

# pm_from_recording.sh の末尾
"$PYTHON3" "$SCRIPT_DIR/pm_embed.py" --index-name pm  # 該当インデックスのみ
```

---

## 環境変数

| 変数 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | 必須 | なし | Bot Token（`xoxb-`）|
| `SLACK_APP_TOKEN` | 必須 | なし | App-Level Token（`xapp-`）|
| `OPENAI_API_BASE` | 必須 | なし | vLLMエンドポイント |
| `OPENAI_API_KEY` | 任意 | `"dummy"` | APIキー |
| `OPENAI_MODEL` | 任意 | `"gemma4"` | モデル名 |
| `QA_CONFIG` | 任意 | `data/qa_config.yaml` | 設定ファイルパス |

---

## トラブルシューティング

**`/ask` を実行しても何も返ってこない:**
1. `tail -50 logs/pm_qa_server.log` でエラーを確認
2. Slackアプリに `commands` スコープが付与されているか確認

**「記録が見つかりません」と返ってくる:**
- 対象チャンネルに対応するインデックスに当該会議・チャンネルが含まれているか `qa_config.yaml` を確認
- `pm_embed.py --full-rebuild --index-name <name>` で再構築

**別チャンネルのインデックスを使っているように見える:**
- `qa_config.yaml` の `channel_map` にそのチャンネルIDが設定されているか確認
- 回答末尾の `（検索対象: xxx）` でどのインデックスが使われたか確認できる

**re-rankエラーがログに出る:**
- `re-rankエラー: ...` と出た場合、LLMのコンテキスト長超過の可能性がある
- `TOP_K_RETRIEVE` を減らすか、`rerank preview` の文字数（`pm_qa_server.py` の400）を下げる

**LLMエラーが返ってくる:**
- `curl http://localhost:8000/v1/models` でvLLMサーバーの起動を確認

**SudachiPyが利用不可とログに出る:**
- `~/.venv_aarch64/bin/pip install sudachipy sudachidict_core` を実行
- インストール後 `pm_embed.py --full-rebuild` でインデックスを再構築する

---

## 制限事項と今後の改善候補

### 現状の制約

| 項目 | 現状 | 備考 |
|---|---|---|
| 検索方式 | SudachiPy形態素解析 + FTS5 キーワードマッチ | 意味的な近さは考慮しない |
| インデックス更新 | 手動または既存スクリプトへの追記 | crontabで定期自動更新が望ましい |
| 出典リンク | Slack投稿済み議事録のみリンクあり | `pm_minutes_import.py --post-to-slack` で増やせる |

### 回答品質向上の改善候補（優先度順）

#### P1: セマンティック検索の導入（最大効果）

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

#### P2: チャンク設計の改善

- **見出しの引き継ぎ**: 各チャンク先頭に所属する議題名を付与（例: `【2026-01-14 リーダー会議 / GPU性能評価】`）することでLLMの文脈理解が向上
- **親子チャンク**: 小チャンク（200文字）でFTS検索 → ヒットしたら親チャンク（1000文字）を回答生成に渡す
- **議題単位の分割**: 現在の段落分割から `##` 見出し単位の分割に変更

#### P3: Re-rankingの改善

- **スコアリング方式**: 現在の「番号選択」から「各チャンクに0〜10点のスコアをつけよ」に変更することで判定精度が向上
- **Cross-Encoderモデル**: ms-marco等の小型モデルで質問とチャンクのペアを直接スコアリング（LLM呼び出し不要）

#### P4: 回答生成プロンプトの改善

- **インライン引用の強制**: 回答中に `（2026-01-14 リーダー会議）` のようにインライン引用させることで根拠なき回答を抑制
- **時系列フィルタリング**: 「最近の」「直近の」などの表現を検出して検索対象を直近N件に絞る
- **Chain-of-Thought**: 回答前に取得情報と質問の関連性を考えさせてから回答させる

#### P5: クエリ拡張

- 質問をそのまま検索するのではなく、LLMに「別の言い方で3パターン生成」させて複数クエリで検索しマージする（HyDE: Hypothetical Document Embeddings）
