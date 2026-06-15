#!/usr/bin/env python3
"""Argus 用 LLM 候補のスモーク評価。

複数モデル（ローカル gemma4 / RiVault 上の Kimi / V4-Flash / GLM-4.7-Flash 等）に
同一プロンプトを投げ、出力・所要時間・token 数を並べて比較する。

- 本番 DB / Slack / argus_state.db には一切書き込まない
- 結果は標準出力（人間用 Markdown）と --json で機械可読出力
- 認証: source ~/.secrets/rivault_tokens.sh を事前に行うこと

例:
    source ~/.secrets/rivault_tokens.sh
    python3 scripts/eval/argus_model_smoke.py
    python3 scripts/eval/argus_model_smoke.py --models deepseek-ai/DeepSeek-V4-Flash Kimi-K2-Thinking
    python3 scripts/eval/argus_model_smoke.py --think  # think モードを有効化（V4 系のみ）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any

import requests

DEFAULT_MODELS = [
    "deepseek-ai/DeepSeek-V4-Flash",
    "Kimi-K2-Thinking",
    "zai-org/GLM-4.7-Flash",
]

PROMPTS = [
    {
        "name": "brief_short",
        "system": "あなたは日本語で簡潔に回答する PM アシスタントです。",
        "user": (
            "次の社内議事録メモから、決定事項とアクションアイテムを箇条書きで抽出してください。\n\n"
            "メモ:\n"
            "- GB200 NVL4 が利用可能になった。次回ミーティングで担当を決める。\n"
            "- Whisper の VAD パラメータを 2026-06-10 までに田中が再評価。\n"
            "- 議事録 RAG の精度が低い件、佐藤が原因調査中。週次で進捗共有する。\n"
        ),
    },
    {
        "name": "risk_json",
        "system": (
            "あなたはプロジェクトリスクを抽出する PM アシスタントです。"
            "出力は JSON 配列のみで、各要素は {risk, severity(low|med|high), evidence} とする。"
            "前置きや```記号は出さない。"
        ),
        "user": (
            "以下のステータスからリスクを抽出してください。\n"
            "- マイルストーン M3 の進捗が予定より2週間遅延、担当者リソース不足の可能性。\n"
            "- ベンダー A から納品仕様の最終版がまだ来ていない（期日は今週金曜）。\n"
            "- ローカル LLM のメモリが Whisper 同居で逼迫、OOM が散発。\n"
        ),
    },
    {
        "name": "investigate_japanese",
        "system": "あなたは日本語の社内資料を読み解く調査アシスタントです。",
        "user": (
            "「富岳NEXT」の後継機計画における理研・富士通・NVIDIA の役割分担を、"
            "一般論として推測される範囲で 200 字以内にまとめてください。"
            "未確定事項は「未確定」と明記してください。"
        ),
    },
]


@dataclass
class Trial:
    model: str
    prompt: str
    think: bool
    ok: bool
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    output: str
    reasoning: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_payload(model: str, system: str, user: str, max_tokens: int, think: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": True,
    }
    m = model.lower()
    if "kimi" in m:
        # Kimi-K2-Thinking: thinking 常時 ON、temperature=1.0 だと揺れが大きいので 0.3
        payload["temperature"] = 0.3
    elif "deepseek-v4" in m or "v4-flash" in m:
        # V4-Flash の think モード: 公式仕様未確認のため両方の慣用キーを送る
        payload["temperature"] = 0.3
        if think:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["thinking"] = {"type": "disabled"}
    else:
        # GLM 系: thinking 無効化
        payload["temperature"] = 0.3
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def call(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    think: bool,
    timeout: int,
) -> Trial:
    payload = _build_payload(model, system, user, max_tokens, think)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.time()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta", {}) or {}
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("content"):
                    content_parts.append(delta["content"])
            if chunk.get("usage"):
                usage = chunk["usage"]
        latency_ms = int((time.time() - started) * 1000)
        content = "".join(content_parts).strip()
        reasoning = "".join(reasoning_parts).strip()
        if not content and reasoning:
            content = reasoning
            reasoning = ""
        return Trial(
            model=model,
            prompt=system + "\n\n" + user,
            think=think,
            ok=True,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            output=content,
            reasoning=reasoning,
        )
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return Trial(
            model=model,
            prompt=system + "\n\n" + user,
            think=think,
            ok=False,
            latency_ms=latency_ms,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            output="",
            reasoning="",
            error=f"{type(exc).__name__}: {exc}",
        )


def render_md(trials: list[Trial]) -> str:
    lines: list[str] = ["# Argus Model Smoke Results\n"]
    by_prompt: dict[str, list[Trial]] = {}
    for t in trials:
        # 1行目（system の冒頭）でグルーピング
        head = t.prompt.splitlines()[0][:40]
        by_prompt.setdefault(head, []).append(t)
    for head, items in by_prompt.items():
        lines.append(f"## Prompt: {head}\n")
        lines.append("| Model | think | ok | latency_ms | prompt | completion | total |")
        lines.append("|---|---|---|---|---|---|---|")
        for t in items:
            lines.append(
                f"| `{t.model}` | {t.think} | {t.ok} | {t.latency_ms} | "
                f"{t.prompt_tokens or '-'} | {t.completion_tokens or '-'} | {t.total_tokens or '-'} |"
            )
        lines.append("")
        for t in items:
            lines.append(f"### `{t.model}` (think={t.think})")
            if not t.ok:
                lines.append(f"ERROR: {t.error}")
            else:
                if t.reasoning:
                    lines.append(f"<details><summary>reasoning ({len(t.reasoning)} chars)</summary>\n\n```\n{t.reasoning[:2000]}\n```\n</details>\n")
                lines.append("```\n" + t.output + "\n```")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                   help="比較するモデル ID のリスト")
    p.add_argument("--think", action="store_true",
                   help="think モードを有効化（V4-Flash のみ意味あり）")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--json", action="store_true", help="JSON で出力")
    p.add_argument("--prompts", nargs="+",
                   choices=[pp["name"] for pp in PROMPTS],
                   help="特定のプロンプトだけ実行（既定は全部）")
    args = p.parse_args()

    base_url = os.environ.get("RIVAULT_URL")
    token = os.environ.get("RIVAULT_TOKEN")
    if not base_url or not token:
        print("ERROR: RIVAULT_URL / RIVAULT_TOKEN が未設定。"
              "source ~/.secrets/rivault_tokens.sh を実行してください", file=sys.stderr)
        return 2

    selected = [pp for pp in PROMPTS if not args.prompts or pp["name"] in args.prompts]
    trials: list[Trial] = []
    for prompt in selected:
        for model in args.models:
            print(f"[{prompt['name']}] {model} think={args.think} ...", file=sys.stderr, flush=True)
            t = call(
                base_url, token, model, prompt["system"], prompt["user"],
                max_tokens=args.max_tokens, think=args.think, timeout=args.timeout,
            )
            print(f"  -> ok={t.ok} latency={t.latency_ms}ms tokens={t.completion_tokens}",
                  file=sys.stderr, flush=True)
            trials.append(t)

    if args.json:
        print(json.dumps([t.to_dict() for t in trials], ensure_ascii=False, indent=2))
    else:
        print(render_md(trials))
    return 0


if __name__ == "__main__":
    sys.exit(main())
