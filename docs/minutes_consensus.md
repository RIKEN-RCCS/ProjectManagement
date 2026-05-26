# Self-Consistency による議事録生成

`generate_minutes_local.py` の標準動作。同一プロンプトを N 回サンプリングし、
embedding クラスタリング + 投票 + LLM 集約によって表現ブレと取捨選択ブレを吸収する。

> **2026-05-25 から `--consensus 3` がデフォルト**。同一録画 (65 分会議) で実測した
> 結果、エンドツーエンドの追加コストは baseline 比 +15〜25% に留まり、表現ブレの
> 吸収効果に対して許容範囲だったため標準動作に格上げした。単発生成に戻したい場合は
> `--consensus 1` を明示する。

## 背景

gemma4 (reasoning モード, temperature=0.6) は同じ Whisper 入力でも reasoning の
分岐により出力が揺れる:

- セクションの切り方（`## 議論内容` 配下の `### サブタイトル` 数・粒度）
- 段落構成・語尾
- 決定事項やアクションアイテムの取捨選択（重要事項を 1 度しか捕捉しないことがある）

1 回の生成では「たまたま 1 度しか言及されなかった些末事項」と「会議の本旨」が
区別できない。**N 回独立にサンプリングして N/2 票以上で出てきた主張だけを採用**
することでノイズを統計的に削る。

## 全体フロー (N=3 の例)

```
Stage 2 (議事内容) / Stage 3 (決定事項・AI) — --consensus 3 指定時
 ┌──────────────────────────────────────────────────────────┐
 │ ① N 回サンプリング                                         │
 │   同一プロンプト × temperature 0.55 / 0.65 / 0.75          │
 │   → ドラフト d₁, d₂, d₃                                   │
 │                                                            │
 │ ② 構造化分解                                               │
 │   Stage 2: ### セクション単位 (title, body) に切る         │
 │   Stage 3: 決定事項 (- 箇条書き) と AI (表の行) に切る    │
 │                                                            │
 │ ③ Embedding (RiVault bge-m3:567m, 1024 次元)               │
 │                                                            │
 │ ④ グリーディクラスタリング (cosine ≥ 0.78, 中心は累積平均) │
 │                                                            │
 │ ⑤ 投票: クラスタ内ユニークドラフト数 ≥ ⌈N/2⌉ = 2 を採用    │
 │                                                            │
 │ ⑥ LLM 集約 (gemma4)                                        │
 │   各クラスタを「全件共通の事実だけで再構成」プロンプトで    │
 │   1 つにマージ                                             │
 │                                                            │
 │ ⑦ 結合: 採用クラスタを並べて最終 Markdown を構築           │
 └──────────────────────────────────────────────────────────┘
```

## アルゴリズム詳細

### ① サンプリング (`_sample_n_times`)

```python
deltas = np.linspace(-0.1, 0.1, n)   # N=3 → -0.1, 0.0, +0.1
for δ in deltas:
    temperature = base_temperature + δ   # 0.55, 0.65, 0.75
    text = call_local_llm(prompt, ..., temperature=temperature)
```

温度を僅かに振ることで同一の reasoning trace に陥ることを防ぐ。
空応答が返ったら `no_stream=True` でリトライ。規定数集まらなければ集まった分だけで集約。

### ② 構造化分解

| Stage | 関数 | 単位 |
|---|---|---|
| 2 | `_split_sections(text)` | `### タイトル` で切って `(title, body)` のリスト |
| 3 | `_split_decisions_list(text)` | `- xxx` の各 bullet |
| 3 | `_split_action_rows(text)` | `\| 担当者 \| タスク \| 期限 \|` テーブルの各行 |

これで「セクション」「決定」「AI」が比較可能な最小単位になる。

### ③④ Embedding + グリーディクラスタリング (`_greedy_cluster`)

```python
vecs = embed_batch(items)        # RiVault bge-m3 で N×1024
clusters, centers = [], []
for i, v in enumerate(vecs):
    if not clusters:
        clusters.append([i]); centers.append(v); continue
    sims = cosine_similarity_matrix(v, np.stack(centers))
    best = argmax(sims)
    if sims[best] ≥ 0.78:
        clusters[best].append(i)
        # クラスタ中心はインクリメンタル平均で更新
        centers[best] = (centers[best] * n_old + v) / (n_old + 1)
    else:
        clusters.append([i]); centers.append(v)
```

