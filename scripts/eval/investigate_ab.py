#!/usr/bin/env python3
"""investigate_ab.py — investigate 検索パラメータ拡大 A/B 評価（baseline vs expanded）

gemma4（小型モデル）前提で固定されている investigate の検索パラメータ
（TOP_K_RERANK / re-rank プレビュー長 / search_text 抜粋長 / --file 全文QAの窓サイズ）を
glm-5.2（長コンテキスト・高推論）向けに拡大した場合の end-to-end 回答品質を、
scripts/eval/investigate_gold.yaml のゴールド質問セットで A/B 比較する。

- 各エントリ×各アーム（baseline=現行既定値 / expanded=拡大値）で
  scripts/argus/pm_argus_agent.py --investigate を本番同一コードパスで
  subprocess 実行する（--to-box/--to-slack/--to-canvas 等は付けない = 無副作用、stdout のみ）
- 両アームとも --agent-timeout（既定1200秒）を明示的に pm_argus_agent.py --timeout として
  渡し、同一の調査予算で比較する（未指定だと pm_argus_agent.py 内部既定の480秒予算で
  動き、expanded アームだけ予算切れの静かな劣化を受けて「拡大の効果」でなく
  「480秒に収まるか」を測ってしまうため）。subprocess の kill タイムアウトは
  agent_timeout + 300秒に自動設定する（CLI からは指定しない）
- 回答本文に pm_argus_agent.py の予算切れ注記
  （"タイムアウト予算超過のため未読込の断片"）が含まれるかを検出し、JSONL に
  budget_truncated_baseline / budget_truncated_expanded として記録する
- judge は scripts/eval/argus_ab_judge.py の call_judge / parse_judge_output を
  import 流用する（判定ロジックの重複実装を避ける）
- 回答本文は既定では保存しない（judge には in-memory で渡す）。--save-answers
  指定時のみ data/eval/investigate_ab/{run開始時刻}_{id}_{arm}.txt に保存し、
  JSONL に answer_path_baseline / answer_path_expanded を記録する。保存した
  平文回答（機密の可能性がある調査結果）は確認後に削除する運用とする。
  JSONL 自体には --save-answers の有無によらず本文を入れず文字数のみ記録する
- returncode 非0・タイムアウトは answer=None として記録し、judge はスキップして
  prefer_arm="error" とする
- cmd_run は実行前に RIVAULT_URL / RIVAULT_TOKEN の設定を確認する
  （investigate を全件走らせた後に judge 呼び出しで気付くのを防ぐため）

注意（env スコープ）: ARMS の env（ARGUS_TOP_K_RERANK 等）はプロセス全体スコープの
環境変数であり、investigate の検索層だけでなく Slack 抽出のナレッジ検索
（cli_utils.retrieve_knowledge_for_extraction 経由）にも影響する。この評価用に
export したシェルで pm_ingest.py 等の本番パイプラインを実行しないこと。

例:
    source ~/.secrets/rivault_tokens.sh
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run --entry frontflow-blue-nvhpc-bug
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run --save-answers
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py report
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = REPO_ROOT / "scripts"
_EVAL_DIR = Path(__file__).resolve().parent
for _p in (_SCRIPTS_DIR, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argus_ab_judge  # noqa: E402 — scripts/eval 内の同居モジュールを流用（call_judge/parse_judge_output）

DEFAULT_GOLD = _EVAL_DIR / "investigate_gold.yaml"
DEFAULT_JSONL = REPO_ROOT / "data" / "eval" / "investigate_ab.jsonl"
DEFAULT_ANSWERS_DIR = REPO_ROOT / "data" / "eval" / "investigate_ab"

_WIN_TIE_THRESHOLD = 0.60
_REQUIRED_FIELDS = ("id", "question", "reference", "since")

# pm_argus_agent.py --timeout の既定値（investigate の本番既定は480秒だが、
# A/B の両アームには明示的にこの値を渡し予算を固定する）
_DEFAULT_AGENT_TIMEOUT = 1200
# subprocess の kill タイムアウトの、agent_timeout に対するマージン（秒）
_KILL_TIMEOUT_MARGIN = 300

# pm_argus_agent.py:1272 の予算切れ注記マーカー（run_document_qa の
# skipped_windows 由来の制限事項）。このテキストが答えに含まれる場合、
# 予算不足で窓の一部が読まれずに回答が生成されたことを示す。
_BUDGET_MARKER = "タイムアウト予算超過のため未読込の断片"

# アーム定義: baseline=現行既定値（env override なし） / expanded=glm-5.2 向け拡大値。
# 既定値はここでは変更しない（A/B 合格後に本体側の既定を見直す）。
ARMS: dict[str, dict[str, str]] = {
    "baseline": {},
    "expanded": {
        "ARGUS_TOP_K_RERANK": "10",
        "ARGUS_SEARCH_EXCERPT_CHARS": "1200",
        "ARGUS_RERANK_PREVIEW_CHARS": "800",
        "ARGUS_DOC_QA_WINDOW": "150000",
    },
}

JUDGE_SYSTEM = (
    "あなたは PM 調査アシスタントの回答品質を審査する厳格な評価者です。"
    "評価観点: (1) 参照事実のカバー — 参照事実に該当する内容を正確に含むか、"
    "(2) 根拠性 — 出典（会議・Slack・資料名・日付）を明示しているか、"
    "(3) 質問への直接性 — 問いに正面から答えているか、"
    "(4) 正確性 — 参照事実と矛盾する断定をしていないか。"
    "冗長さ自体は減点しない。"
    "prefer は 'A'/'B'/'tie'、短い rationale を付けてください。"
    "出力は JSON のみ。スキーマ: {prefer:'A'|'B'|'tie', rationale:str}"
)


# --------------------------------------------------------------------------- #
# ゴールドセット読み込み
# --------------------------------------------------------------------------- #

def load_gold(path: Path) -> list[dict]:
    """investigate_gold.yaml を読み込み、必須フィールドを検証する。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("entries") or []
    for entry in entries:
        missing = [field for field in _REQUIRED_FIELDS if not entry.get(field)]
        if missing:
            raise ValueError(
                f"gold entry '{entry.get('id', '?')}' に必須フィールドが欠落: {missing}"
            )
    return entries


