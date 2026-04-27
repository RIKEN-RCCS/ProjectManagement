#!/usr/bin/env python3
"""
pm_argus_agent.py — Argus Investigation Agent

LLM が自律的にツールを選択・呼び出して段階的にプロジェクトデータを分析する
マルチステップ Agent。/argus-investigate Slack コマンドおよび CLI から利用する。

Usage:
    # CLI モード（標準出力のみ）
    python3 scripts/pm_argus_agent.py --investigate "M3の遅延原因を調査" --dry-run
    python3 scripts/pm_argus_agent.py --investigate "先週の決定事項の実行状況" --max-steps 5

環境変数:
    OPENAI_API_BASE / RIVAULT_URL — LLM バックエンド（pm_argus.py と同じ）
    SLACK_BOT_TOKEN — Slack 返信用（Slack コマンド時のみ）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import (
    open_pm_db,
    fetch_milestone_progress,
    fetch_assignee_workload,
    fetch_overdue_items,
    fetch_weekly_trends,
    fetch_unacknowledged_decisions,
    fetch_summary_stats,
)
from format_utils import (
    format_milestone_table,
    format_overdue_list,
    format_assignee_table,
    format_weekly_trends as format_trends_table,
    format_decisions_list,
)

import yaml

logger = logging.getLogger("pm_argus_agent")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_QA_CONFIG = _DATA_DIR / "qa_config.yaml"
_DEFAULT_SINCE_DAYS = 30
_DEFAULT_MAX_STEPS = 5
_DEFAULT_TIMEOUT = 180.0
_CONTEXT_CHAR_LIMIT = 100_000


# =========================================================================== #
#  qa_config.yaml からインデックスDB・チャンネルリスト解決
# =========================================================================== #

def _resolve_index_and_channels(
    channel_id: str | None = None,
) -> tuple[Path, list[str]]:
    """qa_config.yaml を読み、channel_id に対応する index_db と channels を返す。"""
    if not _QA_CONFIG.exists():
        return _DATA_DIR / "qa_pm.db", []

    with open(_QA_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    indices = cfg.get("indices") or {}
    channel_map = cfg.get("channel_map") or {}
    default_index = cfg.get("default_index", "pm")

    index_name = channel_map.get(channel_id, default_index) if channel_id else default_index
    index_cfg = indices.get(index_name, {})
    db_rel = index_cfg.get("db", f"data/qa_{index_name}.db")
    index_db = _REPO_ROOT / db_rel
    channels = index_cfg.get("channels", [])
    return index_db, channels


# =========================================================================== #
#  AgentContext
# =========================================================================== #

@dataclass
class AgentContext:
    conn: Any
    today: str
    since: str
    no_encrypt: bool = False
    data_dir: Path = field(default_factory=lambda: _DATA_DIR)
    minutes_dir: Path = field(default_factory=lambda: _MINUTES_DIR)
    index_db: Path = field(default_factory=lambda: _DATA_DIR / "qa_pm.db")
    channels: list[str] = field(default_factory=list)


# =========================================================================== #
#  ToolDef & Registry
# =========================================================================== #

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, str]
    fn: Callable[[dict, AgentContext], str]


def _tool_get_milestone_progress(args: dict, ctx: AgentContext) -> str:
    rows = fetch_milestone_progress(ctx.conn)
    if not rows:
        return "（マイルストーンが登録されていません）"
    return format_milestone_table(rows, ctx.today)


def _tool_get_overdue_items(args: dict, ctx: AgentContext) -> str:
    items = fetch_overdue_items(ctx.conn, ctx.today, ctx.since)
    assignee = args.get("assignee")
    milestone = args.get("milestone")
    if assignee:
        items = [i for i in items if assignee in (i.get("assignee") or "")]
    if milestone:
        items = [i for i in items if i.get("milestone_id") == milestone]
    limit = int(args.get("limit", 20))
    items = items[:limit]
    if not items:
        filt = []
        if assignee:
            filt.append(f"assignee={assignee}")
        if milestone:
            filt.append(f"milestone={milestone}")
        return f"（該当する期限超過アイテムなし{' (' + ', '.join(filt) + ')' if filt else ''}）"
    return format_overdue_list(items, limit=limit)


def _tool_get_assignee_workload(args: dict, ctx: AgentContext) -> str:
    rows = fetch_assignee_workload(ctx.conn, ctx.today)
    if not rows:
        return "（担当者データなし）"
    return format_assignee_table(rows)


def _tool_get_weekly_trends(args: dict, ctx: AgentContext) -> str:
    weeks = int(args.get("weeks", 4))
    rows = fetch_weekly_trends(ctx.conn, weeks=weeks)
    if not rows:
        return "（トレンドデータなし）"
    return format_trends_table(rows)


def _tool_get_unacknowledged_decisions(args: dict, ctx: AgentContext) -> str:
    since = args.get("since", ctx.since)
    rows = fetch_unacknowledged_decisions(ctx.conn, since)
    if not rows:
        return "（未確認決定事項なし）"
    return format_decisions_list(rows)


def _tool_search_action_items(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import _query_action_items
    items = _query_action_items(
        ctx.conn,
        assignee=args.get("assignee"),
        status=args.get("status"),
        milestone=args.get("milestone"),
        keyword=args.get("keyword"),
        limit=int(args.get("limit", 20)),
    )
    if not items:
        return "（該当するアクションアイテムなし）"
    lines = []
    for i in items:
        parts = [
            f"ID:{i['id']}",
            f"[{i.get('status', '?')}]",
            i.get("content", "")[:80],
        ]
        if i.get("assignee"):
            parts.append(f"担当:{i['assignee']}")
        if i.get("due_date"):
            parts.append(f"期限:{i['due_date']}")
        if i.get("milestone_id"):
            parts.append(f"MS:{i['milestone_id']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _tool_search_decisions(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import _query_decisions
    items = _query_decisions(
        ctx.conn,
        keyword=args.get("keyword"),
        limit=int(args.get("limit", 20)),
    )
    if not items:
        return "（該当する決定事項なし）"
    lines = []
    for d in items:
        lines.append(f"ID:{d['id']} [{d.get('decided_at', '?')}] {d.get('content', '')[:100]}")
    return "\n".join(lines)


def _tool_search_text(args: dict, ctx: AgentContext) -> str:
    from pm_qa_server import retrieve_chunks, rerank_chunks, format_context
    query = args.get("query", "")
    if not query:
        return "（検索クエリが空です）"
    chunks = retrieve_chunks(query, ctx.index_db, k=30)
    if not chunks:
        return f"（「{query}」に一致する情報なし）"
    reranked = rerank_chunks(query, chunks)
    return format_context(reranked)


def _tool_get_slack_messages(args: dict, ctx: AgentContext) -> str:
    from pm_argus import fetch_raw_messages
    channel_id = args.get("channel_id", "")
    if not channel_id:
        return f"channel_id が必要です。利用可能なチャンネル: {', '.join(ctx.channels)}"
    if ctx.channels and channel_id not in ctx.channels:
        return (
            f"チャンネル {channel_id} は現在のインデックスの対象外です。"
            f" 利用可能なチャンネル: {', '.join(ctx.channels)}"
        )
    since = args.get("since", ctx.since)
    max_chars = int(args.get("max_chars", 10000))
    return fetch_raw_messages(
        channel_id, since, data_dir=ctx.data_dir, no_encrypt=ctx.no_encrypt,
        max_chars=max_chars,
    )


TOOLS: list[ToolDef] = [
    ToolDef(
        name="get_milestone_progress",
        description="マイルストーンの完了率・期限・残日数を一覧表示する",
        parameters={},
        fn=_tool_get_milestone_progress,
    ),
    ToolDef(
        name="get_overdue_items",
        description="期限超過のアクションアイテムを一覧表示する。担当者・マイルストーンでフィルタ可能",
        parameters={"assignee": "担当者名（部分一致）", "milestone": "マイルストーンID（例: M3）", "limit": "取得件数（デフォルト20）"},
        fn=_tool_get_overdue_items,
    ),
    ToolDef(
        name="get_assignee_workload",
        description="担当者別のオープンAI件数・期限超過件数を一覧表示する",
        parameters={},
        fn=_tool_get_assignee_workload,
    ),
    ToolDef(
        name="get_weekly_trends",
        description="週次のアクションアイテム作成数・完了数のトレンドを表示する",
        parameters={"weeks": "直近何週間分か（デフォルト4）"},
        fn=_tool_get_weekly_trends,
    ),
    ToolDef(
        name="get_unacknowledged_decisions",
        description="まだ確認されていない決定事項を一覧表示する",
        parameters={"since": "この日付以降（YYYY-MM-DD、省略時はデフォルト期間）"},
        fn=_tool_get_unacknowledged_decisions,
    ),
    ToolDef(
        name="search_action_items",
        description="アクションアイテムを条件検索する（担当者・状態・マイルストーン・キーワード）",
        parameters={"assignee": "担当者名", "status": "open または closed", "milestone": "マイルストーンID", "keyword": "内容のキーワード", "limit": "取得件数（デフォルト20）"},
        fn=_tool_search_action_items,
    ),
    ToolDef(
        name="search_decisions",
        description="決定事項をキーワードで検索する",
        parameters={"keyword": "検索キーワード", "limit": "取得件数（デフォルト20）"},
        fn=_tool_search_decisions,
    ),
    ToolDef(
        name="search_text",
        description="議事録・Slackメッセージを全文検索する（FTS5 + LLM re-ranking）",
        parameters={"query": "検索クエリ（自然言語可）"},
        fn=_tool_search_text,
    ),
    ToolDef(
        name="get_slack_messages",
        description="特定チャンネルの生Slackメッセージを取得する",
        parameters={"channel_id": "SlackチャンネルID（例: C08SXA4M7JT）", "since": "この日付以降（YYYY-MM-DD）", "max_chars": "最大文字数（デフォルト10000）"},
        fn=_tool_get_slack_messages,
    ),
]

_TOOL_MAP: dict[str, ToolDef] = {t.name: t for t in TOOLS}


# =========================================================================== #
#  Tool Description Builder
# =========================================================================== #

def _build_tool_descriptions() -> str:
    lines = []
    for i, t in enumerate(TOOLS, 1):
        params_desc = "なし"
        if t.parameters:
            params_desc = ", ".join(f"`{k}`: {v}" for k, v in t.parameters.items())
        lines.append(f"{i}. **{t.name}** — {t.description}\n   引数: {params_desc}")
    return "\n".join(lines)


# =========================================================================== #
#  Protocol Parser
# =========================================================================== #

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FINAL_ANSWER_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)


def parse_tool_calls(response: str) -> list[dict]:
    results = []
    for m in _TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            results.append({"error": f"JSONパースエラー: {m.group(1)[:100]}"})
    return results


def parse_final_answer(response: str) -> str | None:
    m = _FINAL_ANSWER_RE.search(response)
    return m.group(1).strip() if m else None


# =========================================================================== #
#  System Prompt
# =========================================================================== #

_AGENT_SYSTEM_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」の調査エージェントです。

## あなたの役割
ユーザーの質問に対し、利用可能なツールを使って段階的にデータを収集・分析し、
根拠に基づいた詳細な回答を生成してください。

## 利用可能なツール

ツールを呼び出すには以下の形式を使ってください:

<tool_call>
{{"name": "ツール名", "args": {{"引数名": "値"}}}}
</tool_call>

1つの応答に複数のツール呼び出しを含めることができます。

{tool_descriptions}

## 回答の完了

分析が完了したら、必ず以下の形式で最終回答を出力してください:

<final_answer>
最終回答をここに記載（Markdown形式）
</final_answer>

## 例

### 例1: マイルストーン調査

[User]
M3の遅延原因を調べて

[Assistant]
M3マイルストーンの期限超過アイテムを確認します。

<tool_call>
{{"name": "get_overdue_items", "args": {{"milestone": "M3"}}}}
</tool_call>

[User]
[Tool Result: get_overdue_items]
| ID | 内容 | 担当者 | 期限 |
|---|---|---|---|
| 42 | GPU性能テスト実施 | 西澤 | 2026-04-10 |
| 45 | ドライバ互換性確認 | 西澤 | 2026-04-15 |

[Assistant]
M3には西澤さん担当の期限超過が集中しています。関連議論を検索します。

<tool_call>
{{"name": "search_text", "args": {{"query": "M3 GPU ドライバ"}}}}
</tool_call>

### 例2: 決定事項の追跡

[User]
直近の決定事項が実行されているか確認して

[Assistant]
未確認決定事項と関連アクションアイテムを照合します。

<tool_call>
{{"name": "get_unacknowledged_decisions", "args": {{}}}}
</tool_call>

## 制約
- 最大 {max_steps} 回のツール呼び出しが可能です。効率的に使ってください。
- データの具体的根拠（アイテムID・担当者名・期限・マイルストーン名）を必ず引用してください。
- 推測ではなく、ツールから得たデータに基づいて回答してください。
- 分析が終わったら必ず <final_answer> タグで回答を出力してください。
"""


