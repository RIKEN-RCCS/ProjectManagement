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

---

# 追補: kimi-k3 評価（2026-07-29）

RIKYU に追加配信された `kimi-k3`（max_input 928k / max_output 977k）を同一プロトコルで
追加評価した。glm-5.2 の出力は 2026-07-13 の凍結レコードを再利用（同一サンプルのため有効）。
前回になかった **investigate agent ループ実走**（tool-call 能力、Qwen が落ちた軸）も実施。

## 結論

**本番 glm-5.2 の置き換えは見送り。用途限定採用も見送り。**
kimi-k2.6 からの世代改善は明確（総合 3.15 → 4.79、risk 1.44 → 4.67、truncation 100%→50%）だが、
brief/risk では glm-5.2 に及ばず、レイテンシは 7〜10 倍（中央値 20.8s vs 2.8s）。
investigate 単発品質は glm-5.2 に明確勝利（5.00/5、7勝0敗3分）だが、agent ループ実走で
3 問中 1 問がタイムアウト予算枯渇で完全失敗（raw 検索結果の生ダンプ）し、運用安定性が不足。

## 品質（DeepSeek-V4-Flash ジャッジ、盲検・半数 swap、seed=7）

| kind | kimi-k3 | glm-5.2 | 勝敗 (k3-glm-tie) | | kimi-k3 | kimi-k2.6 | 勝敗 |
|---|---|---|---|---|---|---|---|
| brief | 4.40 | **4.60** | 4-6-0 | | **4.80** | 4.00 | 8-1-1 |
| risk | 3.80 | **4.80** | 3-7-0 | | **4.67** | 1.44 | 9-0-0 |
| investigate | **5.00** | 4.10 | **7-0-3** | | **4.90** | 4.00 | 8-1-1 |
| **総合** | 4.40 | **4.50** | 14-13-3 | | **4.79** | 3.15 | **25-2-2** |

（vs kimi-k2.6 の risk は judge JSON パース失敗 1 件を除き n=9）

## 効率（max_tokens=4096 固定、2026-07-13 の値と併記）

| model | median latency | avg completion tokens | risk truncation |
|---|---|---|---|
| qwen3.6-35b | 1.7s | 438 | 0/10 |
| **glm-5.2** | 2.8s | 803 | 0/10 |
| **kimi-k3** | 20.8s | 2011 | **5/10** |
| kimi-k2.6 | 10.3s | 2971 | 10/10 |

kimi-k3 の kind 別 median: brief 19.1s / risk 71.7s / investigate 16.3s。
thinking 無効化は k2.6 同様**不可**（`enable_thinking:false` / `thinking:disabled` とも無視、
実測で brief 1 サンプルに reasoning 3990 字）。

## investigate agent ループ実走（tool-call 能力）

`ARGUS_SKIP_LLM_SECRETS=1` + `LOCAL_LLM_URL/MODEL` 上書きで `pm_argus_agent.py --investigate`
を investigate_gold.yaml の 3 問で実走（max-steps 20 / timeout 480s）:

| 質問 | 結果 | 備考 |
|---|---|---|
| Benchpark ビルド状況 | **失敗** | STEP5 で予算枯渇 → forced synthesis も失敗、8分31秒 |
| FrontFlow/blue 不具合 | 成功 | STEP3 完結、約3分10秒 |
| GENESIS ライセンス | 成功 | STEP1 完結、gold と整合 |

tool_call 自体は生成できる（Qwen の「常に 0 件」とは別物）が、1 呼び出し 7〜87 秒の
ばらつきで多段ステップが総予算を食い潰す。initial-search の並列 rewrite/re-rank も
低速さでタイムアウト → 日付降順フォールバックに劣化する場面が全問で発生。

## 制約・注意

- 結果は `data/eval/rikyu_ab.db` に追記（既存データ無変更、git 管理外）。
- `call_rikyu()` は RIKYU_URL の /v1 込み形式変更後もコード修正不要で動作。
- agent ループ実走は EMBED_API_BASE を確保した環境（localLLM.sh source 済み）での結果。

## 再評価: HF モデルカード推奨条件への補正（2026-07-29 同日）

HuggingFace モデルカード（moonshotai/Kimi-K3）と初回評価の使い方に 3 つの食い違い
（temperature 0.3 vs 推奨 1.0、`reasoning_effort` 未使用、preserved thinking mode 未対応）が
判明したため、補正して再評価した。記録名 `kimi-k3-tuned`（temp1.0/top_p0.95）と
`kimi-k3-low`（+effort=low）で旧レコードと分離。

### 結論: 補正しても見送りは変わらず

| kind | 旧 kimi-k3 vs glm | tuned vs glm | low vs glm |
|---|---|---|---|
| brief | 4.40-**4.60** (4-6-0) | 4.40-**4.70** (3-6-1) | 3.80-**4.90** (0-9-1) |
| risk | 3.80-**4.80** (3-7-0) | 4.00-**4.78** (2-3-4) | 4.40-**4.60** (4-2-4) |
| investigate | **5.00**-4.10 (7-0-3) | **4.80**-4.20 (7-1-2) | 4.50-**4.70** (4-4-2) |
| 総合 | 4.40-**4.50** | 4.41-**4.55** | 4.23-**4.73** |

- **`reasoning_effort` は kimi-k3 の thinking 抑制に効かない**（risk の平均 reasoning 長:
  旧 6382字 → tuned 4349字 → low **7096字（増加）**。truncation 5/10→6/10 で悪化、
  latency 中央値 19.6〜22.3s で glm-5.2 の 2.8s と 7〜8 倍差は不変）。単発プローブでの
  縮小はサンプリング分散だった。`enable_thinking:false` / `thinking:disabled` / 
  `reasoning_effort` の 3 手段すべてが RIKYU 配信の kimi-k3 では無効。
- **agent ループ実走**: 前回失敗した Benchpark 問含め 3 問完走。ただし全問 STEP1 単発
  （tool_call 0 件）で完結したため、前回の失敗モード（多段ツール呼び出しでの予算枯渇）と
  preserved reasoning（`ARGUS_PRESERVE_REASONING=1`、Option B 近似）は**未検証のまま**。
- **再訪条件**: tool_call を要する複雑な investigate 質問セットでのループ安定性検証。
  investigate 単発品質は補正前後とも glm-5.2 に一貫して勝ち越し（7-0-3 / 7-1-2）ており、
  ループ安定性さえ確証できれば investigate 限定採用の余地は残る。

### 評価用に追加した opt-in 機構（デフォルト OFF、本番挙動不変）

- `ARGUS_REASONING_EFFORT`（llm.py local ルート → payload の `reasoning_effort`）
- `ARGUS_PRESERVE_REASONING=1`（pm_argus_agent STEP ループ、直前ステップの reasoning_content
  を `<previous_step_reasoning>` ブロックとして次プロンプトへ。モデルカードの preserved
  thinking mode の近似。忠実対応は messages API 移行が必要で見送り）
- argus_ab.py: `--models "alias=real"` エイリアス、`--reasoning-effort`、`--top-p`

### 記録上の注意

- 生成中に `AB_DB` 未指定で条件1の 30 件が `v4flash_ab.db` に誤書き込みされる事故が発生。
  同一サンプル確認の上 `rikyu_ab.db` へ移行し、`v4flash_ab.db` は元状態に復元済み
  （DeepSeek-V4-Flash 60 / gemma-4 30 / GLM-4.7-Flash 30）。以降は AB_DB を明示。