# --------------------------------------------------------------------------- #
# RIVAULT プリフライト
# --------------------------------------------------------------------------- #

def rivault_configured() -> bool:
    """RIVAULT_URL / RIVAULT_TOKEN が両方設定されているか確認する。"""
    return bool(os.environ.get("RIVAULT_URL")) and bool(os.environ.get("RIVAULT_TOKEN"))


# --------------------------------------------------------------------------- #
# investigate subprocess 実行
# --------------------------------------------------------------------------- #

def build_investigate_cmd(entry: dict, agent_timeout: float) -> list[str]:
    """gold エントリから pm_argus_agent.py --investigate のコマンド行を組み立てる。

    agent_timeout は pm_argus_agent.py --timeout（内部の調査予算）に渡す。
    mode=docqa かつ file フィールドがあれば --file を追加する。
    """
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "argus" / "pm_argus_agent.py"),
        "--investigate", entry["question"], "--since", entry["since"], "--no-intent-header",
        "--timeout", str(agent_timeout),
    ]
    if entry.get("mode") == "docqa" and entry.get("file"):
        cmd += ["--file", entry["file"]]
    return cmd


def _stderr_tail(stderr, n: int = 20) -> str:
    if not stderr:
        return ""
    text = stderr if isinstance(stderr, str) else stderr.decode("utf-8", "replace")
    return "\n".join(text.strip().splitlines()[-n:])