# =========================================================================== #
#  Seed Data (Lean Start)
# =========================================================================== #

def build_seed_data(ctx: AgentContext) -> str:
    stats = fetch_summary_stats(ctx.conn, since=ctx.since, today=ctx.today)
    milestones = fetch_milestone_progress(ctx.conn)
    workload = fetch_assignee_workload(ctx.conn, ctx.today)

    parts = [
        "## プロジェクト概況\n",
        f"- オープンAI: {stats.get('total_open', 0)} 件",
        f"- クローズ済みAI: {stats.get('total_closed', 0)} 件",
        f"- 期限超過: {stats.get('overdue_count', 0)} 件",
        f"- 未確認決定事項: {stats.get('unacknowledged_decisions', 0)} 件",
        f"- 本日: {ctx.today}",
        "",
    ]

    if milestones:
        parts.append("## マイルストーン進捗\n")
        parts.append(format_milestone_table(milestones, ctx.today))
        parts.append("")

    if workload:
        parts.append("## 担当者別負荷\n")
        parts.append(format_assignee_table(workload))
        parts.append("")

    return "\n".join(parts)


# =========================================================================== #
#  Conversation Serializer
# =========================================================================== #

def _serialize_conversation(system: str, messages: list[dict]) -> str:
    parts = [f"[System]\n{system}"]
    for msg in messages:
        label = "[User]" if msg["role"] == "user" else "[Assistant]"
        parts.append(f"\n{label}\n{msg['content']}")
    return "\n".join(parts)


