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

### 手法上の限界（結論を過大に読まないための注記）
- **単一 judge**（Kimi-K2-Thinking のみ）。複数 judge の合議ではないため judge 固有のバイアスを統制できない。
- **tie が 27 件中 0 件** — judge が強制択一に寄っている兆候で、僅差を無理に勝敗へ割り振っている可能性。
- **swap 分布 19:8** — A/B 提示順の入れ替えが均等でなく、位置バイアスの統制が不完全。
- 以上より Qwen3.6 vs DeepSeek の 15-12 は**有意差とみなせず**、序列ではなく「同等」と解釈すべき。

### 結果（DeepSeek-V4-Flash baseline との対戦）

| ペア | 有効n | DeepSeek勝 | 挑戦モデル勝 | tie | 判定 |
|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B-FP8** | 27 | 15 | 12 | 0 | 有意差なし（二項検定 p≈0.7） |
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

質の総合では **DeepSeek と Qwen3.6-35B-A3B-FP8 に有意差なし**（15勝12敗, n=27, 二項検定
p≈0.7。overall 4.22 vs 4.00 も単一 judge の5点尺度では差と呼べない）。ただし DeepSeek は
突出して遅い。Qwen3.6-35B-A3B-FP8 は**品質ほぼ同等で 10〜20倍高速**、かつ簡潔・指示遵守。
Llama-4-Scout / GLM-4.7-FP8 は高速だが品質で明確に劣後し、主力候補から除外。

当初は**用途別ハイブリッド運用**（対話系→Qwen、集約分析→DeepSeek）を最有力とした。
しかし後述の investigate ループ実走検証（2026-07-13）で Qwen が現行コード下では
investigate を駆動できないと判明したため、**最終決定は「全用途 DeepSeek-V4-Flash 単独運用」
（＝2026-06-05 の現行主力を維持、Qwen 採用は見送り）** とした（下記「最終決定」参照）。

### Stage 2 時点の留保（当時）
- factual では Qwen が僅差で上（4.44 vs 4.37）。
- 本 A/B は**単発生成**の評価で、`pm_argus_agent.py --investigate` のマルチステップ
  ツール呼び出しループは未検証 → これを次段（investigate 実走検証）で確認した。

---

## Stage 3: investigate ループ実走検証（2026-07-13、aarch64 ng-dgx-s-07）

Stage 2 が単発生成だったため、実際の `pm_argus_agent.py --investigate`（マルチステップ・
tool-call ループ）で DeepSeek-V4-Flash と Qwen3.6-35B-A3B-FP8 を比較。対象は SCALE-LETKF、
`pm_nvidia_collab_update.sh` の2パス構成（Pass1 max-steps 5 / Pass2 max-steps 15）を
Box アップロードなしで直接再現（本番非破壊）。

### モデル切替方法（コード・secrets・config 無変更）
`scripts/utils/llm.py` の `call_argus_llm()` は毎回 `load_llm_secrets()` で
`~/.secrets/*.sh` を再 source し `RIVAULT_MODEL` を既定値で上書きするため、手動 export だけでは
切替が効かない。既存のエスケープハッチ `ARGUS_SKIP_LLM_SECRETS=1` を併用して回避:
`export RIVAULT_MODEL=<model>; export ARGUS_SKIP_LLM_SECRETS=1`。

### 結果：Qwen3.6-35B-A3B-FP8 は investigate ループ使用不可
- `call_rivault()` は **Kimi 系以外の全モデルで thinking を強制無効化**する
  （`if "kimi" not in model_lower: payload["thinking"]={"type":"disabled"}`）。
- Qwen3.6-35B-A3B-FP8 は thinking 前提の推論モデルで、investigate の複雑な system prompt
  （~2700字）+ tool-call 形式指示に対し、thinking 無効だと **content 0 文字**で即 break
  （STEP1、3回再現）。thinking を有効化しても実質回答が `reasoning_content` 側に出て
  `content`（tool-call 形式）に渡らず、エージェントループでは使えない。
- 単純な「意図解釈」プロンプトでは thinking 無効でも正常応答するため、複雑な
  agentic プロンプトとの組み合わせ固有の問題。

### DeepSeek-V4-Flash は完走（SCALE-LETKF）
| 指標 | Pass1 | Pass2 |
|---|---|---|
| wall-clock | 1分57秒 | 2分06秒 |
| 出力文字数 | ~2,454字（目標500字を大幅超過） | ~4,303字 |
| ステップ | STEP4 で終了 | STEP1 で完結し break |
| ツール呼び出し | 0件 | 0件 |
| 構造遵守 | — | ◎（`# SCALE-LETKF` + `## 1.`〜`## 6.` 連番 + `###`×12 + 表0） |

構造指示・証跡留保（「確認できていない」「不明」）の遵守は良好。
**要検証の観測**: Pass2 は max-steps 15 枠でツールを1度も呼ばず単発生成で完結したのに
日付付き記述を出力しており、証跡が retrieval に裏打ちされているかは別途要確認
（ループ前の事前検索の有無は未確認）。
**副次バグ**: `[INFO] terminology...` 等のログが stdout に漏れレポート先頭に混入。
本番 `pm_nvidia_collab_update.sh` の Box 版レポートにも同混入の可能性（stderr へ回すべき）。

---

## 最終決定（2026-07-13）

**全用途で DeepSeek-V4-Flash 単独運用（Qwen 採用は見送り、現行主力を維持）。**

判断理由:
- 対話系（brief/risk）では Qwen が高速・簡潔で有力だったが、集約分析の中核である
  investigate ループでは現行コード下で Qwen が動作不能（thinking 強制無効との相性）。
- 用途ごとにモデルを分けると、動作可否・thinking ポリシー・tool-call 形式の差異を
  用途別に管理する運用複雑性が増す。Qwen 単独のメリット（対話の速さ）より、
  DeepSeek 一本化による単純性・investigate での確実な動作を優先。
- Qwen を investigate 対応させるには `llm.py` の thinking ポリシー改修 +
  `reasoning_content`/`content` の扱い + tool-call 形式の検証が必要で、投資に見合わないと判断。

---

## 成果物

- 生データ: `data/eval/stage2_ab.db`（元の `data/eval/v4flash_ab.db`（2026-06-05）は無変更）
- 評価スクリプト:
  - Stage 1: `scripts/utils/eval_rivault_models.py`（ライブ）
  - Stage 2: `scripts/eval/argus_ab.py` / `scripts/eval/argus_ab_judge.py`
    （2026-06-05 の archive から復元。import を `scripts/utils/` の新 location に修正、
    廃止済み `open_knowledge_db` 依存を除去、作業 DB を環境変数 `AB_DB` で切替可能化）
  - Stage 3: `pm_argus_agent.py --investigate` を直接実行（既存スクリプト、無改修）。
    モデル切替は env（`RIVAULT_MODEL` + `ARGUS_SKIP_LLM_SECRETS=1`）のみ。
