#!/usr/bin/env python3
"""
direction.py — Argus 垂直軸 機能2: 決定クラスタ集約・方向Δ

設計書 §6「決定を決定クラスタへ集約する統合」の実装。

処理手順: 集合化（グラフ、LLM不使用）→ 命名（LLM+承認）→ 投入量集計（SQL）→
照合Δ（意図された方向との比較）。

集約はLLMの自由な分類ではなく `ledger_edges` の構造（contributes / depends_on）
に基づく。決定クラスタ ＝ 「共通の前提（depends_on同一assumption）に立ち、
同一の目標（contributes同一goal）に貢献する決定の集合」。LLM の裁量は
クラスタへの「命名」のみに限定する（存在しない一貫性の付与を防ぐ、設計書§6）。

出所主義: 所見の全項目は decision/goal/assumption ID に辿れること。
最上位目標は方向の指標であり、単一の達成度スコアには集約しない（設計書§7）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))


# --------------------------------------------------------------------------- #
# 集合化（グラフ、LLM不使用）
# --------------------------------------------------------------------------- #
def compute_decision_clusters(pm_conn) -> list[dict]:
    """decisions →contributes→ goal / decisions →depends_on→ assumption の
    辺から決定クラスタを機械的に集合化する。

    クラスタキー = (goal_id, 依拠する前提IDの集合)。同一目標に貢献し、
    同じ前提集合に依拠する決定同士が同じクラスタになる。
    前提に依拠しない決定は前提集合を空集合として扱う（それでも有効なクラスタ）。

    Returns: 各クラスタを表す dict のリスト。キーは
      goal_id, assumption_ids (list[int], ソート済み), decision_ids (list[int]),
      decisions (list[dict]: id, content, trade_off)
    """
    contributes = pm_conn.execute(
        "SELECT from_id, to_id FROM ledger_edges"
        " WHERE edge_type = 'contributes' AND from_kind = 'decision'"
        "   AND to_kind = 'goal' AND state = 'active'"
    ).fetchall()
    depends_on = pm_conn.execute(
        "SELECT from_id, to_id FROM ledger_edges"
        " WHERE edge_type = 'depends_on' AND from_kind = 'decision'"
        "   AND to_kind = 'assumption' AND state = 'active'"
    ).fetchall()

    # decision_id -> [goal_id, ...]
    decision_goals: dict[str, list[str]] = {}
    for row in contributes:
        decision_goals.setdefault(row["from_id"], []).append(row["to_id"])

    # decision_id -> [assumption_id, ...]
    decision_assumptions: dict[str, list[int]] = {}
    for row in depends_on:
        decision_assumptions.setdefault(row["from_id"], []).append(int(row["to_id"]))

    # (goal_id, frozenset(assumption_ids)) -> [decision_id, ...]
    clusters: dict[tuple[str, frozenset], list[str]] = {}
    for decision_id, goal_ids in decision_goals.items():
        assumption_ids = frozenset(sorted(decision_assumptions.get(decision_id, [])))
        for goal_id in goal_ids:
            clusters.setdefault((goal_id, assumption_ids), []).append(decision_id)

    result = []
    for (goal_id, assumption_ids), decision_ids in clusters.items():
        decisions = []
        for did in decision_ids:
            row = pm_conn.execute(
                "SELECT id, content, trade_off FROM decisions WHERE id = ?", (int(did),)
            ).fetchone()
            if row:
                decisions.append(dict(row))
        result.append({
            "goal_id": goal_id,
            "assumption_ids": sorted(assumption_ids),
            "decision_ids": sorted(int(d) for d in decision_ids),
            "decisions": decisions,
        })
    return result


# --------------------------------------------------------------------------- #
# 投入量の集計
# --------------------------------------------------------------------------- #
def aggregate_cluster_contribution(pm_conn, cluster: dict) -> float:
    """クラスタの投入量（貢献辺の重み付き合計）を集計する。

    ledger_edges.weight が未設定（enrich_items.py は現状 goal_id の列挙のみで
    度合いを推定しない）の場合は 1.0 として扱う（決定1件=貢献度1の簡易近似）。
    """
    total = 0.0
    for decision_id in cluster["decision_ids"]:
        row = pm_conn.execute(
            "SELECT weight FROM ledger_edges WHERE edge_type='contributes'"
            " AND from_kind='decision' AND from_id=? AND to_kind='goal' AND to_id=?",
            (str(decision_id), cluster["goal_id"]),
        ).fetchone()
        total += (row["weight"] if row and row["weight"] is not None else 1.0)
    return total


# --------------------------------------------------------------------------- #
# 照合（意図された方向との乖離 Δ）
# --------------------------------------------------------------------------- #
_WEIGHT_RANK = {"高": 3, "中": 2, "低": 1}


def compute_direction_delta(pm_conn) -> list[dict]:
    """目標ごとに、意図された優先度（ledger_goals.weight）と
    実態の投入量（貢献するクラスタの集計値）を比較する。

    単一の達成度スコアには集約しない（設計書§7）。目標ごとの
    「重みは高いが投入がほぼ無い」を Δ（投入不足領域）として列挙する。
    """
    goals = pm_conn.execute(
        "SELECT goal_id, name, layer, weight FROM ledger_goals"
        " WHERE COALESCE(state,'active')='active' AND weight IS NOT NULL"
    ).fetchall()
    clusters = compute_decision_clusters(pm_conn)

    contribution_by_goal: dict[str, float] = {}
    decisions_by_goal: dict[str, int] = {}
    for c in clusters:
        w = aggregate_cluster_contribution(pm_conn, c)
        contribution_by_goal[c["goal_id"]] = contribution_by_goal.get(c["goal_id"], 0.0) + w
        decisions_by_goal[c["goal_id"]] = decisions_by_goal.get(c["goal_id"], 0) + len(c["decision_ids"])

    results = []
    for g in goals:
        goal_id = g["goal_id"]
        weight_rank = _WEIGHT_RANK.get(g["weight"], 0)
        actual = contribution_by_goal.get(goal_id, 0.0)
        n_decisions = decisions_by_goal.get(goal_id, 0)
        # 重みランクが高いのに実態投入がゼロ、または重みランクに対して投入が薄い場合を
        # 「投入不足」として検出する（単一スコアではなく質的な旗として扱う）
        underserved = weight_rank >= 2 and actual < weight_rank
        results.append({
            "goal_id": goal_id,
            "name": g["name"],
            "weight": g["weight"],
            "actual_contribution": actual,
            "n_contributing_decisions": n_decisions,
            "underserved": underserved,
        })
    return results


def identify_unaddressed_goals(pm_conn) -> list[dict]:
    """入次数ゼロの目標（貢献する決定が1件も存在しない）を検出する。

    設計書 図2「目標（入次数ゼロ）| 機能2 | 貢献する決定が存在しない＝未着手の重要領域」。
    """
    rows = pm_conn.execute(
        "SELECT goal_id, name, layer, weight FROM ledger_goals"
        " WHERE COALESCE(state,'active')='active'"
        "   AND goal_id NOT IN ("
        "     SELECT to_id FROM ledger_edges"
        "     WHERE edge_type='contributes' AND from_kind='decision' AND to_kind='goal'"
        "       AND state='active'"
        "   )"
        " ORDER BY layer, goal_id"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 非収束状態の検出
# --------------------------------------------------------------------------- #
def detect_nonconvergence(pm_conn) -> list[dict]:
    """同一目標に対して複数の異なる前提集合に基づくクラスタが存在する場合を
    「非収束」（両立しない複数の方向に投入している）として検出する。

    設計書§6「収束と発散を区別し、発散の場合は…『両立しない複数の方向に
    投入している。選択が必要』と提示する」。構造的に検出可能な範囲
    （同一goalに対する複数クラスタの併存）にとどめ、内容面の矛盾判定は
    trade_off の記載を提示するのみで自動判定はしない（もっともらしい誤り
    の抑止、設計書§6冒頭）。
    """
    clusters = compute_decision_clusters(pm_conn)
    by_goal: dict[str, list[dict]] = {}
    for c in clusters:
        by_goal.setdefault(c["goal_id"], []).append(c)

    divergent = []
    for goal_id, goal_clusters in by_goal.items():
        if len(goal_clusters) <= 1:
            continue
        trade_offs = [
            {"decision_id": d["id"], "content": d["content"], "trade_off": d["trade_off"]}
            for c in goal_clusters for d in c["decisions"] if d.get("trade_off")
        ]
        divergent.append({
            "goal_id": goal_id,
            "n_clusters": len(goal_clusters),
            "clusters": goal_clusters,
            "trade_offs": trade_offs,
        })
    return divergent


# --------------------------------------------------------------------------- #
# 命名（LLM + 承認）— LLM の裁量はここに限定する
# --------------------------------------------------------------------------- #
_NAME_CLUSTER_PROMPT = """\
以下は、ある目標に貢献する決定事項の集合（決定クラスタ）です。
この集合全体を要約する短い名称（10〜20字程度）を1つだけ提案してください。