**ポイント**:
- 表面文字列ではなく意味ベクトルで比較するので
  「決定した」「決まった」「合意した」が同一クラスタに入る
- インクリメンタル平均でクラスタ中心を更新 → 後続要素が中心からドリフトしない
- O(N²) だが N は数十項目程度なので実用上問題なし

**クラスタリングキーの設計**:
- Stage 2: `f"{title}\n{body[:300]}"` — タイトルだけだと曖昧（「議論内容」が何度も出る）
  なので本文先頭も含める
- Stage 3 AI: `f"[{assignee}] {task}"` — 担当者を埋め込みキーに混ぜることで
  「同じ内容でも担当者が違う AI」を別クラスタに割る

### ⑤ 投票

```python
min_vote = ⌈N/2⌉   # N=3 なら 2
accepted = [cl for cl in clusters
            if len({draft_idx for i in cl}) >= min_vote]
```

**重要**: クラスタサイズではなくユニークドラフト数で判定する。
1 つのドラフトが同じ話題を 3 回繰り返してもクラスタサイズは 3 になるが、
独立票としては 1 票。これを除外することで「特定 reasoning trace の暴走」を弾く。

**Stage 2 のフォールバック**: 全クラスタが投票閾値で却下されたら
閾値を 0.78 → 0.73 に下げて再クラスタリング。それでも通過しなければ
最長ドラフトをそのまま採用。

### ⑥ LLM 集約

採用クラスタごとに gemma4 に再投入。プロンプト構造（Stage 2 例）:

```
以下は同じ会議の独立したサマリ N=3 件のうち、内容が一致する
クラスタです。全件に共通する事実のみ採用し、1 件にしか出てこない
項目は破棄してください。N/2=2 以上に出る主張は採用してください。

### Draft 1
（クラスタ内 d₁ 由来のセクション本文）
### Draft 2
（クラスタ内 d₂ 由来のセクション本文）
...
```

LLM は文章として自然な形で 1 つに圧縮する。`temperature` は通常生成と同じ。

プロンプト定数:
- `CONSENSUS_SECTION_TEMPLATE` — Stage 2 セクション集約
- `CONSENSUS_DECISIONS_TEMPLATE` — Stage 3 決定事項集約
- `CONSENSUS_ACTIONS_TEMPLATE` — Stage 3 アクションアイテム集約（3 列テーブル維持）

### ⑦ 結合

- Stage 2: 採用された各クラスタの集約結果を `## 議事内容\n\n` 配下に並べる
- Stage 3: `## 決定事項` と `## アクションアイテム` を別々に集約 → 結合

既存の単発出力と同じ並び・書式に揃うので、後続パイプライン
(`pm_minutes_import.py`, `pm_ingest.py minutes`) は変更不要。

## なぜ「平均」ではなく「クラスタ + 投票 + LLM 集約」なのか

最初の発想は「N 回 gemma4 に食わせて平均を gemma4 にやらせる」だった。
素朴に実装すると:

- N 個のドラフトを連結してプロンプトに突っ込む → context 長 5x、reasoning 質低下
- LLM に「平均化して」と頼む → 何が共通かが LLM の主観で揺れる（メタ Self-Consistency 問題）

そこで役割を分離した:

1. **共通性の判定は決定論的に embedding + 閾値** で行う（再現可能）
2. **採否は投票** で機械的に決める（多数派の保護）
3. **文章としてのマージだけ LLM に任せる**（語尾の自然化など、LLM が得意な部分のみ）

ブレるべきでない部分（採否）と LLM が得意な部分（文章生成）を切り分けている。

## 環境変数の役割分担

このアルゴリズムは **ローカル vLLM (gemma4)** と **RiVault (bge-m3 embedding)**
の 2 つのバックエンドを併用する。混同を避けるため環境変数を厳密に分離する。

| 環境変数 | 用途 | デフォルト値 |
|---|---|---|
| `OPENAI_API_BASE` / `OPENAI_API_KEY` | ローカル vLLM gemma4（議事録生成・slide_ocr 等） | `http://localhost:8000/v1` / `dummy` |
| `RIVAULT_URL` / `RIVAULT_TOKEN` | RiVault（Argus 応答 + bge-m3 embedding） | RiVault エンドポイント |
| `EMBED_API_BASE` / `EMBED_API_KEY` | embedding 専用の上書き（任意） | デフォルトは RIVAULT_URL |

