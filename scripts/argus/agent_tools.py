"""agent_tools.py — Argus Agent ツール定義・実装レジストリ

pm_argus_agent.py から呼ばれるツール群。
実装本体は argus.mcp_tools（検索・分析）と argus.output_tools（出力）に委譲する。
pm_mcp_server.py（FastMCP）と同じ関数群を提供するため、挙動は統一されている。

このファイルが管理するもの:
  - AgentContext（investigate agent の状態）
  - ToolDef（ツール定義データクラス）
  - TOOLS / _TOOL_MAP（ツールレジストリ）
  - _tool_search_text, _tool_get_slack_messages, _tool_search_mentions
    （AgentContext の conns/channels に依存するためここに残す）
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import (
    fetch_milestone_progress,
    fetch_assignee_workload,
    fetch_overdue_items,
    fetch_weekly_trends,
    fetch_unacknowledged_decisions,
)
from format_utils import (
    format_milestone_table,
    format_overdue_list,
    format_assignee_table,
    format_weekly_trends as format_trends_table,
    format_decisions_list,
)

logger = logging.getLogger("pm_argus_agent")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_ARGUS_CONFIG = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_LEGACY = _DATA_DIR / "qa_config.yaml"

# =========================================================================== #
#  AgentContext
# =========================================================================== #

@dataclass
class AgentContext:
    conns: list[Any]
    today: str
    since: str
    no_encrypt: bool = False
    data_dir: Path = field(default_factory=lambda: _DATA_DIR)
    minutes_dir: Path = field(default_factory=lambda: _MINUTES_DIR)
    index_db: Path = field(default_factory=lambda: _DATA_DIR / "qa_index.db")
    index_name: str = "pm"
    channels: list[str] = field(default_factory=list)
    cited_chunks: list[dict] = field(default_factory=list)
    # 出力ツール用（オプショナル）
    slack_bot_token: str = ""
    box_folder_id: str = ""


# =========================================================================== #
#  ToolDef & Registry
# =========================================================================== #

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, str]
    fn: Callable[[dict, AgentContext], str]


def _query_all(ctx: AgentContext, fn, *args, **kwargs) -> list:
    """全 conns に対して fn(conn, ...) を実行し結果リストを結合する。"""
    out: list = []
    for conn in ctx.conns:
        out.extend(fn(conn, *args, **kwargs))
    return out


# =========================================================================== #
#  mcp_tools への委譲ヘルパ
# =========================================================================== #

def _call_mcp(fn_name: str) -> Callable[[dict, AgentContext], str]:
    """mcp_tools の関数を args 展開して呼ぶラッパーを生成する。"""
    from argus import mcp_tools
    fn = getattr(mcp_tools, fn_name, None)
    if fn is None:
        raise RuntimeError(f"mcp_tools に {fn_name} が見つかりません")

    def wrapper(args: dict, ctx: AgentContext) -> str:
        # ctx.since を注入して期間フィルタを効かせる
        # (today は mcp_tools 関数の引数に存在しないため注入しない)
        args = dict(args)
        args.setdefault("since", ctx.since)
        return fn(**args)
    return wrapper


# =========================================================================== #
#  AgentContext 依存ツール（mcp_tools に移せないもの）
# =========================================================================== #

def _tool_search_text(args: dict, ctx: AgentContext) -> str:
    from argus.mcp_tools import search_text as _mcp_search
    query = args.get("query", "")
    if not query:
        return "（検索クエリが空です）"
    # ctx.index_db を使って cited_chunks を蓄積するためラップ
    result = _mcp_search(query, index_name=ctx.index_name, since=ctx.since)
    # search_text の内部で _format_source_label を使うが、
    # cited_chunks の蓄積は MCP サーバー側で行うためここではスキップ
    return result


def _tool_get_slack_messages(args: dict, ctx: AgentContext) -> str:
    from pm_argus import fetch_raw_messages
    channel_id = args.get("channel_id", "")
    ch_names = _load_channel_names()

    def _fmt_channels(ids: list[str]) -> str:
        return ", ".join(
            f"{cid}({ch_names[cid]})" if cid in ch_names else cid
            for cid in ids
        )

    if channel_id and not channel_id.startswith("C"):
        norm = channel_id.lstrip("#").strip()
        for cid, name in ch_names.items():
            if name == norm:
                channel_id = cid
                break

    if not channel_id:
        return f"channel_id が必要です。利用可能なチャンネル: {_fmt_channels(ctx.channels)}"
    if ctx.channels and channel_id not in ctx.channels:
        ch_label = f"{channel_id}({ch_names[channel_id]})" if channel_id in ch_names else channel_id
        return (
            f"チャンネル {ch_label} は現在のインデックスの対象外です。"
            f" 利用可能なチャンネル: {_fmt_channels(ctx.channels)}"
        )
    since = args.get("since", ctx.since)
    max_chars = int(args.get("max_chars", 10000))
    return fetch_raw_messages(
        channel_id, since, data_dir=ctx.data_dir, no_encrypt=ctx.no_encrypt,
        max_chars=max_chars,
    )


_CHANNEL_NAME_CACHE: dict[str, str] | None = None


def _load_channel_names() -> dict[str, str]:
    global _CHANNEL_NAME_CACHE
    if _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE
    try:
        import pm_qa_server
        if pm_qa_server._channel_names:
            _CHANNEL_NAME_CACHE = dict(pm_qa_server._channel_names)
            return _CHANNEL_NAME_CACHE
    except Exception:
        pass
    names: dict[str, str] = {}
    cfg_path = _ARGUS_CONFIG if _ARGUS_CONFIG.exists() else _QA_CONFIG_LEGACY
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            cn = cfg.get("channel_names") or {}
            if isinstance(cn, dict):
                names = {str(k): str(v) for k, v in cn.items()}
        except Exception as e:
            logger.warning("channel name load error: %s", e)
    _CHANNEL_NAME_CACHE = names
    return names


def _tool_search_mentions(args: dict, ctx: AgentContext) -> str:
    from db_utils import open_db

    user_id = (args.get("user_id") or "").strip()
    name = (args.get("name") or "").strip()
    since = args.get("since") or ctx.since
    until = args.get("until") or ctx.today
    limit = int(args.get("limit", 50))

    if not user_id and not name:
        return "user_id または name のどちらかを指定してください。"

    name_candidates: list[str] = []
    if name:
        for tok in re.split(r"[、,，]", name):
            tok = tok.strip()
            if tok:
                name_candidates.append(tok)
        if name_candidates:
            name = name_candidates[0]

    resolved_uid = None
    resolved_display = None
    if name and not user_id:
        try:
            from argus.patrol.state import PatrolState
            from argus.patrol.users import UserResolver
            from slack_sdk import WebClient
            state = PatrolState(ctx.data_dir / "patrol_state.db")
            bot_token = os.environ.get("SLACK_BOT_TOKEN")
            slack = WebClient(token=bot_token) if bot_token else None
            resolver = UserResolver(state, slack, ctx.data_dir)
            for cand in name_candidates or [name]:
                resolved_uid = resolver.resolve(cand)
                if resolved_uid:
                    name = cand
                    break
            if resolved_uid:
                user_id = resolved_uid
                from db_utils import open_db
                slack_db = ctx.data_dir / "slack.db"
                if slack_db.exists():
                    try:
                        c = open_db(slack_db, encrypt=not ctx.no_encrypt)
                        row = c.execute(
                            "SELECT user_name FROM messages WHERE user_id=?"
                            " AND user_name IS NOT NULL LIMIT 1",
                            (resolved_uid,),
                        ).fetchone()
                        c.close()
                        if row and row["user_name"]:
                            resolved_display = row["user_name"]
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[search_mentions] name→user_id 解決失敗: {e}")

    channels = ctx.channels or []
    if not channels:
        return "検索対象チャンネルがありません。"

    where_parts = ["date(timestamp) >= ?", "date(timestamp) <= ?",
                   "text IS NOT NULL", "text != ''"]
    params: list = [since, until]
    like_parts = []
    if user_id:
        like_parts.append("text LIKE ?")
        params.append(f"%{user_id}%")
    elif name:
        like_parts.append("text LIKE ?")
        params.append(f"%{name}%")
    where_parts.append("(" + " OR ".join(like_parts) + ")")
    if user_id:
        where_parts.append("(user_id IS NULL OR user_id != ?)")
        params.append(user_id)
    where_clause = " AND ".join(where_parts)

    all_rows = []
    db_path = ctx.data_dir / "slack.db"
    if not db_path.exists():
        return f"（data/slack.db が見つかりません）"

    ph = ",".join("?" * len(channels))
    sql = (
        f"SELECT timestamp, user_name, text, permalink, channel_id, 0 AS is_reply "
        f"FROM messages WHERE channel_id IN ({ph}) AND {where_clause} "
        f"UNION ALL "
        f"SELECT timestamp, user_name, text, permalink, channel_id, 1 AS is_reply "
        f"FROM replies WHERE channel_id IN ({ph}) AND {where_clause} "
        f"ORDER BY timestamp DESC LIMIT ?"
    )
    try:
        conn = open_db(db_path, encrypt=not ctx.no_encrypt)
        rows = conn.execute(
            sql,
            channels + params + channels + params + [limit],
        ).fetchall()
        for r in rows:
            all_rows.append((r["timestamp"], r["user_name"], r["text"],
                             r["permalink"], r["is_reply"], r["channel_id"]))
        conn.close()
    except Exception as e:
        all_rows.append((None, None, f"（slack.db クエリエラー: {e}）", None, 0, ""))

    if not all_rows:
        q = f"user_id={user_id}" if user_id else f"name={name}"
        return f"該当メッセージなし ({q}, {since}〜{until})"

    all_rows.sort(key=lambda x: x[0] or "", reverse=True)
    all_rows = all_rows[:limit]

    ch_names = _load_channel_names()
    header_bits = [f"{len(all_rows)} 件", f"{since}〜{until}"]
    if resolved_uid:
        header_bits.append(f"name=\"{name}\" → user_id={resolved_uid}"
                           + (f" (display_name={resolved_display})" if resolved_display else ""))
    lines = [f"# 検索結果: " + "、".join(header_bits)]
    lines.append("")
    lines.append(
        "（注: 以下は生メッセージ。要約や省略せず、そのままユーザーに提示すること。）"
    )
    for ts, user, text, permalink, is_reply, ch_id in all_rows:
        ts_short = (ts or "")[:16]
        ch_name = ch_names.get(ch_id, ch_id)
        tag = "返信" if is_reply else "投稿"
        body = text or ""
        link = f"\n  {permalink}" if permalink else ""
        lines.append("")
        lines.append(f"---")
        lines.append(f"[{ts_short}] #{ch_name} ({tag}) {user}:")
        lines.append(body)
        if link:
            lines.append(link.strip())
    return "\n".join(lines)


# =========================================================================== #
#  出力ツール（output_tools への委譲）
# =========================================================================== #

def _tool_box_upload(args: dict, ctx: AgentContext) -> str:
    from argus.output_tools import box_upload_file
    return box_upload_file(
        local_path=args.get("local_path", ""),
        filename=args.get("filename"),
        folder_id=ctx.box_folder_id or None,
    )


def _tool_slack_post(args: dict, ctx: AgentContext) -> str:
    from argus.output_tools import slack_post_message
    return slack_post_message(
        channel=args.get("channel", ""),
        text=args.get("text", ""),
        thread_ts=args.get("thread_ts"),
    )


def _tool_canvas_post(args: dict, ctx: AgentContext) -> str:
    from argus.output_tools import canvas_post_content
    return canvas_post_content(
        canvas_id=args.get("canvas_id", ""),
        content=args.get("content", ""),
    )


# =========================================================================== #
#  TOOLS レジストリ（mcp_tools の全ツール + AgentContext 依存ツール）
# =========================================================================== #

TOOLS: list[ToolDef] = [
    # --- mcp_tools のツール（引数なし・単純なものは委譲） ---
    ToolDef(
        name="get_milestone_progress",
        description="マイルストーンの完了率・期限・残日数を一覧表示する",
        parameters={},
        fn=_call_mcp("get_milestone_progress"),
    ),
    ToolDef(
        name="get_overdue_items",
        description="期限超過のアクションアイテムを一覧表示する。担当者でフィルタ可能",
        parameters={"assignee": "担当者名（部分一致）", "limit": "取得件数（デフォルト20）"},
        fn=_call_mcp("get_overdue_items"),
    ),
    ToolDef(
        name="get_assignee_workload",
        description="担当者別のオープンAI件数・期限超過件数を一覧表示する",
        parameters={},
        fn=_call_mcp("get_assignee_workload"),
    ),
    ToolDef(
        name="search_action_items",
        description="アクションアイテムを条件検索する（担当者・状態・キーワード）",
        parameters={"assignee": "担当者名", "status": "open または closed", "keyword": "内容のキーワード", "limit": "取得件数（デフォルト20）"},
        fn=_call_mcp("search_action_items"),
    ),
    ToolDef(
        name="search_decisions",
        description="決定事項をキーワードで検索する",
        parameters={"keyword": "検索キーワード", "limit": "取得件数（デフォルト20）"},
        fn=_call_mcp("search_decisions"),
    ),
    ToolDef(
        name="search_text",
        description="議事録・Slackメッセージを全文検索する（FTS5 + LLM re-ranking）",
        parameters={"query": "検索クエリ（自然言語可）"},
        fn=_tool_search_text,
    ),
    ToolDef(
        name="search_text_hybrid",
        description="FTS5 + ベクトル類似度のハイブリッド検索",
        parameters={"query": "検索クエリ", "index_name": "インデックス名（デフォルト: pm）"},
        fn=_call_mcp("search_text_hybrid"),
    ),
    ToolDef(
        name="search_entity",
        description="データ種別と視点の組み合わせでマルチ分析する（pm_data/minutes/slack/box_docs × conservative/aggressive/objective/future_oriented）",
        parameters={
            "query": "調査クエリ",
            "perspective": "視点: conservative（リスク）/ aggressive（機会）/ objective（データ）/ future_oriented（将来性）",
            "data_type": "データ種別: pm_data / minutes / slack / box_docs",
        },
        fn=_call_mcp("search_entity"),
    ),
    ToolDef(
        name="synthesize_answers",
        description="複数の Explorer Agent の回答を統合して総合回答を生成する",
        parameters={"question": "元の質問", "answers": "統合する回答のリスト"},
        fn=_call_mcp("synthesize_answers"),
    ),
    # --- AgentContext 依存ツール ---
    ToolDef(
        name="search_mentions",
        description=(
            "指定ユーザーがメンション（<@Uxxx>）または名指しされた Slack メッセージを"
            "全チャンネルから集計する"
        ),
        parameters={
            "user_id": "Slack user_id",
            "name": "名前文字列（例: 西田）",
            "since": "この日付以降（YYYY-MM-DD）",
            "until": "この日付以前（YYYY-MM-DD）",
            "limit": "取得件数（デフォルト50）",
        },
        fn=_tool_search_mentions,
    ),
    ToolDef(
        name="get_slack_messages",
        description="特定チャンネルの生Slackメッセージを取得する",
        parameters={"channel_id": "SlackチャンネルID", "since": "この日付以降（YYYY-MM-DD）", "max_chars": "最大文字数（デフォルト10000）"},
        fn=_tool_get_slack_messages,
    ),
    # --- 出力ツール ---
    ToolDef(
        name="box_upload_file",
        description="ローカルファイルを Box にアップロードする（副作用あり、ユーザー確認必須）",
        parameters={"local_path": "アップロードするファイルのパス", "filename": "Box 上のファイル名（省略可）"},
        fn=_tool_box_upload,
    ),
    ToolDef(
        name="slack_post_message",
        description="Slack にメッセージを投稿する（副作用あり、ユーザー確認必須）",
        parameters={"channel": "投稿先チャンネルID", "text": "メッセージ本文（Markdown）", "thread_ts": "スレッド返信先（省略可）"},
        fn=_tool_slack_post,
    ),
    ToolDef(
        name="canvas_post_content",
        description="Slack Canvas の内容を置き換える（副作用あり、ユーザー確認必須）",
        parameters={"canvas_id": "Canvas ID（F で始まる）", "content": "新しいコンテンツ（Markdown）"},
        fn=_tool_canvas_post,
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
