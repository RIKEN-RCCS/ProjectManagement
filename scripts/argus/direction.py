#!/usr/bin/env python3
"""
direction.py — Argus 垂直軸 機能2: 方向Δの所見検出

設計書 §6「決定を決定クラスタへ集約する統合」§7「意図された方向の表現」の実装。

2026-07-05 抜本見直し（経緯は LOG.md「Argus 垂直軸の抜本見直し」）:
「クラスタの構造表示」から「所見（finding）の検出」へ出力の主役を転換した。
所見は5種類 — 停滞/未着手・制約違反疑い・論点ブロック・トレードオフ衝突・前提健全性。
いずれも選別ゲート（decisions.ledger_gate='decision'、設計書§4）を通過した決定と
enrich が張った辺（contributes/depends_on/may_violate/blocks）だけを入力とする。
所見が無ければ「無い」と明言する（存在しない一貫性を付与しない、設計書§9）。

旧実装の「投入量Δ」（貢献辺数と重みランクの比較）と「前提集合キーによる非収束検出」は
廃止した — 前者は次元の合わない比較、後者はLLM辺付けの揺らぎを測るだけで
識別要件全件が常に非収束判定になっていた（診断の数値は LOG.md 参照）。

LLM の裁量は (1) クラスタの命名・要約、(2) トレードオフ衝突の判定、
(3) エグゼクティブサマリー（現状の言い換え+提案アクション、提案形式限定）の3箇所。
集合化・停滞・違反疑い・ブロックの検出は SQL のみで行う。
出所主義: 所見の全項目は decision/goal/assumption/issue ID に辿れる。
是正判断は人（PM)が行う（設計書§2）。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))


# --------------------------------------------------------------------------- #
# 表示ユーティリティ
# --------------------------------------------------------------------------- #
_LAYER_LABELS = {
    "top": "最上位目標",
    "identifying": "識別要件",
    "constraint": "制約",
    "tablestakes": "前提条件（必達水準）",
}


def _goal_label(goal_id: str, name: str, layer: str | None) -> str:
    """目標IDを常に種別（layer）付きで表示するための整形。"""
    layer_label = _LAYER_LABELS.get(layer or "", layer or "")
    return f"{goal_id}［{layer_label}］{name}" if layer_label else f"{goal_id}（{name}）"


_SNIPPET_MAXLEN = 60


def _content_snippet(decision_id: int, content: str | None) -> str:
    text = (content or "").strip()
    if len(text) > _SNIPPET_MAXLEN:
        text = text[:_SNIPPET_MAXLEN] + "…"
    return f"d:{decision_id}「{text}」" if text else f"d:{decision_id}"


# --------------------------------------------------------------------------- #
# 集合化（グラフ、LLM不使用）— 選別ゲート通過決定のみ
# --------------------------------------------------------------------------- #
def compute_decision_clusters(pm_conn) -> list[dict]:
    """decisions →contributes→ goal / decisions →depends_on→ assumption の
    辺から決定クラスタを機械的に集合化する。

    クラスタキー = (goal_id, 依拠する前提IDの集合)。同一目標に貢献し、
    同じ前提集合に依拠する決定同士が同じクラスタになる。
    辺は選別ゲート通過決定（ledger_gate='decision'）にしか張られない
    （enrich_items.py の _apply_ledger_judgment）ため、ここでの追加フィルタは不要。
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

    decision_goals: dict[str, list[str]] = {}
    for row in contributes:
        decision_goals.setdefault(row["from_id"], []).append(row["to_id"])

    decision_assumptions: dict[str, list[int]] = {}
    for row in depends_on:
        decision_assumptions.setdefault(row["from_id"], []).append(int(row["to_id"]))

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


def _fetch_identifying_goals(pm_conn) -> list[dict]:
    """重み承認済みの識別要件（所見検出の対象母集団）。"""
    return [dict(r) for r in pm_conn.execute(
        "SELECT goal_id, name, layer, weight FROM ledger_goals"
        " WHERE COALESCE(state,'active')='active'"
        "   AND layer = 'identifying' AND weight IS NOT NULL"
        " ORDER BY goal_id"
    ).fetchall()]


