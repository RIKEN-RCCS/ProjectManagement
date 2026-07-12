# RiVault モデルの Argus 適性評価（2026-07-11 実施）

RiVault（社内 LiteLLM Proxy）がサービングする各 LLM のうち、Argus バックエンドに
適したモデルを2段階で評価した記録。現行主力は `deepseek-ai/DeepSeek-V4-Flash`
（2026-06-05 に gemma4 から切替。経緯は LOG.md 参照）。

---

## 背景の確認事項

### RiVault のサービング構成
RiVault は **LiteLLM Proxy**（`/model/info` 等の LiteLLM 固有ルートで確認）で、背後の
vLLM / Ollama へのゲートウェイ。`/v1/models` は15モデルを返す。

### 量子化
量子化モデルには一貫して ID または実体名に `-FP8` サフィックスが付く命名規則が
この環境で観測された。

| モデル | 量子化 |
|---|---|
| `deepseek-ai/DeepSeek-V4-Flash` | サフィックスなし（**非量子化の可能性が高い**） |
| `zai-org/GLM-4.7-FP8` / `Qwen/Qwen3.6-27B-FP8` / `Qwen/Qwen3.6-35B-A3B-FP8` | FP8（明示） |
| `qwen3-coder:30b`（実体 `Qwen3-Coder-30B-A3B-Instruct-FP8`） | FP8 |
| `zai-org/GLM-4.7-Flash` / `Llama-4-Scout-17B-16E` / `Kimi-K2-Thinking` / `K2-Think` | サフィックスなし |

LiteLLM Proxy 経由の API には `quantization` / `dtype` フィールドが無く、vLLM 起動引数
（`--quantization`）は直接確認できない。DeepSeek-V4-Flash の量子化を確定するには
**RiVault 運用者に起動設定（`config.json` の `quantization_config` または起動引数）を
確認する**必要がある。

---

## Stage 1: 軽量ヒューリスティック・スクリーニング

`scripts/utils/eval_rivault_models.py` で主要8モデルに ja/brief/risk の3タスクを投げ、
速度・日本語比率・リスト構造・thinking漏れ・PMキーワード含有の5軸100点で自動採点。

| 順位 | モデル | avg | 速度 | 日本語 | 構造 | 漏れ | 判定 |
|---|---|---|---|---|---|---|---|
| 1 | Llama-4-Scout-17B-16E | 91 | 30 | 25 | 13 | 15 | ✅ |
| 2 | Qwen3.6-35B-A3B-FP8 | 87 | 30 | 25 | 7 | 15 | ✅ |
| 3 | GLM-4.7-FP8 | 86 | 30 | 25 | 7 | 15 | ✅ |
| 4 | Qwen3.6-27B-FP8 | 84 | 28 | 25 | 7 | 15 | ✅ |
| 5 | GLM-4.7-Flash | 83 | 30 | 22 | 7 | 15 | ✅ |
| 6 | DeepSeek-V4-Flash（現行） | 76 | 20 | 25 | 7 | 15 | ⚠️ |
| 7 | Kimi-K2-Thinking | 66 | 27 | 22 | 10 | 0 | ⚠️ |
| 8 | K2-Think | 62 | 23 | 18 | 7 | 5 | ⚠️ |

**このスクリーニングは質判定には使えない**と判明した。DeepSeek が6位に沈んだ主因は
速度への偏重で、構造軸も行頭番号リストの厳密 regex によるノイズが大きい。実質的な
出力品質は次段の LLM-as-judge で測るべき、というのがこの段階の学び。DeepSeek は
2026-07-06 の 500 障害と異なり今回は正常応答した。

---

## Stage 2: LLM-as-judge 盲検 A/B

上位3挑戦モデル + DeepSeek baseline を対象に、実質的な出力品質を評価。

### 方法
- **サンプル30件**: 本番 pm.db 由来の合成プロンプト（brief 10 / risk 10 / investigate 10）。
  2026-06-05 の A/B 評価（`data/eval/v4flash_ab.db`）と同一サンプルを再利用。
- **応答収集（run）**: 各モデルに同一入力を投げ、出力・latency を記録（thinking 無効・
  temperature 0.3、本番 Argus 同等条件）。
- **採点（judge）**: 中立の `Kimi-K2-Thinking`（コンテスタント外）に2モデル出力を
  **ラベル A/B にマスクした盲検**で提示。4軸を各1-5点（instruction_follow / factual /
  japanese / overall）+ prefer(A/B/tie) + rationale を JSON 出力させる。
  順序バイアス対策として seed でサンプルごとに A/B 提示順を swap。
