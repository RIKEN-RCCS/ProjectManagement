"""mcp_explorer.py — Explorer Agent 分析ロジック

データ種別（pm_data / minutes / slack / box_docs）と
エモーション（conservative / aggressive / objective / future_oriented）の
組み合わせで、異なる視点からの分析を提供する。

pm_mcp_server.py の search_entity ツールから呼ばれることを想定。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logger = logging.getLogger("mcp_explorer")

# --------------------------------------------------------------------------- #
# エモーション定義
# --------------------------------------------------------------------------- #

EMOTION_CONFIG = {
    "conservative": {
        "name": "リスク管理",
        "instruction": "リスク・懸念・問題点に焦点を当てて分析します。",
        "title_prefix": "⚠️ リスク",
        "details": (
            "- 期限超過や未着手のタスク\n"
            "- リソース不足や担当者過負荷\n"
            "- 認識齟齬や未解決の議論\n"
            "- 技術的リスクや依存関係の問題\n"
            "- マイルストーン達成への影響"
        ),
    },
    "aggressive": {
        "name": "機会創出",
        "instruction": "進展・成果・ポジティブなシグナルに焦点を当てて分析します。",
        "title_prefix": "✅ 機会",
        "details": (
            "- 完了したタスクや達成されたマイルストーン\n"
            "- 新たに生まれた協業機会\n"
            "- 技術的ブレークスルーや進展\n"
            "- 拡大・加速できる領域\n"
            "- 他フェーズへの応用可能性"
        ),
    },
    "objective": {
        "name": "データ分析",
        "instruction": "データを中立的・客観的に解釈します。",
        "title_prefix": "📊 観測",
        "details": (
            "- 件数・割合・推移の定量的要約\n"
            "- 担当者別・種別の分布\n"
            "- 統計的に注目すべきパターン\n"
            "- 週次/月次の変化トレンド\n"
            "- 推測や評価を交えず、事実のみ"
        ),
    },
    "future_oriented": {
        "name": "戦略展望",
        "instruction": "長期的影響・将来トレンドに焦点を当てて分析します。",
        "title_prefix": "🔭 展望",
        "details": (
            "- 現在のトレンドが将来に与える影響\n"
            "- フェーズ移行に伴う準備課題\n"
            "- 長期的な技術投資判断\n"
            "- 半年〜1年先のリスク/機会\n"
            "- 戦略的方向性の示唆"
        ),
    },
}

_DATA_TYPE_NAMES = {
    "pm_data": "PMデータ",
    "minutes": "議事録",
    "slack": "Slack会話",
    "box_docs": "ドキュメント",
}


# --------------------------------------------------------------------------- #
# データ種別検索
# --------------------------------------------------------------------------- #

def _search_pm_data(query: str) -> str:
    """pm.db のアクションアイテム・決定事項を検索"""
    from argus.qa_engine import _query_action_items, _query_decisions
    from db_utils import open_pm_db
    from format_utils import format_milestone_table
    _DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
    conn = open_pm_db(_DATA_DIR / "pm.db")
    try:
        parts = []
        items = _query_action_items(conn, keyword=query, limit=10)
        if items:
            lines = ["### アクションアイテム"]
            for r in items:
                lines.append(f"- ID:{r['id']} [{r['status']}] 担当:{r.get('assignee','?')} {r['content'][:100]}")
            parts.append("\n".join(lines))
        decisions = _query_decisions(conn, keyword=query, limit=10)
        if decisions:
            lines = ["### 決定事項"]
            for r in decisions:
                lines.append(f"- D:{r['id']} [{r.get('decided_at','?')}] {r['content'][:100]}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts) if parts else "pm.db に関連データは見つかりませんでした。"
    finally:
        conn.close()


def _search_text_index(query: str, index_db: Path, index_name: str = "pm") -> str:
    """qa_index.db を全文検索して Markdown で返す"""
    from argus.retrieval import retrieve_chunks_hyde
    from argus.pm_qa_server import _format_source_label
    merged = retrieve_chunks_hyde(query, index_db, index_name=index_name, max_merged=15)
    if not merged:
        return "該当する情報は見つかりませんでした。"
    lines = [f"### 検索結果（{len(merged)}件）"]
    for i, c in enumerate(merged, 1):
        label = _format_source_label(c)
        lines.append(f"[{i}] {label}")
        lines.append(f"    {c['content'][:400].strip()}")
    return "\n".join(lines)


def _search_slack(query: str, index_db: Path) -> str:
    """Slack 生メッセージを検索"""
    from argus.retrieval import retrieve_chunks
    from argus.pm_qa_server import _format_source_label
    # source_type=slack_raw を絞り込むため index_name ではなく、retrieve_chunks の
    # 検索結果をあとでフィルタ。ここでは一旦全 index で検索
    chunks = retrieve_chunks(query, index_db, k=20, index_name=None)
    slack_chunks = [c for c in chunks if c.get("source_type") == "slack_raw"][:10]
    if not slack_chunks:
        return "Slack メッセージに関連情報は見つかりませんでした。"
    lines = [f"### Slack メッセージ検索結果"]
    for c in slack_chunks:
        label = _format_source_label(c)
        lines.append(f"- {label}")
        lines.append(f"  {c['content'][:300].strip()}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM 分析
# --------------------------------------------------------------------------- #

def _analyze_with_emotion(
    query: str, search_results: str, emotion: str, data_type: str
) -> str:
    """検索結果に対してエモーション視点で LLM 分析を実行"""
    import cli_utils
    cfg = EMOTION_CONFIG.get(emotion)
    if not cfg:
        return f"不明なエモーション: {emotion}"
    data_type_name = _DATA_TYPE_NAMES.get(data_type, data_type)

    prompt = f"""あなたは富岳NEXTプロジェクトの{data_type_name}分析エージェントです。
{cfg['instruction']}