# --------------------------------------------------------------------------- #
# 所見1: 停滞・未着手の検出（SQLのみ）
# --------------------------------------------------------------------------- #
def detect_stagnation(pm_conn, *, recent_days: int = 90) -> list[dict]:
    """識別要件ごとの決定流量から停滞・未着手を検出する。

    - 未着手: 直接貢献する決定が全期間で0件（設計書 図2「入次数ゼロ」）
    - 停滞: 重み=高 なのに直近 recent_days 日の貢献決定が0件
    全識別要件ぶんの流量（直近6ヶ月の月別件数）を返し、該当なしは status=None。
    """
    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
    six_months_ago = (date.today() - timedelta(days=183)).isoformat()[:7]

    results = []
    for g in _fetch_identifying_goals(pm_conn):
        rows = pm_conn.execute(
            "SELECT substr(d.decided_at,1,10) AS day, d.id FROM ledger_edges e"
            " JOIN decisions d ON d.id = CAST(e.from_id AS INTEGER)"
            " WHERE e.edge_type='contributes' AND e.from_kind='decision'"
            "   AND e.to_kind='goal' AND e.to_id=? AND e.state='active'"
            "   AND COALESCE(d.deleted,0)=0",
            (g["goal_id"],),
        ).fetchall()
        total = len(rows)
        recent = sum(1 for r in rows if (r["day"] or "") >= cutoff)
        monthly: dict[str, int] = {}
        for r in rows:
            month = (r["day"] or "")[:7]
            if month >= six_months_ago:
                monthly[month] = monthly.get(month, 0) + 1

        status = None
        if total == 0:
            status = "未着手"
        elif g["weight"] == "高" and recent == 0:
            status = "停滞"
        results.append({
            **g,
            "total": total,
            "recent": recent,
            "recent_days": recent_days,
            "monthly": dict(sorted(monthly.items())),
            "status": status,
        })
    return results


