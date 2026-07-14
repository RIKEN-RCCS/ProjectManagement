# RiVault 障害報告: DeepSeek-V4-Flash モデルグループの 500 エラー

**報告日**: 2026-07-06
**報告者**: 井上（R-CCS 次世代計算基盤開発部門）
**宛先**: RiVault（llm.ai.r-ccs.riken.jp）管理者

## 事象

`deepseek-ai/DeepSeek-V4-Flash` モデルグループへの chat/completions リクエストが
すべて HTTP 500 で失敗します（2026-07-06 11:47 JST 頃から少なくとも同日中継続を確認）。

```
POST http://llm.ai.r-ccs.riken.jp:11434/v1/chat/completions
model: deepseek-ai/DeepSeek-V4-Flash
→ 500 Internal Server Error
```

サーバーからのエラー本文（整形済み）:

```
litellm.InternalServerError: InternalServerError: Hosted_vllmException -
AsyncCompletions.create() got an unexpected keyword argument 'context_management'
No fallback model group found for original model_group=deepseek-ai/DeepSeek-V4-Flash.
Fallbacks=[{'Kimi-K2-Thinking': ['K2-Think']}, {'Kimi-K2-Instruct': ['K2-Think']},
{'qwen3-coder:30b': ['codellama:7b']}]
```

## 切り分け結果（クライアント側で確認済み）

- クライアントのリクエストボディに `context_management` パラメータは**含まれていません**
  （送信フィールドは model / messages / max_tokens / temperature のみ。当方コードを確認済み）。
  litellm プロキシが hosted_vllm バックエンドへ転送する際に `context_management` 引数を
  付与し、バックエンドの vLLM（OpenAI互換 AsyncCompletions.create）がこれを受け付けずに
  例外になっているように見えます
- **同一時間帯に同じ RiVault の別モデルグループ（Qwen3.6-35B-A3B-FP8）への
  リクエストは正常に成功**しています（画像入力の OCR 用途、数十リクエスト）。
  したがってサービス全体ではなく DeepSeek-V4-Flash モデルグループ固有の事象です
- DeepSeek-V4-Flash にはフォールバックグループが未定義のため、この 500 がそのまま
  クライアントへ返ります

## 推定原因（参考）

litellm の context management / prompt caching 系の機能フラグが
DeepSeek-V4-Flash のモデルグループ設定で有効になっており、hosted_vllm
バックエンドが対応していない `context_management` kwarg として透過されている可能性。

## 依頼

- DeepSeek-V4-Flash モデルグループの litellm 設定確認（`context_management` の付与を止める、
  または対応バックエンドに限定する）
- 恒久対応が難しい場合、DeepSeek-V4-Flash にフォールバックグループの設定を検討いただけると
  クライアント側の影響が緩和されます

## 影響

当方の議事録自動生成パイプライン（会議録音 → Whisper → LLM 議事録生成）が
RiVault フォールバック経路を失い、ローカル LLM 停止時に全ルート失敗となりました。
