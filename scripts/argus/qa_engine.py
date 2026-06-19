"""qa_engine.py — Argus QA エンジン

pm_qa_server.py から分離。クエリ意図分類・構造化クエリ・LLM 回答生成・
Slack フォーマットを担う。Bolt ハンドラ（pm_qa_server.py）とは独立して
unit test 可能なレイヤー。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import open_pm_db, fetch_milestone_progress, fetch_overdue_items, fetch_summary_stats
from argus.retrieval import retrieve_chunks_hyde, rerank_chunks, TOP_K_RERANK

logger = logging.getLogger("pm_qa_server")

_DATA_DIR = _REPO_ROOT / "data"
LLM_TIMEOUT = 120
RERANK_TIMEOUT = 60

_OPENAI_BASE = os.environ.get("LOCAL_LLM_URL", "")

SYSTEM_PROMPT_TEMPLATE = """あなたは富岳NEXTプロジェクトの情報検索アシスタントです。

【回答ルール】
- 以下の「取得した関連情報」のみを根拠として、日本語で回答してください
- 構造化データ検索結果がある場合、そこに含まれるID・担当者・期限・件数は正確に記載してください
- テキスト検索結果がある場合、出典の日付・会議名を可能な限り含めてください
- 情報が見つからない場合は「記録が見つかりません」とだけ回答してください
- 推測・創作はしないでください
- 回答全体は500字以内を目安にしてください（長い場合は要点を箇条書きに）

【プロジェクト文脈】
{project_context}
"""

_CLASSIFY_PROMPT = """\
あなたはクエリ分類器です。質問がどの種類のデータを必要としているか判定してください。

カテゴリ:
- "structured": 担当者・期限・ステータス・マイルストーン・件数など構造化データで回答可能
- "text": 議事録内容・議論の詳細・経緯などテキスト検索が必要
- "hybrid": 両方が必要

構造化データの例:
- 「西澤さんの担当タスクは？」→ structured, query_type=tasks, assignee=西澤
- 「期限超過しているアクションアイテムは？」→ structured, query_type=overdue
- 「M1マイルストーンの進捗は？」→ structured, query_type=milestones, milestone=M1
- 「決定事項の一覧」→ structured, query_type=decisions
- 「オープンなタスクは何件？」→ structured, query_type=stats

テキスト検索の例:
- 「GPU性能の評価方針について」→ text
- 「前回の会議で何が議論された？」→ text

両方の例:
- 「GPU性能に関する決定事項は？」→ hybrid, query_type=decisions, keyword=GPU
- 「西澤さんが議論していた内容は？」→ hybrid, query_type=tasks, assignee=西澤

JSONのみ出力:
{"intent": "structured"|"text"|"hybrid", "entities": {"assignee": null, "milestone": null, "status": null, "keyword": null, "query_type": null}}

