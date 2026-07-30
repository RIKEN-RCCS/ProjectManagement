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

---

# 追補: one-shot 長文脈経路の 2×2 検証（2026-07-29〜30）

上記 2 回の評価は「現状の Argus 実装に適したモデル選定」であり、kimi-k3 の敗因が
アーキテクチャとのミスマッチ（STEP1 前に最大 24 回の補助 LLM 呼び出し、~20s/call の
k3 では 30s タイムアウト → 劣化）にある可能性が残った。視点を替え、**「K3 の実力を
引き出す実装」= 補助 LLM ゼロ + 決定的 broad-recall + 太い文脈 1 回渡し（one-shot）**
を仮説として {現行ループ, one-shot} × {glm-5.2, kimi-k3} の 2×2 + 直接対決を検証した。

- **実装**: `ARGUS_ONESHOT` opt-in 経路（pm_argus_agent.py、既定 OFF・本番不変）。
  retrieve_chunks_hybrid(k=vector_k=top_k) 1 回 → RRF 上位を held_at 昇順で全文詰め →
  call_argus_llm 1 回。全実行で `route_order=` がちょうど 1 回であることを確認済み
- **ハーネス**: investigate_ab.py の ARM_PRESETS（glm-loop / glm-oneshot / k3-loop /
  k3-oneshot、k3 は temp1.0 + RIVAULT 封鎖 + EMBED 明示）、gold 8 問、DeepSeek 盲検 judge
- **記録**: `data/eval/investigate_k3.jsonl`（40 件 = 5 ペア × 8 問、error 0 件）

## N スイープ（本走前、3 問 × N∈{50,200,600}）

- **N=50 で gold reference を完全カバー**。N=200/600 は詳細増のみで正答性向上なし
- **RIKYU 側 nginx の gateway timeout（600s 固定と推定）を発見**: kimi-k3 は設問依存で
  生成が長引き（think 抑制不能の帰結）、salmon 問 N=200/600 で 504 全損を 3 回再現
  （所要 10 分 01〜02 秒でほぼ一定）。client `--timeout` では回避不可。context 量では
  予測できない → **one-shot top-N は 50 に確定**（本走では 504 ゼロ）
- glm-5.2 は 208k 字の文脈でも縮小リトライなし・31s 前後で完走

## 本走結果（勝ち+引き分け率、合格ライン 60%）

| 比較（A vs B） | search (n=6) | docqa (n=2) |
|---|---|---|
| glm-loop vs **k3-oneshot** | B 66.7% 合格 (4-2) | B 100% 合格 (2-0) |
| glm-loop vs **glm-oneshot** | B 83.3% 合格 (5-1) | B 0% 未達 (0-2) |
| glm-loop vs **k3-loop** | B 66.7% 合格 (4-2) | B 50% 未達 (1-1) |
| k3-loop vs **k3-oneshot** | B 40% 未達 (2-3, 判定不能1) | B 50% (1-1) |
| glm-oneshot vs **k3-oneshot** | B 66.7% 合格 (4-2) | B 50% (1-1) |

レイテンシ中央値（アーム別・全出現横断）: **glm-oneshot 30.7s** / glm-loop 69.4s /
k3-oneshot 148.2s（直接対決ペアでは 60.2s — RIKYU 負荷で分散大） / k3-loop 472.1s。

## 主な知見

1. **one-shot 経路自体がモデル非依存に有効（search）**: glm-oneshot は glm-loop に
   83.3% で勝ち、かつ 31s（loop の半分以下）。品質向上の主因は「経路」
2. **その上で k3 はモデルとしても上積みする**: k3-oneshot は glm-oneshot に search
   66.7% で勝ち越し。judge rationale の傾向は「参照事実のカバレッジの広さ・出典の
   質・矛盾情報への批判的言及」で k3 優位、「結論の直接性・具体性」で glm 優位
3. **docqa（--file 全文 QA）は one-shot 不適**: glm-oneshot 0-2。既存の専用文書窓
   （map-reduce）経路を維持する
4. **再訪条件（k3 ループ安定性）は確証**: forced synthesis 発動 0、全問予算内で自然
   終了。ただし re-rank フォールバックが k3-loop でのみ 10 件発生 — 「現行ループの
   補助 LLM は k3 と相性が悪い」の実測裏付け。k3-loop は品質で glm-loop に勝つが
   レイテンシ 7 倍（中央値 472s）で実用性に難
5. 品質順（search）: **k3-oneshot > glm-oneshot > glm-loop ≈ k3-loop**、
   速度順: glm-oneshot ≫ glm-loop > k3-oneshot ≫ k3-loop

## 提案と次のステップ（採否は PM 判断）

- **search 型 investigate の既定を glm-oneshot に切り替える価値が高い**（品質・速度とも
  現行超え。切替は qa デーモン起動環境に `ARGUS_ONESHOT=1` を設定するだけ）
- **k3-oneshot は品質優先のオプトイン**（例: `--deep` / 環境切替）として有望。ただし
  RIKYU nginx 600s 制約下では設問依存の 504 リスクが残るため、`proxy_read_timeout`
  緩和の可否を RIKYU 運用側に確認してから判断
- 確定前に **多段設問（mh- 9 問、キュレーション待ち）での追検証**を行う。n=6/ペア・
  単一 judge・gold 8 問は「1〜2 回の検索で答えられる」設問に偏っており、one-shot に
  有利なバイアスの可能性を排除できていない