def _estimate_chars(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages)


def _compact_messages(messages: list[dict]) -> list[dict]:
    """古いツール結果を1行要約に圧縮し、直近2ターン分は維持する。"""
    if len(messages) <= 4:
        return messages
    compacted = []
    for msg in messages[:-4]:
        if msg["role"] == "user" and msg["content"].startswith("[Tool Result:"):
            first_line = msg["content"].split("\n", 1)[0]
            char_count = len(msg["content"])
            compacted.append({"role": "user", "content": f"{first_line} （{char_count}文字、圧縮済み）"})
        else:
            compacted.append(msg)
    compacted.extend(messages[-4:])
    return compacted


# =========================================================================== #
#  Progress Updater
# =========================================================================== #

def _make_progress_updater(respond: Callable | None, max_respond_calls: int = 2) -> Callable[[str], None]:
    steps: list[str] = []
    respond_count = 0

    def update(msg: str) -> None:
        nonlocal respond_count
        steps.append(msg)
        if respond is not None and respond_count < max_respond_calls:
            try:
                respond(
                    text=":mag: Argus 調査中...\n" + "\n".join(steps),
                    response_type="ephemeral",
                    replace_original=True,
                )
                respond_count += 1
            except Exception as e:
                logger.warning(f"進捗通知エラー: {e}")
        else:
            logger.info(f"[STEP] {msg}")

    return update