質問: """

def classify_intent(question: str) -> dict:
    """質問の意図を分類し、エンティティを抽出する。失敗時はFTSフォールバック。"""
    if not _OPENAI_BASE:
        return {"intent": "text", "entities": {}}

    prompt = _CLASSIFY_PROMPT + question
    try:
        result = call_argus_llm(
            prompt=prompt,
            max_tokens=80,
            timeout=15,
            temperature=0.1,
        )
        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r"^```\w*\n?", "", result)
            result = re.sub(r"\n?```$", "", result)
        import json
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "intent" in parsed:
            parsed.setdefault("entities", {})
            return parsed
    except Exception as e:
        logger.warning(f"Intent分類失敗: {e} → FTSフォールバック")

    return {"intent": "text", "entities": {}}


def _query_action_items(conn, *, assignee=None, status=None, milestone=None, keyword=None, limit=20) -> list[dict]:
    clauses = ["COALESCE(deleted,0)=0"]
    params: list = []
    if assignee:
        clauses.append("assignee LIKE ?")
        params.append(f"%{assignee}%")
    if status:
        clauses.append("status = ?")
        params.append(status)
    if milestone:
        clauses.append("milestone_id = ?")
        params.append(milestone)
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT id, content, assignee, due_date, status, milestone_id, source_ref,
                   requested_by, rationale, source_context, related_ids
            FROM action_items WHERE {where}
            ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, due_date ASC NULLS LAST
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _query_decisions(conn, *, keyword=None, limit=20) -> list[dict]:
    clauses = ["COALESCE(deleted,0)=0"]
    params: list = []
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT id, content, decided_at, source, source_ref,
                   decided_by, rationale, source_context, related_ids
            FROM decisions WHERE {where}
            ORDER BY decided_at DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def run_structured_query(entities: dict, pm_db_paths: list[Path] | None = None) -> str:
    """entities に基づき pm.db を構造化クエリし、整形済みテキストを返す。"""
    query_type = entities.get("query_type") or "tasks"
    assignee = entities.get("assignee")
    milestone = entities.get("milestone")
    status = entities.get("status")
    keyword = entities.get("keyword")

    if not pm_db_paths:
        pm_db_paths = _pm_db_map.get(_default_index, [_REPO_ROOT / "data" / "pm.db"])

    conns: list = []
    for p in pm_db_paths:
        try:
            conns.append(open_pm_db(p, no_encrypt=False))
        except Exception as e:
            logger.warning(f"pm.db接続エラー ({p}): {e}")
    if not conns:
        return ""

    try:
        from datetime import date
        today = date.today().isoformat()

        if query_type == "tasks":
            rows: list[dict] = []
            for c in conns:
                rows.extend(_query_action_items(c, assignee=assignee, status=status, milestone=milestone, keyword=keyword))
            if not rows:
                return ""
            lines = [f"【アクションアイテム検索結果: {len(rows)}件】"]
            for r in rows:
                a = r.get("assignee") or "未定"
                d = r.get("due_date") or "期限なし"
                ms = r.get("milestone_id") or "-"
                lines.append(f"- [ID:{r['id']}] {r['content'][:100]} (担当:{a}, 期限:{d}, 状態:{r['status']}, MS:{ms})")
            return "\n".join(lines)

        elif query_type == "decisions":
            rows = []
            for c in conns:
                rows.extend(_query_decisions(c, keyword=keyword))
            if not rows:
                return ""
            lines = [f"【決定事項検索結果: {len(rows)}件】"]
            for r in rows:
                lines.append(f"- [D:{r['id']}][{r.get('decided_at') or '日付不明'}] {r['content'][:120]}")
            return "\n".join(lines)

        elif query_type == "milestones":
            milestones: list = []
            for c in conns:
                milestones.extend(fetch_milestone_progress(c))
            if milestone:
                milestones = [m for m in milestones if m.get("milestone_id") == milestone]
            if not milestones:
                return ""
            lines = ["【マイルストーン進捗】"]
            for m in milestones:
                total = m.get("total", 0)
                closed = m.get("closed", 0)
                pct = f"{closed}/{total}" if total > 0 else "0/0"
                lines.append(f"- {m['milestone_id']}: {m['name']} (期限:{m.get('due_date') or '-'}, 完了:{pct})")
            return "\n".join(lines)

        elif query_type == "overdue":
            items: list = []
            for c in conns:
                items.extend(fetch_overdue_items(c, today, since=None))
            if assignee:
                items = [i for i in items if assignee in (i.get("assignee") or "")]
            if not items:
                return ""
            lines = [f"【期限超過アイテム: {len(items)}件】"]
            for r in items[:20]:
                a = r.get("assignee") or "未定"
                lines.append(f"- [ID:{r['id']}] {r['content'][:80]} (担当:{a}, 期限:{r['due_date']})")
            return "\n".join(lines)

        elif query_type == "stats":
            from pm_argus import merge_pm_stats, fetch_pm_stats
            stats_list = [fetch_pm_stats(c, today) for c in conns]
            merged = merge_pm_stats(stats_list)
            s = merged["stats"]
            lines = ["【統計情報】"]
            lines.append(f"- オープンAI: {s.get('total_open', 0)}件")
            lines.append(f"- 完了AI: {s.get('total_closed', 0)}件")
            lines.append(f"- 期限超過: {s.get('overdue_count', 0)}件")
            return "\n".join(lines)

    except Exception as e:
        logger.warning(f"構造化クエリエラー: {e}")
        return ""
    finally:
        for c in conns:
            c.close()

    return ""


def generate_answer(question: str, chunks: list[dict], *, structured_context: str = "") -> str:
    if not _OPENAI_BASE:
        return ":warning: LOCAL_LLM_URL が設定されていません。`bash scripts/pm_daemon.sh start qa` 経由で起動してください。"

    parts = []
    if structured_context:
        parts.append(f"## 構造化データ検索結果\n\n{structured_context}")
    if chunks:
        parts.append(f"## テキスト検索結果\n\n{format_context(chunks)}")
    if not parts:
        return "記録が見つかりません。"
    context_str = "\n\n---\n\n".join(parts)
    system = SYSTEM_PROMPT_TEMPLATE.format(project_context=_PROJECT_CONTEXT)
    user_prompt = f"## 取得した関連情報\n\n{context_str}\n---\n\n## 質問\n\n{question}"

    try:
        answer = call_argus_llm(
            prompt=user_prompt,
            max_tokens=MAX_TOKENS,
            system=system,
            timeout=LLM_TIMEOUT,
        )
        return answer.strip()
    except Exception as e:
        logger.exception("LLM呼び出しエラー")
        return f":warning: LLMエラー: {e}"


def format_slack_response(question: str, chunks: list[dict], answer: str,
                          index_name: str, *, search_mode: str = "テキスト検索") -> str:
    header = f"*Q: {question}*\n\n"
    body = answer
    body += f"\n\n_（検索対象: {index_name} / {search_mode}）_"
    return header + body


# --- Slack Bolt ハンドラ ---

def _run_qa(question: str, respond, index_name: str, index_db: Path,
            pm_db_paths: list[Path] | None = None) -> None:
    try:
        logger.info(f"QA開始: [{index_name}] {question[:60]}")

        # Step 1: Intent分類
        intent_result = classify_intent(question)
        intent = intent_result.get("intent", "text")
        entities = intent_result.get("entities", {})
        logger.info(f"  Intent: {intent}, entities: {entities}")

        structured_context = ""
        chunks: list[dict] = []
        search_mode = "テキスト検索"

        # Step 2: Intent に基づく検索
        if intent in ("structured", "hybrid"):
            structured_context = run_structured_query(entities, pm_db_paths=pm_db_paths)
            if structured_context:
                logger.info(f"  構造化クエリ: {len(structured_context)} 文字")
            else:
                logger.info("  構造化クエリ: 結果なし")

        if intent in ("text", "hybrid") or (intent == "structured" and not structured_context):
            chunks = retrieve_chunks_hyde(question, index_db, index_name=index_name)
            logger.info(f"  {len(chunks)} チャンク取得（HyDE拡張後）")
            chunks = rerank_chunks(question, chunks)
            logger.info(f"  re-rank後: {len(chunks)} チャンク")

        # 検索モード判定
        if structured_context and chunks:
            search_mode = "ハイブリッド検索"
        elif structured_context:
            search_mode = "構造化検索"

        # Step 3: 回答生成
        answer = generate_answer(question, chunks, structured_context=structured_context)
        response_text = format_slack_response(
            question, chunks, answer, index_name, search_mode=search_mode,
        )
        respond(text=response_text, response_type="ephemeral", replace_original=True)
        logger.info(f"QA完了 ({search_mode})")
    except Exception as e:
        logger.exception("QA処理エラー")
        respond(
            text=f":warning: エラーが発生しました: {e}",
            response_type="ephemeral",
            replace_original=True,
        )


