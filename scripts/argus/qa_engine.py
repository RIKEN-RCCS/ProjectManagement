"""qa_engine.py — Argus QA エンジン

クエリ意図分類 (classify_intent) と pm.db への構造化クエリヘルパ
(_query_action_items / _query_decisions) を提供する。
mcp_tools.py / mcp_explorer.py / pm_qa_server.py から import される。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm

logger = logging.getLogger("pm_qa_server")

_OPENAI_BASE = os.environ.get("LOCAL_LLM_URL", "")

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


def _query_action_items(conn, *, assignee=None, status=None, milestone=None, keyword=None, limit=20, since=None) -> list[dict]:
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
    if since:
        clauses.append("extracted_at >= ?")
        params.append(since)
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


def _query_decisions(conn, *, keyword=None, limit=20, since=None) -> list[dict]:
    clauses = ["COALESCE(deleted,0)=0"]
    params: list = []
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if since:
        clauses.append("decided_at >= ?")
        params.append(since)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT id, content, decided_at, source, source_ref,
                   decided_by, rationale, source_context, related_ids
            FROM decisions WHERE {where}
            ORDER BY decided_at DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]



