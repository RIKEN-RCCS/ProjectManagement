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
    LOCAL_LLM_URL / RIVAULT_URL — LLM バックエンド（pm_argus.py と同じ）
    SLACK_BOT_TOKEN — Slack 返信用（Slack コマンド時のみ）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
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
_ARGUS_CONFIG = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_LEGACY = _DATA_DIR / "qa_config.yaml"
_DEFAULT_SINCE_DAYS = 30
_DEFAULT_MAX_STEPS = 20
_DEFAULT_TIMEOUT = 480.0
_CONTEXT_CHAR_LIMIT = 100_000


# =========================================================================== #
#  argus_config.yaml からインデックスDB・チャンネルリスト解決
# =========================================================================== #

def _resolve_index_and_channels(
    channel_id: str | None = None,
) -> tuple[Path, list[str], list[Path], str]:
    """argus_config.yaml を読み、channel_id に対応する index_db (統合)・channels・pm_db_paths・index_name を返す。

    全インデックスは統合 data/qa_index.db を共有し、検索時に index_name でフィルタする。
    """
    qa_index = _DATA_DIR / "qa_index.db"
    config_path = _ARGUS_CONFIG if _ARGUS_CONFIG.exists() else _QA_CONFIG_LEGACY
    if not config_path.exists():
        return qa_index, [], [_PM_DB], "pm"

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    indices = cfg.get("indices") or {}
    channel_map = cfg.get("channel_map") or {}
    default_index = cfg.get("default_index", "pm")

    index_name = channel_map.get(channel_id, default_index) if channel_id else default_index
    index_cfg = indices.get(index_name, {})
    channels = index_cfg.get("channels", [])
    pm_db_list = index_cfg.get("pm_db", ["data/pm.db"])
    pm_db_paths = [_REPO_ROOT / p for p in pm_db_list]
    return qa_index, channels, pm_db_paths, index_name



from argus.agent_tools import (  # noqa: F401 — 後方互換のため全 symbol を再 export
    AgentContext, ToolDef, TOOLS, _TOOL_MAP, _query_all,
    _build_tool_descriptions,
    # 従来の _tool_* 関数は agent_tools.py 内で mcp_tools 委譲となったため
    # 個別 export は不要（必要なのは AgentContext / TOOLS / _TOOL_MAP のみ）
)

# =========================================================================== #
#  Protocol Parser
# =========================================================================== #

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FINAL_ANSWER_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)
# Kimi-K2-Thinking が稀に <answer>...</answer> タグでラップして返すケースの救済
# 中身が JSON なら tool_call として扱い、それ以外は final_answer として扱う
_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.DOTALL)
_JSON_TOOL_CALL_RE = re.compile(r"\{[^{}]*?\"name\"\s*:\s*\"[^\"]+\"[^{}]*?\"args\"\s*:\s*\{[^{}]*\}[^{}]*\}", re.DOTALL)


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
    if results:
        return results
    # フォールバック: <answer>{json}</answer> 形式や、生 JSON の混入を検出
    for m in _ANSWER_TAG_RE.finditer(response):
        body = m.group(1).strip()
        for jm in _JSON_TOOL_CALL_RE.finditer(body):
            try:
                obj = json.loads(jm.group(0))
                name = obj.get("name", "")
                args = obj.get("args", {})
                if isinstance(args, dict) and isinstance(name, str) and name:
                    results.append({"name": name, "args": args})
            except json.JSONDecodeError:
                pass
    if results:
        return results
    # <answer> タグなしで生 JSON だけ返ってくるケース
    for jm in _JSON_TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(jm.group(0))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            pass
    return results


def parse_final_answer(response: str) -> str | None:
    m = _FINAL_ANSWER_RE.search(response)
    if m:
        return m.group(1).strip()
    # フォールバック: <answer> タグの中身が JSON でなければ最終回答とみなす
    a = _ANSWER_TAG_RE.search(response)
    if a:
        body = a.group(1).strip()
        # JSON tool_call っぽくない（"name":"..." を含まない）なら最終回答
        if "\"name\"" not in body or "\"args\"" not in body:
            return body
    return None