`generate_minutes_local.py` は `--url` / `--token` 引数および
`OPENAI_API_BASE` / `OPENAI_API_KEY` 環境変数のみで vLLM を指定する。
`embed_utils.py` は `EMBED_API_BASE` → `RIVAULT_URL` の優先順で embedding
エンドポイントを解決する。

**注意**: 過去には `generate_minutes_local.py` が `RIVAULT_URL` を vLLM
エンドポイント名として流用していた歴史がある。これにより `embed_utils` が
`RIVAULT_URL` を読むと vLLM (`/v1/embeddings` 未提供) に向かい 404 になる
バグがあった。2026-05-25 に環境変数を分離してこの干渉を解消した。

## CLI

### generate_minutes_local.py

```
--consensus N                Self-consistency サンプリング数 (default: 3。--consensus 1 で単発生成)
--consensus-threshold FLOAT  embedding クラスタリング cosine 閾値 (default: 0.78)
--consensus-min-vote INT     クラスタ採用に必要な最小独立サンプル数 (default: ⌈N/2⌉)
```

### pm_from_recording.sh / pm_from_recording_auto.sh

```
--consensus N                generate_minutes_local.py に伝搬
```

### Slack /argus-transcribe

```
/argus-transcribe Recording.mp4              # consensus=3 (デフォルト)
/argus-transcribe Recording.mp4 consensus=1  # 単発生成（従来動作）に戻す
/argus-transcribe Recording.mp4 consensus=5  # サンプル数を増やす
```

`pm_argus.py:_run_transcribe` が `command.text` から `consensus=N` を
正規表現でパースし、`run_pipeline(..., consensus_n=N)` 経由で
`run_minutes` に伝搬する。

## コスト

| 項目 | 単発 (`--consensus 1`) | Self-Consistency N=3 (デフォルト) |
|---|---|---|
| LLM 呼び出し (Stage 2) | 1 | 3 サンプル + クラスタ数 (~5) 集約 = ~8 |
| LLM 呼び出し (Stage 3) | 1 | 3 サンプル + 決定 (~3) + AI (~4) 集約 = ~10 |
| Embedding 呼び出し | 0 | 数十項目 × 1 batch (~50ms) |
| 所要時間 (実測 65 分会議) | ~14 分 | ~17 分 (+15〜25%) |

実測は 2026-05-25 の `pm_qa_server.log` から取得（同一録画 4 回比較。embedding が
404 で投票がフォールバックするケースで +12%、embedding 正常で集約 LLM が走る
ケースで +22%）。当初想定の「~5-8x」は LLM 呼び出し回数のみの見積もりで、実際の
壁時計時間は Whisper 文字起こし時間が支配的なため、追加コストは小さく収まる。

## 失敗時の挙動

- **embedding API 失敗**: `_greedy_cluster` が例外送出 → 各 Stage で
  `[ERROR] embedding 失敗、最長ドラフトを採用` を stderr に出力し、
  最長ドラフトをそのまま返す（N=1 単発モード相当に縮退）。
- **サンプリング失敗 (空応答)**: `_sample_n_times` 内で `no_stream=True`
  にしてリトライ。規定数集まらなければ集まった分だけで集約。
- **投票閾値で全クラスタ却下 (Stage 2)**: 閾値を 0.05 下げて再試行。
  それでも通らなければ最長ドラフトを採用。
- **投票閾値で全クラスタ却下 (Stage 3)**: 「（なし）」を返す
  （決定事項・AI が独立サンプル間で一致しないなら情報として信頼性が低い）。

## 実装ファイル

- `scripts/recording/generate_minutes_local.py` — 中心実装
  - `_sample_n_times` / `_split_sections` / `_split_decisions_list` /
    `_split_action_rows` / `_greedy_cluster`
  - `_consensus_stage2` / `_consensus_stage3`
  - `CONSENSUS_*_TEMPLATE`
- `scripts/embed_utils.py` — RiVault bge-m3 経由で embedding 取得
- `scripts/pm_from_recording.sh` / `pm_from_recording_auto.sh` — CLI 経路
- `scripts/argus/pm_argus.py:_run_transcribe` — Slack 経路で `consensus=N` 解析
- `scripts/recording/transcribe_pipeline.py:run_pipeline` / `run_minutes` —
  Slack 経路から `--consensus` を伝搬

## 関連ドキュメント

- `docs/architecture.md` — 全体アーキテクチャ
- `docs/distill_policy.md` — knowledge.db 蒸留での類似パターン
  （Stage 2 で同様に embedding + LLM judge を使う）
