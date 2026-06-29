"""llm.py — LLM 呼び出しユーティリティ

call_argus_llm / call_rivault / call_local_llm / strip_think_blocks を一元管理する。
cli_utils.py から移動済み（後方互換のため cli_utils.py は `from utils.llm import *` を維持）。
"""
from __future__ import annotations

import contextvars as _contextvars
import os
import re
import sys
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# vLLM モデル自動検出
# --------------------------------------------------------------------------- #

def detect_vllm_model(base_url: str, api_key: str | None = None) -> str:
    """vLLM の /v1/models エンドポイントからモデル名を自動取得する。"""
    import json
    import urllib.request
    url = base_url.rstrip("/") + "/models"
    if api_key is None:
        api_key = os.environ.get("RIVAULT_TOKEN") or os.environ.get("LOCAL_LLM_TOKEN", "dummy")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        models = [m["id"] for m in data.get("data", [])]
        if not models:
            raise RuntimeError(f"vLLM にモデルが見つかりません: {url}")
        return models[0]
    except Exception as e:
        raise RuntimeError(f"vLLM モデル自動取得に失敗: {url} — {e}") from e


# --------------------------------------------------------------------------- #
# RiVault コンテキストフラグ
# --------------------------------------------------------------------------- #

_prefer_rivault: _contextvars.ContextVar[bool] = _contextvars.ContextVar(
    "prefer_rivault", default=False
)


class prefer_rivault:
    """with ブロック内の call_argus_llm() で RiVault を最優先で使う。"""
    def __enter__(self):
        self._token = _prefer_rivault.set(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _prefer_rivault.reset(self._token)
        return False


allow_rivault_fallback = prefer_rivault  # 後方互換エイリアス


# --------------------------------------------------------------------------- #
# ローカル LLM 呼び出し（OpenAI 互換 API）
# --------------------------------------------------------------------------- #

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
) -> str:
    import json as _json

    import requests
    print(f"[INFO] LLM call: backend=local model={model} url={base_url} think={think}",
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
    if think:
        if not no_chat_template_kwargs:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        payload["top_p"] = 0.95
        payload["skip_special_tokens"] = False
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
        return stripped

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
    print(" 完了", flush=True, file=sys.stderr)

    content = "".join(content_parts)
    print(f"[INFO] 生成トークン数（strip前）: {len(content)} chars, think={think}", file=sys.stderr)
    stripped = strip_think_blocks(content)
    print(f"[INFO] 生成トークン数（strip後）: {len(stripped)} chars", file=sys.stderr)
    return stripped


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
    _fallback_to_local: bool = True,
) -> str:
    """OpenAI 互換 API を requests で直接呼び出す。"""
    rivault_url = os.environ.get("RIVAULT_URL", "").rstrip("/")
    is_rivault = bool(rivault_url) and base_url.rstrip("/") == rivault_url

    try:
        return _call_local_llm_inner(
            prompt, model=model, base_url=base_url, api_key=api_key,
            timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
            system=system, no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=temperature,
        )
    except Exception as exc:
        if not (_fallback_to_local and is_rivault):
            raise
        local_base = os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")
        if local_base.rstrip("/") == rivault_url:
            raise
        print(f"[WARN] call_local_llm: RiVault 失敗 ({type(exc).__name__}: {exc})。"
              f"local ({local_base}) にフォールバック", file=sys.stderr)
        local_model = detect_vllm_model(local_base)
        return _call_local_llm_inner(
            prompt, model=local_model, base_url=local_base,
            api_key=os.environ.get("LOCAL_LLM_TOKEN", "dummy"),
            timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
            system=system, no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=temperature,
        )


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
    base_url = os.environ.get("RIVAULT_URL")
    if not base_url:
        raise RuntimeError(
            "RIVAULT_URL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください"
        )
    api_key = os.environ.get("RIVAULT_TOKEN", "dummy")
    if model is None:
        model = os.environ.get("RIVAULT_MODEL", "zai-org/GLM-4.7-Flash")
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


# --------------------------------------------------------------------------- #
# Claude Code 互換 LLM 呼び出し（ANTHROPIC_BASE_URL 向け）
# --------------------------------------------------------------------------- #

