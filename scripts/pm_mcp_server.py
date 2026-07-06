#!/usr/bin/env python3
"""
pm_mcp_server.py — Multi-Agent 調査システム MCP Server

Orchestrator (Claude Code) から呼ばれる MCP ツール群を提供する。
ツールの実装本体は argus.mcp_tools（検索・分析）と argus.output_tools（出力）にある。

Usage:
    source ~/.secrets/slack_tokens.sh
    source ~/.secrets/rivault_tokens.sh
    PYTHONPATH=scripts ~/.venv_aarch64/bin/python3 scripts/pm_mcp_server.py

環境変数:
    LOCAL_LLM_URL    — LLM エンドポイント（定義は ~/.secrets/localLLM.sh。未設定時はエラー）
    LOCAL_LLM_TOKEN  — API トークン (デフォルト: dummy)
    ARGUS_CONFIG     — argus_config.yaml のパス
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastmcp import FastMCP

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pm_mcp")

mcp = FastMCP("pm-multi-agent")

# 後方互換のための公開名
PM_MCP_TOOLS = None  # 実際のツール一覧は argus.mcp_tools.MCP_TOOLS を参照

# 全ツールを argus.mcp_tools と argus.output_tools に委譲


# =========================================================================== #
#  構造化データ検索（委譲）
# =========================================================================== #

@mcp.tool()
def search_decisions(keyword: str, limit: int = 20) -> str:
    """pm.db の決定事項をキーワード検索する"""
    from argus.mcp_tools import search_decisions as _impl
    return _impl(keyword=keyword, limit=limit)


@mcp.tool()
def search_action_items(
    keyword: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> str:
    """pm.db のアクションアイテムを検索する（担当者・ステータス・キーワードで絞り込み）"""
    from argus.mcp_tools import search_action_items as _impl
    return _impl(keyword=keyword, assignee=assignee, status=status, limit=limit)


@mcp.tool()
def get_milestone_progress() -> str:
    """マイルストーンごとの進捗状況（完了率・期限）を取得する"""
    from argus.mcp_tools import get_milestone_progress as _impl
    return _impl()


@mcp.tool()
def get_overdue_items(assignee: str | None = None, limit: int = 20) -> str:
    """期限超過しているアクションアイテムを取得する"""
    from argus.mcp_tools import get_overdue_items as _impl
    return _impl(assignee=assignee, limit=limit)


@mcp.tool()
def get_assignee_workload() -> str:
    """担当者別の負荷（オープン件数・期限超過件数）を取得する"""
    from argus.mcp_tools import get_assignee_workload as _impl
    return _impl()


# =========================================================================== #
#  全文検索（委譲）
# =========================================================================== #

@mcp.tool()
def search_text(query: str, index_name: str = "pm") -> str:
    """議事録・Slackメッセージを全文検索する。FTS5 + LLM re-ranking を使用"""
    from argus.mcp_tools import search_text as _impl
    return _impl(query=query, index_name=index_name)


@mcp.tool()
def search_text_hybrid(query: str, index_name: str = "pm") -> str:
    """FTS5 + ベクトル類似度のハイブリッド検索"""
    from argus.mcp_tools import search_text_hybrid as _impl
    return _impl(query=query, index_name=index_name)


# =========================================================================== #
#  Explorer Agent（委譲）
# =========================================================================== #

@mcp.tool()
def search_entity(query: str, perspective: str, data_type: str = "pm_data") -> str:
    """特定の視点（conservative/aggressive/objective/future_oriented）と
    データ種別（pm_data/minutes/slack/box_docs）で分析する。"""
    from argus.mcp_tools import search_entity as _impl
    return _impl(query=query, perspective=perspective, data_type=data_type)


@mcp.tool()
def synthesize_answers(question: str, answers: list[str]) -> str:
    """複数の Explorer Agent の回答を統合して総合回答を生成する。"""
    from argus.mcp_tools import synthesize_answers as _impl
    return _impl(question=question, answers=answers)


# =========================================================================== #
#  出力ツール（委譲）
# =========================================================================== #

@mcp.tool()
def box_upload_file(
    local_path: str,
    filename: str | None = None,
) -> str:
    """ローカルファイルを Box にアップロード（既存ファイルはバージョン更新）し、
    共有リンクを返す。常にユーザー確認後に呼び出すこと。"""
    from argus.output_tools import box_upload_file as _impl
    return _impl(local_path, filename=filename)


@mcp.tool()
def slack_post_message(
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> str:
    """Slack チャンネルにメッセージを投稿する。常にユーザー確認後に呼び出すこと。"""
    from argus.output_tools import slack_post_message as _impl
    return _impl(channel, text, thread_ts=thread_ts)


@mcp.tool()
def canvas_post_content(
    canvas_id: str,
    content: str,
) -> str:
    """Slack Canvas の内容を置き換える。常にユーザー確認後に呼び出すこと。"""
    from argus.output_tools import canvas_post_content as _impl
    return _impl(canvas_id, content)


# =========================================================================== #
#  ヘルスチェック（委譲）
# =========================================================================== #

@mcp.tool()
def check_health() -> str:
    """MCP Server と各 DB の状態を確認する"""
    from argus.mcp_tools import check_health as _impl
    return _impl()


# =========================================================================== #
#  起動
# =========================================================================== #

if __name__ == "__main__":
    print("[INFO] pm-multi-agent MCP Server starting on port 8002...", file=sys.stderr)
    mcp.run()
