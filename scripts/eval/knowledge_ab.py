#!/usr/bin/env python3
"""knowledge_ab.py — Slack抽出ナレッジ検索 A/B評価（keywords / rerank / extraction / triage 比較モード）

retrieve_knowledge_for_extraction() の抽出される背景資料
（decisions/action_items 抽出プロンプトへの注入テキスト）の質、および
extract_from_thread() 自体の抽出結果の質を LLM-as-a-judge で比較する。
--compare で比較軸を切り替える:

- keywords（既定）: 第一段トピックキーワード抽出を keyword_mode="llm" / "sudachi" の
  両方で実行して比較する（method 名は "llm" / "sudachi"）
- rerank: keyword_mode="llm" 固定で、re-rank を llm_rerank=False / True の
  両方で実行して比較する（method 名は "norerank" / "rerank"）
- extraction: ingest.slack.extract_from_thread() を consensus_n=3（本番既定） /
  consensus_n=1（単発）の両方で実行し、抽出された decisions/action_items の質を
  比較する（method 名は "consensus3" / "consensus1"）。pm.db は milestones 取得の
  読み取りのみに使い、is_already_extracted/mark_extracted/save_slack_items 等の
  書き込み系関数は一切呼ばない
- triage: ingest.slack.extract_from_thread() を triage_mode="two_stage"（本番既定） /
  triage_mode="integrated"（抽出プロンプトに3ゲートを統合した1パス版）の両方で
  実行し、抽出された decisions/action_items の質を比較する（method 名は
  "two_stage" / "integrated"）。pm.db の扱いは extraction と同じ

- Slack スレッドは data/slack.db から scripts/ingest/slack.py の
  open_slack_db / fetch_threads 経由で取得する（直 sqlite3 接続はしない）
- judge は scripts/eval/argus_ab_judge.py の call_judge / parse_judge_output を
  import 流用する（判定ロジックの重複実装を避ける）
- 両出力が同一、または両方とも「該当なし」相当の場合は judge を呼ばず auto-tie とする
- 出力 JSONL にはスレッド本文を保存しない（keywords/rerank: thread_ts / 両手法の
  キーワード / identical / swap / prefer_method / rationale のみ。extraction/triage:
  キーワードの代わりに件数のみの items_{name}（d=N,a=M形式）で抽出結果全文は
  保存しない）
- --item-bearing: pm.db の decisions/action_items(source='slack', 非削除) を
  生んだスレッドだけに候補を絞り込む狙い撃ちサンプリング。どの --compare でも
  使用可能。指定時は --since-days による期間フィルタを適用しない。
  母集団が「現行パイプラインが item を生んだスレッド」に限定されるため偏りがあり、
  過剰抽出（誤って item を生んでいないか）の検出にはランダムサンプル
  （--item-bearing 無指定）の run との併用が必要

例:
    source ~/.secrets/rivault_tokens.sh
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --limit 30
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --compare rerank --limit 30
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --compare extraction
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py run --compare triage --item-bearing
    ~/.venv_aarch64/bin/python3 scripts/eval/knowledge_ab.py report
"""
from __future__ import annotations

import argparse
import json
import random
import re
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
from db_utils import open_pm_db  # noqa: E402
from ingest.slack import (  # noqa: E402
    extract_from_thread,
    fetch_milestones,
    fetch_threads,
    load_context_from_claude_md,
    open_slack_db,
)

DEFAULT_SLACK_DB = REPO_ROOT / "data" / "slack.db"
DEFAULT_QA_DB = REPO_ROOT / "data" / "qa_index.db"
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_JSONL = REPO_ROOT / "data" / "eval" / "knowledge_ab.jsonl"

_WIN_TIE_THRESHOLD = 0.60
# --compare extraction/triage 時の --limit 既定（未指定時）。他モードより LLM コストが高いため
# 抑える（スレッド毎に最大 consensus3 サンプル + triage、consensus1 + triage、judge の合計呼び出し。
# triage は consensus_n=1 固定だが two_stage/integrated それぞれ抽出+（two_stageのみ）triage を呼ぶ）。
_EXTRACTION_LIMIT_DEFAULT = 15
_DEFAULT_LIMIT = 30

