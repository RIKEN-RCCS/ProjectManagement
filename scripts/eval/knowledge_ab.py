#!/usr/bin/env python3
"""knowledge_ab.py — Slack抽出ナレッジ検索 A/B評価（keywords / rerank 比較モード）

retrieve_knowledge_for_extraction() の抽出される背景資料
（decisions/action_items 抽出プロンプトへの注入テキスト）の質を
LLM-as-a-judge で比較する。--compare で比較軸を切り替える:

- keywords（既定）: 第一段トピックキーワード抽出を keyword_mode="llm" / "sudachi" の
  両方で実行して比較する（method 名は "llm" / "sudachi"）
- rerank: keyword_mode="llm" 固定で、re-rank を llm_rerank=False / True の
  両方で実行して比較する（method 名は "norerank" / "rerank"）

- Slack スレッドは data/slack.db から scripts/ingest/slack.py の
  open_slack_db / fetch_threads 経由で取得する（直 sqlite3 接続はしない）
- judge は scripts/eval/argus_ab_judge.py の call_judge / parse_judge_output を
  import 流用する（判定ロジックの重複実装を避ける）
- 両出力が同一、または両方とも「該当なし」相当の場合は judge を呼ばず auto-tie とする
- 出力 JSONL にはスレッド本文を保存しない（thread_ts / 両手法のキーワード / identical /
  swap / prefer_method / rationale のみ）

例:
    source ~/.secrets/rivault_tokens.sh
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --limit 30
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --compare rerank --limit 30
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py report
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = REPO_ROOT / "scripts"
_EVAL_DIR = Path(__file__).resolve().parent
for _p in (_SCRIPTS_DIR, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argus_ab_judge  # noqa: E402 — scripts/eval 内の同居モジュールを流用
from cli_utils import retrieve_knowledge_for_extraction  # noqa: E402
from ingest.slack import fetch_threads, open_slack_db  # noqa: E402

DEFAULT_SLACK_DB = REPO_ROOT / "data" / "slack.db"
DEFAULT_QA_DB = REPO_ROOT / "data" / "qa_index.db"
DEFAULT_JSONL = REPO_ROOT / "data" / "eval" / "knowledge_ab.jsonl"

_WIN_TIE_THRESHOLD = 0.60

# --compare モード → (method名, retrieve_knowledge_for_extraction への追加kwargs) のペア
_COMPARE_VARIANTS: dict[str, list[tuple[str, dict]]] = {
    "keywords": [
        ("sudachi", {"keyword_mode": "sudachi"}),
        ("llm", {"keyword_mode": "llm"}),
    ],
    "rerank": [
        ("norerank", {"keyword_mode": "llm", "llm_rerank": False}),
        ("rerank", {"keyword_mode": "llm", "llm_rerank": True}),
    ],
}

JUDGE_SYSTEM = (
    "あなたは Slack スレッドからの決定事項・アクションアイテム抽出を支援する"
    "背景資料（過去ナレッジ検索結果）の質を審査する厳格な評価者です。"
    "評価観点は「decisions/アクションアイテム抽出の背景資料としての関連性」のみです。"
    "無関係なナレッジを注入するくらいなら『該当する過去議論なし』のほうが望ましいと判断してください。"
    "prefer は 'A' / 'B' / 'tie' のいずれかとし、短い rationale を付けてください。"
    "出力は JSON オブジェクトのみ。コードフェンス不要。スキーマ: "
    "{prefer:'A'|'B'|'tie', rationale:str}"
)


class _CaptureLogger:
    """retrieve_knowledge_for_extraction() の info ログから
    'キーワード(sudachi|llm)=...' 行を回収するための最小ロガー。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        self.messages.append(msg)

    def warning(self, msg: str) -> None:
        self.messages.append(msg)


def _extract_keyword_line(logger: _CaptureLogger) -> str:
    for msg in logger.messages:
        if "キーワード(" in msg:
            return msg
    return ""


def _is_effectively_empty(text: str) -> bool:
    t = (text or "").strip()
    return t == "" or t == "（該当する過去議論なし）"


# --------------------------------------------------------------------------- #
# サンプル収集
# --------------------------------------------------------------------------- #