# =========================================================================== #
#  System Prompt
# =========================================================================== #

_AGENT_SYSTEM_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
ユーザーの質問に対し、利用可能なツールを駆使して徹底的に調査し、
根拠に基づく詳細な回答を生成してください。

## ツール呼び出し形式

<tool_call>
{{"name": "ツール名", "args": {{"引数名": "値"}}}}
</tool_call>

1ステップで複数の <tool_call> を並列に並べてよい。タグ名は厳密に:
ツール呼び出しは `<tool_call>`、最終回答は `<final_answer>`。

{tool_descriptions}

## 進め方

- 質問の内容に応じて適切なツールを選択すること
- `search_text` で全文検索、`search_decisions` で決定事項検索、
  `search_entity` で異なる視点（conservative/aggressive/objective/future_oriented）
  とデータ種別（pm_data/minutes/slack/box_docs）の組み合わせで多角的に分析すること
- 複数の視点とデータ種別を掛け合わせて検索すると、より深い洞察が得られる
- 同じツールを異なる引数で呼ぶことは有用。遠慮せずに必要なだけツールを使うこと
- 得られた結果に対して `synthesize_answers` を使って複数 Explorer の分析を統合することも検討する
- 質問が意思決定や制約に関する場合は `search_decisions` と `search_text` を併用する
- 特定ユーザーのメンション状況は `search_mentions` を使う
- `search_entity` を複数の perspective × data_type の組み合わせで並列実行し、
  多角的な分析結果を得ることが推奨される
- 調査結果を Box/Slack/Canvas に出力する必要がある場合は出力ツールを使うこと
  （出力前にユーザーに確認を取ること）

## 最終回答

- 収集したすべての情報を総合し、根拠を明示した回答を生成すること
- 数値・日付・人名・会議名・決定事項IDなど具体的根拠を引用すること
- 推測ではなくツール結果に基づいて答えること
- 回答の長さに制限はない。必要なだけ詳しく説明すること
- 必ず `<final_answer>` タグで終わること
"""


# =========================================================================== #
#  Seed Data (Lean Start)
# =========================================================================== #

def build_seed_data(ctx: AgentContext) -> str:
    all_stats = [fetch_summary_stats(c, since=ctx.since, today=ctx.today) for c in ctx.conns]
    stats: dict = {}
    for s in all_stats:
        for k, v in s.items():
            stats[k] = stats.get(k, 0) + v
    milestones = _query_all(ctx, fetch_milestone_progress)
    all_wl: list = []
    for c in ctx.conns:
        all_wl.extend(fetch_assignee_workload(c, ctx.today))
    wl_map: dict[str, dict] = {}
    for w in all_wl:
        name = w["assignee"]
        if name in wl_map:
            wl_map[name]["total_open"] += w["total_open"]
            wl_map[name]["overdue"] += w["overdue"]
            wl_map[name]["no_due_date"] += w.get("no_due_date", 0)
        else:
            wl_map[name] = {**w}
    workload = sorted(wl_map.values(), key=lambda x: (-x["overdue"], -x["total_open"]))

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


_QUERY_REWRITE_PROMPT = """\
あなたは富岳NEXTプロジェクト（理研×富士通×NVIDIA、次世代スーパーコンピュータ開発）の AI アシスタントです。
ユーザーが Slack の `/argus-investigate` に投げた **短く曖昧な質問** を、社内ナレッジ検索エージェント向けに展開します。

質問:
{question}

このプロジェクトに登場する固有名詞の例（参考、すべて社内用語）:
- EEA (Early Evaluation Application): 評価対象アプリ群、EEA-1 / EEA-2 などのフェーズあり
- コデザイン: 富岳NEXT のハード・ソフト協調設計活動
- スケールアウトネットワーク: ノード間相互接続
- HBM, ノード構成, ベンチマーク, 性能予測, LQCD, GENESIS, NICAM, Petsy, PyTorch 等