# --compare モード → (method名, retrieve_knowledge_for_extraction への追加kwargs) のペア。
# extraction/triage モードのみ extract_from_thread() への kwargs（別シグネチャ）を保持する。
_COMPARE_VARIANTS: dict[str, list[tuple[str, dict]]] = {
    "keywords": [
        ("sudachi", {"keyword_mode": "sudachi"}),
        ("llm", {"keyword_mode": "llm"}),
    ],
    "rerank": [
        ("norerank", {"keyword_mode": "llm", "llm_rerank": False}),
        ("rerank", {"keyword_mode": "llm", "llm_rerank": True}),
    ],
    "extraction": [
        ("consensus3", {"consensus_n": 3, "enable_triage": True}),
        ("consensus1", {"consensus_n": 1, "enable_triage": True}),
    ],
    "triage": [
        ("two_stage", {"consensus_n": 1, "enable_triage": True, "triage_mode": "two_stage"}),
        ("integrated", {"consensus_n": 1, "enable_triage": True, "triage_mode": "integrated"}),
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

JUDGE_SYSTEM_EXTRACTION = (
    "あなたは Slack スレッドから抽出された決定事項・アクションアイテムの品質を審査する"
    "厳格な評価者です。評価観点は次の4点です: "
    "(1) 実在性 — スレッド本文に根拠がある項目か、"
    "(2) 重要度 — 些末な事項でないか、"
    "(3) 粒度 — 抽象的すぎたり細かすぎたりしないか、"
    "(4) 過不足 — スレッドの重要な決定事項・アクションアイテムを漏らしていないか。"
    "スレッドに根拠のない項目や些末な項目を過剰に抽出している側は減点してください"
    "（過剰抽出は減点）。両方とも decisions/action_items が0件で、"
    "それがスレッドの内容として妥当な場合は tie としてください。"
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


def _is_effectively_empty_extraction(extracted: dict) -> bool:
    """extract_from_thread() の返り値が decisions/action_items とも0件か判定する。"""
    return not (extracted.get("decisions") or []) and not (extracted.get("action_items") or [])


def _format_item_counts(extracted: dict) -> str:
    """JSONL 保存用に件数のみを 'd=N,a=M' 形式で返す（内容全文は保存しない）。"""
    d = len(extracted.get("decisions") or [])
    a = len(extracted.get("action_items") or [])
    return f"d={d},a={a}"


# --------------------------------------------------------------------------- #
# サンプル収集
# --------------------------------------------------------------------------- #

def _collect_candidate_threads(
    slack_conn, since_days: int, min_chars: int, *, apply_cutoff: bool = True,
) -> list[dict]:
    cutoff = None
    if apply_cutoff:
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
# 狙い撃ちサンプリング（--item-bearing）
# --------------------------------------------------------------------------- #

# Slack permalink 形式: https://.../archives/{channel_id}/p{sec:10桁}{micro:6桁}
_ITEM_SOURCE_REF_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def _collect_item_bearing_thread_keys(pm_conn) -> set[tuple[str, str]]:
    """開いた pm.db 接続（読み取り専用用途）の decisions/action_items
    （source='slack', 非削除）から実際に決定事項・アクションアイテムを生んだ
    (channel_id, thread_ts) の集合を返す。source_ref が正規表現にマッチしない
    件数は stderr に出力する（正規表現自体は拡張しない）。"""
    keys: set[tuple[str, str]] = set()
    unmatched = 0
    for table in ("decisions", "action_items"):
        rows = pm_conn.execute(
            f"SELECT source_ref FROM {table}"
            " WHERE source='slack' AND COALESCE(deleted,0)=0"
        ).fetchall()
        for row in rows:
            source_ref = row["source_ref"]
            m = _ITEM_SOURCE_REF_RE.search(source_ref) if source_ref else None
            if not m:
                unmatched += 1
                continue
            channel_id, sec, micro = m.group(1), m.group(2), m.group(3)
            keys.add((channel_id, f"{sec}.{micro}"))
    print(f"[INFO] source_ref 未マッチ: {unmatched} 件", file=sys.stderr)
    return keys


def _apply_item_bearing_filter(candidates: list[dict], pm_conn) -> list[dict]:
    keys = _collect_item_bearing_thread_keys(pm_conn)
    filtered = [c for c in candidates if (c.get("channel_id"), c.get("thread_ts")) in keys]
    print(
        f"--item-bearing: 候補 {len(candidates)} 件 → 実績のあるスレッド {len(filtered)} 件に絞り込み",
        file=sys.stderr,
    )
    return filtered


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    # --limit 未指定時は compare モードごとの既定値を適用する
    # （extraction は LLM コストが高いため既定を下げる）
    if args.limit is None:
        args.limit = (
            _EXTRACTION_LIMIT_DEFAULT if args.compare in ("extraction", "triage") else _DEFAULT_LIMIT
        )

    if args.compare in ("extraction", "triage"):
        return _run_extraction_compare(args)

    slack_db_path = Path(args.slack_db)
    qa_db_path = Path(args.qa_db)
    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    item_bearing = getattr(args, "item_bearing", False)
    slack_conn = open_slack_db(slack_db_path, no_encrypt=args.no_encrypt)
    try:
        candidates = _collect_candidate_threads(
            slack_conn, args.since_days, args.min_chars, apply_cutoff=not item_bearing,
        )
    finally:
        slack_conn.close()

    print(f"候補スレッド: {len(candidates)} 件 "
          f"({'--item-bearing のため期間フィルタなし' if item_bearing else f'直近{args.since_days}日'}, "
          f"{args.min_chars}字以上)", file=sys.stderr)

    if item_bearing:
        pm_conn = open_pm_db(Path(args.pm_db), no_encrypt=args.no_encrypt)
        try:
            candidates = _apply_item_bearing_filter(candidates, pm_conn)
        finally:
            pm_conn.close()

    rng = random.Random(args.seed)
    rng.shuffle(candidates)
    sampled = candidates[: args.limit] if args.limit else candidates
    print(f"サンプリング: {len(sampled)} 件 (seed={args.seed}, compare={args.compare})",
          file=sys.stderr)

    sampling_label = "item_bearing" if item_bearing else "random"
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
                "compare": args.compare,
                "sampling": sampling_label,
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


def _run_extraction_compare(args: argparse.Namespace) -> int:
    """--compare extraction/triage: ingest.slack.extract_from_thread() の

    - extraction: consensus_n=3（本番既定） vs consensus_n=1（単発）
    - triage: triage_mode=two_stage（既定） vs triage_mode=integrated（1パス統合）

    を比較する。pm.db への接続は run 全体で1本に統合し、--item-bearing の絞り込みと
    fetch_milestones() の両方に使い回す（読み取りのみ。is_already_extracted/
    mark_extracted/save_slack_items 等、pm.db のアイテムを追加・更新する関数は
    一切呼ばない。open_pm_db が実行する標準マイグレーションは走る）。
    ナレッジ文脈（knowledge_context）はスレッドごとに1回だけ retrieve_knowledge_for_extraction()
    で取得し、両アームへ同一の値を渡す（非決定的なナレッジ検索が比較の交絡要因に
    ならないようにするため）。
    """
    slack_db_path = Path(args.slack_db)
    pm_db_path = Path(args.pm_db)
    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    item_bearing = getattr(args, "item_bearing", False)
    slack_conn = open_slack_db(slack_db_path, no_encrypt=args.no_encrypt)
    try:
        candidates = _collect_candidate_threads(
            slack_conn, args.since_days, args.min_chars, apply_cutoff=not item_bearing,
        )
    finally:
        slack_conn.close()

    print(f"候補スレッド: {len(candidates)} 件 "
          f"({'--item-bearing のため期間フィルタなし' if item_bearing else f'直近{args.since_days}日'}, "
          f"{args.min_chars}字以上)", file=sys.stderr)

    context = load_context_from_claude_md()
    pm_conn = open_pm_db(pm_db_path, no_encrypt=args.no_encrypt)
    try:
        if item_bearing:
            candidates = _apply_item_bearing_filter(candidates, pm_conn)
        milestones = fetch_milestones(pm_conn)
    finally:
        pm_conn.close()
    print(f"マイルストーン: {len(milestones)} 件（読み取りのみ、アイテムの追加・更新は行わない）",
          file=sys.stderr)

    rng = random.Random(args.seed)
    rng.shuffle(candidates)
    sampled = candidates[: args.limit] if args.limit else candidates
    print(f"サンプリング: {len(sampled)} 件 (seed={args.seed}, compare={args.compare})",
          file=sys.stderr)

    sampling_label = "item_bearing" if item_bearing else "random"
    qa_db_path = REPO_ROOT / "data" / "qa_index.db"
    (name_0, kwargs_0), (name_1, kwargs_1) = _COMPARE_VARIANTS[args.compare]

    with open(jsonl_path, "a", encoding="utf-8") as out_f:
        for i, thread in enumerate(sampled, 1):
            thread_ts = thread["thread_ts"]
            thread_text = thread["thread_text"]
            print(f"  [{i}/{len(sampled)}] thread_ts={thread_ts} "
                  f"({len(thread_text)}字) ...", file=sys.stderr, flush=True)

            # 両アームへ同一のナレッジ文脈を渡す（extract_from_thread 内部の
            # 個別呼び出しに任せると、両アームで別々に検索されて差異の要因になる）
            knowledge_context = retrieve_knowledge_for_extraction(
                thread_text, qa_db_path=qa_db_path, top_k=3, index_name="pm-all",
            )

            # LLM の空応答等で extract_json が例外を投げても run 全体を止めない
            # （本番 run() も per-thread で握りつぶす設計。失敗レコードは
            #  prefer_method="extract_failed" として report の分母から自然に外れる）
            fail_reason = None
            out_0 = out_1 = None
            try:
                out_0 = extract_from_thread(
                    thread, context, milestones, REPO_ROOT,
                    knowledge_context=knowledge_context, **kwargs_0,
                )
            except (TypeError, ValueError):
                # kwargs 誤り・triage_mode 不正等の配線ミスは握りつぶさず落とす
                # （放置すると全件 extract_failed の静かな run になる）
                raise
            except Exception as e:
                fail_reason = f"{name_0}: {e}"
            if fail_reason is None:
                try:
                    out_1 = extract_from_thread(
                        thread, context, milestones, REPO_ROOT,
                        knowledge_context=knowledge_context, **kwargs_1,
                    )
                except (TypeError, ValueError):
                    raise
                except Exception as e:
                    fail_reason = f"{name_1}: {e}"
            if fail_reason is not None:
                record = {
                    "thread_ts": thread_ts,
                    "channel_id": thread.get("channel_id"),
                    "compare": args.compare,
                    "sampling": sampling_label,
                    "prefer_method": "extract_failed",
                    "rationale": fail_reason[:200],
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_f.flush()
                print(f"    -> prefer_method=extract_failed ({fail_reason[:80]})",
                      file=sys.stderr)
                continue

            record = {
                "thread_ts": thread_ts,
                "channel_id": thread.get("channel_id"),
                f"items_{name_0}": _format_item_counts(out_0),
                f"items_{name_1}": _format_item_counts(out_1),
                "compare": args.compare,
                "sampling": sampling_label,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

            if out_0 == out_1:
                record.update(identical=True, swap=None,
                              prefer_method="tie", rationale="identical_output")
            elif _is_effectively_empty_extraction(out_0) and _is_effectively_empty_extraction(out_1):
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
                    f"# 抽出結果 A\n"
                    f"{json.dumps(out_a, ensure_ascii=False, indent=2)}\n\n"
                    f"# 抽出結果 B\n"
                    f"{json.dumps(out_b, ensure_ascii=False, indent=2)}\n\n"
                    "候補 A と B のどちらが decisions/アクションアイテムの抽出結果として"
                    "より適切か判定し、JSON で答えてください。"
                )
                raw, _latency_ms, err = argus_ab_judge.call_judge(
                    args.judge_model, JUDGE_SYSTEM_EXTRACTION, prompt,
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

# compare モードごとの「新方式（挑戦者）」「既定（ベースライン）方式」の method 名。
# extraction は「N=1 でも品質が落ちないか」を測るのが目的のため、
# consensus1（挑戦者）/ consensus3（本番既定・ベースライン）とする。
# triage は「1パス統合（integrated）でも品質が落ちないか」を測るのが目的のため、
# integrated（挑戦者）/ two_stage（本番既定・ベースライン）とする。
_COMPARE_NEW_METHOD = {"keywords": "llm", "rerank": "rerank", "extraction": "consensus1", "triage": "integrated"}
_COMPARE_BASELINE_METHOD = {"keywords": "sudachi", "rerank": "norerank", "extraction": "consensus3", "triage": "two_stage"}
# 不合格時に案内する退避策（keywords/rerank は既定有効・opt-out env、
# extraction/triage は CLI 引数で本番既定へ戻す）
_FALLBACK_ENV = {
    "keywords": "ARGUS_DISABLE_LLM_KEYWORDS=1",
    "rerank": "ARGUS_DISABLE_LLM_RERANK=1",
    "extraction": "--slack-consensus 3 を明示指定",
    "triage": "--slack-triage-mode two_stage を明示指定",
}

# compare モードごとの JSONL レコードキーの接頭辞。keywords/rerank はキーワード行
# （keywords_{name}）、extraction/triage は件数のみ（items_{name}）を保存する。
_RECORD_PREFIX = {"keywords": "keywords", "rerank": "keywords", "extraction": "items", "triage": "items"}


def _detect_compare_mode(rec: dict) -> str | None:
    # JSONL のレコードは {prefix}_{name_0}/{prefix}_{name_1} キーの有無で
    # どの compare モードのレコードかを判別する（複数 compare モードの
    # レコードが同じ JSONL に混在していても集計を分離できる）。
    for mode, variants in _COMPARE_VARIANTS.items():
        prefix = _RECORD_PREFIX.get(mode, "keywords")
        name_0, name_1 = variants[0][0], variants[1][0]
        if f"{prefix}_{name_0}" in rec and f"{prefix}_{name_1}" in rec:
            return mode
    return None


def cmd_report(args: argparse.Namespace) -> int:
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL が見つかりません: {jsonl_path}", file=sys.stderr)
        return 2

    # (compare, sampling) の2次元で層別集計する。compare は record の "compare"
    # フィールドを優先し、無ければ従来どおり _detect_compare_mode() で判別する
    # （旧レコードとの後方互換）。sampling が無い旧レコードは "unknown" 扱い。
    groups: dict[tuple[str, str], dict[str, int]] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            mode = rec.get("compare") or _detect_compare_mode(rec) or "unknown"
            sampling = rec.get("sampling", "unknown")
            counts = groups.setdefault((mode, sampling), {})
            pm = rec.get("prefer_method", "parse_failed")
            counts[pm] = counts.get(pm, 0) + 1

    if not groups:
        print("レコードなし", file=sys.stderr)
        return 1

    print("# Knowledge A/B Report\n")
    any_valid = False
    for (mode, sampling), counts in groups.items():
        total = sum(counts.values())
        new_method = _COMPARE_NEW_METHOD.get(mode)
        baseline_method = _COMPARE_BASELINE_METHOD.get(mode)
        print(f"## compare={mode} sampling={sampling} (n={total})\n")
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
        description="Slack抽出ナレッジ検索 A/B評価（keywords: LLM vs SudachiPy / "
                     "rerank: LLM re-rank有無 / extraction: 抽出consensus_n=3 vs 1 / "
                     "triage: トリアージ方式two_stage vs integrated）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="サンプリング→両手法実行→judge→JSONL追記")
    r.add_argument("--compare", choices=sorted(_COMPARE_VARIANTS), default="keywords",
                   help="比較軸: keywords=キーワード抽出(sudachi/llm)、"
                        "rerank=re-rank有無(norerank/rerank、keyword_modeはllm固定)、"
                        "extraction=extract_from_thread()のconsensus_n(3=本番既定/1=単発)、"
                        "triage=トリアージ方式(two_stage=本番既定/integrated=1パス統合)")
    r.add_argument("--slack-db", default=str(DEFAULT_SLACK_DB), metavar="PATH")
    r.add_argument("--qa-db", default=str(DEFAULT_QA_DB), metavar="PATH")
    r.add_argument("--pm-db", default=str(DEFAULT_PM_DB), metavar="PATH",
                   help="pm.db のパス（--compare extraction/triage 時の milestones 取得、"
                        "および --item-bearing 時の絞り込みに使用。読み取り専用で、"
                        "アイテムの追加・更新は行わない。open_pm_db の標準マイグレーションは走る）")
    r.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    r.add_argument("--limit", type=int, default=None,
                   help=f"サンプル数上限（省略時: keywords/rerank={_DEFAULT_LIMIT}、"
                        f"extraction/triage={_EXTRACTION_LIMIT_DEFAULT}）")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--since-days", type=int, default=180)
    r.add_argument("--min-chars", type=int, default=200)
    r.add_argument("--item-bearing", action="store_true",
                   help="pm.db の decisions/action_items(source='slack', 非削除) を"
                        "生んだスレッドだけに候補を絞り込む狙い撃ちサンプリング。"
                        "指定時は --since-days による期間フィルタを適用しない"
                        "（item を生んだスレッドは古いものも対象にするため）。"
                        "母集団が『現行パイプラインが item を生んだスレッド』に限定され偏りがあるため、"
                        "過剰抽出の検出にはランダムサンプル（本フラグ無指定）の run との併用が必要")
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