def _collect_candidate_threads(
    slack_conn, since_days: int, min_chars: int,
) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
    channel_rows = slack_conn.execute(
        "SELECT DISTINCT channel_id FROM messages"
    ).fetchall()
    channel_ids = [r[0] for r in channel_rows]

    candidates: list[dict] = []
    for channel_id in channel_ids:
        threads = fetch_threads(slack_conn, channel_id, cutoff)
        for t in threads:
            if len(t.get("thread_text") or "") >= min_chars:
                candidates.append({**t, "channel_id": channel_id})
    return candidates


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    slack_db_path = Path(args.slack_db)
    qa_db_path = Path(args.qa_db)
    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    slack_conn = open_slack_db(slack_db_path, no_encrypt=args.no_encrypt)
    try:
        candidates = _collect_candidate_threads(slack_conn, args.since_days, args.min_chars)
    finally:
        slack_conn.close()

    print(f"候補スレッド: {len(candidates)} 件 (直近{args.since_days}日, "
          f"{args.min_chars}字以上)", file=sys.stderr)

    rng = random.Random(args.seed)
    rng.shuffle(candidates)
    sampled = candidates[: args.limit] if args.limit else candidates
    print(f"サンプリング: {len(sampled)} 件 (seed={args.seed}, compare={args.compare})",
          file=sys.stderr)

    (name_0, kwargs_0), (name_1, kwargs_1) = _COMPARE_VARIANTS[args.compare]

    with open(jsonl_path, "a", encoding="utf-8") as out_f:
        for i, thread in enumerate(sampled, 1):
            thread_ts = thread["thread_ts"]
            thread_text = thread["thread_text"]
            print(f"  [{i}/{len(sampled)}] thread_ts={thread_ts} "
                  f"({len(thread_text)}字) ...", file=sys.stderr, flush=True)

            log_0 = _CaptureLogger()
            out_0 = retrieve_knowledge_for_extraction(
                thread_text, qa_db_path=qa_db_path, top_k=args.top_k,
                index_name=args.index_name, logger=log_0, **kwargs_0,
            )
            log_1 = _CaptureLogger()
            out_1 = retrieve_knowledge_for_extraction(
                thread_text, qa_db_path=qa_db_path, top_k=args.top_k,
                index_name=args.index_name, logger=log_1, **kwargs_1,
            )

            record = {
                "thread_ts": thread_ts,
                "channel_id": thread.get("channel_id"),
                f"keywords_{name_0}": _extract_keyword_line(log_0),
                f"keywords_{name_1}": _extract_keyword_line(log_1),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

            if out_0 == out_1:
                record.update(identical=True, swap=None,
                              prefer_method="tie", rationale="identical_output")
            elif _is_effectively_empty(out_0) and _is_effectively_empty(out_1):
                record.update(identical=False, swap=None,
                              prefer_method="tie", rationale="both_empty")
            else:
                swap = rng.randint(0, 1)
                methods = [(name_0, out_0), (name_1, out_1)]
                if swap:
                    methods = methods[::-1]
                (method_a, out_a), (method_b, out_b) = methods
                prompt = (
                    f"# Slackスレッド（抜粋）\n{thread_text[:4000]}\n\n"
                    f"# 背景資料候補 A\n{out_a[:3000] or '（該当する過去議論なし）'}\n\n"
                    f"# 背景資料候補 B\n{out_b[:3000] or '（該当する過去議論なし）'}\n\n"
                    "候補 A と B のどちらが decisions/アクションアイテム抽出の背景資料として"
                    "より適切か判定し、JSON で答えてください。"
                )
                raw, _latency_ms, err = argus_ab_judge.call_judge(
                    args.judge_model, JUDGE_SYSTEM, prompt,
                    max_tokens=args.judge_max_tokens, timeout=args.judge_timeout,
                )
                parsed = argus_ab_judge.parse_judge_output(raw) if raw else None
                if not parsed:
                    record.update(identical=False, swap=bool(swap),
                                  prefer_method="parse_failed",
                                  rationale=err or "parse_failed")
                else:
                    prefer = parsed.get("prefer", "tie")
                    if prefer == "A":
                        prefer_method = method_a
                    elif prefer == "B":
                        prefer_method = method_b
                    else:
                        prefer_method = "tie"
                    record.update(identical=False, swap=bool(swap),
                                  prefer_method=prefer_method,
                                  rationale=parsed.get("rationale", ""))

            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_f.flush()
            print(f"    -> prefer_method={record['prefer_method']}", file=sys.stderr)

    print(f"完了: {jsonl_path} に追記", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

# compare モードごとの「新方式」「既定（ベースライン）方式」の method 名。
# JSONL のレコードは keywords_{name_0}/keywords_{name_1} キーの有無で
# どの compare モードのレコードかを判別する（複数 compare モードの
# レコードが同じ JSONL に混在していても集計を分離できる）。
_COMPARE_NEW_METHOD = {"keywords": "llm", "rerank": "rerank"}
_COMPARE_BASELINE_METHOD = {"keywords": "sudachi", "rerank": "norerank"}
# 不合格時に案内する退避用 env（両機能とも既定有効・opt-out 方式）
_FALLBACK_ENV = {"keywords": "ARGUS_DISABLE_LLM_KEYWORDS=1", "rerank": "ARGUS_DISABLE_LLM_RERANK=1"}


def _detect_compare_mode(rec: dict) -> str | None:
    for mode, variants in _COMPARE_VARIANTS.items():
        name_0, name_1 = variants[0][0], variants[1][0]
        if f"keywords_{name_0}" in rec and f"keywords_{name_1}" in rec:
            return mode
    return None


def cmd_report(args: argparse.Namespace) -> int:
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL が見つかりません: {jsonl_path}", file=sys.stderr)
        return 2

    groups: dict[str, dict[str, int]] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            mode = _detect_compare_mode(rec) or "unknown"
            counts = groups.setdefault(mode, {})
            pm = rec.get("prefer_method", "parse_failed")
            counts[pm] = counts.get(pm, 0) + 1

    if not groups:
        print("レコードなし", file=sys.stderr)
        return 1

    print("# Knowledge A/B Report\n")
    any_valid = False
    for mode, counts in groups.items():
        total = sum(counts.values())
        new_method = _COMPARE_NEW_METHOD.get(mode)
        baseline_method = _COMPARE_BASELINE_METHOD.get(mode)
        print(f"## compare={mode} (n={total})\n")
        for method, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {method}: {n}")

        if new_method is None:
            print("  (compare モード不明。method 別件数のみ表示)\n")
            continue

        new_wins = counts.get(new_method, 0)
        baseline_wins = counts.get(baseline_method, 0)
        ties = counts.get("tie", 0)
        valid = new_wins + baseline_wins + ties
        if valid == 0:
            print("  有効サンプルなし（judge解析失敗のみ）。判定不能\n")
            continue

        any_valid = True
        win_tie_rate = (new_wins + ties) / valid
        fallback_env = _FALLBACK_ENV.get(mode, "")
        print(f"\n  {new_method} 勝ち+引き分け率: {win_tie_rate:.1%} ({new_wins + ties}/{valid})")
        if win_tie_rate >= _WIN_TIE_THRESHOLD:
            print(f"  合否判定: 合格（≥{_WIN_TIE_THRESHOLD:.0%}）→ 既定有効のままロールアウト可\n")
        else:
            print(f"  合否判定: 未達（<{_WIN_TIE_THRESHOLD:.0%}）→ "
                  f"退避 env（{fallback_env}）で無効化して既定を見直す\n")

    return 0 if any_valid else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="Slack抽出ナレッジ検索 A/B評価（keywords: LLM vs SudachiPy / rerank: LLM re-rank有無）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="サンプリング→両手法実行→judge→JSONL追記")
    r.add_argument("--compare", choices=sorted(_COMPARE_VARIANTS), default="keywords",
                   help="比較軸: keywords=キーワード抽出(sudachi/llm)、"
                        "rerank=re-rank有無(norerank/rerank、keyword_modeはllm固定)")
    r.add_argument("--slack-db", default=str(DEFAULT_SLACK_DB), metavar="PATH")
    r.add_argument("--qa-db", default=str(DEFAULT_QA_DB), metavar="PATH")
    r.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    r.add_argument("--limit", type=int, default=30)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--since-days", type=int, default=180)
    r.add_argument("--min-chars", type=int, default=200)
    r.add_argument("--top-k", type=int, default=3, help="本番と同一パラメータ")
    r.add_argument("--index-name", default="pm-all", help="本番と同一パラメータ")
    r.add_argument("--judge-model", default="Kimi-K2-Thinking")
    # Kimi-K2-Thinking は thinking 無効化不可で think に 2-3k トークンを使うため
    # 4096 未満だと本文が出る前に切れて parse_failed になる
    r.add_argument("--judge-max-tokens", type=int, default=4096)
    r.add_argument("--judge-timeout", type=int, default=300)
    r.add_argument("--no-encrypt", action="store_true",
                   help="slack.db を平文モードで開く")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="JSONL集計・合否判定（≥60%%）")
    rp.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