以下を JSON で出力してください（コードブロック禁止、JSON のみ）:

{{
  "intent": "ユーザーが本当に知りたいこと（1〜2文、推測でよい）",
  "entities": ["質問に含まれる/想起される固有名詞や略語の正規形（最大6個）"],
  "search_queries": ["検索エンジンに投げる具体クエリ（2〜4個、日本語/英語混在可、固有名詞優先）"]
}}

注意:
- ユーザー語彙を一般用語に置き換えない（例: 「EEA」を「欧州経済領域」と展開してはいけない。これは社内略語）。
- タイプミス・省略形は元のまま entities に残し、正規形を**併記**する。
- 推測が不確実なら intent に「（推測）」を付ける。
"""


def _rewrite_query(question: str) -> dict | None:
    """ユーザー質問を意図 / 固有名詞 / 検索クエリに展開する。失敗時は None。"""
    prompt = _QUERY_REWRITE_PROMPT.format(question=question.strip())
    try:
        t0 = time.time()
        response = call_argus_llm(prompt, max_tokens=512, timeout=30, think=False)
        elapsed = time.time() - t0
        logger.info(f"[rewrite] LLM応答 {len(response)} chars, {elapsed:.1f}s")
        # JSON 抽出（前後の余計な文字を許容）
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if not m:
            logger.warning(f"[rewrite] JSON 抽出失敗: {response[:200]}")
            return None
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return None
        intent = str(data.get("intent", "")).strip()
        entities = [str(x) for x in data.get("entities", []) if x][:6]
        queries = [str(x) for x in data.get("search_queries", []) if x][:4]
        if not intent and not entities and not queries:
            return None
        return {"intent": intent, "entities": entities, "search_queries": queries}
    except Exception as e:
        logger.warning(f"[rewrite] 失敗: {e}")
        return None


def _format_rewrite_for_seed(rewrite: dict) -> str:
    """seed_data 冒頭に注入する形式に整形。"""
    parts = ["## 質問の解釈（自動展開）\n"]
    if rewrite.get("intent"):
        parts.append(f"- **意図**: {rewrite['intent']}")
    if rewrite.get("entities"):
        parts.append(f"- **関連語**: {', '.join(rewrite['entities'])}")
    if rewrite.get("search_queries"):
        parts.append(f"- **推奨検索クエリ**: {', '.join(rewrite['search_queries'])}")
    parts.append("")
    parts.append("（上記は LLM による自動展開。原質問の語を優先しつつ、固有名詞や言い換えで補完して検索すること）")
    parts.append("")
    return "\n".join(parts)


def run_agent(
    question: str,
    seed_data: str,
    respond: Callable | None,
    ctx: AgentContext,
    *,
    max_steps: int = _DEFAULT_MAX_STEPS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """質問を1回のLLM呼び出しで処理する。

    マルチステップのツール実行ループは行わない。
    LLM は内部 reasoning でツール呼び出しを判断するか、直接回答を生成する。
    Orchestrator（Claude Code + pm-multi-agent）に任せられないタスクは
    DeepSeek-V4-Flash などのモデルが single-shot で処理する。
    """
    tool_desc = _build_tool_descriptions()
    system_prompt = _AGENT_SYSTEM_PROMPT.format(
        tool_descriptions=tool_desc,
        max_steps=max_steps,
    )

    progress = _make_progress_updater(None)
    progress("質問の意図を解釈中...")

    rewrite = _rewrite_query(question)
    rewrite_block = ""
    if rewrite:
        rewrite_block = _format_rewrite_for_seed(rewrite)
        logger.info(
            f"[rewrite] intent={rewrite.get('intent', '')[:80]!r}"
            f" entities={rewrite.get('entities')}"
            f" queries={rewrite.get('search_queries')}"
        )

    intent_header = ""
    if rewrite and rewrite.get("intent"):
        intent_header = f"> **ご質問の解釈**: {rewrite['intent']}\n\n"

    user_prompt = (
        f"## 調査依頼\n\n{question}\n\n"
        f"{rewrite_block}"
        f"{seed_data}\n\n"
        f"上記のプロジェクトデータとツールを活用して調査し、"
        f"`<final_answer>` タグで回答してください。"
        f"ツールを使う場合は `<tool_call>` 形式で呼び出し、"
        f"その結果を受け取ったら続けて回答を生成してください。"
    )

    progress("LLM に問い合わせ中...")
    start_time = time.monotonic()
    try:
        response = call_argus_llm(
            user_prompt,
            system=system_prompt,
            max_tokens=32768,
            timeout=int(timeout),
            think=True,
        )
    except Exception as e:
        logger.exception(f"[investigate] LLM呼び出しエラー: {e}")
        return f"調査中にエラーが発生しました: {e}"

    elapsed = time.monotonic() - start_time
    logger.info(f"[investigate] 完了 {len(response)} chars, {elapsed:.1f}s")

    final = parse_final_answer(response)
    if final:
        return intent_header + _append_sources_section(final, ctx)

    # tool_call が含まれていれば実行して再帰
    tool_calls = parse_tool_calls(response)
    if tool_calls:
        progress(f"ツール{len(tool_calls)}件を実行中...")
        result_parts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            fut_map = {pool.submit(execute_tool, tc["name"], tc["args"], ctx): tc for tc in tool_calls}
            for future in concurrent.futures.as_completed(fut_map, timeout=120):
                tc = fut_map[future]
                try:
                    result = future.result()
                    result_parts.append(f"[Tool Result: {tc['name']}]\n{result}")
                except Exception as e:
                    result_parts.append(f"[Tool Error: {tc['name']}]\n{type(e).__name__}: {e}")
        tool_results = "\n\n".join(result_parts)
        # ツール結果を加えて2回目呼び出し
        final_prompt = (
            f"{user_prompt}\n\n"
            f"上記のツールを実行した結果:\n\n{tool_results}\n\n"
            f"これらの結果を踏まえて `<final_answer>` で回答してください。"
        )
        try:
            response2 = call_argus_llm(
                final_prompt,
                system=system_prompt,
                max_tokens=32768,
                timeout=int(timeout),
                think=True,
            )
        except Exception as e:
            logger.exception(f"[investigate] 2回目LLMエラー: {e}")
            return intent_header + _append_sources_section(
                f"ツール実行結果:\n{tool_results}\n（回答生成中にエラー: {e}）", ctx)

        final = parse_final_answer(response2)
        if final:
            return intent_header + _append_sources_section(final, ctx)
        # 最終回答タグがなければ生テキストを返す
        clean = re.sub(r"<[^>]+>", "", response2).strip()
        return intent_header + _append_sources_section(clean if clean else response2, ctx)

    # tool_call も final_answer もない場合
    clean = re.sub(r"<[^>]+>", "", response).strip()
    logger.warning(f"[investigate] final_answer なし。生テキストを返却 ({len(clean)} chars)")
    return intent_header + _append_sources_section(clean if clean else response, ctx)


_SLACK_REF_CACHE: dict[str, list[str]] = {}


def _fetch_slack_references_for_box(box_file_id: str, limit: int = 2) -> list[str]:
    """box_file_id に紐づく Slack 共有パーマリンクを最大 limit 件返す（新しい順）。"""
    if not box_file_id:
        return []
    if box_file_id in _SLACK_REF_CACHE:
        return _SLACK_REF_CACHE[box_file_id]
    try:
        from db_utils import open_db
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent.parent / "data" / "box_docs.db"
        if not path.exists():
            _SLACK_REF_CACHE[box_file_id] = []
            return []
        conn = open_db(path, encrypt=True)
        rows = conn.execute(
            "SELECT slack_permalink, shared_at, shared_by FROM slack_references"
            " WHERE box_file_id=? AND slack_permalink IS NOT NULL"
            " ORDER BY shared_at DESC LIMIT ?",
            (box_file_id, limit),
        ).fetchall()
        conn.close()
    except Exception:
        _SLACK_REF_CACHE[box_file_id] = []
        return []
    out = []
    for r in rows:
        link = r["slack_permalink"]
        date = (r["shared_at"] or "")[:10]
        by = r["shared_by"] or ""
        label_bits = " ".join(b for b in [date, by] if b)
        out.append(f"<{link}|{label_bits or 'Slack'}>")
    _SLACK_REF_CACHE[box_file_id] = out
    return out


def _append_sources_section(answer: str, ctx: AgentContext) -> str:
    """ctx.cited_chunks にあるチャンクから「## 出典」セクションを生成して回答末尾に付与する。"""
    from argus.pm_qa_server import _format_source_label

    extra_sections: list[str] = []

    # 出典セクション
    if ctx.cited_chunks:
        seen: set[tuple] = set()
        items: list[tuple[int, str]] = []
        for i, chunk in enumerate(ctx.cited_chunks, 1):
            key = (chunk.get("source_type"), chunk.get("source_ref"), chunk.get("held_at"))
            if key in seen:
                continue
            seen.add(key)
            label = _format_source_label(chunk)
            ref = chunk.get("source_ref") or ""
            source_type = chunk.get("source_type", "")
            if source_type == "slack_raw" and ref:
                link = f"<{ref}|スレッドを開く>"
            elif source_type == "web" and ref:
                link = f"<{ref}|リンク>"
            elif source_type == "box_document" and ref:
                link = f"<{ref}|Boxで開く>"
                slack_links = _fetch_slack_references_for_box(chunk.get("record_id") or "")
                if slack_links:
                    link = link + " / Slack共有: " + ", ".join(slack_links)
            elif source_type == "minutes_content" and ref:
                held_at = chunk.get("held_at") or ""
                link = f"{held_at} {ref}".strip()
            else:
                link = ref
            items.append((i, f"- [{i}] {label}" + (f" — {link}" if link else "")))
        if items:
            extra_sections.append("\n".join(["", "## 出典"] + [s for _, s in items]))

    if not extra_sections:
        return answer
    return answer.rstrip() + "\n" + "\n\n".join(extra_sections)