def run_investigate_arm(entry: dict, arm_env: dict[str, str], *, agent_timeout: float) -> dict:
    """1 エントリ × 1 アームを subprocess 実行する。

    pm_argus_agent.py --timeout に agent_timeout を明示的に渡し（両アーム同一予算）、
    subprocess の kill タイムアウトは agent_timeout + _KILL_TIMEOUT_MARGIN 秒に自動設定する。

    戻り値: {"answer": str|None, "latency_s": float, "error": str, "budget_truncated": bool}。
    returncode 非0・タイムアウト・その他例外は answer=None、error にメッセージを入れる
    （この場合 budget_truncated は False）。
    """
    cmd = build_investigate_cmd(entry, agent_timeout)
    kill_timeout = agent_timeout + _KILL_TIMEOUT_MARGIN
    env = {**os.environ, **arm_env}
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=kill_timeout,
        )
        latency_s = time.time() - started
        if proc.returncode != 0:
            return {
                "answer": None, "latency_s": latency_s, "budget_truncated": False,
                "error": f"returncode={proc.returncode}: {_stderr_tail(proc.stderr)}",
            }
        answer = proc.stdout.strip()
        return {
            "answer": answer, "latency_s": latency_s, "error": "",
            "budget_truncated": _BUDGET_MARKER in answer,
        }
    except subprocess.TimeoutExpired as exc:
        latency_s = time.time() - started
        err_tail = _stderr_tail(exc.stderr)
        return {
            "answer": None, "latency_s": latency_s, "budget_truncated": False,
            "error": f"TimeoutExpired: {kill_timeout}s" + (f" stderr_tail={err_tail}" if err_tail else ""),
        }
    except Exception as exc:
        return {
            "answer": None, "latency_s": time.time() - started, "budget_truncated": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    if not rivault_configured():
        print("ERROR: RIVAULT_URL / RIVAULT_TOKEN が未設定です。"
              "source ~/.secrets/rivault_tokens.sh してください。", file=sys.stderr)
        return 2

    gold_path = Path(args.gold)
    entries = load_gold(gold_path)
    if args.entry:
        entries = [e for e in entries if e["id"] == args.entry]
        if not entries:
            print(f"ERROR: entry id '{args.entry}' が gold に見つかりません: {gold_path}",
                  file=sys.stderr)
            return 2

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    answers_dir = Path(args.answers_dir)
    if args.save_answers:
        answers_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    with open(jsonl_path, "a", encoding="utf-8") as out_f:
        for i, entry in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] id={entry['id']} (mode={entry.get('mode', 'search')}) ...",
                  file=sys.stderr, flush=True)

            results: dict[str, dict] = {}
            answer_paths: dict[str, str | None] = {}
            for arm_name, arm_env in ARMS.items():
                print(f"  arm={arm_name} 実行中...", file=sys.stderr, flush=True)
                result = run_investigate_arm(entry, arm_env, agent_timeout=args.agent_timeout)
                results[arm_name] = result
                if args.save_answers:
                    answer_path = answers_dir / f"{run_ts}_{entry['id']}_{arm_name}.txt"
                    answer_path.write_text(result["answer"] or "", encoding="utf-8")
                    answer_paths[arm_name] = str(answer_path)
                else:
                    answer_paths[arm_name] = None
                status = "OK" if result["answer"] is not None else f"ERROR ({result['error']})"
                print(f"    -> {status} ({result['latency_s']:.1f}s)", file=sys.stderr)

            record = {
                "id": entry["id"],
                "mode": entry.get("mode", "search"),
                "compare": "search_expansion",
                "sampling": "gold",
                "latency_baseline_s": results["baseline"]["latency_s"],
                "latency_expanded_s": results["expanded"]["latency_s"],
                "chars_baseline": len(results["baseline"]["answer"] or ""),
                "chars_expanded": len(results["expanded"]["answer"] or ""),
                "budget_truncated_baseline": results["baseline"]["budget_truncated"],
                "budget_truncated_expanded": results["expanded"]["budget_truncated"],
                "answer_path_baseline": answer_paths["baseline"],
                "answer_path_expanded": answer_paths["expanded"],
                "swap": None,
                "prefer_arm": None,
                "rationale": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

            if results["baseline"]["answer"] is None or results["expanded"]["answer"] is None:
                record["prefer_arm"] = "error"
                record["rationale"] = "; ".join(
                    f"{name}: {r['error']}" for name, r in results.items() if r["answer"] is None
                )
            else:
                swap = rng.randint(0, 1)
                record["swap"] = bool(swap)
                arms_order = [("baseline", results["baseline"]["answer"]),
                              ("expanded", results["expanded"]["answer"])]
                if swap:
                    arms_order = arms_order[::-1]
                (arm_a, ans_a), (arm_b, ans_b) = arms_order
                prompt = (
                    f"# 質問\n{entry['question']}\n\n"
                    f"# 参照事実\n{entry['reference']}\n\n"
                    f"# 回答 A\n{ans_a[:12000]}\n\n"
                    f"# 回答 B\n{ans_b[:12000]}\n\n"
                    "候補 A と B のどちらが調査回答としてより優れているか判定し、JSON で答えてください。"
                )
                raw, _latency_ms, err = argus_ab_judge.call_judge(
                    args.judge_model, JUDGE_SYSTEM, prompt,
                    max_tokens=args.judge_max_tokens, timeout=args.judge_timeout,
                )
                parsed = argus_ab_judge.parse_judge_output(raw) if raw else None
                if not parsed:
                    record["prefer_arm"] = "parse_failed"
                    record["rationale"] = err or "parse_failed"
                else:
                    prefer = parsed.get("prefer", "tie")
                    if prefer == "A":
                        record["prefer_arm"] = arm_a
                    elif prefer == "B":
                        record["prefer_arm"] = arm_b
                    else:
                        record["prefer_arm"] = "tie"
                    record["rationale"] = parsed.get("rationale", "")

            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_f.flush()
            print(f"    -> prefer_arm={record['prefer_arm']}", file=sys.stderr)

    print(f"完了: {jsonl_path} に追記", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def aggregate_report(records: list[dict]) -> dict[str, dict]:
    """mode 別に prefer_arm を集計し、勝率・合否・平均レイテンシを算出する。

    error/parse_failed は win_tie_rate の分母（valid）から除外する。
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        groups.setdefault(rec.get("mode", "search"), []).append(rec)

    report: dict[str, dict] = {}
    for mode, recs in groups.items():
        counts: dict[str, int] = {}
        lat_baseline: list[float] = []
        lat_expanded: list[float] = []
        for r in recs:
            prefer_arm = r.get("prefer_arm") or "parse_failed"
            counts[prefer_arm] = counts.get(prefer_arm, 0) + 1
            if r.get("latency_baseline_s") is not None:
                lat_baseline.append(r["latency_baseline_s"])
            if r.get("latency_expanded_s") is not None:
                lat_expanded.append(r["latency_expanded_s"])

        expanded_wins = counts.get("expanded", 0)
        baseline_wins = counts.get("baseline", 0)
        ties = counts.get("tie", 0)
        valid = expanded_wins + baseline_wins + ties
        win_tie_rate = (expanded_wins + ties) / valid if valid else None

        report[mode] = {
            "counts": counts,
            "total": len(recs),
            "valid": valid,
            "win_tie_rate": win_tie_rate,
            "passed": bool(win_tie_rate is not None and win_tie_rate >= _WIN_TIE_THRESHOLD),
            "avg_latency_baseline_s": (sum(lat_baseline) / len(lat_baseline)) if lat_baseline else None,
            "avg_latency_expanded_s": (sum(lat_expanded) / len(lat_expanded)) if lat_expanded else None,
        }
    return report


def cmd_report(args: argparse.Namespace) -> int:
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL が見つかりません: {jsonl_path}", file=sys.stderr)
        return 2

    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    if not records:
        print("レコードなし", file=sys.stderr)
        return 1

    report = aggregate_report(records)
    print("# Investigate A/B Report (search expansion: baseline vs expanded)\n")
    any_valid = False
    for mode, stats in report.items():
        print(f"## mode={mode} (n={stats['total']})\n")
        for name, n in sorted(stats["counts"].items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {n}")

        if stats["valid"] == 0:
            print("  有効サンプルなし（judge解析失敗/実行エラーのみ）。判定不能\n")
            continue

        any_valid = True
        wins_ties = stats["counts"].get("expanded", 0) + stats["counts"].get("tie", 0)
        print(f"\n  expanded 勝ち+引き分け率: {stats['win_tie_rate']:.1%} ({wins_ties}/{stats['valid']})")
        if stats["passed"]:
            print(f"  合否判定: 合格（≥{_WIN_TIE_THRESHOLD:.0%}）→ 拡大値の既定採用可\n")
        else:
            print(f"  合否判定: 未達（<{_WIN_TIE_THRESHOLD:.0%}）→ 既定値のまま据え置き\n")

        avg_b = stats["avg_latency_baseline_s"]
        avg_e = stats["avg_latency_expanded_s"]
        if avg_b is not None or avg_e is not None:
            b_label = f"{avg_b:.1f}s" if avg_b is not None else "N/A"
            e_label = f"{avg_e:.1f}s" if avg_e is not None else "N/A"
            print(f"  平均レイテンシ: baseline={b_label} expanded={e_label}\n")

    return 0 if any_valid else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="investigate 検索パラメータ拡大（glm-5.2向け）の A/B 評価 "
                     "（baseline=現行既定値 / expanded=拡大値）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="ゴールド質問セット実行→judge→JSONL追記")
    r.add_argument("--gold", default=str(DEFAULT_GOLD), metavar="PATH")
    r.add_argument("--entry", default=None, help="指定した id のエントリのみ実行")
    r.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    r.add_argument("--answers-dir", default=str(DEFAULT_ANSWERS_DIR), metavar="PATH")
    r.add_argument("--agent-timeout", type=float, default=_DEFAULT_AGENT_TIMEOUT,
                   help="pm_argus_agent.py --timeout に渡す調査予算（秒）。両アーム共通"
                        f"（既定{_DEFAULT_AGENT_TIMEOUT}秒）。subprocess の kill タイムアウトは"
                        f"この値+{_KILL_TIMEOUT_MARGIN}秒に自動設定される")
    r.add_argument("--save-answers", action="store_true",
                   help="回答本文をファイル保存する（既定では保存しない）。"
                        "保存した平文回答は確認後に削除する運用とする")
    r.add_argument("--judge-model", default="Kimi-K2-Thinking")
    # Kimi-K2-Thinking は thinking 無効化不可で think に 2-3k トークンを使うため
    # 4096 未満だと本文が出る前に切れて parse_failed になる
    r.add_argument("--judge-max-tokens", type=int, default=4096)
    r.add_argument("--judge-timeout", type=int, default=600)
    r.add_argument("--seed", type=int, default=7)
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="JSONL集計・合否判定（≥60%%）")
    rp.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
