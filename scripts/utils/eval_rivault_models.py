#!/usr/bin/env python3
"""RiVault モデル評価スクリプト — Argus バックエンド適性テスト.

RiVault に追加されたモデルが Argus バックエンドとして使えるかを多角的に評価する。

使い方:
  source ~/.secrets/rivault_tokens.sh
  ~/.venv_aarch64/bin/python3 scripts/utils/eval_rivault_models.py
  ~/.venv_aarch64/bin/python3 scripts/utils/eval_rivault_models.py --models Qwen/Qwen3.6-35B-A3B-FP8
  ~/.venv_aarch64/bin/python3 scripts/utils/eval_rivault_models.py --skip deepseek --task brief
  ~/.venv_aarch64/bin/python3 scripts/utils/eval_rivault_models.py --output results.json

評価軸:
  speed     - TTFT と総生成時間（インタラクティブ用途の許容範囲）
  japanese  - 日本語文字比率（Argus は日本語 PM 出力必須）
  structure - 番号付きリスト形式（brief/risk の出力形式）
  no_leak   - thinking 内容の漏れ出し検出
  relevance - PM キーワードの含有率
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #

_RIVAULT_URL = ""
_RIVAULT_TOKEN = ""

_KIMI_MODELS = frozenset({"kimi-k2-thinking", "k2-think"})


def _is_kimi(model: str) -> bool:
    return model.lower() in _KIMI_MODELS


# --------------------------------------------------------------------------- #
# テストタスク定義
# --------------------------------------------------------------------------- #

TASKS: dict[str, dict] = {
    "ja": {
        "name": "日本語応答テスト",
        "desc": "短い日本語での応答能力（基礎）",
        "system": "あなたは日本語で応答する AI アシスタントです。",
        "prompt": (
            "プロジェクト管理において「リスク」と「課題」の違いを3文以内で説明してください。"
            "必ず日本語で答えてください。"
        ),
        "max_tokens": 512,
        "timeout": 60,
        "keywords": ["リスク", "課題", "将来", "発生", "顕在"],
        "min_chars": 80,
        "max_chars": 600,
    },
    "brief": {
        "name": "PM ブリーフ（Argus メインタスク）",
        "desc": "会議情報から今週のアクション5件を抽出・優先付け",
        "system": "あなたはプロジェクトマネジメント支援 AI です。与えられた情報を日本語で簡潔に分析してください。",
        "prompt": """\
【直近の会議決定事項】
1. A400 スーパーコンピュータの調達仕様を 7 月末までに確定させる（担当: 山田）
2. GPU オフロード評価環境を 8 月に構築する（担当: 鈴木）
3. ベンダーとの NDA 締結を 6 月末までに完了させる（担当: 法務チーム）→ 未完了

【Slack での直近の議論】
- 「A400 の仕様書、まだ山田さんから来てないけど大丈夫ですか」（2026-06-27）
- 「NDA が遅れているとベンダー側が別のプロジェクトを優先してしまう」（2026-06-26）
- GPU 環境の構築に必要な電源工事が 3 ヶ月かかるという情報（2026-06-25）

【アクションアイテム（未完了）】
- AI-045: A400 仕様書ドラフト作成（担当: 山田、期限: 2026-07-31）
- AI-067: ベンダー NDA 締結（担当: 法務、期限: 2026-06-30）→ 期限超過
- AI-078: 電源工事業者への見積依頼（担当: 施設、期限: 未定）

プロジェクトマネージャーが今週取るべき最重要アクション5件を、優先度順に日本語で列挙してください。各アクションは2〜3文で具体的に。
""",
        "max_tokens": 2048,
        "timeout": 120,
        "keywords": ["NDA", "法務", "山田", "電源", "仕様書", "期限", "ベンダー"],
        "min_chars": 300,
        "max_chars": 2000,
    },
    "risk": {
        "name": "リスク抽出テスト",
        "desc": "プロジェクト状況からリスクを識別・分類",
        "system": "あなたはプロジェクトマネジメント支援 AI です。与えられた情報を日本語で分析してください。",
        "prompt": """\
