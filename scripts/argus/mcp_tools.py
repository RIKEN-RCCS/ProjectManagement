"""mcp_tools.py — pm-multi-agent 全ツールの実装本体

pm_mcp_server.py（FastMCP 経由）と pm_argus_agent.py（/argus-investigate）の
両方から import して使われる。呼び出し形式は MCP ツールと同じシグネチャ。
出力ツール（box_upload / slack_post / canvas_post）は output_tools.py にある。
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logger = logging.getLogger("pm_mcp_tools")

_DATA_DIR = _REPO_ROOT / "data"
_QA_INDEX = _DATA_DIR / "qa_index.db"


# =========================================================================== #
#  DB 接続ヘルパ
# =========================================================================== #

def _get_pm_conn():
    from db_utils import open_pm_db
    return open_pm_db(_DATA_DIR / "pm.db")


def _has_pm_db() -> bool:
    return (_DATA_DIR / "pm.db").exists()


# =========================================================================== #
#  構造化データ検索
# =========================================================================== #

def search_decisions(keyword: str, limit: int = 50, since: str | None = None) -> str:
    """pm.db の決定事項をキーワード検索する"""
    if not _has_pm_db():
        return "pm.db が見つかりません。"
    from argus.qa_engine import _query_decisions
    conn = _get_pm_conn()
    try:
        rows = _query_decisions(conn, keyword=keyword, limit=limit, since=since)
        if not rows:
            return f"該当する決定事項は見つかりませんでした（キーワード: {keyword}）。"
        lines = [f"## 決定事項検索結果（{len(rows)}件）"]
        for r in rows:
            header = f"- **D:{r['id']}** [{r.get('decided_at', '?')}] {r['content'][:200]}"
            lines.append(header)
            if r.get("decided_by"):
                lines.append(f"  - 判断者: {r['decided_by']}")
            if r.get("rationale"):
                lines.append(f"  - 根拠: {r['rationale'][:150]}")
        return "\n".join(lines)
    finally:
        conn.close()


def search_action_items(
    keyword: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 50,
    since: str | None = None,
) -> str:
    """pm.db のアクションアイテムを検索する（担当者・ステータス・キーワードで絞り込み）"""
    if not _has_pm_db():
        return "pm.db が見つかりません。"
    from argus.qa_engine import _query_action_items
    conn = _get_pm_conn()
    try:
        rows = _query_action_items(conn, assignee=assignee, status=status,
                                    keyword=keyword, limit=limit, since=since)
        if not rows:
            return "該当するアクションアイテムは見つかりませんでした。"
        lines = [f"## アクションアイテム検索結果（{len(rows)}件）"]
        for r in rows:
            status_mark = "✅" if r["status"] == "closed" else "⬜"
            due = f" 期限:{r['due_date']}" if r.get("due_date") else ""
            assign = f" 担当:{r['assignee']}" if r.get("assignee") else ""
            ms = f" MS:{r['milestone_id']}" if r.get("milestone_id") else ""
            lines.append(
                f"- {status_mark} **ID:{r['id']}**{due}{assign}{ms}"
                f"\n  {r['content'][:120]}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


def get_milestone_progress() -> str:
    """マイルストーンごとの進捗状況（完了率・期限）を取得する"""
    if not _has_pm_db():
        return "pm.db が見つかりません。"
    from db_utils import fetch_milestone_progress
    from format_utils import format_milestone_table
    conn = _get_pm_conn()
    try:
        rows = fetch_milestone_progress(conn)
        if not rows:
            return "マイルストーンが登録されていません。"
        today = date.today().isoformat()
        return format_milestone_table(rows, today)
    finally:
        conn.close()


def get_overdue_items(assignee: str | None = None, limit: int = 50, since: str | None = None) -> str:
    """期限超過しているアクションアイテムを取得する"""
    if not _has_pm_db():
        return "pm.db が見つかりません。"
    from db_utils import fetch_overdue_items
    from format_utils import format_overdue_list
    conn = _get_pm_conn()
    try:
        today = date.today().isoformat()
        rows = fetch_overdue_items(conn, today, since or "2000-01-01")
        if assignee:
            rows = [r for r in rows if assignee in (r.get("assignee") or "")]
        rows = rows[:limit]
        if not rows:
            return "期限超過アイテムはありません。"
        return format_overdue_list(rows, limit=limit)
    finally:
        conn.close()


def get_assignee_workload() -> str:
    """担当者別の負荷（オープン件数・期限超過件数）を取得する"""
    if not _has_pm_db():
        return "pm.db が見つかりません。"
    from db_utils import fetch_assignee_workload
    from format_utils import format_assignee_table
    conn = _get_pm_conn()
    try:
        today = date.today().isoformat()
        rows = fetch_assignee_workload(conn, today)
        if not rows:
            return "担当者データはありません。"
        return format_assignee_table(rows)
    finally:
        conn.close()


# =========================================================================== #
#  全文検索
# =========================================================================== #

def search_text(query: str, index_name: str = "pm", since: str | None = None) -> str:
    """議事録・Slackメッセージを全文検索する。FTS5 + LLM re-ranking を使用"""
    if not _QA_INDEX.exists():
        return "qa_index.db が見つかりません。pm_embed.py でインデックスを構築してください。"
    from argus.pm_qa_server import _format_source_label
    from argus.retrieval import rerank_chunks, retrieve_chunks_hyde
    merged = retrieve_chunks_hyde(query, _QA_INDEX, index_name=index_name, max_merged=50, since_date=since)
    if not merged:
        return f"「{query}」に一致する情報は見つかりませんでした。"
    reranked = rerank_chunks(query, merged, format_source_label=_format_source_label)
    lines = [f"## 全文検索結果（{len(reranked)}件）"]
    for i, c in enumerate(reranked, 1):
        label = _format_source_label(c)
        lines.append(f"[{i}] 出典: {label}")
        lines.append(f"    {c['content'][:400].strip()}")
        lines.append("")
    return "\n".join(lines)


def search_text_hybrid(query: str, index_name: str = "pm", since: str | None = None) -> str:
    """FTS5 + ベクトル類似度のハイブリッド検索"""
    if not _QA_INDEX.exists():
        return "qa_index.db が見つかりません。"
    from argus.pm_qa_server import _format_source_label
    from argus.retrieval import retrieve_chunks_hybrid
    chunks = retrieve_chunks_hybrid(query, _QA_INDEX, k=50, index_name=index_name, since_date=since)
    if not chunks:
        return f"「{query}」に一致する情報は見つかりませんでした。"
    lines = [f"## ハイブリッド検索結果（{len(chunks)}件）"]
    for i, c in enumerate(chunks, 1):
        label = _format_source_label(c)
        lines.append(f"[{i}] 出典: {label}（スコア: {c.get('rrf_score', 0):.2f}）")
        lines.append(f"    {c['content'][:400].strip()}")
        lines.append("")
    return "\n".join(lines)


# =========================================================================== #
#  Explorer Agent
# =========================================================================== #

def search_entity(query: str, perspective: str, data_type: str = "pm_data", since: str | None = None) -> str:
    """特定の視点（conservative/aggressive/objective/future_oriented）と
    データ種別（pm_data/minutes/slack/box_docs）で分析する。"""
    from argus.mcp_explorer import run_explorer
    return run_explorer(query, data_type, perspective, _QA_INDEX, since=since)


def synthesize_answers(question: str, answers: list[str]) -> str:
    """複数の Explorer Agent の回答を統合して総合回答を生成する。"""
    from utils.llm import call_argus_llm
    sections = "\n\n---\n\n".join(
        f"## Explorer {i+1}\n\n{a}" for i, a in enumerate(answers)
    )
    prompt = (
        f"あなたは富岳NEXTプロジェクトの分析結果を統合するエキスパートです。\n"
        f"以下の複数の視点からの分析結果を統合し、ユーザーの質問に対する\n"
        f"総合的な回答を生成してください。\n\n"
        f"## ユーザーの質問\n{question}\n\n"
        f"## 各視点の分析結果\n{sections}\n\n"
        f"## 出力形式\n"
        f"冒頭に2-3行の結論、その後に関連する分析結果を統合した詳細。"
        f"出典は必ず引用すること。\n"
        f"各 Explorer の出力を単に連結するのではなく、関連する洞察をまとめて\n"
        f"1つのストーリーとして提示すること。"
    )
    try:
        return call_argus_llm(prompt, max_tokens=4096)
    except Exception as e:
        return f"統合中にエラーが発生しました: {e}\n\n元の分析結果:\n{sections}"


# =========================================================================== #
#  ヘルスチェック
# =========================================================================== #

def check_health() -> str:
    """MCP Server と各 DB の状態を確認する"""
    lines = ["## MCP Server ヘルスチェック", "- MCP Server: 稼働中（ポート 8002）"]
    for name, path in [
        ("pm.db", _DATA_DIR / "pm.db"),
        ("qa_index.db", _QA_INDEX),
        ("slack.db", _DATA_DIR / "slack.db"),
    ]:
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        status = "✅" if exists else "❌"
        lines.append(f"- {status} {name}: {size/1024:.0f}KB")
    return "\n".join(lines)