# =========================================================================== #
#  Agent Loop
# =========================================================================== #

def execute_tool(name: str, args: dict, ctx: AgentContext) -> str:
    tool = _TOOL_MAP.get(name)
    if tool is None:
        available = ", ".join(_TOOL_MAP.keys())
        return f"エラー: ツール「{name}」は存在しません。利用可能なツール: {available}"
    try:
        return tool.fn(args, ctx)
    except Exception as e:
        return f"エラー: {name} の実行に失敗しました — {e}"


def run_agent(
    question: str,
    seed_data: str,
    respond: Callable | None,
    ctx: AgentContext,
    *,
    max_steps: int = _DEFAULT_MAX_STEPS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    tool_desc = _build_tool_descriptions()
    system_prompt = _AGENT_SYSTEM_PROMPT.format(
        tool_descriptions=tool_desc,
        max_steps=max_steps,
    )

    messages: list[dict] = [
        {"role": "user", "content": f"## 調査依頼\n\n{question}\n\n{seed_data}"},
    ]

    progress = _make_progress_updater(None)
    progress(f"シードデータ収集完了。調査開始（最大{max_steps}ステップ）")

    call_history: list[str] = []
    parse_error_count = 0
    start_time = time.monotonic()

    for step in range(1, max_steps + 1):
        elapsed = time.monotonic() - start_time
        if elapsed > timeout:
            logger.warning(f"タイムアウト ({timeout}s) に到達。ステップ {step} で中断")
            progress(f"タイムアウト（{int(elapsed)}秒経過）。現時点の分析結果を返します")
            break

        if _estimate_chars(messages) > _CONTEXT_CHAR_LIMIT:
            messages = _compact_messages(messages)
            logger.info(f"コンテキスト圧縮: {_estimate_chars(messages)} chars")

        prompt = _serialize_conversation(system_prompt, messages)
        logger.info(f"[investigate] Step {step}/{max_steps}: LLM呼び出し ({len(prompt)} chars)")

        try:
            response = call_argus_llm(
                prompt,
                max_tokens=4096,
                timeout=min(60, int(timeout - elapsed)),
            )
        except Exception as e:
            logger.exception(f"[investigate] LLM呼び出しエラー: {e}")
            progress(f"LLMエラー: {e}")
            break

        final = parse_final_answer(response)
        if final:
            logger.info(f"[investigate] <final_answer> 検出 (Step {step})")
            return final

        tool_calls = parse_tool_calls(response)

        if not tool_calls:
            parse_error_count += 1
            if parse_error_count >= 2:
                logger.warning("[investigate] 2回連続でツール呼び出し/最終回答なし。生テキストを返却")
                clean = re.sub(r"<[^>]+>", "", response).strip()
                return clean if clean else response
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": (
                "[System] ツール呼び出しか最終回答が検出できませんでした。\n"
                "ツールを使う場合は <tool_call>{...}</tool_call> 形式で、\n"
                "回答が完了したら <final_answer>...</final_answer> 形式で出力してください。"
            )})
            continue

        parse_error_count = 0
        messages.append({"role": "assistant", "content": response})

        result_parts = []
        for tc in tool_calls:
            if "error" in tc:
                result_parts.append(f"[Tool Error]\n{tc['error']}")
                continue

            call_key = json.dumps(tc, sort_keys=True, ensure_ascii=False)
            if call_key in call_history:
                result_parts.append(
                    f"[Tool Result: {tc['name']}]\n"
                    f"（同一引数での再呼び出し。前回と同じ結果です。別の引数を試すか <final_answer> で回答してください）"
                )
                continue
            call_history.append(call_key)

            tool_name = tc["name"]
            tool_args = tc["args"]
            args_desc = ", ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
            progress(f"Step {step}/{max_steps}: {tool_name}({args_desc})")

            result = execute_tool(tool_name, tool_args, ctx)
            result_parts.append(f"[Tool Result: {tool_name}]\n{result}")

        messages.append({"role": "user", "content": "\n\n".join(result_parts)})

    # ステップ上限到達: 最後のアシスタント応答を使う
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            clean = re.sub(r"<tool_call>.*?</tool_call>", "", msg["content"], flags=re.DOTALL).strip()
            if clean:
                return clean
    return "調査が完了しませんでした。より具体的な質問で再度お試しください。"