【プロジェクト現況】
- GPU クラスタの納期が 2026-09-30 に設定されているが、調達仕様がまだ確定していない
- 主要ベンダーとの契約交渉が停滞しており、担当者のアサイン状況が不明確
- ソフトウェアチームは先行して開発を進めているが、ハードウェア仕様に依存する部分が多い
- 並走している別プロジェクト（A400 調達）との工数競合が発生している

上記の状況から、このプロジェクトが直面している主なリスクを3件挙げ、それぞれの影響度（高/中/低）と対応策を日本語で記載してください。
""",
        "max_tokens": 1024,
        "timeout": 90,
        "keywords": ["リスク", "納期", "調達", "影響", "対応", "仕様"],
        "min_chars": 200,
        "max_chars": 1500,
    },
}


# --------------------------------------------------------------------------- #
# モデル一覧取得
# --------------------------------------------------------------------------- #

def fetch_models() -> list[str]:
    url = _RIVAULT_URL.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {_RIVAULT_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


# --------------------------------------------------------------------------- #
# 1 モデル × 1 タスクの実行
# --------------------------------------------------------------------------- #

def _run_one(model: str, task: dict) -> dict:
    """モデルにタスクを投げてメトリクスを返す。"""
    kimi = _is_kimi(model)
    messages = []
    if task["system"]:
        messages.append({"role": "system", "content": task["system"]})
    messages.append({"role": "user", "content": task["prompt"]})

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": task["max_tokens"],
        "stream": True,
        "temperature": 1.0 if kimi else 0.3,
    }
    if not kimi:
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {
        "Authorization": f"Bearer {_RIVAULT_TOKEN}",
        "Content-Type": "application/json",
    }
    url = _RIVAULT_URL.rstrip("/") + "/chat/completions"

    t0 = time.perf_counter()
    first_token_t: float | None = None
    content_parts: list[str] = []
    reasoning_chars = 0
    error: str | None = None

    try:
        resp = requests.post(
            url, headers=headers, json=payload,
            stream=True, timeout=task["timeout"],
        )
        if resp.status_code >= 400:
            error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        else:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode() if isinstance(raw, bytes) else raw
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                    tok = delta.get("content") or ""
                    reasoning_chars += len(delta.get("reasoning_content") or "")
                    if tok:
                        if first_token_t is None:
                            first_token_t = time.perf_counter()
                        content_parts.append(tok)
                except Exception:
                    pass
    except requests.exceptions.Timeout:
        error = f"TIMEOUT ({task['timeout']}s)"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    elapsed = time.perf_counter() - t0
    ttft = (first_token_t - t0) if first_token_t else elapsed
    raw_output = "".join(content_parts).strip()

    # thinking タグ除去
    clean = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw_output).strip()

    # thinking 漏れ検出（タグなし英語 CoT が先頭に来るケースも含む）
    think_leak = "<think>" in raw_output or (
        bool(raw_output) and not re.search(r"[^\x00-\x7F]", raw_output[:100])
        and len(raw_output) > 200
    )

    return {
        "elapsed": round(elapsed, 2),
        "ttft": round(ttft, 2),
        "raw_chars": len(raw_output),
        "output_chars": len(clean),
        "reasoning_chars": reasoning_chars,
        "think_leak": think_leak,
        "output": clean,
        "error": error,
    }


# --------------------------------------------------------------------------- #
# スコアリング
# --------------------------------------------------------------------------- #

def _score(metrics: dict, task: dict) -> dict:
    if metrics["error"]:
        return {"total": 0, "speed": 0, "japanese": 0, "structure": 0,
                "no_leak": 0, "relevance": 0, "verdict": "❌ エラー"}

    out = metrics["output"]
    chars = metrics["output_chars"]

    # --- speed (30点) ---
    ttft = metrics["ttft"]
    if ttft < 0.5:
        speed = 30
    elif ttft < 1.0:
        speed = 25
    elif ttft < 2.0:
        speed = 18
    elif ttft < 5.0:
        speed = 8
    else:
        speed = 0

    # --- japanese (25点) ---
    jp_chars = len(re.findall(r"[^\x00-\x7F]", out))
    jp_ratio = jp_chars / max(chars, 1)
    if jp_ratio > 0.65:
        japanese = 25
    elif jp_ratio > 0.4:
        japanese = 15
    elif jp_ratio > 0.2:
        japanese = 5
    else:
        japanese = 0

    # --- structure (20点) ---
    has_list = bool(re.search(r"^\s*[1-5１-５][\.\．\)）\s]", out, re.MULTILINE))
    structure = 20 if has_list else 0

    # --- no_leak (15点) ---
    no_leak = 15 if not metrics["think_leak"] else 0

    # --- relevance (10点) ---
    keywords = task.get("keywords", [])
    found = sum(1 for kw in keywords if kw in out)
    relevance = round(found / max(len(keywords), 1) * 10)

    # --- length penalty ---
    if chars < task.get("min_chars", 0):
        japanese = max(0, japanese - 10)
        structure = max(0, structure - 10)
    if chars > task.get("max_chars", 9999):
        speed = max(0, speed - 5)

    total = speed + japanese + structure + no_leak + relevance

    if total >= 80:
        verdict = "✅ 適合"
    elif total >= 60:
        verdict = "⚠️  要検討"
    else:
        verdict = "❌ 不適合"

    return {
        "total": total,
        "speed": speed,
        "japanese": japanese,
        "structure": structure,
        "no_leak": no_leak,
        "relevance": relevance,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #

def _print_result(model: str, task_id: str, task: dict,
                  metrics: dict, score: dict) -> None:
    name = model[-42:] if len(model) > 42 else model
    if metrics["error"]:
        print(f"  [{task_id}] {name}")
        print(f"    エラー: {metrics['error']}")
        return

    out_preview = metrics["output"][:200].replace("\n", " ")
    if len(metrics["output"]) > 200:
        out_preview += "…"

    thinking_note = f" (thinking {metrics['reasoning_chars']}chars)" \
        if metrics["reasoning_chars"] > 0 else ""
    leak_note = " ⚠️thinking漏れ" if metrics["think_leak"] else ""

    print(f"  [{task_id}] {name}")
    print(f"    速度: {metrics['elapsed']:.1f}s (TTFT {metrics['ttft']:.1f}s){thinking_note}{leak_note}")
    print(f"    出力: {metrics['output_chars']}文字")
    print(f"    プレビュー: {out_preview}")
    print(f"    スコア: {score['total']}/100  "
          f"速度{score['speed']} 日本語{score['japanese']} "
          f"構造{score['structure']} 漏れ{score['no_leak']} 関連{score['relevance']}"
          f"  → {score['verdict']}")


def _print_summary(results: list[dict]) -> None:
    print(f"\n{'='*70}")
    print("【総合評価サマリー】")
    print(f"{'='*70}")
    print(f"{'モデル':<42} {'avg':>5} {'速度':>4} {'日本語':>6} {'構造':>4} {'漏れ':>4} {'判定'}")
    print(f"{'─'*70}")

    for r in sorted(results, key=lambda x: -x["avg_score"]):
        name = r["model"][-40:] if len(r["model"]) > 40 else r["model"]
        avg = r["avg_score"]
        speed = r["avg_speed"]
        jp = r["avg_japanese"]
        struct = r["avg_structure"]
        leak = r["avg_no_leak"]
        verdict = r["verdict"]
        print(f"{name:<42} {avg:>5.0f} {speed:>4.0f} {jp:>6.0f} {struct:>4.0f} {leak:>4.0f}  {verdict}")


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #

def main() -> int:
    global _RIVAULT_URL, _RIVAULT_TOKEN
    import os

    parser = argparse.ArgumentParser(
        description="RiVault モデル Argus 適性評価",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--models", nargs="+", metavar="MODEL",
        help="評価するモデルを指定（省略時: /v1/models から全取得）",
    )
    parser.add_argument(
        "--skip", nargs="+", default=[], metavar="PATTERN",
        help="スキップするモデルの部分文字列（例: deepseek codellama）",
    )
    parser.add_argument(
        "--task", choices=list(TASKS.keys()) + ["all"], default="all",
        help="実行するタスク（デフォルト: all）",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="結果を JSON ファイルに保存",
    )
    parser.add_argument(
        "--timeout", type=int,
        help="タイムアウト秒数（各タスクの設定を上書き）",
    )
    args = parser.parse_args()

    _RIVAULT_URL = os.environ.get("RIVAULT_URL", "").rstrip("/")
    _RIVAULT_TOKEN = os.environ.get("RIVAULT_TOKEN", "dummy")
    if not _RIVAULT_URL:
        print("[ERROR] RIVAULT_URL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください",
              file=sys.stderr)
        return 1

    # モデル一覧
    if args.models:
        models = args.models
    else:
        try:
            models = fetch_models()
        except Exception as exc:
            print(f"[ERROR] モデル一覧取得失敗: {exc}", file=sys.stderr)
            return 1

    # スキップ処理
    if args.skip:
        orig = models
        models = [m for m in models
                  if not any(s.lower() in m.lower() for s in args.skip)]
        skipped = [m for m in orig if m not in models]
        if skipped:
            print(f"スキップ: {', '.join(skipped)}\n")

    # タスク選択
    task_ids = list(TASKS.keys()) if args.task == "all" else [args.task]

    # タイムアウト上書き
    if args.timeout:
        for t in TASKS.values():
            t["timeout"] = args.timeout

    print(f"RiVault: {_RIVAULT_URL}")
    print(f"評価モデル数: {len(models)} / タスク: {', '.join(task_ids)}")
    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    summary_per_model: dict[str, dict] = {}

    for model in models:
        print(f"\n{'─'*70}")
        print(f"▶ {model}")
        print(f"{'─'*70}")

        model_scores: list[dict] = []
        model_detail: list[dict] = []

        for task_id in task_ids:
            task = TASKS[task_id]
            print(f"\n  実行中: {task['name']}...", end="", flush=True)
            metrics = _run_one(model, task)
            print(" 完了")
            score = _score(metrics, task)
            _print_result(model, task_id, task, metrics, score)

            model_scores.append(score)
            model_detail.append({
                "task_id": task_id,
                "metrics": metrics,
                "score": score,
            })

        # モデル集計
        valid_scores = [s for s in model_scores if s["total"] > 0]
        avg = sum(s["total"] for s in valid_scores) / max(len(valid_scores), 1)
        avg_speed = sum(s["speed"] for s in model_scores) / max(len(model_scores), 1)
        avg_jp = sum(s["japanese"] for s in model_scores) / max(len(model_scores), 1)
        avg_struct = sum(s["structure"] for s in model_scores) / max(len(model_scores), 1)
        avg_leak = sum(s["no_leak"] for s in model_scores) / max(len(model_scores), 1)

        if avg >= 80:
            verdict = "✅ 適合"
        elif avg >= 60:
            verdict = "⚠️  要検討"
        else:
            verdict = "❌ 不適合"

        summary_per_model[model] = {
            "model": model,
            "avg_score": avg,
            "avg_speed": avg_speed,
            "avg_japanese": avg_jp,
            "avg_structure": avg_struct,
            "avg_no_leak": avg_leak,
            "verdict": verdict,
            "tasks": model_detail,
        }

    results = list(summary_per_model.values())
    _print_summary(results)

    # JSON 保存
    if args.output:
        out_path = Path(args.output)
        out_data = {
            "timestamp": datetime.now().isoformat(),
            "rivault_url": _RIVAULT_URL,
            "tasks": task_ids,
            "results": results,
        }
        out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n結果を保存: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