- **max_tokens は 4096**。初回 2048 では Kimi の thinking が JSON 出力前に予算切れ
  （parse_failed が27〜53%）となり有効判定が不足したため再実行した
  （Kimi は thinking で 2-3k token 消費するため最低 4096 必要という既知の知見）。

### 結果（DeepSeek-V4-Flash baseline との対戦）

| ペア | 有効n | DeepSeek勝 | 挑戦モデル勝 | tie | 判定 |
|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B-FP8** | 27 | 15 | 12 | 0 | DeepSeek やや優勢（僅差） |
| Llama-4-Scout-17B-16E | 27 | 27 | 0 | 0 | DeepSeek 圧勝 |
| GLM-4.7-FP8 | 23※ | 21 | 2 | 0 | DeepSeek 圧勝 |

※GLM ペアは6件欠落（プロセス早期終了）だが大差のため結論不変。

### Qwen3.6-35B-A3B-FP8 vs DeepSeek-V4-Flash 詳細（n=27）

軸別平均（1-5点）:

| 軸 | DeepSeek-V4-Flash | Qwen3.6-35B-A3B-FP8 |
|---|---|---|
| instruction_follow | **4.44** | 3.89 |
| factual | 4.37 | **4.44** |
| japanese | 4.26 | 4.26 |
| overall | **4.22** | 4.00 |

タスク種別 prefer:

| kind | n | DeepSeek | Qwen | 傾向 |
|---|---|---|---|---|
| brief | 8 | 5 | 3 | DeepSeek |
| investigate | 10 | 6 | 4 | DeepSeek |
| risk | 9 | 4 | 5 | Qwen 逆転 |

judge の rationale から読み取れる性格:
- **DeepSeek**: 網羅性・情報の構造化が強い。ただし冗長で字数超過しがち（指示遵守で減点される一因）。
- **Qwen3.6-35B-A3B-FP8**: 簡潔で字数・形式の指示を守る。factual は僅かに上。risk では逆転勝ち。

### 速度（同一サンプルの latency）

| モデル | brief | risk | 質 |
|---|---|---|---|
| Qwen3.6-35B-A3B-FP8 | ~1.5s | ~2.6s | DeepSeek と僅差 |
| DeepSeek-V4-Flash（現行） | ~25s | ~31–51s | 最高 |
| Llama-4-Scout | ~8s | ~13s | 明確に劣後 |
| GLM-4.7-FP8 | ~5s | ~10s | 明確に劣後 |

---

## 結論・推奨

質の総合では **DeepSeek-V4-Flash がわずかに上**（15-12、overall 4.22 vs 4.00）。ただし
突出して遅い。Qwen3.6-35B-A3B-FP8 は**品質ほぼ同等で 10〜20倍高速**、かつ簡潔・指示遵守。
Llama-4-Scout / GLM-4.7-FP8 は高速だが品質で明確に劣後し、主力候補から除外。

**用途でモデルを分けるハイブリッド運用が最適解**:

- **対話即応系（Slack の `/argus-brief` `/argus-risk` 等）** → `Qwen3.6-35B-A3B-FP8`。
  簡潔・指示遵守・高速が体感品質に直結。Block Kit の字数制約とも相性が良い。
- **広範な情報集約・状況分析・判断材料の生成（`pm_nvidia_collab_update.sh` の
  investigate バッチ等、無人 cron）** → `DeepSeek-V4-Flash`。
  網羅性・情報の構造化・指示遵守で優位。investigate タスクでも 6-4 で優勢。
  最大の弱点である遅さが無人バッチでは無関係になる。

### 留保
- factual では Qwen が僅差で上（4.44 vs 4.37）。証跡引用を最重視する用途では Qwen も候補。
- 本 A/B は**単発生成**の評価で、`pm_argus_agent.py --investigate` が使う
  **マルチステップのツール呼び出しループ（検索→推論の反復、max-steps 15）は未検証**。
  エージェント文脈での推論力・ツール使用の巧拙は別途、実際の investigate ループで
  両モデルの出力を並べて比較する必要がある。

---

## 成果物

- 生データ: `data/eval/stage2_ab.db`（元の `data/eval/v4flash_ab.db`（2026-06-05）は無変更）
- 評価スクリプト:
  - Stage 1: `scripts/utils/eval_rivault_models.py`（ライブ）
  - Stage 2: `scripts/eval/argus_ab.py` / `scripts/eval/argus_ab_judge.py`
    （2026-06-05 の archive から復元。import を `scripts/utils/` の新 location に修正、
    廃止済み `open_knowledge_db` 依存を除去、作業 DB を環境変数 `AB_DB` で切替可能化）