# =========================================================================== #
#  Slack Handler
# =========================================================================== #

_ID_REF_RE = re.compile(
    r"(?P<full>"
    r"(?P<kind>a|d|AI|決定|ID)"      # 種別
    r"\s*[:： ]\s*"
    r"(?P<id>\d{1,6})"
    r")"
)


def _expand_id_references(text: str, conns: list) -> str:
    """出力中の `a:670` / `AI:670` / `決定:42` / `ID:670` 等を content[:60] で展開する。

    参照先が action_items なら `a:670 "xxxxx..."`、decisions なら `d:42 "xxxxx..."`。
    pm.db に見つからないIDは元のまま残す。
    """
    cache: dict[tuple[str, int], str | None] = {}

    def _lookup(table: str, item_id: int) -> str | None:
        key = (table, item_id)
        if key in cache:
            return cache[key]
        snippet: str | None = None
        for conn in conns:
            try:
                row = conn.execute(
                    f"SELECT content FROM {table} WHERE id = ? AND COALESCE(deleted,0)=0",
                    (item_id,),
                ).fetchone()
            except Exception:
                continue
            if row and row["content"]:
                s = row["content"].replace("\n", " ").strip()
                snippet = s[:60] + ("…" if len(s) > 60 else "")
                break
        cache[key] = snippet
        return snippet

    def _replace(m: re.Match) -> str:
        kind = m.group("kind")
        item_id = int(m.group("id"))
        # 種別から対象テーブルを推定。a/AI → action_items、d/決定 → decisions、
        # ID は両方試す（action_items 優先）
        if kind in ("a", "AI"):
            tables = ["action_items"]
            norm = f"a:{item_id}"
        elif kind in ("d", "決定"):
            tables = ["decisions"]
            norm = f"d:{item_id}"
        else:  # ID
            tables = ["action_items", "decisions"]
            norm = f"ID:{item_id}"

        for t in tables:
            snippet = _lookup(t, item_id)
            if snippet:
                prefix = "a" if t == "action_items" else "d"
                if kind == "ID":
                    return f"{prefix}:{item_id} “{snippet}”"
                return f"{norm} “{snippet}”"
        return m.group("full")

    return _ID_REF_RE.sub(_replace, text)