以下の情報を分析し、{cfg['name']}観点で3〜5件の洞察を列挙してください。

各項目の形式:
- **{cfg['title_prefix']}** 洞察タイトル
  - 状況: （データに基づく現状説明）
  - 根拠: （具体的な数値・日付・引用）

## 検索結果

{search_results}

## 分析指示

{cfg['details']}
"""
    try:
        return cli_utils.call_argus_llm(prompt, max_tokens=2048, temperature=0.3)
    except Exception as e:
        return f"LLM 分析中にエラーが発生しました: {e}\n\n元データ:\n{search_results[:1000]}"


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #

def run_explorer(
    query: str,
    data_type: str,
    emotion: str,
    index_db: Path | None = None,
) -> str:
    """データ種別×エモーションの組み合わせで調査を実行する。

    Args:
        query: 調査クエリ
        data_type: データ種別 (pm_data | minutes | slack | box_docs)
        emotion: エモーション (conservative | aggressive | objective | future_oriented)
        index_db: qa_index.db のパス（None の場合は自動解決）

    Returns:
        Markdown 形式の分析結果
    """
    if index_db is None:
        _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        index_db = _REPO_ROOT / "data" / "qa_index.db"

    logger.info(f"Explorer: data_type={data_type}, emotion={emotion}, query={query}")

    # データ種別に応じた検索
    try:
        if data_type == "pm_data":
            search_result = _search_pm_data(query)
        elif data_type == "minutes":
            search_result = _search_text_index(query, index_db, index_name="pm")
        elif data_type == "slack":
            search_result = _search_slack(query, index_db)
        elif data_type == "box_docs":
            search_result = _search_text_index(query, index_db, index_name="pm")
        else:
            return f"不明なデータ種別: {data_type}"
    except Exception as e:
        logger.exception(f"検索エラー: {e}")
        search_result = f"検索中にエラーが発生しました: {e}"

    # エモーション分析
    analysis = _analyze_with_emotion(query, search_result, emotion, data_type)

    # ヘッダー付きで返却
    cfg = EMOTION_CONFIG.get(emotion, {})
    data_type_name = _DATA_TYPE_NAMES.get(data_type, data_type)
    header = f"### {cfg.get('name', emotion)} × {data_type_name}\n\n"
    return header + analysis


def run_explorer_suite(
    query: str,
    emotions: list[str] | None = None,
    data_types: list[str] | None = None,
    index_db: Path | None = None,
) -> dict[str, str]:
    """複数のデータ種別×エモーションの組み合わせを実行し、結果辞書を返す。

    Returns:
        {"pm_data+conservative": "分析結果...", ...}
    """
    if emotions is None:
        emotions = list(EMOTION_CONFIG.keys())
    if data_types is None:
        data_types = list(_DATA_TYPE_NAMES.keys())
    results = {}
    for dt in data_types:
        for em in emotions:
            key = f"{dt}+{em}"
            try:
                results[key] = run_explorer(query, dt, em, index_db)
            except Exception as e:
                results[key] = f"エラー: {e}"
    return results
