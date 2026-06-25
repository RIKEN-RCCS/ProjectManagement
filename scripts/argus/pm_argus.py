#!/usr/bin/env python3
"""
pm_argus.py — Argus AI Project Intelligence System

データ収集・プロンプト構築ロジック + --brief-to-canvas CLI モード。

Slack (/argus-brief, /argus-draft, /argus-risk) コマンドのバックグラウンド処理と、
cron による毎朝の自動ブリーフィング生成 (--brief-to-canvas) を担う。

Usage:
    # ブリーフィング生成 → Canvas 投稿
    python3 scripts/pm_argus.py --brief-to-canvas --canvas-id <CANVAS_ID>

    # ブリーフィング生成 → 標準出力のみ（--dry-run）
    python3 scripts/pm_argus.py --brief-to-canvas --dry-run

    # リスク分析のみ
    python3 scripts/pm_argus.py --risk --dry-run

環境変数:
    RIVAULT_URL   — RiVault エンドポイント URL
    RIVAULT_TOKEN — RiVault API トークン
    SLACK_BOT_TOKEN — Canvas 投稿時に必要（slack_sdk 用）
"""

import argparse
import concurrent.futures
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger("pm_argus")

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context

from argus.prompts import (  # noqa: F401 — 後方互換のため全プロンプト定数を再 export
    _BRIEF_ORCHESTRATOR_PROMPT,
    _BRIEF_PROMPT,
    _BRIEF_WORKER_CONVERSATION_PROMPT,
    _BRIEF_WORKER_MINUTES_PROMPT,
    _BRIEF_WORKER_PM_PROMPT,
    _DAILY_SUMMARY_PROMPT,
    _DRAFT_AGENDA_PROMPT,
    _DRAFT_REPORT_PROMPT,
    _DRAFT_REQUEST_PROMPT,
    _RISK_ORCHESTRATOR_PROMPT,
    _RISK_PROMPT,
    _RISK_WORKER_CONVERSATION_PROMPT,
    _RISK_WORKER_KNOWLEDGE_PROMPT,
    _RISK_WORKER_MINUTES_PROMPT,
    _RISK_WORKER_PM_PROMPT,
)

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_ARGUS_CONFIG_FILE = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_FILE_LEGACY = _DATA_DIR / "qa_config.yaml"


_DEFAULT_SINCE_DAYS = 30
_DRAFT_REPORT_SINCE_DAYS = 14
_WORKER_MAX_CHARS = 8000  # Worker に渡す各セクションの最大文字数

from argus.narrate import (  # noqa: F401 — 後方互換のため全シンボルを再 export
    _build_channel_name_map,
    _build_stats_section,
    _collect_all_data,
    _fetch_single_pm_stats,
    _filter_mentions_for_user,
    _fmt_closed_items,
    _format_period_description,
    # 設定・データ収集（pm_qa_server / pm_argus_agent からも参照）
    _load_argus_config,
    _load_channel_ids,
    _load_minutes_names,
    _narrate_action_blocks,
    _narrate_lock,
    _narrate_sessions,
    # narrate セッション
    _NarrateSession,
    _parse_command_args,
    _post_argus_video,
    _post_argus_voice,
    _post_today_voice,
    # Orchestrator
    _run_brief,
    _run_brief_worker,
    _run_draft,
    _run_narrate,
    _run_narrate_build,
    _run_narrate_cancel,
    _run_risk,
    _run_risk_worker,
    _run_today_only,
    _run_transcribe,
    # transcribe ジョブ管理（narrate.py に移動済み）
    _transcribe_jobs,
    _transcribe_lock,
    # プロンプト構築
    build_brief_prompt,
    build_draft_prompt,
    build_risk_prompt,
    fetch_background_knowledge,
    fetch_pm_stats,
    fetch_raw_messages,
    fetch_recent_minutes,
    load_pm_db_paths,
    merge_pm_stats,
    resolve_index_name,
)