# =========================================================================== #
#  調査結果の出力先フラグパース
# =========================================================================== #

_OUTPUT_FLAG_RE = re.compile(r"\b(--to-box|--to-slack|--to-canvas)\b")


def _parse_output_flags(text: str) -> dict[str, bool]:
    """コマンドテキストから出力先フラグを抽出する。"""
    flags = {"box": False, "slack": False, "canvas": False}
    for m in _OUTPUT_FLAG_RE.finditer(text):
        flag = m.group(1)
        if flag == "--to-box":
            flags["box"] = True
        elif flag == "--to-slack":
            flags["slack"] = True
        elif flag == "--to-canvas":
            flags["canvas"] = True
    return flags


def _strip_output_flags(text: str) -> str:
    """出力先フラグを取り除いた純粋な質問文を返す。"""
    return _OUTPUT_FLAG_RE.sub("", text).strip()


def _output_to_box(result: str, today: str) -> str:
    """調査結果を一時ファイルに書き出して Box にアップロードする。"""
    import tempfile
    from argus.output_tools import box_upload_file

    result_md = f"# Argus 調査結果 ({today})\n\n{result}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f"argus_investigate_{today}_",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(result_md)
        tmp_path = f.name
    try:
        fname = f"argus_investigate_{today}.md"
        out = box_upload_file(tmp_path, filename=fname)
        return out
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _output_to_slack(result: str, today: str, channel_id: str) -> str:
    """調査結果をチャンネルに公開投稿する。"""
    from argus.output_tools import slack_post_message

    text = f"*Argus 調査結果* ({today})\n\n{result}"
    return slack_post_message(channel_id, text)


