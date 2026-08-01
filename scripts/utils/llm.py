"""llm.py — LLM 呼び出しユーティリティ

call_argus_llm / call_rivault / call_local_llm / strip_think_blocks を一元管理する。
cli_utils.py から移動済み（後方互換のため cli_utils.py は `from utils.llm import *` を維持）。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, overload

from utils import net_guard  # noqa: F401 (import 時の install() 副作用のため)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# secrets 読み込み
# --------------------------------------------------------------------------- #

_LLM_SECRET_FILES = (Path.home() / ".secrets/localLLM.sh", Path.home() / ".secrets/rivault_tokens.sh")
_LLM_ENV_PREFIXES = (
    "LOCAL_LLM_", "LOCAL_OCR_", "RIVAULT_", "EMBED_", "ARGUS_PREFER_RIVAULT", "MINUTES_VISION_",
)

_llm_secrets_mtime_cache: tuple | None = None


def load_llm_secrets() -> None:
    """secrets ファイルを bash で source し、LLM関連環境変数を os.environ へ反映する。

    ファイルに定義がある変数は現在の環境変数を上書きする（source と同じ意味論 = ファイルが正）。
    ファイル不在は黙ってスキップする。mtime が前回と同じなら subprocess を省略する。
    テスト等で環境変数を直接制御したい場合は ARGUS_SKIP_LLM_SECRETS=1 を設定すると
    ファイルの source をスキップする。
    """
    if os.environ.get("ARGUS_SKIP_LLM_SECRETS") == "1":
        return
    global _llm_secrets_mtime_cache
    mtimes = tuple(f.stat().st_mtime if f.exists() else None for f in _LLM_SECRET_FILES)
    if mtimes == _llm_secrets_mtime_cache:
        return

    import subprocess
    source_cmds = " ".join(f"source {f} 2>/dev/null;" for f in _LLM_SECRET_FILES)
    try:
        result = subprocess.run(
            ["bash", "-c", f"{source_cmds} env -0"],
            capture_output=True,
        )
    except Exception:
        return
    for entry in result.stdout.split(b"\x00"):
        if not entry:
            continue
        key, _, value = entry.decode("utf-8", errors="replace").partition("=")
        if key.startswith(_LLM_ENV_PREFIXES):
            os.environ[key] = value
    _llm_secrets_mtime_cache = mtimes


# --------------------------------------------------------------------------- #
# CoT 除去
# --------------------------------------------------------------------------- #

def strip_think_blocks(text: str) -> str:
    """CoT を除去して日本語本文のみを返す。

    対応パターン:
    1. <think>...</think> タグ付きブロック（Qwen3/ELYZA 系）
    2. タグなし英語 CoT の前置き（Nemotron 系）— 日本語文字が最初に現れる段落から抽出する
    """
    if "<think>" in text and "</think>" not in text:
        return ""
    text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()

    if text and not re.search(r"[^\x00-\x7F]", text[:200]):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"[　-鿿豈-﫿]", line):
                text = "\n".join(lines[i:]).strip()
                break

    return text


_THINK_TAG_RE = re.compile(r"<think>([\s\S]*?)</think>")


def _extract_think_fallback(content: str) -> str:
    """reasoning_content が空だったときに、strip 前の raw content 内の
    <think>...</think> ブロックからフォールバック抽出する。
    閉じタグ欠落（truncation で思考ブロックが途中で切れた場合）は諦めて空文字列を返す。
    """
    m = _THINK_TAG_RE.search(content)
    return m.group(1).strip() if m else ""


# --------------------------------------------------------------------------- #
# vLLM モデル自動検出
# --------------------------------------------------------------------------- #

def _assert_model_pin(model_id: str | None) -> None:
    """本番経路で使うモデルが model_pin.yaml の宣言内にあるか照合する（§4.6）。

    既定は warn（記録のみ）。`ARGUS_MODEL_PIN=enforce` で拒否に切り替わる。
    **照合の失敗そのものでは落とさない** — pin の読み込み不良で本番が止まる方が損失が
    大きいため。ただし enforce の ModelPinError は素通しする（それが目的なので）。
    """
    if not model_id:
        return
    try:
        from utils.model_pin import ModelPinError, assert_model_allowed
    except Exception:
        return
    try:
        assert_model_allowed(model_id)
    except ModelPinError:
        raise
    except Exception:
        logger.exception("[MODELPIN] 照合に失敗しました（続行）")


def _resolve_local_token(base_url: str) -> tuple[str, str]:
    """base_url に対応する API トークンと、使用した環境変数名を解決する。

    RIVAULT_URL と同じホストを指す base_url にのみ RIVAULT_TOKEN を使う。
    それ以外（RIKYU 等のローカル系エンドポイント）は
    LOCAL_LLM_TOKEN → RIKYU_TOKEN → RIVAULT_TOKEN → "dummy" の順で解決する
    （末尾 `/` や `/v1` の有無に影響されないよう netloc で比較する）。
    """
    import urllib.parse
    rivault_url = os.environ.get("RIVAULT_URL", "")
    if rivault_url:
        rivault_netloc = urllib.parse.urlparse(rivault_url).netloc
        base_netloc = urllib.parse.urlparse(base_url).netloc
        if base_netloc and base_netloc == rivault_netloc:
            return os.environ.get("RIVAULT_TOKEN", "dummy"), "RIVAULT_TOKEN"
    for var in ("LOCAL_LLM_TOKEN", "RIKYU_TOKEN", "RIVAULT_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value, var
    return "dummy", "dummy"


def _token_for_base(base_url: str) -> str:
    """base_url に対応する API トークンを解決する（`_resolve_local_token` のトークン値のみ版）。"""
    return _resolve_local_token(base_url)[0]


def detect_vllm_model(base_url: str, api_key: str | None = None) -> str:
    """vLLM の /v1/models エンドポイントからモデル名を自動取得する。"""
    import json
    import urllib.request
    url = base_url.rstrip("/") + "/models"
    token_var = "explicit"
    if api_key is None:
        api_key, token_var = _resolve_local_token(base_url)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        models = [m["id"] for m in data.get("data", [])]
        if not models:
            raise RuntimeError(f"vLLM にモデルが見つかりません: {url}")
        return models[0]
    except Exception as e:
        # トークンの値は絶対にログへ出さない。変数名のみ記録する（診断に必要なため）。
        raise RuntimeError(f"vLLM モデル自動取得に失敗: {url} (token={token_var}) — {e}") from e


# --------------------------------------------------------------------------- #
# RiVault コンテキストフラグ
# --------------------------------------------------------------------------- #

# RiVault の優先制御は routing_priority の順序で行う（prefer_rivault / allow_rivault_fallback 廃止）


# --------------------------------------------------------------------------- #
# ローカル LLM 呼び出し（OpenAI 互換 API）
# --------------------------------------------------------------------------- #

@overload
def _call_local_llm_inner(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: Literal[False] = False,
) -> str: ...


@overload
def _call_local_llm_inner(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: Literal[True] = True,
) -> tuple[str, str]: ...


@overload
def _call_local_llm_inner(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: bool = False,
) -> str | tuple[str, str]: ...


def _call_local_llm_inner(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: bool = False,
) -> str | tuple[str, str]:
    import json as _json

    import requests
    print(f"[INFO] LLM call: backend=local model={model} url={base_url} think={think}"
          + (f" reasoning_effort={reasoning_effort}" if reasoning_effort is not None else ""),
          file=sys.stderr)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    effective_temp = temperature if temperature is not None else (0.6 if think else 0.8)
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": effective_temp,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if think:
        if not no_chat_template_kwargs:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        payload["top_p"] = 0.95
        payload["skip_special_tokens"] = False
    elif not no_chat_template_kwargs:
        # reasoning 既定モデル（RIKYU glm-5.2 等）は非think指定でも内部思考し、
        # 低 max_tokens だと思考でトークンを使い切り content=0文字を返す。
        # think=False は「思考しない」を明示的に伝える。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if no_chat_template_kwargs:
        payload["top_k"] = 20
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"

    if no_stream:
        payload["stream"] = False
        _retry_steps = [max_tokens // 2, max_tokens // 4, 512]
        for _attempt in range(len(_retry_steps) + 1):
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code != 400:
                break
            err_body = resp.text[:1000]
            is_ctx_overflow = bool(re.search(r"maximum context length.*input tokens", err_body))
            if not is_ctx_overflow:
                print(f"[ERROR] vLLM 400: {err_body}", file=sys.stderr)
                resp.raise_for_status()
            if _attempt < len(_retry_steps):
                reduced = _retry_steps[_attempt]
                print(f"[WARN] コンテキスト長超過。max_tokens {payload['max_tokens']} → {reduced} に縮小再試行",
                      file=sys.stderr)
                payload["max_tokens"] = reduced
            else:
                m = re.search(r"at least (\d+) input tokens", err_body)
                m2 = re.search(r"maximum context length is (\d+) tokens", err_body)
                input_tok = m.group(1) if m else "?"
                max_ctx = m2.group(1) if m2 else "?"
                print(f"[ERROR] vLLM 400: {err_body[:500]}", file=sys.stderr)
                raise RuntimeError(
                    f"プロンプトが長すぎます (入力 {input_tok} トークン / 上限 {max_ctx})。"
                    f"日数範囲を狭めるか、RiVault の回復を待ってください。"
                )
        if resp.status_code >= 400:
            print(f"[ERROR] vLLM {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        print(f"[INFO] 生成トークン数（strip前）: {len(content)} chars, think={think}", file=sys.stderr)
        stripped = strip_think_blocks(content)
        print(f"[INFO] 生成トークン数（strip後）: {len(stripped)} chars", file=sys.stderr)
        if not return_reasoning:
            return stripped
        reasoning_content = (msg.get("reasoning_content") or "").strip()
        if not reasoning_content:
            reasoning_content = _extract_think_fallback(content)
        return stripped, reasoning_content

    # ストリーミング（デフォルト）
    payload["stream"] = True
    _retry_steps_stream = [max_tokens // 2, max_tokens // 4, 512]
    for _attempt in range(len(_retry_steps_stream) + 1):
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
        if resp.status_code < 400:
            break
        if resp.status_code == 400:
            err_body = resp.text[:1000]
            is_ctx_overflow = bool(re.search(r"maximum context length.*input tokens", err_body))
            if is_ctx_overflow and _attempt < len(_retry_steps_stream):
                reduced = _retry_steps_stream[_attempt]
                print(f"[WARN] コンテキスト長超過。max_tokens {payload['max_tokens']} → {reduced} に縮小再試行",
                      file=sys.stderr)
                payload["max_tokens"] = reduced
                continue
            print(f"[ERROR] vLLM {resp.status_code}: {err_body}", file=sys.stderr)
            if is_ctx_overflow:
                m = re.search(r"at least (\d+) input tokens", err_body)
                m2 = re.search(r"maximum context length is (\d+) tokens", err_body)
                input_tok = m.group(1) if m else "?"
                max_ctx = m2.group(1) if m2 else "?"
                raise RuntimeError(
                    f"プロンプトが長すぎます (入力 {input_tok} トークン / 上限 {max_ctx})。"
                    f"日数範囲を狭めるか、RiVault の回復を待ってください。"
                )
        print(f"[ERROR] vLLM {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    print("[INFO] 生成中 ", end="", flush=True, file=sys.stderr)
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = _json.loads(data_str)
        except _json.JSONDecodeError:
            continue
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        token = delta.get("content") or ""
        if token:
            content_parts.append(token)
            print(".", end="", flush=True, file=sys.stderr)
        if return_reasoning:
            # 不要時（既定）は reasoning_content を一切蓄積しない
            # （100KB級の思考トークンを積んで捨てるのを防ぐ）
            reasoning_token = delta.get("reasoning_content") or ""
            if reasoning_token:
                reasoning_parts.append(reasoning_token)
    print(" 完了", flush=True, file=sys.stderr)

    content = "".join(content_parts)
    print(f"[INFO] 生成トークン数（strip前）: {len(content)} chars, think={think}", file=sys.stderr)
    stripped = strip_think_blocks(content)
    print(f"[INFO] 生成トークン数（strip後）: {len(stripped)} chars", file=sys.stderr)
    if not return_reasoning:
        return stripped
    reasoning_content = "".join(reasoning_parts).strip()
    if not reasoning_content:
        reasoning_content = _extract_think_fallback(content)
    return stripped, reasoning_content


@overload
def call_local_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: Literal[False] = False,
    _fallback_to_local: bool = True,
) -> str: ...


@overload
def call_local_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: Literal[True] = True,
    _fallback_to_local: bool = True,
) -> tuple[str, str]: ...


@overload
def call_local_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: bool = False,
    _fallback_to_local: bool = True,
) -> str | tuple[str, str]: ...


def call_local_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    return_reasoning: bool = False,
    _fallback_to_local: bool = True,
) -> str | tuple[str, str]:
    """OpenAI 互換 API を requests で直接呼び出す。

    reasoning_effort: None のとき payload に送らない（既存挙動維持）。
    return_reasoning: True のとき (content, reasoning_content) のタプルを返す。
        既定 False では従来通り content の文字列のみを返す（既存呼び出し元への影響なし）。
    """
    rivault_url = os.environ.get("RIVAULT_URL", "").rstrip("/")
    is_rivault = bool(rivault_url) and base_url.rstrip("/") == rivault_url

    try:
        return _call_local_llm_inner(
            prompt, model=model, base_url=base_url, api_key=api_key,
            timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
            system=system, no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=temperature, reasoning_effort=reasoning_effort,
            return_reasoning=return_reasoning,
        )
    except Exception as exc:
        if not (_fallback_to_local and is_rivault):
            raise
        local_base = os.environ.get("LOCAL_LLM_URL")
        if not local_base:
            raise RuntimeError("LOCAL_LLM_URL 未設定（~/.secrets/localLLM.sh を確認）") from exc
        if local_base.rstrip("/") == rivault_url:
            raise
        print(f"[WARN] call_local_llm: RiVault 失敗 ({type(exc).__name__}: {exc})。"
              f"local ({local_base}) にフォールバック", file=sys.stderr)
        # フォールバックは障害時にしか通らない経路なので、_try_local() (line ~976) と
        # 同じ形に揃える。ここだけ LOCAL_LLM_MODEL / pin 照合を素通りさせると、
        # 「普段は守られているが、壊れたときだけ守られない」状態になってしまう。
        local_model = os.environ.get("LOCAL_LLM_MODEL") or detect_vllm_model(local_base)
        _assert_model_pin(local_model)
        return _call_local_llm_inner(
            prompt, model=local_model, base_url=local_base,
            api_key=os.environ.get("LOCAL_LLM_TOKEN", "dummy"),
            timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
            system=system, no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=temperature, reasoning_effort=reasoning_effort,
            return_reasoning=return_reasoning,
        )


# --------------------------------------------------------------------------- #
# マルチモーダル (vision) 呼び出し
# --------------------------------------------------------------------------- #

def _log_int(value: int | None) -> int:
    """ログ出力用に None を 0 へ丸める（harness 側パーサの \\d+ 前提を満たす契約）。"""
    return value if isinstance(value, int) else 0


def call_vision_llm(
    prompt: str,
    image_paths: Sequence[str | Path],
    *,
    model: str,
    base_url: str,
    api_key: str = "dummy",
    system: str = "",
    max_tokens: int = 16384,
    timeout: int = 900,
    temperature: float | None = None,
    image_labels: Sequence[str] | None = None,
    return_usage: bool = False,
) -> str | tuple[str, dict]:
    """複数画像を投入するマルチモーダル LLM 呼び出し（常時ストリーミング）。

    pm_box_crawl._ocr_image（1 画像・非ストリーム・失敗時 None を返す OCR 専用実装、
    3 系統が依存）とは独立した関数であり、_ocr_image 自体には手を入れない。

    content 配列は画像ごとに（ラベルテキスト（image_labels 指定時のみ）+ image_url）を
    並べ、最後に prompt 本文を積む。image_url の data URI は data:image/png;base64,...
    形式（base64 エンコードは 1 回のみ行い、コンテキスト長超過時の再試行間で使い回す）。

    リトライは 400 のコンテキスト長超過時のみ行う。画像を均等間引きして
    n → ceil(n/2) → ceil(n/4) 枚で再試行し、それでも 400 が続く場合は
    RuntimeError（入力トークン数を含む）を送出する。5xx・タイムアウト・接続エラーは
    リトライせずそのまま例外を送出する（呼び出し側にテキストフォールバックがある前提）。

    return_usage=True のとき (content, usage_dict) のタプルを返す。usage_dict は
    prompt_tokens / completion_tokens / total_tokens / image_tokens / cached_tokens を
    含む（vLLM の usage.prompt_tokens_details 配下にある場合もこの階層にフラット化する）。
    """
    import base64 as _base64
    import json as _json
    import math
    import time as _time

    import requests

    paths = [Path(p) for p in image_paths]
    for p in paths:
        if not p.exists():
            raise ValueError(f"画像ファイルが存在しません: {p}")

    labels: list[str] | None = None
    if image_labels is not None:
        labels = list(image_labels)
        if len(labels) != len(paths):
            raise ValueError(
                "image_labels の要素数は image_paths と一致させてください "
                f"(image_paths={len(paths)}, image_labels={len(labels)})"
            )

    encoded_images: list[str] = []
    for p in paths:
        with open(p, "rb") as f:
            encoded_images.append(_base64.b64encode(f.read()).decode("ascii"))

    n_images = len(paths)

    def _thinned_indices(target: int) -> list[int]:
        if target >= n_images:
            return list(range(n_images))
        step = n_images / target
        return sorted({int(i * step) for i in range(target)})

    def _build_content(idx_list: list[int]) -> list[dict]:
        content: list[dict] = []
        for i in idx_list:
            if labels is not None:
                content.append({"type": "text", "text": labels[i]})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded_images[i]}"},
            })
        content.append({"type": "text", "text": prompt})
        return content

    def _build_messages(idx_list: list[int]) -> list[dict]:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": _build_content(idx_list)})
        return messages

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"

    print(f"[INFO] LLM call: backend=vision model={model} url={base_url} images={n_images}",
          file=sys.stderr)

    started = _time.time()
    targets = [n_images, math.ceil(n_images / 2), math.ceil(n_images / 4)]
    resp = None
    used_indices: list[int] = list(range(n_images))
    for attempt, target in enumerate(targets):
        used_indices = _thinned_indices(target)
        payload: dict = {
            "model": model,
            "messages": _build_messages(used_indices),
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
        if resp.status_code < 400:
            break
        if resp.status_code == 400:
            err_body = resp.text[:1000]
            is_ctx_overflow = bool(re.search(r"maximum context length.*input tokens", err_body))
            if is_ctx_overflow and attempt < len(targets) - 1:
                next_target = targets[attempt + 1]
                actual_next = len(_thinned_indices(min(next_target, n_images)))
                print(f"[WARN] vision: コンテキスト長超過。画像 {len(used_indices)}枚 → "
                      f"{actual_next}枚に間引いて再試行", file=sys.stderr)
                resp.close()
                continue
            if is_ctx_overflow:
                m = re.search(r"at least (\d+) input tokens", err_body)
                m2 = re.search(r"maximum context length is (\d+) tokens", err_body)
                input_tok = m.group(1) if m else "?"
                max_ctx = m2.group(1) if m2 else "?"
                print(f"[ERROR] vision LLM 400: {err_body[:500]}", file=sys.stderr)
                resp.close()
                raise RuntimeError(
                    f"画像を間引いてもコンテキスト長を超過します "
                    f"(入力 {input_tok} トークン / 上限 {max_ctx})。"
                )
        print(f"[ERROR] vision LLM {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()

    content_parts: list[str] = []
    usage: dict = {}
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = _json.loads(data_str)
        except _json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta", {}) or {}
            token = delta.get("content") or ""
            if token:
                content_parts.append(token)
            # reasoning_content は蓄積しない（捨てる）
        if chunk.get("usage"):
            usage = chunk["usage"]

    content = "".join(content_parts)
    stripped = strip_think_blocks(content)

    details = usage.get("prompt_tokens_details") or {}
    usage_out = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "image_tokens": usage.get("image_tokens", details.get("image_tokens")),
        "cached_tokens": usage.get("cached_tokens", details.get("cached_tokens")),
    }
    latency_ms = int((_time.time() - started) * 1000)
    # ログの usage 値は常に整数（欠測は0）で出す契約。harness側パーサ（\d+ 前提）が
    # None 混入で行ごと欠測しないようにするため（usage_out 自体は None を保持する）。
    print(f"[INFO] vision usage: prompt={_log_int(usage_out['prompt_tokens'])} "
          f"image={_log_int(usage_out['image_tokens'])} completion={_log_int(usage_out['completion_tokens'])} "
          f"cached={_log_int(usage_out['cached_tokens'])} latency_ms={latency_ms} "
          f"images={len(used_indices)}", file=sys.stderr)

    if not stripped.strip():
        raise RuntimeError(
            "vision LLM から空の応答が返されました "
            f"(completion_tokens={_log_int(usage_out['completion_tokens'])})。"
            "reasoning で max_tokens を消費し尽くした可能性があります。"
            "max_tokens を増やすか think/reasoning 設定を確認してください。"
        )

    if not return_usage:
        return stripped
    return stripped, usage_out


# --------------------------------------------------------------------------- #
# RiVault 呼び出し
# --------------------------------------------------------------------------- #

def call_rivault(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = 300,
    max_tokens: int = 8192,
    temperature: float | None = None,
    system: str = "",
) -> str:
    """RiVault (GLM-4.7-Flash, 200k context) を呼び出す。"""
    load_llm_secrets()
    base_url = os.environ.get("RIVAULT_URL")
    if not base_url:
        raise RuntimeError(
            "RIVAULT_URL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください"
        )
    api_key = os.environ.get("RIVAULT_TOKEN", "dummy")
    if model is None:
        model = os.environ.get("RIVAULT_MODEL")
        _assert_model_pin(model)
        if not model:
            raise RuntimeError(
                "RIVAULT_MODEL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください"
            )
    print(f"[INFO] LLM call: backend=rivault model={model} url={base_url}", file=sys.stderr)
    import json as _json

    import requests as _requests
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    payload["temperature"] = temperature if temperature is not None else 0.3
    model_lower = model.lower()
    if "kimi" not in model_lower:
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"
    resp = _requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
    if resp.status_code >= 400:
        err_text = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
        print(f"[ERROR] RiVault {resp.status_code}: {err_text}", file=sys.stderr)
        resp.raise_for_status()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    print("[INFO] Argus 生成中 ", end="", flush=True, file=sys.stderr)
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = _json.loads(data_str)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            reasoning_text = delta.get("reasoning_content") or ""
            content_text = delta.get("content") or ""
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
            if content_text:
                content_parts.append(content_text)
                print(".", end="", flush=True, file=sys.stderr)
        except _json.JSONDecodeError:
            continue
    print(" 完了", flush=True, file=sys.stderr)
    content = "".join(content_parts).strip()
    reasoning = "".join(reasoning_parts).strip()
    if not content and reasoning:
        print(f"[WARN] RiVault: content 空・reasoning_content のみ ({len(reasoning)} chars)。reasoning を返却",
              file=sys.stderr)
        content = reasoning
        reasoning = ""
    if reasoning:
        print(f"[INFO] RiVault: reasoning_content={len(reasoning)} chars, content={len(content)} chars",
              file=sys.stderr)
    return strip_think_blocks(content)


# _call_anthropic_compat は廃止。claude_code ルートは routing_priority から削除済み。


# --------------------------------------------------------------------------- #
# Argus 統合エントリポイント（ルーティング付き）
# --------------------------------------------------------------------------- #

def _load_llm_routing_priority() -> list[str] | None:
    """argus_config.yaml の llm.routing_priority を読み込む。
    設定がない / 空リスト → None（呼び出し元 call_argus_llm() は即 RuntimeError）。
    """
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent.parent / "data" / "argus_config.yaml"
    if not cfg_path.exists():
        return None
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    llm_cfg = cfg.get("llm")
    if not isinstance(llm_cfg, dict):
        return None
    priority = llm_cfg.get("routing_priority")
    if not isinstance(priority, list) or not priority:
        return None
    valid = {"rivault", "local"}
    seen: set[str] = set()
    for r in priority:
        if r not in valid:
            raise ValueError(
                f"Invalid route '{r}' in llm.routing_priority. Valid: {valid}"
            )
        if r in seen:
            raise ValueError(f"Duplicate route '{r}' in llm.routing_priority")
        seen.add(r)
    return priority


def _is_route_available(route: str) -> bool:
    if route == "rivault":
        return bool(os.environ.get("RIVAULT_URL", "").strip())
    elif route == "local":
        return bool(os.environ.get("LOCAL_LLM_URL", "").strip())
    return False


_VALID_REASONING_EFFORTS = {"low", "high", "max"}


def _resolve_reasoning_effort_env() -> str | None:
    """ARGUS_REASONING_EFFORT を whitelist 検証して返す。

    未設定または不正値の場合は None（payload に送らない）。不正値をそのまま
    送信するとサーバ側 400 を誘発し、call_argus_llm の fallback ロジックが
    静かに別ルート（≒別モデル）にフォールバックして測るという罠があるため、
    ここで検証して WARN を出す。
    """
    value = os.environ.get("ARGUS_REASONING_EFFORT")
    if not value:
        return None
    if value not in _VALID_REASONING_EFFORTS:
        print(f"[WARN] ARGUS_REASONING_EFFORT の値 '{value}' は不正です"
              f"（有効値: {sorted(_VALID_REASONING_EFFORTS)}）。送信しません。",
              file=sys.stderr)
        return None
    return value


def _resolve_llm_temperature_env() -> float | None:
    """ARGUS_LLM_TEMPERATURE を local ルート限定で解決する。

    未設定または float 変換に失敗した場合は None（payload には送らず、
    call_local_llm 側の既定値 0.6/0.8 がそのまま使われる = 現在の挙動と完全同一）。
    call_argus_llm の temperature 引数が明示された場合はこの env より優先される
    （呼び出し元でこの関数を参照する前に判定する）。
    """
    value = os.environ.get("ARGUS_LLM_TEMPERATURE")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        print(f"[WARN] ARGUS_LLM_TEMPERATURE の値 '{value}' は float に変換できません。"
              "送信しません。", file=sys.stderr)
        return None


@overload
def call_argus_llm(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
    think: bool = False,
    temperature: float | None = None,
    no_chat_template_kwargs: bool = False,
    fallback: bool = True,
    return_reasoning: Literal[False] = False,
) -> str: ...


@overload
def call_argus_llm(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
    think: bool = False,
    temperature: float | None = None,
    no_chat_template_kwargs: bool = False,
    fallback: bool = True,
    return_reasoning: Literal[True] = True,
) -> tuple[str, str]: ...


@overload
def call_argus_llm(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
    think: bool = False,
    temperature: float | None = None,
    no_chat_template_kwargs: bool = False,
    fallback: bool = True,
    return_reasoning: bool = False,
) -> str | tuple[str, str]: ...


def call_argus_llm(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
    think: bool = False,
    temperature: float | None = None,
    no_chat_template_kwargs: bool = False,
    fallback: bool = True,
    return_reasoning: bool = False,
) -> str | tuple[str, str]:
    """Argus 用 LLM 呼び出し。

    argus_config.yaml の llm.routing_priority に従ってルーティングする。
    利用可能なルート: rivault, local。claude_code ルートは廃止。

    think は local ルートのみ有効。rivault ルートでは think は call_rivault に伝播せず、
    thinking の有無は RIVAULT_MODEL 依存（kimi 系＝常時 ON・無効化不可、それ以外＝
    thinking:disabled を強制）。

    temperature 引数を明示しない場合、local ルート限定で ARGUS_LLM_TEMPERATURE
    env（未設定/不正値時は無視）が適用される（`_resolve_llm_temperature_env`）。
    top_p は local 経路の呼び出し引数に無いため env 経由の制御はできない
    （既知の非対称性。think=True 時のみ payload に固定値 0.95 が入る）。

    return_reasoning: True のとき (content, reasoning_content) のタプルを返す。
    local ルート限定の対応（call_local_llm の return_reasoning をそのまま利用）。
    rivault ルートで解決された場合は reasoning_content を取得する経路がないため
    reasoning_content は常に空文字列 "" になる。既定 False では従来通り str のみを
    返し、既存呼び出し元への影響はない。
    """
    load_llm_secrets()

    def _try_rivault() -> str | tuple[str, str]:
        if think:
            logger.debug("think=True は rivault ルートには伝播しません"
                         "（thinking の有無は RIVAULT_MODEL 依存: kimi系=常時ON・無効化不可、"
                         "それ以外=thinking:disabled を強制）")
        result = call_rivault(
            prompt, timeout=timeout, max_tokens=max_tokens, system=system,
            temperature=temperature,
        )
        if not return_reasoning:
            return result
        print("[WARN] call_argus_llm: reasoning_content は常に空です (route=rivault) "
              "— rivault ルートには reasoning_content を取得する経路がありません",
              file=sys.stderr)
        return (result, "")

    def _try_local() -> str | tuple[str, str]:
        local_base = os.environ.get("LOCAL_LLM_URL")
        if not local_base:
            raise RuntimeError("LOCAL_LLM_URL 未設定（~/.secrets/localLLM.sh を確認）")
        import requests as _req
        try:
            _req.get(local_base.removesuffix("/v1").rstrip("/") + "/health", timeout=3)
        except Exception as exc:
            raise RuntimeError(f"ローカル LLM ({local_base}) に接続できません: {exc}") from exc
        model = os.environ.get("LOCAL_LLM_MODEL") or detect_vllm_model(local_base)
        _assert_model_pin(model)
        # ARGUS_REASONING_EFFORT: 未設定/不正値時は None のまま payload に送らない（既存挙動維持）
        reasoning_effort = _resolve_reasoning_effort_env()
        # ARGUS_LLM_TEMPERATURE: temperature 引数が明示された場合はそちらを優先し、
        # 未指定（None）の場合のみ env を参照する（未設定/不正値時は現在の挙動と完全同一）
        effective_temperature = (
            temperature if temperature is not None else _resolve_llm_temperature_env()
        )
        result = call_local_llm(
            prompt, model=model, base_url=local_base,
            api_key=os.environ.get("LOCAL_LLM_TOKEN", "dummy"),
            timeout=timeout, max_tokens=max_tokens, system=system,
            no_stream=True, think=think,
            no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=effective_temperature,
            reasoning_effort=reasoning_effort,
            return_reasoning=return_reasoning,
        )
        if return_reasoning and isinstance(result, tuple) and not result[1].strip():
            print(f"[WARN] call_argus_llm: reasoning_content が空です "
                  f"(route=local model={model}, <think>タグ抽出も失敗)",
                  file=sys.stderr)
        return result

    _try_functions = {
        "rivault": _try_rivault,
        "local": _try_local,
    }

    # --- Config-driven ルーティング（argus_config.yaml の routing_priority のみ） ---
    config_priority = _load_llm_routing_priority()
    if config_priority is None:
        raise RuntimeError(
            "argus_config.yaml に llm.routing_priority が設定されていません。"
        )
    available = [r for r in config_priority if _is_route_available(r)]
    if not available:
        raise RuntimeError("No LLM routes available from llm.routing_priority")
    route_str = ">".join(available)
    print(f"[INFO] call_argus_llm: route_order={route_str} "
          f"think={think} fallback={fallback}", file=sys.stderr)
    last_error: Exception | None = None
    for route in available:
        try:
            return _try_functions[route]()
        except Exception as exc:
            last_error = exc
            if not fallback:
                raise
            print(f"[WARN] call_argus_llm: {route} 失敗 ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            continue
    raise RuntimeError("全 LLM ルート失敗") from last_error


# call_claude は廃止。call_argus_llm を直接使用すること。
