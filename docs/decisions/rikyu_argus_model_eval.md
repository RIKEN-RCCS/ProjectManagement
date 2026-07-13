# RIKYU 3モデルの Argus 運用性能評価（2026-07-13）

RIKYU（新 OpenAI 互換 vLLM サービング）が配信する 3 モデルを Argus のバックエンド
LLM として運用した場合の性能を、既存 A/B ハーネスで評価した。

- **候補**: `glm-5.2` / `kimi-k2.6` / `qwen3.6-35b`（RIKYU）
- **ジャッジ**: `deepseek-ai/DeepSeek-V4-Flash`（RiVault、候補3系列いずれとも無縁の中立モデル）
- **手法**: `scripts/eval/argus_ab.py`（`--target rikyu` を追加）+ `argus_ab_judge.py`
- **サンプル**: brief / risk / investigate 相当の合成プロンプト各10件（計30、`data/eval/rikyu_ab.db`）
- **採点**: 盲検 A/B、総当たり3ペア×30、instruction/factual/japanese/overall の4軸1-5点
- **条件**: 本番 brief/risk と同じ**非think運用**（`enable_thinking:false` 送信）、`max_tokens=4096`

## 結論

**glm-5.2 を Argus バックエンドの第一候補として推奨。** 総合品質最高（4.78/5）、
主要ワークロード（brief/risk）で最良、risk の JSON 生成は完璧（5.00/5、全勝）、
低レイテンシ・truncation ゼロ。qwen3.6-35b は最速・最省トークンで次点。
kimi-k2.6 は thinking を無効化できず構造化タスク（risk）で破綻するため、brief/risk 用途は不適。

## 品質（DeepSeek ジャッジ、avg_overall /5、勝率）

| kind | glm-5.2 | qwen3.6-35b | kimi-k2.6 |
|---|---|---|---|
| **brief** | **4.80** (80%) | 4.30 (30%) | 4.10 (30%) |
| **investigate** | 4.55 (40%) | 4.40 (25%) | **4.70** (40%) |
| **risk** | **5.00** (100%) | 4.11 (50%) | **1.50** (0%) |
| **総合** | **4.78** | 4.28 | 3.50 |

Head-to-head（勝-負-分）: glm vs kimi = **22-5**、glm vs qwen = **20-4**、kimi vs qwen = 9-16。

## 効率（RIKYU 実測、非think・30サンプル平均）

| model | avg latency | avg completion tokens | truncation(≥4000tok) |
|---|---|---|---|
| **qwen3.6-35b** | **2.1s**（最速） | **438**（最少） | 0 |
| **glm-5.2** | 3.9s | 803 | 0 |
| **kimi-k2.6** | 10.3s（最遅） | 2971（最多） | **10**（risk全件） |

## モデル別所見

- **glm-5.2 — 推奨**: brief/risk で圧倒的。指示遵守・事実整合・日本語すべて高得点。
  risk は前置きなしの妥当な JSON 配列を安定生成（10/10 valid）。速度も実用的。
- **qwen3.6-35b — 次点**: 非think時は最速・最省トークン。品質も堅実（総合4.28）。
  速度/コスト最優先なら有力。ただし brief/risk 品質は glm に一歩譲る。
- **kimi-k2.6 — brief/risk 不適**: `enable_thinking:false` / `thinking:disabled` を
  **無視して常に reasoning**するため（[[reference_rikyu_serving]]）、risk で全10件が
  4096 token 上限に達し JSON 未完成 → 品質 1.50/5・全敗。investigate（自由記述の
  推論系）でのみ glm と互角（4.70）だが、その用途は RiVault gemma4 reasoning が既に担う。

## 制約・注意

- 本評価は retrieval を固定し **LLM の生成品質のみ**を分離評価する合成 A/B。
  investigate は「単発回答の合成品質」を測っており、**多段ツール呼び出し（agent ループ）
  の function-calling 能力は測っていない**。investigate をこれらモデルで本番運用するなら
  agent 経路のツール呼び出し互換性を別途検証すること。
- サンプルは V4-Flash 評価時（2026-06）の合成プロンプトを凍結再利用（`build_samples` は
  knowledge.db 廃止で現在動作しないため）。入力としての妥当性は保たれる。
- 結果DB `data/eval/rikyu_ab.db` は git 管理外（ローカルのみ）。parse 失敗はrisk 2件のみ。