# =========================================================================== #
#  Slack Handler
# =========================================================================== #

def _run_investigate(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-investigate のバックグラウンド処理"""
    try:
        from pm_argus import _parse_command_args
        cmd_text = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()

        conn = open_pm_db(_PM_DB, no_encrypt=no_encrypt)

        index_db, channels = _resolve_index_and_channels(channel_id)

        ctx = AgentContext(
            conn=conn,
            today=today,
            since=since_date,
            no_encrypt=no_encrypt,
            data_dir=_DATA_DIR,
            minutes_dir=_MINUTES_DIR,
            index_db=index_db,
            channels=channels,
        )

        seed_data = build_seed_data(ctx)
        result = run_agent(
            question=cmd_text,
            seed_data=seed_data,
            respond=respond,
            ctx=ctx,
        )

        conn.close()

        # Slack ephemeral は約 3000 文字が実用上限
        _SLACK_MAX_CHARS = 2900
        header = f"*Argus 調査結果* ({today})\n\n"
        if len(header) + len(result) > _SLACK_MAX_CHARS:
            result = result[:_SLACK_MAX_CHARS - len(header) - 20] + "\n\n（...以下省略）"

        try:
            respond(
                text=header + result,
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception as e:
            logger.error(f"[investigate] 最終結果の Slack 送信エラー: {e}")
            logger.info(f"[investigate] 結果テキスト:\n{result[:500]}")
        logger.info("[investigate] 完了")

    except Exception as e:
        logger.exception("[investigate] エラー")
        try:
            respond(
                text=f":warning: Argus 調査エラー: {e}",
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception:
            pass


# =========================================================================== #
#  CLI Mode
# =========================================================================== #

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Argus Investigation Agent")
    parser.add_argument("--investigate", required=True, help="調査内容")
    parser.add_argument("--max-steps", type=int, default=_DEFAULT_MAX_STEPS, help="最大ステップ数")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT, help="タイムアウト（秒）")
    parser.add_argument("--days", type=int, default=_DEFAULT_SINCE_DAYS, help="直近何日分を対象にするか")
    parser.add_argument("--db", default=str(_PM_DB), help="pm.db のパス")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--dry-run", action="store_true", help="LLM呼び出しなし（シードデータ確認用）")
    args = parser.parse_args()

    today = date.today().isoformat()
    since_date = (date.today() - timedelta(days=args.days)).isoformat()

    conn = open_pm_db(Path(args.db), no_encrypt=args.no_encrypt)

    index_db, channels = _resolve_index_and_channels()

    ctx = AgentContext(
        conn=conn,
        today=today,
        since=since_date,
        no_encrypt=args.no_encrypt,
        data_dir=_DATA_DIR,
        minutes_dir=_MINUTES_DIR,
        index_db=index_db,
        channels=channels,
    )

    seed_data = build_seed_data(ctx)

    if args.dry_run:
        print("=== シードデータ ===")
        print(seed_data)
        print(f"\n=== 調査質問 ===\n{args.investigate}")
        print(f"\n=== ツール一覧 ===\n{_build_tool_descriptions()}")
        conn.close()
        return

    result = run_agent(
        question=args.investigate,
        seed_data=seed_data,
        respond=None,
        ctx=ctx,
        max_steps=args.max_steps,
        timeout=args.timeout,
    )

    conn.close()

    print("\n=== Argus 調査結果 ===\n")
    print(result)


if __name__ == "__main__":
    main()