def _output_to_canvas(result: str, today: str) -> str:
    """調査結果を Canvas に投稿する（要: SLACK_USER_TOKEN）。"""
    from argus.output_tools import canvas_post_content
    from cli_utils import resolve_report_canvas_id

    canvas_id = resolve_report_canvas_id()
    if not canvas_id:
        return "Canvas ID が設定されていません（PM_REPORT_CANVAS_ID 環境変数または argus_config.yaml report.canvas_id）"
    content = f"# Argus 調査結果 ({today})\n\n{result}"
    return canvas_post_content(canvas_id, content)


def _run_investigate(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-investigate のバックグラウンド処理"""
    try:
        from pm_argus import _parse_command_args
        cmd_text = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")
        user_id = command.get("user_id", "")

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()

        index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels(channel_id)
        conns = [open_pm_db(p, no_encrypt=no_encrypt) for p in pm_db_paths]

        ctx = AgentContext(
            conns=conns,
            today=today,
            since=since_date,
            no_encrypt=no_encrypt,
            data_dir=_DATA_DIR,
            minutes_dir=_MINUTES_DIR,
            index_db=index_db,
            index_name=index_name,
            channels=channels,
        )

        seed_data = build_seed_data(ctx)
        result = run_agent(
            question=cmd_text,
            seed_data=seed_data,
            respond=respond,
            ctx=ctx,
        )

        # ID 参照 (a:670 / d:42 / AI:670 / 決定:42 / ID:670) を content[:60] で展開
        result = _expand_id_references(result, conns)

        for c in conns:
            c.close()

        # --- 出力ツールフラグのパース ---
        # コマンド末尾に --to-box / --to-slack / --to-canvas が付いていたら
        # 調査結果を各出力先にも送信する（ユーザー確認はコマンド入力時に済んでいる想定）
        _output_flags = _parse_output_flags(cmd_text)
        _cmd_cleaned = _strip_output_flags(cmd_text)

        _output_result_lines = []

        if _output_flags.get("box"):
            out = _output_to_box(result, today)
            _output_result_lines.append(f"> 📦 Box: {out}")
            logger.info("[investigate] output_to_box: %s", out)

        if _output_flags.get("slack"):
            out = _output_to_slack(result, today, channel_id)
            _output_result_lines.append(f"> 💬 Slack: {out}")
            logger.info("[investigate] output_to_slack: %s", out)

        if _output_flags.get("canvas"):
            out = _output_to_canvas(result, today)
            _output_result_lines.append(f"> 📋 Canvas: {out}")
            logger.info("[investigate] output_to_canvas: %s", out)

        # --- 元の Slack ephemeral 応答 ---
        # ephemeral は section block 3000 文字上限。超過時は分割して複数ブロックにする
        _SLACK_BLOCK_LIMIT = 2900
        header = f"*Argus 調査結果* ({today})\n\n"
        output_footer = ""
        if _output_result_lines:
            output_footer = "\n\n" + "\n".join(_output_result_lines)
            len_footer = len(output_footer)
        else:
            len_footer = 0
        body_raw = header + result + output_footer

        from utils.slack_post import _to_slack_mrkdwn, _split_mrkdwn_to_blocks

        # Section block の制限内に分割
        body = _to_slack_mrkdwn(body_raw)
        blocks = _split_mrkdwn_to_blocks(body)

        try:
            respond(
                blocks=blocks,
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
    parser.add_argument("--to-box", action="store_true", help="調査結果を Box にアップロード")
    parser.add_argument("--to-slack", type=str, default="", help="調査結果を指定チャンネルに Slack 投稿（channel_id）")
    parser.add_argument("--to-canvas", action="store_true", help="調査結果を Canvas に投稿")
    args = parser.parse_args()

    today = date.today().isoformat()
    since_date = (date.today() - timedelta(days=args.days)).isoformat()

    index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels()
    if args.db != str(_PM_DB):
        pm_db_paths = [Path(args.db)]
    conns = [open_pm_db(p, no_encrypt=args.no_encrypt) for p in pm_db_paths]

    ctx = AgentContext(
        conns=conns,
        today=today,
        since=since_date,
        no_encrypt=args.no_encrypt,
        data_dir=_DATA_DIR,
        minutes_dir=_MINUTES_DIR,
        index_db=index_db,
        index_name=index_name,
        channels=channels,
    )

    seed_data = build_seed_data(ctx)

    if args.dry_run:
        print("=== シードデータ ===")
        print(seed_data)
        print(f"\n=== 調査質問 ===\n{args.investigate}")
        print(f"\n=== ツール一覧 ===\n{_build_tool_descriptions()}")
        for c in conns:
            c.close()
        return

    result = run_agent(
        question=args.investigate,
        seed_data=seed_data,
        respond=None,
        ctx=ctx,
        max_steps=args.max_steps,
        timeout=args.timeout,
    )

    for c in conns:
        c.close()

    # --- 出力先オプション ---
    _output_summary = []
    if result:
        if args.to_box:
            out = _output_to_box(result, today)
            _output_summary.append(f"[Box] {out}")
            print(f"[output] {out}")
        if args.to_slack:
            out = _output_to_slack(result, today, args.to_slack)
            _output_summary.append(f"[Slack] {out}")
            print(f"[output] {out}")
        if args.to_canvas:
            out = _output_to_canvas(result, today)
            _output_summary.append(f"[Canvas] {out}")
            print(f"[output] {out}")

    print("\n=== Argus 調査結果 ===\n")
    print(result)


if __name__ == "__main__":
    main()
