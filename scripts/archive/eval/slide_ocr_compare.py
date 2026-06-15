#!/usr/bin/env python3
"""スライドOCRのモデル比較。

指定ディレクトリ内の PNG をローカル vLLM (gemma-4) と RiVault のマルチモーダルモデルで
OCR し、結果と所要時間を Markdown で並べる。

例:
    source ~/.secrets/rivault_tokens.sh
    python3 scripts/eval/slide_ocr_compare.py \
        --frames-dir /tmp/slide_compare/frames \
        --rivault-models Qwen/Qwen3.6-35B-A3B-FP8 Qwen/Qwen3.6-27B-FP8 google/gemma3:12b \
        --out /tmp/slide_compare/report.md
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "recording"))

from slide_ocr import MEETING_FRAME_OCR_PROMPT  # noqa: E402


def ocr_one(img_path: Path, base_url: str, api_key: str, model: str,
            *, prompt: str, max_tokens: int = 4096, timeout: int = 180) -> tuple[str, int, str]:
    """Returns (output, latency_ms, error)."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    started = time.time()
    try:
        resp = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return text, int((time.time() - started) * 1000), ""
    except Exception as exc:
        return "", int((time.time() - started) * 1000), f"{type(exc).__name__}: {exc}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--frames-dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--rivault-models", nargs="+", default=["Qwen/Qwen3.6-35B-A3B-FP8"])
    p.add_argument("--include-local", action="store_true", default=True,
                   help="ローカル vLLM (LOCAL_LLM_URL) も比較対象に含める")
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=180)
    args = p.parse_args()

    frames = sorted(args.frames_dir.glob("*.png"))
    if not frames:
        print(f"PNG が見つかりません: {args.frames_dir}", file=sys.stderr)
        return 2

    backends: list[tuple[str, str, str, str]] = []  # (label, base_url, api_key, model)
    if args.include_local:
        local_base = os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")
        try:
            r = requests.get(local_base.removesuffix("/v1") + "/v1/models", timeout=5)
            r.raise_for_status()
            local_model = r.json()["data"][0]["id"]
            backends.append(("local", local_base, os.environ.get("LOCAL_LLM_TOKEN", "dummy"),
                             local_model))
        except Exception as exc:
            print(f"[WARN] local vLLM 接続失敗、スキップ: {exc}", file=sys.stderr)

    rv_url = os.environ.get("RIVAULT_URL")
    rv_token = os.environ.get("RIVAULT_TOKEN")
    if rv_url and rv_token:
        for m in args.rivault_models:
            backends.append(("rivault", rv_url, rv_token, m))
    else:
        print("[WARN] RIVAULT_URL/RIVAULT_TOKEN 未設定、RiVault スキップ", file=sys.stderr)

    if not backends:
        return 2

    md = ["# Slide OCR 比較\n", f"frames: {len(frames)} 枚 (`{args.frames_dir}`)\n"]
    md.append("## サマリ\n")
    md.append("| backend | model | n_ok | total_latency_ms | avg_latency_ms |")
    md.append("|---|---|---|---|---|")

    results: dict[str, list[dict]] = {}
    for backend_label, base_url, api_key, model in backends:
        key = f"{backend_label}:{model}"
        print(f"== {key} ==", file=sys.stderr, flush=True)
        rows = []
        for i, f in enumerate(frames, 1):
            print(f"  [{i}/{len(frames)}] {f.name} ...", file=sys.stderr, flush=True)
            text, lat, err = ocr_one(
                f, base_url, api_key, model,
                prompt=MEETING_FRAME_OCR_PROMPT,
                max_tokens=args.max_tokens, timeout=args.timeout,
            )
            ok = bool(text and not err)
            print(f"    -> ok={ok} latency={lat}ms chars={len(text)}", file=sys.stderr, flush=True)
            rows.append({"frame": f.name, "text": text, "latency_ms": lat,
                         "ok": ok, "error": err})
        results[key] = rows
        n_ok = sum(1 for r in rows if r["ok"])
        total_lat = sum(r["latency_ms"] for r in rows)
        avg_lat = total_lat // max(len(rows), 1)
        md.append(f"| {backend_label} | `{model}` | {n_ok}/{len(rows)} | {total_lat} | {avg_lat} |")

    # フレーム別の比較
    md.append("\n## フレーム別比較\n")
    for i, f in enumerate(frames):
        md.append(f"\n### {f.name}\n")
        for key, rows in results.items():
            r = rows[i]
            md.append(f"\n#### `{key}` ({r['latency_ms']}ms)\n")
            if not r["ok"]:
                md.append(f"```\nERROR: {r['error']}\n```")
            else:
                md.append("```")
                md.append(r["text"][:3000])
                md.append("```")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(md), encoding="utf-8")
    print(f"レポート出力: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