## 制約・注意

- judge は DeepSeek-V4-Flash 単一・盲検 swap（seed 7）。parse_failed 1 件
  （k3-loop vs k3-oneshot / genesis-gpu-kernel-bottleneck）は集計から除外
- k3 系アームは HF 推奨 temp 1.0（`ARGUS_LLM_TEMPERATURE`）。top_p は local 経路に
  引数がなく非対応（think=True 時 0.95 固定）— 前回評価と同じ非対称性
- one-shot は出典を回答末尾「## 出典」にモデル自身が列挙する方式（`ctx.cited_chunks`
  は不使用）。全 40 件で出典欠落なし

---

# 追補2: 検索バグ発見・修正後の再計測と多段設問（2026-07-30）

## 経緯

K3 敗因の深掘りで検索段バグ 2 件を発見: (1) `sanitize_fts_query` が全角括弧等を除去せず
日本語質問で FTS が全段不成立、(2) その「日付降順フォールバック」結果（関連度シグナルなし）が
RRF で vector 候補 50 件を数学的に押し出す（mh-nvl72 実測: top-50 全件が実行当日の無関係
チャンク、証跡 0/7）。生の質問文で検索する one-shot アームが系統的に不利になっていた。
修正（sanitize 全角対応 + 日付フォールバックの RRF 遮断、`knowledge_context.py` の重複実装も
一本化）の上、影響 18 レコードを隔離し 11 問（mh- 9 + gold 2）× 5 ペアを再計測した。

**バグの影響は甚大**: 隔離 18 件のうち **9 件（50%）で勝敗が反転**。修正前データでは
信頼できる判定ができていなかった。

## 多段設問（mh- 9 問、変遷・突合型、since 1 年超）の結果

| 比較（A vs B） | 勝敗 | B の勝ち+引分率 | レイテンシ中央値 |
|---|---|---|---|
| glm-loop vs k3-oneshot | 5-4 | 44.4% 未達 | 76s / 249s |
| glm-loop vs glm-oneshot | 7-2 | **22.2% 未達** | 83s / 52s |
| glm-loop vs **k3-loop** | 2-5（判定不能2） | **71.4% 合格** | 86s / 368s |
| k3-loop vs k3-oneshot | 5-4 | 44.4% 未達 | 455s / 253s |
| glm-oneshot vs **k3-oneshot** | 3-5（判定不能1） | **62.5% 合格** | 66s / 139s |

## 最終結論（gold 8 問 + mh- 9 問、バグ修正後）

1. **経路の優劣は設問型に依存する**。単発 search 型（gold）では one-shot が圧勝
   （glm-oneshot 83.3%）だが、多段変遷型（mh-）では逆転し loop が優位
   （glm-oneshot 22.2%、k3-oneshot 44.4%）。broad-recall N=50 の 1 回検索では
   1 年超に分散した変遷の中間段階を拾いきれず、反復検索が recall で勝る
2. **モデル軸は経路によらず一貫して K3 > GLM**。one-shot 直接対決は gold 62.5% /
   mh- 62.5% と同率で K3 勝ち越し。loop 対決（mh-）も k3-loop が 71.4% で glm-loop に勝つ
3. **多段設問の品質首位は k3-loop**（71.4%）。ただしレイテンシ中央値 455s（glm-loop の
   5 倍超）と rerank フォールバック 18 件（全て k3-loop。補助 LLM と K3 の相性問題は
   バグ修正後も残存）を代償にする
4. 勝敗を分ける軸は「参照事実の網羅」と「日付・数値の正確さ」。glm-oneshot は多段設問で
   日付・数値の取り違えが繰り返し減点された

## 運用への示唆（docs/kimi-k3-migration.md の設計メモと整合）

- 「K3 の強みは長時間自律」というメモの読みを mh- 実測が裏付けた。k3-loop はループ設計が
  gemma4/glm 向けに調律されたまま（preserved thinking 近似のみ・補助 LLM は glm 想定
  30s タイムアウト）でも品質首位 — メモ優先度 1 の API クライアント層再設計
  （reasoning_content 往復・ストリーミング・逐次永続化）後の k3-loop が本命
- 単発 search 型には one-shot が即戦力（glm-oneshot 31〜66s / k3-oneshot はやや上の品質）。
  経路の使い分けには設問型の事前判定（rewrite 段の intent 分類の流用等）が新課題
- 判定に使える実測ノブ: 単発型 → one-shot、変遷・突合・集計型 → loop

## 計測上の残課題

- glm-loop の tool_calls_total / steps_used が全観測で 0 / 3 に張り付き — glm ルートの
  stderr 形式に対する `_extract_run_metrics` のパース疑義。k3-loop は 0〜9 / 1〜4 と
  正常に変動しており、mh- 設問が多段を誘発すること自体は k3-loop 側で実証済み
- K3 の 27 字エラー様応答が 2 件（2026-07-30 15:52〜16:52 の窓に集中、RIKYU 一時不調と
  推定）。answer 非 None のため error 記録されない — 最小回答長ガードの追加を検討
- 記録: data/eval/investigate_k3.jsonl（85 件）、隔離分は同 _pre_sanitize_fix.jsonl