# --------------------------------------------------------------------------- #
# 所見2: 制約違反疑い（SQLのみ — 疑いの判定自体は enrich 時に済んでいる）
# --------------------------------------------------------------------------- #
def detect_constraint_violations(pm_conn) -> list[dict]:
    """enrich が張った may_violate 辺（決定→制約）を列挙する。

    疑いの提示まで（判断はPM、設計書§7「制約は違反の有無を検査する」）。
    PMが棄却した辺（state='rejected'）は表示しない。
    """
    rows = pm_conn.execute(
        "SELECT e.from_id AS decision_id, e.to_id AS goal_id, e.rationale,"
        "       d.content, g.name AS goal_name, g.layer"
        " FROM ledger_edges e"
        " JOIN decisions d ON d.id = CAST(e.from_id AS INTEGER)"
        " JOIN ledger_goals g ON g.goal_id = e.to_id"
        " WHERE e.edge_type='may_violate' AND e.state='active'"
        "   AND COALESCE(d.deleted,0)=0"
        " ORDER BY e.to_id, d.id"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 所見3: 論点ブロック（SQLのみ）
# --------------------------------------------------------------------------- #
def detect_issue_blocks(pm_conn) -> list[dict]:
    """open な論点ごとに、そのブロック対象領域で為された決定（blocks辺）を列挙する。

    設計書§3「論点 →ブロック→ 決定: 論点をブロック数で優先度付け」。
    「論点が未解決のまま、その前提を固める決定が進行している」ことの機械的な指摘。
    """
    issues = pm_conn.execute(
        "SELECT issue_id, content, owner, due_date FROM ledger_issues"
        " WHERE COALESCE(state,'open')='open' ORDER BY issue_id"
    ).fetchall()
    results = []
    for issue in issues:
        blocked = pm_conn.execute(
            "SELECT d.id, d.content FROM ledger_edges e"
            " JOIN decisions d ON d.id = CAST(e.to_id AS INTEGER)"
            " WHERE e.edge_type='blocks' AND e.from_kind='issue' AND e.from_id=?"
            "   AND e.state='active' AND COALESCE(d.deleted,0)=0"
            " ORDER BY d.id",
            (issue["issue_id"],),
        ).fetchall()
        results.append({
            "issue_id": issue["issue_id"],
            "content": issue["content"],
            "owner": issue["owner"],
            "due_date": issue["due_date"],
            "blocked_decisions": [dict(r) for r in blocked],
        })
    return results


# --------------------------------------------------------------------------- #
# 所見4: トレードオフ衝突（LLM 1呼び出し — 非収束の新定義）
# --------------------------------------------------------------------------- #
_CONFLICT_DETECT_PROMPT = """\
以下は、富岳NEXTプロジェクトの識別要件（目標）ごとの決定事項一覧です。
各決定には「捨てた案」（その決定の際に採らなかった選択肢）が付いているものがあります。

「ある決定Aが捨てた案を、別の決定Bが採用している」または「AとBの選択が両立しない」
組み合わせ**だけ**を抽出してください。

## 制約（厳守）
- 一覧に無い事実・IDを作らないこと。話題が近いだけの組は挙げない
- 本当に両立しない組だけを挙げる。**無ければ空配列 [] が正常**
- reason には両立しない理由を1文で書く

## 決定一覧
{goals_text}

## 出力（JSON配列のみ、説明不要）
[{{"goal_id": "G-XXX", "decision_ids": [111, 222], "reason": "1文"}}]
"""


def detect_tradeoff_conflicts(pm_conn) -> list[dict]:
    """同一識別要件に貢献する決定同士のトレードオフ衝突を検出する。

    設計書§6「矛盾はトレードオフの衝突として機械的に検出する」の実装。
    候補（同一目標に2件以上の貢献決定がある識別要件）をSQLで絞り、
    衝突判定のみ LLM 1呼び出しで行う。LLM失敗時は空（所見なし扱い）。
    これが「非収束」の新定義 — 旧実装の前提集合キーによるクラスタ併存判定は
    LLM辺付けの揺らぎを測るだけだったため廃止した。
    """
    goals = _fetch_identifying_goals(pm_conn)
    blocks: list[str] = []
    valid_pairs: dict[str, set[int]] = {}
    for g in goals:
        rows = pm_conn.execute(
            "SELECT d.id, d.content, d.trade_off FROM ledger_edges e"
            " JOIN decisions d ON d.id = CAST(e.from_id AS INTEGER)"
            " WHERE e.edge_type='contributes' AND e.from_kind='decision'"
            "   AND e.to_kind='goal' AND e.to_id=? AND e.state='active'"
            "   AND COALESCE(d.deleted,0)=0 ORDER BY d.id",
            (g["goal_id"],),
        ).fetchall()
        if len(rows) < 2:
            continue
        valid_pairs[g["goal_id"]] = {r["id"] for r in rows}
        lines = [f"### {g['goal_id']}（{g['name']}）"]
        for r in rows:
            lines.append(f"- d:{r['id']} {r['content']}")
            if r["trade_off"]:
                lines.append(f"  捨てた案: {r['trade_off']}")
        blocks.append("\n".join(lines))

    if not blocks:
        return []

    try:
        import json as _json

        from utils.llm import call_argus_llm
        raw = call_argus_llm(
            _CONFLICT_DETECT_PROMPT.format(goals_text="\n\n".join(blocks)),
            timeout=120, max_tokens=2048,
        )
        start, end = raw.find("["), raw.rfind("]")
        parsed = _json.loads(raw[start:end + 1]) if start != -1 and end > start else []
    except Exception:
        return []

    conflicts = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        goal_id = item.get("goal_id")
        ids = item.get("decision_ids") or []
        if goal_id not in valid_pairs or len(ids) < 2:
            continue
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            continue
        if not all(i in valid_pairs[goal_id] for i in ids):
            continue
        conflicts.append({
            "goal_id": goal_id,
            "decision_ids": sorted(ids),
            "reason": str(item.get("reason") or "")[:300],
        })
    return conflicts


# --------------------------------------------------------------------------- #
# 所見5: 前提の健全性（SQLのみ）
# --------------------------------------------------------------------------- #
def assumption_health(pm_conn) -> list[dict]:
    """各前提の確信度・最終確認日・依拠決定数を返す。

    confidence='low' は機能1の着地処理（patrol/detect.py detect_external_signals）が
    反証シグナルを検出した状態であり、依拠する決定群が要レビュー。
    """
    rows = pm_conn.execute(
        "SELECT a.id, a.content, a.confidence, a.last_reviewed_at,"
        "       (SELECT COUNT(*) FROM ledger_edges e"
        "         WHERE e.edge_type='depends_on' AND e.to_kind='assumption'"
        "           AND e.to_id=CAST(a.id AS TEXT) AND e.state='active') AS n_dependents"
        " FROM ledger_assumptions a"
        " WHERE COALESCE(a.state,'active')='active' ORDER BY a.id"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 命名・要約（LLM + 承認）— 集合化そのものへのLLMの裁量はここに限定する
# --------------------------------------------------------------------------- #
_NAME_CLUSTER_PROMPT = """\
以下は、ある目標に貢献する決定事項の集合（決定クラスタ）です。次の2つを出力してください。

1. 名称: この集合が「他の選択肢ではなく何を選んだか」が一目でわかる短い名称
   （10〜20字程度）。単なる話題ラベル（例:「〇〇に関する決定群」）ではなく、
   選んだ方向性が伝わる表現にすること。同じ目標に複数のクラスタが存在する場合に
   名称同士を見比べるだけで対立軸が分かるようにするためです。
2. 要約: この集合の決定事項が全体として何を選んだかを1〜2文で要約すること。
   決定事項の本文に書かれていない事実を作らないこと。

## 目標
{goal_name}

## 決定事項
{decisions_text}

## 出力形式（この2行のみ、前置き・説明不要）
名称: <名称>
要約: <要約>
"""


def summarize_cluster_with_llm(cluster: dict, goal_name: str) -> dict | None:
    """クラスタに短い名称と要約を付ける（人の承認が前提、pm.dbへは書き込まない）。

    戻り値は {"name": str, "summary": str | None}。要約の抽出に失敗しても
    名称が取れていれば name のみで返す。
    """
    try:
        from utils.llm import call_argus_llm
    except ImportError:
        return None

    decisions_text = "\n".join(f"- {d['content']}" for d in cluster["decisions"]) or "(該当なし)"
    prompt = _NAME_CLUSTER_PROMPT.format(goal_name=goal_name, decisions_text=decisions_text)

    try:
        result = call_argus_llm(prompt, timeout=30, max_tokens=200).strip()
    except Exception:
        return None
    if not result:
        return None

    name = None
    summary = None
    for line in result.splitlines():
        line = line.strip()
        if line.startswith("名称:") or line.startswith("名称："):
            name = line.split(":", 1)[-1].split("：", 1)[-1].strip(' 「」"')
        elif line.startswith("要約:") or line.startswith("要約："):
            summary = line.split(":", 1)[-1].split("：", 1)[-1].strip(' 「」"')
    if not name:
        # フォーマット逸脱時は1行目を名称としてフォールバック
        name = result.splitlines()[0].strip(' 「」"')
    return {"name": name or None, "summary": summary or None}


# --------------------------------------------------------------------------- #
# エグゼクティブサマリー（LLM + 承認）— 目標別の現状言い換え + 提案アクション
# --------------------------------------------------------------------------- #
_EXECUTIVE_SUMMARY_PROMPT = """\
あなたは富岳NEXTプロジェクトのPMアシスタントです。以下は、最上位目標に貢献する
識別要件（差別化の核となる目標、重みはPM承認済み）について、台帳（選別ゲートを通過した
決定のみ）から機械的に検出した所見です。この所見だけを根拠に、目標ごとに現状を短く
言い換え、PMへの提案アクションを1つずつ示してください。

## 最上位目標（文脈。この目標自体は対象に含めない）
{top_goal_text}

## 制約（厳守）
- 所見・クラスタ情報に無い事実を作らないこと。ID（G-*/C-*/Q-*/d:N）が無い推測は書かない
- 「現状」は渡された情報（状態・決定流量・クラスタ名/要約・所見）の言い換えに留め、
  新しい評価や解釈を加えないこと
- 「提案アクション」は「〜してはどうか」「〜を検討」という提案の形にすること。
  「〜すべき」という断定・命令にしないこと。是正が必要とすら決めつけないこと
  （健全な目標には「現状維持でよいか確認してはどうか」等でよい）
- すべての目標について、目標ごとに出力すること

## 目標別の機械的所見
{goals_text}

## 横断所見（該当する目標の現状・アクションに反映してよい）
{cross_text}

## 出力形式（目標ごとに次の3行、他の文章は書かない）
### <目標ID>
現状: <1〜2文>
提案アクション: <1文>
"""


def build_executive_summary(
    pm_conn,
    stagnation: list[dict],
    conflicts: list[dict],
    violations: list[dict],
    issue_blocks: list[dict],
    named_clusters: dict[str, list[dict]],
) -> str | None:
    """所見をもとに、識別要件ごとの「現状」と「提案アクション」を整理する（LLM+承認）。

    アクションは提案形式（〜してはどうか／〜を検討）に限定し断定はしない。
    「是正判断は人が行う」（設計書§2）。
    """
    if not stagnation:
        return None

    top_goal = pm_conn.execute(
        "SELECT goal_id, name FROM ledger_goals"
        " WHERE layer = 'top' AND COALESCE(state,'active')='active' LIMIT 1"
    ).fetchone()
    top_goal_text = (
        f"{top_goal['goal_id']}（{top_goal['name']}） — 識別要件はこれに貢献する目標"
        if top_goal else "(未設定)"
    )

    conflict_goals = {c["goal_id"] for c in conflicts}

    goal_blocks = []
    for g in stagnation:
        goal_id = g["goal_id"]
        status = g["status"] or ("衝突あり" if goal_id in conflict_goals else "健全")
        if g["status"] and goal_id in conflict_goals:
            status += "・衝突あり"
        lines = [
            f"### {_goal_label(goal_id, g['name'], g['layer'])} 重み={g['weight']}",
            f"状態: {status}",
            f"決定流量: 全期間{g['total']}件, 直近{g['recent_days']}日{g['recent']}件",
        ]
        clusters = named_clusters.get(goal_id, [])
        if clusters:
            lines.append("クラスタ:")
            for c in clusters:
                label = c.get("name") or "(名称未設定)"
                detail = label
                if c.get("summary"):
                    detail += f" — {c['summary']}"
                ids_text = ", ".join(f"d:{did}" for did in c["decision_ids"])
                lines.append(f"- {detail}（{ids_text}）")
        for c in conflicts:
            if c["goal_id"] == goal_id:
                ids = ", ".join(f"d:{i}" for i in c["decision_ids"])
                lines.append(f"衝突: {ids} — {c['reason']}")
        goal_blocks.append("\n".join(lines))

    cross_lines = []
    for v in violations:
        cross_lines.append(
            f"- 制約違反疑い {v['goal_id']}: d:{v['decision_id']} — {v['rationale'] or ''}"
        )
    for ib in issue_blocks:
        if ib["blocked_decisions"]:
            ids = ", ".join(f"d:{d['id']}" for d in ib["blocked_decisions"])
            cross_lines.append(
                f"- 論点 {ib['issue_id']} が未解決のまま、ブロック対象領域で決定が進行: {ids}"
            )
    cross_text = "\n".join(cross_lines) or "(なし)"

    prompt = _EXECUTIVE_SUMMARY_PROMPT.format(
        top_goal_text=top_goal_text,
        goals_text="\n\n".join(goal_blocks),
        cross_text=cross_text,
    )

    try:
        from utils.llm import call_argus_llm
        result = call_argus_llm(prompt, timeout=90, max_tokens=1500).strip()
        return result or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 有向グラフの静止画像化（最上位目標→識別要件→決定クラスタ、LLM不使用）
# --------------------------------------------------------------------------- #
_GOAL_STATUS_COLORS = {
    "未着手": "#e74c3c",
    "停滞": "#e67e22",
    "衝突": "#8e44ad",
    "健全": "#27ae60",
}
_TOP_GOAL_COLOR = "#f1c40f"
_CLUSTER_NODE_COLOR = "#bdc3c7"


def _setup_japanese_font_for_graph(logger) -> str | None:
    """matplotlib に日本語フォントを登録する。見つからなければ None を返し豆腐表示を許容する
    （画像生成自体は失敗させない）。
    """
    import glob

    from matplotlib import font_manager

    for name in ("Noto Sans CJK JP", "IPAGothic", "TakaoGothic", "VL Gothic"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            return name

    for path in glob.glob("/usr/share/fonts/**/NotoSansCJK*.ttc", recursive=True):
        try:
            font_manager.fontManager.addfont(path)
        except Exception:
            continue
    if any(f.name == "Noto Sans CJK JP" for f in font_manager.fontManager.ttflist):
        return "Noto Sans CJK JP"

    logger.warning("[direction] 日本語フォントが見つかりません。グラフのラベルが文字化けする可能性があります")
    return None


def _wrap_graph_label(text: str, width: int = 8) -> str:
    import textwrap
    if not text:
        return ""
    return "\n".join(textwrap.wrap(text, width=width, max_lines=3, placeholder="…"))


def render_direction_graph(
    pm_conn,
    named_clusters: dict[str, list[dict]],
    status_by_goal: dict[str, str],
) -> Path | None:
    """最上位目標→識別要件→決定クラスタの階層構造をPNGに描画する。

    ノード色は所見検出の結果（status_by_goal: 未着手/停滞/衝突/健全）。
    matplotlib/networkx/フォントが使えない環境では None を返し、テキストレポートのみの
    従来動作に縮退する（画像生成の失敗でコマンド全体を失敗させない）。
    """
    import logging
    import os
    import tempfile

    logger = logging.getLogger("pm_argus")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
        import networkx as nx
    except ImportError:
        logger.warning("[direction] matplotlib/networkx が無いためグラフ画像を生成しません")
        return None

    goals = _fetch_identifying_goals(pm_conn)
    if not goals:
        return None

    top_goal = pm_conn.execute(
        "SELECT goal_id, name FROM ledger_goals"
        " WHERE layer = 'top' AND COALESCE(state,'active')='active' LIMIT 1"
    ).fetchone()

    contributes_to_top = {
        row["from_id"]: row["to_id"]
        for row in pm_conn.execute(
            "SELECT from_id, to_id FROM ledger_edges"
            " WHERE edge_type='contributes' AND from_kind='goal' AND to_kind='goal'"
            "   AND state='active'"
        ).fetchall()
    }

    try:
        graph = nx.DiGraph()
        node_labels: dict[str, str] = {}
        node_colors: dict[str, str] = {}
        node_sizes: dict[str, float] = {}
        node_shapes: dict[str, str] = {}
        tiers: dict[str, int] = {}

        if top_goal:
            top_id = top_goal["goal_id"]
            graph.add_node(top_id)
            node_labels[top_id] = _wrap_graph_label(top_goal["name"], width=10)
            node_colors[top_id] = _TOP_GOAL_COLOR
            node_sizes[top_id] = 4000
            node_shapes[top_id] = "s"
            tiers[top_id] = 0

        cluster_nodes_by_goal: dict[str, list[str]] = {}
        for g in goals:
            goal_id = g["goal_id"]
            graph.add_node(goal_id)
            status = status_by_goal.get(goal_id, "健全")
            node_labels[goal_id] = _wrap_graph_label(f"{goal_id} {g['name']}", width=10)
            node_colors[goal_id] = _GOAL_STATUS_COLORS.get(status, _GOAL_STATUS_COLORS["健全"])
            node_sizes[goal_id] = 1800
            node_shapes[goal_id] = "o"
            tiers[goal_id] = 1
            top_target = contributes_to_top.get(goal_id)
            if top_target and top_target in graph:
                graph.add_edge(goal_id, top_target)

            children = []
            for idx, c in enumerate(named_clusters.get(goal_id, [])):
                node_id = f"{goal_id}#c{idx}"
                graph.add_node(node_id)
                node_labels[node_id] = _wrap_graph_label(c.get("name") or "(名称未設定)", width=8)
                node_colors[node_id] = _CLUSTER_NODE_COLOR
                n_decisions = len(c.get("decision_ids") or [])
                node_sizes[node_id] = 300 + min(n_decisions, 20) * 60
                node_shapes[node_id] = "o"
                tiers[node_id] = 2
                graph.add_edge(node_id, goal_id)
                children.append(node_id)
            cluster_nodes_by_goal[goal_id] = children

        if graph.number_of_nodes() <= 1:
            return None

        for node, tier in tiers.items():
            graph.nodes[node]["tier"] = tier

        font_name = _setup_japanese_font_for_graph(logger)
        if font_name:
            plt.rcParams["font.family"] = font_name

        # 手動の階層レイアウト: multipartite_layout は各tier内を均等配置するだけで
        # 親子関係を無視し目標ノードが密集・重複するため、各目標をその子クラスタ群の
        # 重心の真上に置く
        cluster_order = [n for g in goals for n in cluster_nodes_by_goal.get(g["goal_id"], [])]
        n_clusters = len(cluster_order)
        pos: dict[str, tuple[float, float]] = {}
        for i, node in enumerate(cluster_order):
            pos[node] = ((i + 0.5) / max(n_clusters, 1), 0.0)

        for i, g in enumerate(goals):
            goal_id = g["goal_id"]
            children = cluster_nodes_by_goal.get(goal_id, [])
            if children:
                x = sum(pos[c][0] for c in children) / len(children)
            else:
                x = (i + 0.5) / len(goals)
            pos[goal_id] = (x, 1.0)

        if top_goal:
            top_id = top_goal["goal_id"]
            xs = [pos[g["goal_id"]][0] for g in goals]
            pos[top_id] = (sum(xs) / len(xs) if xs else 0.5, 2.0)

        fig_width = max(22, n_clusters * 1.1)
        fig, ax = plt.subplots(figsize=(fig_width, 10))

        for shape in ("s", "o"):
            nodes = [n for n in graph.nodes if node_shapes.get(n) == shape]
            if not nodes:
                continue
            nx.draw_networkx_nodes(
                graph, pos, nodelist=nodes, ax=ax,
                node_color=[node_colors[n] for n in nodes],
                node_size=[node_sizes[n] for n in nodes],
                node_shape=shape,
                edgecolors="#2c3e50", linewidths=1.0,
            )

        nx.draw_networkx_edges(
            graph, pos, ax=ax, arrows=True, arrowsize=12,
            edge_color="#95a5a6", width=1.2, connectionstyle="arc3,rad=0.05",
        )

        # draw_networkx_labels は font_family のデフォルトが "sans-serif" 固定であり、
        # 未指定だと rcParams["font.family"] を上書きして日本語が豆腐になる。明示指定が必須
        label_font = font_name or "sans-serif"
        top_and_goal_labels = {n: label for n, label in node_labels.items() if tiers[n] in (0, 1)}
        cluster_labels = {n: label for n, label in node_labels.items() if tiers[n] == 2}
        nx.draw_networkx_labels(
            graph, pos, labels=top_and_goal_labels, ax=ax, font_size=9, font_family=label_font
        )
        nx.draw_networkx_labels(
            graph, pos, labels=cluster_labels, ax=ax, font_size=7, font_family=label_font
        )

        legend_handles = [
            mpatches.Patch(color=color, label=status)
            for status, color in _GOAL_STATUS_COLORS.items()
        ]
        legend_handles.append(mpatches.Patch(color=_TOP_GOAL_COLOR, label="最上位目標"))
        legend_handles.append(
            mpatches.Patch(color=_CLUSTER_NODE_COLOR, label="決定クラスタ（サイズ=決定件数）")
        )
        ax.legend(handles=legend_handles, loc="lower center", ncol=3, fontsize=8, frameon=False)

        ax.set_title("Argus 方向Δ 台帳グラフ（最上位目標 → 識別要件 → 決定クラスタ）", fontsize=12)
        ax.axis("off")

        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="argus_direction_")
        os.close(fd)
        fig.savefig(tmp_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return Path(tmp_path)
    except Exception:
        logger.exception("[direction] グラフ画像の生成に失敗しました（テキストレポートは影響を受けません）")
        return None


# --------------------------------------------------------------------------- #
# レポート生成（Slack/Canvas/CLI 共通）— 所見型
# --------------------------------------------------------------------------- #
def build_direction_report(
    pm_conn, *, use_llm_naming: bool = True, include_graph: bool = False
) -> tuple[str, Path | None]:
    """方向Δの所見型レポートを Markdown で組み立てる。

    出所主義: 各所見は goal_id/decision_id/assumption_id/issue_id に辿れる形で提示する。
    所見が無ければ「無い」と明言する。最上位目標の達成度を単一スコアに集約しない（設計書§7）。
    戻り値は常に (レポート本文, グラフ画像パス) のタプル。
    """
    gate_stats = pm_conn.execute(
        "SELECT SUM(CASE WHEN ledger_gate='decision' THEN 1 ELSE 0 END) AS n_decision,"
        "       SUM(CASE WHEN ledger_gate='trace' THEN 1 ELSE 0 END) AS n_trace,"
        "       SUM(CASE WHEN ledger_gate IS NULL THEN 1 ELSE 0 END) AS n_ungated"
        " FROM decisions WHERE COALESCE(deleted,0)=0"
    ).fetchone()

    stagnation = detect_stagnation(pm_conn)
    violations = detect_constraint_violations(pm_conn)
    issue_blocks = detect_issue_blocks(pm_conn)
    conflicts = detect_tradeoff_conflicts(pm_conn) if use_llm_naming else []
    health = assumption_health(pm_conn)
    clusters = compute_decision_clusters(pm_conn)

    goal_meta = {
        g["goal_id"]: g for g in
        pm_conn.execute("SELECT goal_id, name, layer FROM ledger_goals").fetchall()
    }

    def goal_label(goal_id: str) -> str:
        g = goal_meta.get(goal_id)
        return _goal_label(goal_id, g["name"], g["layer"]) if g else goal_id

    lines = ["# Argus 方向Δレポート（所見型）", ""]
    lines.append(
        f"選別ゲート: 台帳対象 {gate_stats['n_decision'] or 0}件 / "
        f"作業の痕跡 {gate_stats['n_trace'] or 0}件 / 未判定 {gate_stats['n_ungated'] or 0}件"
        "（台帳対象の決定だけが以下の分析に使われる）"
    )
    lines.append("")

    if not stagnation:
        lines.append("台帳に重み承認済みの識別要件がありません。`pm_ingest.py ledger` でのシード投入が必要です。")
        return "\n".join(lines), None

    # クラスタの命名・要約を先に確定（エグゼクティブサマリー・一覧・グラフで使い回す）
    named_clusters: dict[str, list[dict]] = {}
    for c in clusters:
        g = goal_meta.get(c["goal_id"])
        goal_name = g["name"] if g else c["goal_id"]
        info = summarize_cluster_with_llm(c, goal_name) if use_llm_naming else None
        named_clusters.setdefault(c["goal_id"], []).append({
            "name": info.get("name") if info else None,
            "summary": info.get("summary") if info else None,
            "decision_ids": c["decision_ids"],
        })

    conflict_goals = {c["goal_id"] for c in conflicts}
    status_by_goal: dict[str, str] = {}
    for g in stagnation:
        if g["status"] == "未着手":
            status_by_goal[g["goal_id"]] = "未着手"
        elif g["goal_id"] in conflict_goals:
            status_by_goal[g["goal_id"]] = "衝突"
        elif g["status"] == "停滞":
            status_by_goal[g["goal_id"]] = "停滞"
        else:
            status_by_goal[g["goal_id"]] = "健全"

    graph_path = (
        render_direction_graph(pm_conn, named_clusters, status_by_goal)
        if include_graph else None
    )

    decision_content = {
        d["id"]: d["content"] for c in clusters for d in c["decisions"]
    }

    # ---- 所見 ----
    lines.append("## 所見（機械的検出。各項目はIDに辿れる。是正判断はPMが行う）")
    lines.append("")
    n_findings = 0

    for g in stagnation:
        if not g["status"]:
            continue
        n_findings += 1
        flow = ", ".join(f"{m}:{n}件" for m, n in g["monthly"].items()) or "直近6ヶ月 0件"
        lines.append(
            f"- **{g['status']}** {goal_label(g['goal_id'])}: 重み={g['weight']}, "
            f"全期間{g['total']}件, 直近{g['recent_days']}日{g['recent']}件（{flow}）"
        )

    for v in violations:
        n_findings += 1
        lines.append(
            f"- **制約違反疑い** {goal_label(v['goal_id'])}: "
            f"{_content_snippet(int(v['decision_id']), v['content'])} — {v['rationale'] or '（根拠未記載）'}"
        )

    for ib in issue_blocks:
        if not ib["blocked_decisions"]:
            continue
        n_findings += 1
        lines.append(
            f"- **論点ブロック** {ib['issue_id']}（未解決、担当={ib['owner'] or '未割当'}）のブロック対象領域で"
            f"決定{len(ib['blocked_decisions'])}件が進行:"
        )
        for d in ib["blocked_decisions"]:
            lines.append(f"  - {_content_snippet(d['id'], d['content'])}")

    for c in conflicts:
        n_findings += 1
        ids_text = "; ".join(
            _content_snippet(i, decision_content.get(i)) for i in c["decision_ids"]
        )
        lines.append(
            f"- **トレードオフ衝突** {goal_label(c['goal_id'])}: {ids_text} — {c['reason']}"
        )

    for a in health:
        if a["confidence"] == "low":
            n_findings += 1
            lines.append(
                f"- **前提要レビュー** 前提#{a['id']}「{(a['content'] or '')[:50]}…」: "
                f"確信度low、依拠決定{a['n_dependents']}件が影響範囲"
            )

    if n_findings == 0:
        lines.append("（所見なし — 検出器5種（停滞・制約違反疑い・論点ブロック・トレードオフ衝突・前提要レビュー）すべて該当なし）")
    lines.append("")

    # ---- 目標別エグゼクティブサマリー ----
    summary = build_executive_summary(
        pm_conn, stagnation, conflicts, violations, issue_blocks, named_clusters
    ) if use_llm_naming else None
    if summary:
        lines.append("## 目標別エグゼクティブサマリー（Argusによる言い換えと提案 — 是正判断はPMが行う）")
        lines.append("")
        lines.append(summary)
        lines.append("")

    # ---- 前提の健全性 ----
    if health:
        lines.append("## 前提の健全性")
        lines.append("")
        for a in health:
            reviewed = (a["last_reviewed_at"] or "未確認")[:10]
            lines.append(
                f"- 前提#{a['id']}「{(a['content'] or '')[:60]}…」: "
                f"確信度={a['confidence'] or '-'}, 最終確認={reviewed}, 依拠決定={a['n_dependents']}件"
            )
        lines.append("")

    # ---- 識別要件への直接貢献決定 ----
    lines.append("## 識別要件への直接貢献決定（選別ゲート通過分のみ）")
    lines.append("")
    cluster_idx_by_goal: dict[str, int] = {}
    identifying_ids = {g["goal_id"] for g in stagnation}
    shown_any = False
    for c in clusters:
        if c["goal_id"] not in identifying_ids:
            continue
        shown_any = True
        idx = cluster_idx_by_goal.get(c["goal_id"], 0)
        cluster_idx_by_goal[c["goal_id"]] = idx + 1
        info = named_clusters.get(c["goal_id"], [])[idx]
        label = info["name"] or "（名称未設定）"
        assumption_note = (
            f"、前提 {c['assumption_ids']} に依拠" if c["assumption_ids"] else ""
        )
        lines.append(f"### {label}")
        lines.append(f"目標: {goal_label(c['goal_id'])}{assumption_note}")
        if info["summary"]:
            lines.append(f"要約: {info['summary']}")
        for d in c["decisions"]:
            lines.append(f"- d:{d['id']} {d['content']}")
        lines.append("")
    if not shown_any:
        lines.append("（識別要件への直接貢献決定はまだありません）")
        lines.append("")

    lines.append(
        "---\n_機械は所見の検出（停滞・制約違反疑い・論点ブロック・トレードオフ衝突・前提健全性）までを担う。"
        "是正判断はプロジェクト管理者が行う（設計書§2「Δの是正は人が行う」）。_"
    )
    return "\n".join(lines), graph_path