# CLI モード（--brief-to-canvas / --risk / --dry-run）
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Argus — AI Project Intelligence System CLI"
    )
    parser.add_argument("--brief-to-canvas", action="store_true",
                        help="ブリーフィングを生成して Canvas に投稿")
    parser.add_argument("--risk", action="store_true",
                        help="リスク分析を生成して Canvas に投稿（--dry-run で投稿なし）")
    parser.add_argument("--canvas-id", default=None, metavar="ID",
                        help="投稿先 Canvas ID（必須）")
    parser.add_argument("--dry-run", action="store_true",
                        help="Canvas 投稿なし・標準出力のみ")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DB を暗号化しない（平文モード）")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="データ収集の開始日（デフォルト: 30日前）")
    parser.add_argument("--days", type=int, default=None, metavar="N",
                        help="直近何日分を対象にするか（デフォルト: 30日。--since と同時指定時は --since 優先）")
    parser.add_argument("--today-only", action="store_true",
                        help="今日のデータのみ収集（--days と --since を無視）")
    parser.add_argument("--assignee", default=None, metavar="NAME",
                        help="担当者フォーカス（例: --assignee 西澤）")
    parser.add_argument("--topic", default=None, metavar="TEXT",
                        help="話題フォーカス（例: --topic Benchpark）")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="pm.db のパス（デフォルト: data/pm.db）")
    parser.add_argument("--index-name", default=None, metavar="NAME",
                        help="argus_config.yaml の indices.{name} を選択して "
                             "channels / minutes / pm_db を絞り込む（例: pm-hpc）。"
                             "省略時は default_index。")
    args = parser.parse_args()

    today = date.today().isoformat()

    if args.today_only:
        # 今日のデータのみ
        days = 0
        since_date = today
    else:
        # 既存のロジック
        days = args.days if args.days is not None else _DEFAULT_SINCE_DAYS
        since_date = args.since or (date.today() - timedelta(days=days)).isoformat()
    pm_db_paths_cli = [Path(args.db)] if args.db else load_pm_db_paths(args.index_name)

    context = load_claude_md_context()
    print(f"[INFO] since: {since_date} / today: {today} / "
          f"index: {args.index_name or '(default)'}", file=sys.stderr)

    messages, minutes, stats, knowledge_summary, web_articles = _collect_all_data(
        today, since_date,
        no_encrypt=args.no_encrypt,
        pm_db_paths=pm_db_paths_cli,
        index_name=args.index_name,
    )

    if args.brief_to_canvas:
        # マルチWorker + Orchestrator で生成
        print("[INFO] 多視点 Worker でブリーフィング生成中...", file=sys.stderr)
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        focus_lines = []
        if args.assignee:
            focus_lines.append(f"担当者フォーカス: {args.assignee}")
        if args.topic:
            focus_lines.append(f"話題フォーカス: {args.topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        worker_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            wfuts = {
                pool.submit(_run_brief_worker, "pm", stats_section): "pm",
                pool.submit(_run_brief_worker, "conversation", conversation_section): "conversation",
                pool.submit(_run_brief_worker, "minutes", minutes_section): "minutes",
            }
            for f in concurrent.futures.as_completed(wfuts):
                name = wfuts[f]
                try:
                    worker_results[name] = f.result()
                except Exception as e:
                    worker_results[name] = f"（{name} Worker エラー: {e}）"
                    print(f"[WARN] Worker {name} 失敗: {e}", file=sys.stderr)

        orch_prompt = _BRIEF_ORCHESTRATOR_PROMPT.format(
            context=context,
            knowledge_summary=knowledge_summary or "（蒸留ナレッジなし）",
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
        )
        print("[INFO] Orchestrator 統合中...", file=sys.stderr)
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")

        title = "Argus 日次活動サマリー" if days == 0 else "Argus ブリーフィング"
        canvas_content = f"# {title} ({today})\n\n{result}\n\n_生成: {today} JST_"

        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    elif args.risk:
        # マルチWorker + Orchestrator で生成
        print("[INFO] 多視点 Worker でリスク分析生成中...", file=sys.stderr)
        s = stats.get("stats", {})
        stats_section = _build_stats_section(stats, s, today)
        conversation_section = (messages or "（データなし）")[-_WORKER_MAX_CHARS:]
        minutes_section = (minutes or "（データなし）")[-_WORKER_MAX_CHARS:]
        knowledge_section = knowledge_summary or "（蒸留ナレッジなし）"
        focus_lines = []
        if args.assignee:
            focus_lines.append(f"担当者フォーカス: {args.assignee}")
        if args.topic:
            focus_lines.append(f"話題フォーカス: {args.topic}")
        focus_section_str = "\n".join(focus_lines) if focus_lines else "なし"

        worker_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            wfuts = {
                pool.submit(_run_risk_worker, "pm", stats_section): "pm",
                pool.submit(_run_risk_worker, "conversation", conversation_section): "conversation",
                pool.submit(_run_risk_worker, "minutes", minutes_section): "minutes",
                pool.submit(_run_risk_worker, "knowledge", knowledge_section): "knowledge",
            }
            for f in concurrent.futures.as_completed(wfuts):
                name = wfuts[f]
                try:
                    worker_results[name] = f.result()
                except Exception as e:
                    worker_results[name] = f"（{name} Worker エラー: {e}）"
                    print(f"[WARN] Worker {name} 失敗: {e}", file=sys.stderr)

        orch_prompt = _RISK_ORCHESTRATOR_PROMPT.format(
            context=context,
            focus_section=focus_section_str,
            worker_pm=worker_results.get("pm", "（エラー）"),
            worker_conversation=worker_results.get("conversation", "（エラー）"),
            worker_minutes=worker_results.get("minutes", "（エラー）"),
            worker_knowledge=worker_results.get("knowledge", "（エラー）"),
        )
        print("[INFO] Orchestrator 統合中...", file=sys.stderr)
        result = call_argus_llm(orch_prompt, system="あなたはAIインテリジェンスシステムArgusです。")
        canvas_content = f"# Argus リスク分析 ({today})\n\n{result}\n\n_生成: {today} JST_"
        print("\n" + "=" * 60)
        print(canvas_content)
        print("=" * 60)

        if args.dry_run:
            print("[INFO] --dry-run: Canvas 投稿をスキップ", file=sys.stderr)
            return

        canvas_id = args.canvas_id
        if not canvas_id:
            print("[ERROR] Canvas ID が不明。--canvas-id を指定してください",
                  file=sys.stderr)
            sys.exit(1)

        from canvas_utils import post_to_canvas, sanitize_for_canvas
        post_to_canvas(canvas_id, sanitize_for_canvas(canvas_content))
        print(f"[INFO] Canvas {canvas_id} に投稿しました", file=sys.stderr)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