## 目標
{goal_name}

## 決定事項
{decisions_text}

## 出力
名称のみを1行で出力してください（説明・前置き不要）。
"""


def name_cluster_with_llm(cluster: dict, goal_name: str) -> str | None:
    """クラスタに短い名称を提案する（人の承認が前提、pm.dbへは書き込まない）。

    LLM の裁量はこの「命名」のみに限定する（設計書§6：存在しない一貫性の
    付与を防ぐため、集合化そのものはLLMに行わせない）。
    """
    try:
        from utils.llm import call_argus_llm
    except ImportError:
        return None

    decisions_text = "\n".join(f"- {d['content']}" for d in cluster["decisions"]) or "(該当なし)"
    prompt = _NAME_CLUSTER_PROMPT.format(goal_name=goal_name, decisions_text=decisions_text)

    try:
        result = call_argus_llm(prompt, timeout=30, max_tokens=100).strip()
        return result.splitlines()[0].strip(' 「」"') if result else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# レポート生成（Slack/Canvas/CLI 共通）
# --------------------------------------------------------------------------- #
def build_direction_report(pm_conn, *, use_llm_naming: bool = True) -> str:
    """方向Δレポートを Markdown で組み立てる。

    出所主義: 各所見は goal_id/decision_id/assumption_id に辿れる形で提示する。
    最上位目標の達成度を単一スコアに集約しない（設計書§7）。
    """
    clusters = compute_decision_clusters(pm_conn)
    delta = compute_direction_delta(pm_conn)
    unaddressed = identify_unaddressed_goals(pm_conn)
    divergent = detect_nonconvergence(pm_conn)

    goal_names = {g["goal_id"]: g["name"] for g in
                  pm_conn.execute("SELECT goal_id, name FROM ledger_goals").fetchall()}

    lines = ["# Argus 方向Δレポート（機能2）", ""]

    if not clusters:
        lines.append(
            "台帳に decision→goal（contributes）/ decision→assumption（depends_on）の"
            "辺が存在しないため、集約対象がありません。`enrich_items.py` による"
            "決定エンリッチメントの実行が必要です。"
        )
        return "\n".join(lines)

    lines.append("## 投入不足領域（意図された優先度 vs 実態の投入量）")
    lines.append("")
    underserved = [d for d in delta if d["underserved"]]
    if underserved:
        for d in underserved:
            lines.append(
                f"- **{d['goal_id']}**（{d['name']}）: 重み={d['weight']}, "
                f"貢献決定={d['n_contributing_decisions']}件, 投入量={d['actual_contribution']:.1f}"
            )
    else:
        lines.append("（投入不足として検出された目標はありません）")
    lines.append("")

    if unaddressed:
        lines.append("## 未着手の目標（貢献する決定が0件）")
        lines.append("")
        for g in unaddressed:
            lines.append(f"- **{g['goal_id']}**（{g['name']}）weight={g['weight'] or '-'}")
        lines.append("")

    if divergent:
        lines.append("## 非収束（同一目標に複数方向のクラスタが併存）")
        lines.append("")
        for div in divergent:
            lines.append(f"- **{div['goal_id']}**: {div['n_clusters']} クラスタが併存。選択が必要")
            for t in div["trade_offs"]:
                lines.append(f"  - d:{t['decision_id']} 捨てた案: {t['trade_off']}")
        lines.append("")

    lines.append("## 決定クラスタ一覧")
    lines.append("")
    for c in clusters:
        goal_name = goal_names.get(c["goal_id"], c["goal_id"])
        name = None
        if use_llm_naming:
            name = name_cluster_with_llm(c, goal_name)
        label = name or f"（{goal_name} に貢献する決定群）"
        assumption_note = (
            f"、前提 {c['assumption_ids']} に依拠" if c["assumption_ids"] else ""
        )
        lines.append(f"### {label}")
        lines.append(f"目標: {c['goal_id']}（{goal_name}）{assumption_note}")
        for d in c["decisions"]:
            lines.append(f"- d:{d['id']} {d['content']}")
        lines.append("")

    lines.append(
        "---\n_機械は集合化・集計・命名までを担う。是正判断はプロジェクト管理者が行う"
        "（設計書§2「Δの是正は人が行う」）。_"
    )
    return "\n".join(lines)