def _call_anthropic_compat(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
    think: bool = False,
    temperature: float | None = None,
) -> str:
    """Claude Code 用エンドポイント（OpenAI 互換 API）を呼び出す。

    settings.json の env に書かれた ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN /
    ANTHROPIC_DEFAULT_OPUS_MODEL を環境変数から読み取り、OpenAI 互換
    /chat/completions にリクエストする。
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("ANTHROPIC_BASE_URL が設定されていません")
    # settings.json の ANTHROPIC_BASE_URL は "http://localhost:8001"（/v1 なし）。
    # _call_local_llm_inner は base_url + "/chat/completions" で呼ぶので /v1 を補う。
    if not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "dummy")
    model = (
        os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or "deepseek-ai/DeepSeek-V4-Flash"
    )
    return _call_local_llm_inner(
        prompt, model=model, base_url=base_url, api_key=api_key,
        timeout=timeout, max_tokens=max_tokens, system=system,
        no_stream=True, think=think, temperature=temperature,
    )


# --------------------------------------------------------------------------- #
# Argus 統合エントリポイント（ルーティング付き）
# --------------------------------------------------------------------------- #

def _load_llm_routing_priority() -> list[str] | None:
    """argus_config.yaml の llm.routing_priority を読み込む。
    設定がない / 空リスト → None（後方互換モード）。
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
    valid = {"claude_code", "rivault", "local"}
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
    if route == "claude_code":
        return bool(os.environ.get("ANTHROPIC_BASE_URL", "").strip())
    elif route == "rivault":
        return bool(os.environ.get("RIVAULT_URL", "").strip())
    elif route == "local":
        return True
    return False

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
) -> str:
    """Argus 用 LLM 呼び出し。ルーティング優先順位:

    argus_config.yaml の llm.routing_priority が設定されていればそれを尊重し、
    未設定の場合は従来の env-var ベースのルーティング（後方互換）。
    """

    def _try_claude_code() -> str:
        return _call_anthropic_compat(
            prompt, timeout=timeout, max_tokens=max_tokens, system=system,
            think=think, temperature=temperature,
        )

    def _try_rivault() -> str:
        return call_rivault(
            prompt, timeout=timeout, max_tokens=max_tokens, system=system,
            temperature=temperature,
        )

    def _try_local() -> str:
        local_base = os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")
        import requests as _req
        try:
            _req.get(local_base.removesuffix("/v1").rstrip("/") + "/health", timeout=3)
        except Exception as exc:
            raise RuntimeError(f"ローカル LLM ({local_base}) に接続できません: {exc}") from exc
        model = os.environ.get("LOCAL_LLM_MODEL") or detect_vllm_model(local_base)
        return call_local_llm(
            prompt, model=model, base_url=local_base,
            api_key=os.environ.get("LOCAL_LLM_TOKEN", "dummy"),
            timeout=timeout, max_tokens=max_tokens, system=system,
            no_stream=True, think=think,
            no_chat_template_kwargs=no_chat_template_kwargs,
            temperature=temperature,
        )

    _try_functions = {
        "claude_code": _try_claude_code,
        "rivault": _try_rivault,
        "local": _try_local,
    }

    # --- Config-driven ルーティング ---
    config_priority = _load_llm_routing_priority()
    if config_priority is not None:
        available = [r for r in config_priority if _is_route_available(r)]
        if _prefer_rivault.get() and "rivault" in available:
            available.remove("rivault")
            available.insert(0, "rivault")
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

    # --- 後方互換: env-var ベースのルーティング ---
    anthropic_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()

    if anthropic_url:
        _route = "claude_code"
        _reason = "ANTHROPIC_BASE_URL set"
    elif _prefer_rivault.get() or os.environ.get("ARGUS_PREFER_RIVAULT") == "1" \
            or bool(os.environ.get("RIVAULT_URL")):
        _route = "rivault"
        _reason = "RIVAULT_URL set" if os.environ.get("RIVAULT_URL") else (
            "prefer_rivault()" if _prefer_rivault.get() else "ARGUS_PREFER_RIVAULT=1")
    else:
        _route = "local"
        _reason = "default-local"
    print(f"[INFO] call_argus_llm: route={_route} reason={_reason} "
          f"think={think} fallback={fallback}", file=sys.stderr)

    # プライマリ・セカンダリの順序を決定
    if _route == "claude_code":
        primary, primary_name = _try_claude_code, "claude_code"
        secondary, secondary_name = _try_local, "local"
    elif _route == "rivault":
        primary, primary_name = _try_rivault, "rivault"
        secondary, secondary_name = _try_local, "local"
    else:
        primary, primary_name = _try_local, "local"
        secondary, secondary_name = _try_rivault, "rivault"

    try:
        return primary()
    except Exception as exc:
        if not fallback:
            raise
        print(f"[WARN] call_argus_llm: {primary_name} 失敗 ({type(exc).__name__}: {exc})。"
              f"{secondary_name} にフォールバック", file=sys.stderr)
        return secondary()


def call_claude(prompt: str, *, model: str | None = None, timeout: int = 120) -> str:
    """call_argus_llm() の薄いラッパー。既存コードとの互換性を保つ。"""
    max_tokens = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "4096"))
    if model:
        old = os.environ.get("LOCAL_LLM_MODEL")
        os.environ["LOCAL_LLM_MODEL"] = model
        try:
            return call_argus_llm(prompt, timeout=timeout, max_tokens=max_tokens)
        finally:
            if old is None:
                os.environ.pop("LOCAL_LLM_MODEL", None)
            else:
                os.environ["LOCAL_LLM_MODEL"] = old
    return call_argus_llm(prompt, timeout=timeout, max_tokens=max_tokens)
