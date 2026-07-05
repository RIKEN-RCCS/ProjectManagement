#!/usr/bin/env python3
"""
enrich_items.py — ナレッジ文脈付きエンリッチメントエンジン

pm.db の既存 decisions / action_items に対し、過去ナレッジ（pm.db 構造化データ +
FTS5 全文検索）を参照して判断者・根拠・関連 ID を補完する。

2パスアーキテクチャの Pass 2 に相当する。

使い方:
  # dry-run（標準出力のみ、DB更新なし）
  python3 scripts/enrich_items.py --dry-run --since 2026-03-01

  # 特定IDのみ
  python3 scripts/enrich_items.py --id d:42 a:15 --dry-run

  # 実行（pm.db更新）
  python3 scripts/enrich_items.py --since 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import init_pm_db, open_pm_db

from enrich.knowledge_context import (
    extract_topic_keywords,
    fetch_fts_context,
    fetch_participant_patterns,
    fetch_recent_knowledge,
    format_knowledge_for_prompt,
)

# ---------------------------------------------------------------------------
# 参加者リスト（バリデーション用）
# ---------------------------------------------------------------------------
_VALID_NAMES: set[str] | None = None


def _load_valid_names() -> set[str]:
    global _VALID_NAMES
    if _VALID_NAMES is not None:
        return _VALID_NAMES

    names: set[str] = set()
    project_md = _REPO_ROOT / "docs" / "project.md"
    if not project_md.exists():
        _VALID_NAMES = names
        return names

    content = project_md.read_text(encoding="utf-8")

    # ステークホルダー・参加者セクションのみを対象にする
    participant_section = []
    capture = False
    for line in content.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者)", line):
            capture = True
            continue
        if capture and re.match(r"^###\s+", line):
            capture = False
        if capture:
            participant_section.append(line)
    section_text = "\n".join(participant_section)

    # "- 姓 名 English_Name email:" パターン（日本語名）
    for m in re.finditer(r"^- ([^\s]+(?:[\s　])[^\s]+)\s", section_text, re.MULTILINE):
        raw = m.group(1)
        full = raw.replace("　", "").replace(" ", "")
        names.add(full)
        parts = re.split(r"[\s　]+", raw)
        if parts:
            names.add(parts[0].strip())
    # 英語名
    for m in re.finditer(r"^- .+?\s([A-Z][a-z]+ [A-Z][a-z]+)\s", section_text, re.MULTILINE):
        names.add(m.group(1))
        names.add(m.group(1).split()[1])  # Last name
    # 組織名
    for org in ["NVIDIA", "富士通", "理研"]:
        names.add(org)

    _VALID_NAMES = names
    return names


def _validate_name(name: str | None) -> str | None:
    """名前が参加者リストに存在するか検証する。"""
    if not name:
        return None
    valid = _load_valid_names()
    if not valid:
        return name  # リストが空なら検証スキップ
    # 正規化して照合
    cleaned = name.replace(" ", "").replace("　", "")
    if cleaned in valid:
        return name
    # 姓のみで照合
    parts = re.split(r"[\s　]+", name)
    if parts and parts[0] in valid:
        return name
    return None


# ---------------------------------------------------------------------------
# エンリッチメントプロンプト
# ---------------------------------------------------------------------------
# 台帳判定ルール（設計書§4 選別ゲート・§7 貢献/制約/論点）。
# ENRICH_DECISION_PROMPT（新規決定のPass 2自動エンリッチ）と
# REGRADE_LEDGER_PROMPT（--ledger-regrade 遡及一括再判定）で共有し、判定基準の
# 乖離を防ぐ。2026-07-05 の抜本見直しで導入（経緯は LOG.md）。
LEDGER_JUDGMENT_RULES = """\
A. **選別ゲート (ledger_gate)**: 台帳は活動のログではなく「選択の地図」である。
   次の3問のいずれかに「はい」なら "decision"（台帳対象）、すべて「いいえ」なら
   "trace"（作業の痕跡）と判定する:
   1. この決定を覆すと、他の作業のやり直しが生じるか
   2. この決定は選択肢を排除するか（採らなかった案が存在するか）
   3. この決定は資源や方向を確定させるか
   「次の自明な手順」「単なる作業の割り当て・日程・事務手続き」は "trace"。
   判定理由を ledger_gate_reason に1文で記す。

B. **貢献先 (contributes_to_goals)**: ledger_gate="decision" の場合のみ判定する
   （"trace" なら空配列）。下記「貢献先候補」のうち、この決定がその要件の
   **定義そのものに直接**資するものだけを選ぶ。判定基準:「この決定の内容を、
   その要件の達成・前進の事例として報告書に書けるか」。書けないなら選ばない。
   **大半の決定はどれにも該当しない（空配列）のが正常**である。運営・契約・体制・
   文書管理・広報などの決定は、それ自体が要件の技術的内容を進めない限り該当しない。

C. **依拠前提 (depends_on_assumptions)**: 反実仮想テストで判定する —
   「下記の前提が明日崩れたと仮定したとき、この決定は見直し・やり直しが必要になるか」。
   必要になる場合のみ選ぶ。話題が近い・関係者が同じ、だけでは選ばない。
   該当なし（空配列）が正常。

D. **制約違反疑い (constraint_flags)**: 下記「制約一覧」の検査句に照らし、この決定が
   制約の境界を侵している**疑いがある場合のみ** {"goal_id": "...", "reason": "疑いの根拠1文"}
   を挙げる。「違反していない」「疑いは低い」「むしろ制約に沿っている」と判断した場合は
   **挙げない**（reason に否定的判断を書いてまで挙げない）。確定判定ではなく疑いの提示で
   あり、最終判断はプロジェクト管理者が行う。該当なしが正常。

E. **論点ブロック該当 (blocked_by_issues)**: 下記「未解決論点一覧」のブロック対象領域に
   該当する決定であれば、その issue_id を挙げる。該当なしが正常。
"""

ENRICH_DECISION_PROMPT = """あなたは富岳NEXTプロジェクトのPMアシスタントです。
以下の決定事項について、過去のナレッジを参照して情報を補完してください。

## 補完対象（決定事項）
ID: d:{id}
内容: {content}
決定日: {decided_at}
出典: {source_context}
出典参照: {source_ref}

## 過去のナレッジ
{knowledge}

## プロジェクト文脈
{project_context}

## 補完ルール

1. **判断者 (decided_by)**: この決定を下した人物（最終的な意思決定者）。
   - 元テキストや出典に名前が明示されていれば、その名前を使い confidence: "explicit" とする
   - 明示されていなければ、過去のナレッジ（担当者パターン・関連する過去の決定）から推測し confidence: "inferred" とする。推測の根拠を evidence に具体的に記載する
   - 推測できない場合は null

2. **根拠 (rationale)**: なぜこの決定がなされたか。
   - 出典 (source_context) の内容をベースに、過去の関連決定・議論から文脈を補完して2-3文で記述
   - 「関連ドキュメント・公開情報」セクションに該当資料があれば、「〇〇設計書に準拠」のように明示的に引用する
   - 単に content を繰り返すだけの記述は不可

3. **関連ID (related_ids)**: この決定に関連する過去の決定事項・アクションアイテムのID。
   - 同じトピックの過去の決定 → "d:{{id}}" 形式
   - この決定によって発生/影響を受けるアクションアイテム → "a:{{id}}" 形式
   - 過去のナレッジに表示されたIDの中から選択すること（存在しないIDを生成しない）

4. **推測不能なら null**。無理に埋めない。正確さが最優先。

5. **台帳判定**: 以下のルールに従い、選別ゲート・貢献先・依拠前提・制約違反疑い・
   論点ブロック該当を判定する。一覧に無い ID を生成しないこと。

{ledger_rules}

## 貢献先候補（識別要件・前提条件のみ。最上位目標・制約は候補に含まれない）
{ledger_goals}

## 制約一覧（違反検査の対象）
{ledger_constraints}

## 台帳前提一覧（依拠先の候補）
{ledger_assumptions}

## 未解決論点一覧
{ledger_issues}

## 出力（JSON のみ、説明不要）

```json
{{
  "decided_by": "名前 or null",
  "decided_by_confidence": "explicit or inferred or null",
  "rationale": "根拠2-3文 or null",
  "related_ids": ["d:42", "a:15"],
  "ledger_gate": "decision or trace",
  "ledger_gate_reason": "判定理由1文",
  "contributes_to_goals": ["G-PHYS"],
  "depends_on_assumptions": [1, 3],
  "constraint_flags": [{{"goal_id": "C-SOVEREIGN", "reason": "疑いの根拠1文"}}],
  "blocked_by_issues": ["Q-FP64"],
  "evidence": "推測の根拠説明（inferred時のみ、それ以外はnull）"
}}
```
"""

ENRICH_ACTION_ITEM_PROMPT = """あなたは富岳NEXTプロジェクトのPMアシスタントです。
以下のアクションアイテムについて、過去のナレッジを参照して情報を補完してください。

## 補完対象（アクションアイテム）
ID: a:{id}
内容: {content}
担当者: {assignee}
期限: {due_date}
マイルストーン: {milestone_id}
出典参照: {source_ref}

## 過去のナレッジ
{knowledge}

## プロジェクト文脈
{project_context}

## 補完ルール

1. **依頼者 (requested_by)**: このタスクを依頼・指示した人物（担当者ではなく意思決定者）。
   - 元テキストや出典に名前が明示されていれば confidence: "explicit"
   - 過去のナレッジから推測すれば confidence: "inferred"、根拠を evidence に記載
   - 推測できない場合は null

2. **根拠 (rationale)**: なぜこのタスクが必要か。
   - 関連する過去の決定・議論から文脈を補完して2-3文で記述
   - 「関連ドキュメント・公開情報」セクションに該当資料があれば、「〇〇設計書に基づく」のように明示的に引用する
   - content の繰り返しは不可

3. **出典文脈 (source_context)**: このタスクが発生した背景の要約（1-2文）。
   - 関連する議論・決定の経緯を簡潔にまとめる

4. **関連ID (related_ids)**: 過去のナレッジに表示されたIDから選択。

5. **推測不能なら null**。

## 出力（JSON のみ、説明不要）

```json
{{
  "requested_by": "名前 or null",
  "requested_by_confidence": "explicit or inferred or null",
  "rationale": "根拠2-3文 or null",
  "source_context": "背景1-2文 or null",
  "related_ids": ["d:42", "a:15"],
  "evidence": "推測の根拠説明（inferred時のみ、それ以外はnull）"
}}
```
"""

# --ledger-regrade（遡及一括再判定）用の軽量プロンプト。decided_by/rationale 等の
# 既存エンリッチ結果には触れず、台帳判定5項目のみを再判定する。判定ルール本体は
# LEDGER_JUDGMENT_RULES を ENRICH_DECISION_PROMPT と共有する。
REGRADE_LEDGER_PROMPT = """あなたは富岳NEXTプロジェクトのPMアシスタントです。
以下の決定事項について、前提・意思決定台帳（有向グラフ）への取り込み判定を行ってください。

## 判定対象（決定事項）
ID: d:{id}
内容: {content}
決定日: {decided_at}
根拠: {rationale}
背景: {source_context}
捨てた案: {trade_off}

## 台帳判定ルール

{ledger_rules}

## 貢献先候補（識別要件・前提条件のみ。最上位目標・制約は候補に含まれない）
{ledger_goals}

## 制約一覧（違反検査の対象）
{ledger_constraints}

## 台帳前提一覧（依拠先の候補）
{ledger_assumptions}

## 未解決論点一覧
{ledger_issues}

## 出力（JSON のみ、説明不要）

```json
{{
  "ledger_gate": "decision or trace",
  "ledger_gate_reason": "判定理由1文",
  "contributes_to_goals": [],
  "depends_on_assumptions": [],
  "constraint_flags": [],
  "blocked_by_issues": []
}}
```
"""


# ---------------------------------------------------------------------------
# エンリッチメントコア
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output: {text[:300]}")


def _validate_related_ids(ids: list, pm_conn) -> list[str]:
    """related_ids が pm.db に実在するか検証し、有効なもののみ返す。"""
    valid = []
    for ref in ids:
        if not isinstance(ref, str):
            continue
        m = re.match(r"^(d|a):(\d+)$", ref)
        if not m:
            continue
        table = "decisions" if m.group(1) == "d" else "action_items"
        row = pm_conn.execute(
            f"SELECT id FROM {table} WHERE id = ?", (int(m.group(2)),)
        ).fetchone()
        if row:
            valid.append(ref)
    return valid


# 貢献先候補に許す layer。最上位目標（top）は「すべての決定が適合を主張できる目標は
# 目標として機能しない」（設計書§7）ため直接貢献を禁止。制約（constraint）は貢献先では
# なく違反検査の対象（同§7）。TS を残すのは「規模のみで競う決定が最上位目標への貢献
# 判定から自動的に除外される」（同§8）ため。
_CONTRIBUTES_ALLOWED_LAYERS = ("identifying", "tablestakes")

_LAYER_LABELS_FOR_PROMPT = {
    "identifying": "識別要件",
    "tablestakes": "前提条件（規模・スループット等、商用クラスタも掲げうる必達水準）",
}


def _fetch_ledger_goals_for_prompt(pm_conn) -> str:
    """貢献先候補（識別要件+前提条件のみ）をプロンプト向けに整形する。"""
    try:
        rows = pm_conn.execute(
            "SELECT goal_id, layer, name FROM ledger_goals"
            " WHERE COALESCE(state,'active')='active'"
            f"   AND layer IN ({','.join('?' * len(_CONTRIBUTES_ALLOWED_LAYERS))})"
            " ORDER BY layer, goal_id",
            _CONTRIBUTES_ALLOWED_LAYERS,
        ).fetchall()
    except Exception:
        return "（台帳未投入）"
    if not rows:
        return "（台帳未投入）"
    return "\n".join(
        f"- {r['goal_id']}（{_LAYER_LABELS_FOR_PROMPT.get(r['layer'], r['layer'])}）: {r['name']}"
        for r in rows
    )


def _fetch_ledger_constraints_for_prompt(pm_conn) -> str:
    """制約（C-*）と検査句をプロンプト向けに整形する（違反検査の対象一覧）。"""
    try:
        rows = pm_conn.execute(
            "SELECT goal_id, name, identification_test FROM ledger_goals"
            " WHERE COALESCE(state,'active')='active' AND layer = 'constraint'"
            " ORDER BY goal_id"
        ).fetchall()
    except Exception:
        return "（台帳未投入）"
    if not rows:
        return "（制約未登録）"
    return "\n".join(
        f"- {r['goal_id']}: {r['name']} — 検査: {r['identification_test'] or '（検査句未登録）'}"
        for r in rows
    )


def _fetch_ledger_issues_for_prompt(pm_conn) -> str:
    """未解決論点（open）をプロンプト向けに整形する（ブロック該当判定の対象一覧）。"""
    try:
        rows = pm_conn.execute(
            "SELECT issue_id, content FROM ledger_issues"
            " WHERE COALESCE(state,'open') = 'open' ORDER BY issue_id"
        ).fetchall()
    except Exception:
        return "（台帳未投入）"
    if not rows:
        return "（未解決論点なし）"
    return "\n".join(f"- {r['issue_id']}: {r['content']}" for r in rows)


def _validate_ledger_goal_ids(ids: list, pm_conn, *, layers: tuple = _CONTRIBUTES_ALLOWED_LAYERS) -> list[str]:
    """goal_id が指定 layer の ledger_goals に実在するか検証し、有効なもののみ返す。"""
    valid = []
    for ref in ids or []:
        if not isinstance(ref, str):
            continue
        row = pm_conn.execute(
            f"SELECT goal_id FROM ledger_goals WHERE goal_id = ?"
            f" AND layer IN ({','.join('?' * len(layers))})",
            (ref, *layers),
        ).fetchone()
        if row:
            valid.append(ref)
    return valid


def _validate_constraint_flags(flags: list, pm_conn) -> list[dict]:
    """constraint_flags の goal_id が制約（layer='constraint'）に実在するか検証する。"""
    valid = []
    for flag in flags or []:
        if not isinstance(flag, dict):
            continue
        goal_id = flag.get("goal_id")
        if not isinstance(goal_id, str):
            continue
        if _validate_ledger_goal_ids([goal_id], pm_conn, layers=("constraint",)):
            valid.append({"goal_id": goal_id, "reason": str(flag.get("reason") or "")[:200]})
    return valid


def _validate_issue_ids(ids: list, pm_conn) -> list[str]:
    """blocked_by_issues が open な ledger_issues に実在するか検証する。"""
    valid = []
    for ref in ids or []:
        if not isinstance(ref, str):
            continue
        row = pm_conn.execute(
            "SELECT issue_id FROM ledger_issues WHERE issue_id = ?"
            " AND COALESCE(state,'open') = 'open'",
            (ref,),
        ).fetchone()
        if row:
            valid.append(ref)
    return valid


def _write_decision_goal_edges(pm_conn, decision_id: int, goal_ids: list[str]) -> None:
    """決定 → 目標 の contributes 辺を ledger_edges に UPSERT する。"""
    now = datetime.now().isoformat()
    for goal_id in goal_ids:
        pm_conn.execute(
            """
            INSERT INTO ledger_edges
                (edge_type, from_kind, from_id, to_kind, to_id, source, state, created_at)
            VALUES ('contributes', 'decision', ?, 'goal', ?, 'enrich', 'active', ?)
            ON CONFLICT(edge_type, from_kind, from_id, to_kind, to_id) DO NOTHING
            """,
            (str(decision_id), goal_id, now),
        )


def _fetch_ledger_assumptions_for_prompt(pm_conn) -> str:
    """ledger_assumptions をプロンプト向けに整形する（依拠先辺 depends_on の候補一覧）。"""
    try:
        rows = pm_conn.execute(
            "SELECT id, content, monitor_target FROM ledger_assumptions"
            " WHERE COALESCE(state,'active')='active' ORDER BY id"
        ).fetchall()
    except Exception:
        return "（台帳未投入）"
    if not rows:
        return "（台帳未投入）"
    return "\n".join(
        f"- {r['id']}: {r['content']}"
        + (f"（監視対象: {r['monitor_target']}）" if r["monitor_target"] else "")
        for r in rows
    )


def _validate_ledger_assumption_ids(ids: list, pm_conn) -> list[int]:
    """depends_on_assumptions が ledger_assumptions に実在するか検証し、有効なもののみ返す。"""
    valid = []
    for ref in ids or []:
        try:
            aid = int(ref)
        except (TypeError, ValueError):
            continue
        row = pm_conn.execute(
            "SELECT id FROM ledger_assumptions WHERE id = ?", (aid,)
        ).fetchone()
        if row:
            valid.append(aid)
    return valid


def _write_decision_assumption_edges(pm_conn, decision_id: int, assumption_ids: list[int]) -> None:
    """決定 → 前提 の depends_on 辺を ledger_edges に UPSERT する。"""
    now = datetime.now().isoformat()
    for assumption_id in assumption_ids:
        pm_conn.execute(
            """
            INSERT INTO ledger_edges
                (edge_type, from_kind, from_id, to_kind, to_id, source, state, created_at)
            VALUES ('depends_on', 'decision', ?, 'assumption', ?, 'enrich', 'active', ?)
            ON CONFLICT(edge_type, from_kind, from_id, to_kind, to_id) DO NOTHING
            """,
            (str(decision_id), str(assumption_id), now),
        )


def _apply_ledger_judgment(pm_conn, decision_id: int, result: dict) -> None:
    """台帳判定の結果（ゲート+4種の辺）を保存する。

    冪等性: この決定に紐づく enrich 由来の辺を全て消してから書き直す。
    手動・シード由来の辺（source が 'enrich' 以外）は消さない。
    gate='trace'（作業の痕跡）の場合は辺を一切張らない（設計書§4:
    荷重を持つ決定だけを台帳へ取り込む）。
    """
    gate = result.get("ledger_gate")
    if gate not in ("decision", "trace"):
        gate = None
    pm_conn.execute(
        "UPDATE decisions SET ledger_gate = ?, ledger_gate_reason = ? WHERE id = ?",
        (gate, result.get("ledger_gate_reason"), decision_id),
    )

    pm_conn.execute(
        "DELETE FROM ledger_edges WHERE from_kind='decision' AND from_id=? AND source='enrich'",
        (str(decision_id),),
    )
    pm_conn.execute(
        "DELETE FROM ledger_edges WHERE to_kind='decision' AND to_id=? AND source='enrich'",
        (str(decision_id),),
    )

    if gate != "decision":
        return

    if result.get("contributes_to_goals"):
        _write_decision_goal_edges(pm_conn, decision_id, result["contributes_to_goals"])
    if result.get("depends_on_assumptions"):
        _write_decision_assumption_edges(pm_conn, decision_id, result["depends_on_assumptions"])

    now = datetime.now().isoformat()
    for flag in result.get("constraint_flags") or []:
        pm_conn.execute(
            """
            INSERT INTO ledger_edges
                (edge_type, from_kind, from_id, to_kind, to_id, source, rationale, state, created_at)
            VALUES ('may_violate', 'decision', ?, 'goal', ?, 'enrich', ?, 'active', ?)
            ON CONFLICT(edge_type, from_kind, from_id, to_kind, to_id) DO NOTHING
            """,
            (str(decision_id), flag["goal_id"], flag.get("reason"), now),
        )
    for issue_id in result.get("blocked_by_issues") or []:
        pm_conn.execute(
            """
            INSERT INTO ledger_edges
                (edge_type, from_kind, from_id, to_kind, to_id, source, state, created_at)
            VALUES ('blocks', 'issue', ?, 'decision', ?, 'enrich', 'active', ?)
            ON CONFLICT(edge_type, from_kind, from_id, to_kind, to_id) DO NOTHING
            """,
            (issue_id, str(decision_id), now),
        )


def _is_trivial_rationale(rationale: str | None, content: str) -> bool:
    """rationale が content の実質コピーかどうかを簡易判定する。"""
    if not rationale:
        return True
    r = rationale.replace(" ", "").replace("。", "").replace("、", "")
    c = content.replace(" ", "").replace("。", "").replace("、", "")
    if len(r) < 10:
        return True
    # 80% 以上の文字が content に含まれていたらコピーとみなす
    overlap = sum(1 for ch in r if ch in c)
    return overlap / len(r) > 0.8 if r else True


def enrich_decision(
    item: dict,
    knowledge_text: str,
    project_context: str,
    pm_conn,
) -> dict:
    prompt = ENRICH_DECISION_PROMPT.format(
        id=item["id"],
        content=item["content"],
        decided_at=item.get("decided_at") or "不明",
        source_context=item.get("source_context") or "なし",
        source_ref=item.get("source_ref") or "なし",
        knowledge=knowledge_text,
        project_context=project_context,
        ledger_rules=LEDGER_JUDGMENT_RULES,
        ledger_goals=_fetch_ledger_goals_for_prompt(pm_conn),
        ledger_constraints=_fetch_ledger_constraints_for_prompt(pm_conn),
        ledger_assumptions=_fetch_ledger_assumptions_for_prompt(pm_conn),
        ledger_issues=_fetch_ledger_issues_for_prompt(pm_conn),
    )

    try:
        raw = call_argus_llm(prompt, timeout=300)
        result = _extract_json(raw)
    except Exception as e:
        return {"error": str(e)}

    # バリデーション
    decided_by = result.get("decided_by")
    confidence = result.get("decided_by_confidence")
    if confidence == "inferred":
        decided_by = _validate_name(decided_by)
        if decided_by is None:
            confidence = None
            result["evidence"] = None

    related_ids = _validate_related_ids(result.get("related_ids") or [], pm_conn)

    rationale = result.get("rationale")
    if _is_trivial_rationale(rationale, item["content"]):
        rationale = None

    return {
        "decided_by": decided_by,
        "decided_by_confidence": confidence,
        "rationale": rationale,
        "related_ids": related_ids,
        "evidence": result.get("evidence"),
        **_validate_ledger_judgment(result, pm_conn),
    }


def _validate_ledger_judgment(result: dict, pm_conn) -> dict:
    """LLM出力の台帳判定5項目をバリデーションして正規化する。

    ENRICH_DECISION_PROMPT（Pass 2自動エンリッチ）と REGRADE_LEDGER_PROMPT
    （--ledger-regrade）の両方から使う。
    """
    gate = result.get("ledger_gate")
    if gate not in ("decision", "trace"):
        gate = None
    return {
        "ledger_gate": gate,
        "ledger_gate_reason": (str(result.get("ledger_gate_reason") or "")[:300] or None),
        "contributes_to_goals": _validate_ledger_goal_ids(
            result.get("contributes_to_goals") or [], pm_conn
        ),
        "depends_on_assumptions": _validate_ledger_assumption_ids(
            result.get("depends_on_assumptions") or [], pm_conn
        ),
        "constraint_flags": _validate_constraint_flags(
            result.get("constraint_flags") or [], pm_conn
        ),
        "blocked_by_issues": _validate_issue_ids(
            result.get("blocked_by_issues") or [], pm_conn
        ),
    }


def enrich_action_item(
    item: dict,
    knowledge_text: str,
    project_context: str,
    pm_conn,
) -> dict:
    prompt = ENRICH_ACTION_ITEM_PROMPT.format(
        id=item["id"],
        content=item["content"],
        assignee=item.get("assignee") or "未定",
        due_date=item.get("due_date") or "なし",
        milestone_id=item.get("milestone_id") or "なし",
        source_ref=item.get("source_ref") or "なし",
        knowledge=knowledge_text,
        project_context=project_context,
    )

    try:
        raw = call_argus_llm(prompt, timeout=300)
        result = _extract_json(raw)
    except Exception as e:
        return {"error": str(e)}

    requested_by = result.get("requested_by")
    confidence = result.get("requested_by_confidence")
    if confidence == "inferred":
        requested_by = _validate_name(requested_by)
        if requested_by is None:
            confidence = None
            result["evidence"] = None

    related_ids = _validate_related_ids(result.get("related_ids") or [], pm_conn)

    rationale = result.get("rationale")
    if _is_trivial_rationale(rationale, item["content"]):
        rationale = None

    source_context = result.get("source_context")
    if source_context and _is_trivial_rationale(source_context, item["content"]):
        source_context = None

    return {
        "requested_by": requested_by,
        "requested_by_confidence": confidence,
        "rationale": rationale,
        "source_context": source_context,
        "related_ids": related_ids,
        "evidence": result.get("evidence"),
    }


# ---------------------------------------------------------------------------
# バッチ処理
# ---------------------------------------------------------------------------

def enrich_batch(
    pm_conn,
    decisions: list[dict],
    action_items: list[dict],
    *,
    project_context: str,
    config_path: Path | None = None,
    dry_run: bool = False,
    log=print,
) -> tuple[list[dict], list[dict]]:
    """decisions と action_items をバッチエンリッチする。

    Returns:
        (enriched_decisions, enriched_action_items)
        各要素は元の dict に enrichment フィールドを追加したもの。
    """
    all_content = [d["content"] for d in decisions] + [a["content"] for a in action_items]
    if not all_content:
        return [], []

    log("[INFO] ナレッジ取得中...")

    # キーワード抽出
    all_keywords: list[str] = []
    seen: set[str] = set()
    for content in all_content:
        for kw in extract_topic_keywords(content):
            if kw not in seen:
                seen.add(kw)
                all_keywords.append(kw)
    all_keywords = all_keywords[:20]

    # ナレッジ取得
    structured = fetch_recent_knowledge(pm_conn, all_keywords)
    fts_chunks = fetch_fts_context(all_keywords, config_path=config_path)
    patterns = fetch_participant_patterns(pm_conn)

    knowledge = {
        "decisions": structured["decisions"],
        "action_items": structured["action_items"],
        "fts_chunks": fts_chunks,
        "participant_patterns": patterns,
    }
    knowledge_text = format_knowledge_for_prompt(knowledge)

    log(f"[INFO] ナレッジ: decisions={len(structured['decisions'])}件, "
        f"AI={len(structured['action_items'])}件, FTS={len(fts_chunks)}チャンク")

    # エンリッチ
    enriched_decisions: list[dict] = []
    for i, d in enumerate(decisions, 1):
        log(f"\n[d:{d['id']}] ({i}/{len(decisions)}) {d['content'][:60]}...")
        result = enrich_decision(d, knowledge_text, project_context, pm_conn)

        if result.get("error"):
            log(f"  [WARN] エンリッチ失敗: {result['error']}")
            enriched_decisions.append({**d, "_enrichment": None})
            continue

        _print_enrichment(result, "decision", log)
        enriched_decisions.append({**d, "_enrichment": result})

        if not dry_run:
            _save_decision_enrichment(pm_conn, d["id"], result)
            _apply_ledger_judgment(pm_conn, d["id"], result)

    enriched_ais: list[dict] = []
    for i, a in enumerate(action_items, 1):
        log(f"\n[a:{a['id']}] ({i}/{len(action_items)}) {a['content'][:60]}...")
        result = enrich_action_item(a, knowledge_text, project_context, pm_conn)

        if result.get("error"):
            log(f"  [WARN] エンリッチ失敗: {result['error']}")
            enriched_ais.append({**a, "_enrichment": None})
            continue

        _print_enrichment(result, "action_item", log)
        enriched_ais.append({**a, "_enrichment": result})

        if not dry_run:
            _save_action_item_enrichment(pm_conn, a["id"], result)

    if not dry_run:
        pm_conn.commit()

    return enriched_decisions, enriched_ais


def _print_enrichment(result: dict, item_type: str, log=print):
    if item_type == "decision":
        by = result.get("decided_by")
        conf = result.get("decided_by_confidence")
        if by:
            log(f"  decided_by  : {by} ({conf})")
    else:
        by = result.get("requested_by")
        conf = result.get("requested_by_confidence")
        if by:
            log(f"  requested_by: {by} ({conf})")

    if result.get("evidence"):
        log(f"  evidence    : {result['evidence']}")
    if result.get("rationale"):
        log(f"  rationale   : {result['rationale']}")
    if result.get("source_context"):
        log(f"  context     : {result['source_context']}")
    if result.get("related_ids"):
        log(f"  related_ids : {result['related_ids']}")
    if result.get("ledger_gate"):
        log(f"  ledger_gate : {result['ledger_gate']}"
            + (f" — {result['ledger_gate_reason']}" if result.get("ledger_gate_reason") else ""))
    if result.get("contributes_to_goals"):
        log(f"  ledger貢献先: {result['contributes_to_goals']}")
    if result.get("depends_on_assumptions"):
        log(f"  ledger依拠先: {result['depends_on_assumptions']}")
    if result.get("constraint_flags"):
        log(f"  制約違反疑い: {result['constraint_flags']}")
    if result.get("blocked_by_issues"):
        log(f"  論点ブロック: {result['blocked_by_issues']}")


def _save_decision_enrichment(pm_conn, decision_id: int, result: dict):
    # rationale は流入（モードA: 決定確定時の直接捕捉、鮮度が高い）が既に埋めている場合がある。
    # エンリッチ（モードB相当: 遡及的再構成、確度が低い）は、既存値が空のときだけ補完する。
    # 新しい推測値が存在するからといって、既に確定済みの値を上書きしてはならない。
    pm_conn.execute(
        """UPDATE decisions SET
               decided_by = ?,
               decided_by_confidence = ?,
               rationale = CASE
                   WHEN rationale IS NULL OR TRIM(rationale) = '' THEN ?
                   ELSE rationale
               END,
               related_ids = ?
           WHERE id = ?""",
        (
            result.get("decided_by"),
            result.get("decided_by_confidence"),
            result.get("rationale"),
            json.dumps(result.get("related_ids", []), ensure_ascii=False),
            decision_id,
        ),
    )


def _save_action_item_enrichment(pm_conn, ai_id: int, result: dict):
    pm_conn.execute(
        """UPDATE action_items SET
               requested_by = ?,
               requested_by_confidence = ?,
               rationale = ?,
               source_context = ?,
               related_ids = ?
           WHERE id = ?""",
        (
            result.get("requested_by"),
            result.get("requested_by_confidence"),
            result.get("rationale"),
            result.get("source_context"),
            json.dumps(result.get("related_ids", []), ensure_ascii=False),
            ai_id,
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fetch_target_items(
    pm_conn, *, since: str | None = None, item_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """対象の decisions / action_items を取得する。"""
    decisions: list[dict] = []
    action_items: list[dict] = []

    if item_ids:
        for ref in item_ids:
            m = re.match(r"^(d|a):(\d+)$", ref)
            if not m:
                print(f"[WARN] 不正なID形式: {ref}（d:N or a:N）", file=sys.stderr)
                continue
            table = "decisions" if m.group(1) == "d" else "action_items"
            row = pm_conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (int(m.group(2)),)
            ).fetchone()
            if row:
                (decisions if m.group(1) == "d" else action_items).append(dict(row))
            else:
                print(f"[WARN] 見つかりません: {ref}", file=sys.stderr)
        return decisions, action_items

    where_clause = "COALESCE(deleted, 0) = 0"
    params: list = []
    if since:
        where_clause += " AND decided_at >= ?"
        params.append(since)

    rows = pm_conn.execute(
        f"SELECT * FROM decisions WHERE {where_clause} ORDER BY decided_at DESC",
        params,
    ).fetchall()
    decisions = [dict(r) for r in rows]

    where_clause2 = "COALESCE(deleted, 0) = 0"
    params2: list = []
    if since:
        where_clause2 += " AND extracted_at >= ?"
        params2.append(since)

    rows = pm_conn.execute(
        f"SELECT * FROM action_items WHERE {where_clause2} ORDER BY extracted_at DESC",
        params2,
    ).fetchall()
    action_items = [dict(r) for r in rows]

    return decisions, action_items


def ledger_regrade(
    pm_conn,
    *,
    item_ids: list[str] | None = None,
    redo_all: bool = False,
    dry_run: bool = False,
    log=print,
) -> None:
    """全決定の台帳判定（ゲート+辺）のみを再実施する遡及一括モード。

    decided_by/rationale 等の既存エンリッチ結果には触れない。ナレッジ取得も行わず、
    決定自身のフィールド（content/rationale/source_context/trade_off）だけで判定する
    軽量プロンプト（REGRADE_LEDGER_PROMPT）を決定1件=LLM1呼び出しで回す。

    再開可能性: 1件ごとにコミットする。既定では ledger_gate が未設定（NULL）の決定のみを
    対象とするため、中断後に同じコマンドを再実行すれば続きから処理される。
    redo_all=True で判定済みも含めて全件やり直す。
    """
    if item_ids:
        id_nums = []
        for ref in item_ids:
            m = re.match(r"^d:(\d+)$", ref)
            if m:
                id_nums.append(int(m.group(1)))
            else:
                log(f"[WARN] --ledger-regrade では d:N 形式のみ対応: {ref}")
        if not id_nums:
            log("[INFO] 対象なし")
            return
        placeholders = ",".join("?" * len(id_nums))
        rows = pm_conn.execute(
            f"SELECT id, content, decided_at, rationale, source_context, trade_off"
            f" FROM decisions WHERE id IN ({placeholders})",
            id_nums,
        ).fetchall()
    else:
        where = "COALESCE(deleted, 0) = 0"
        if not redo_all:
            where += " AND ledger_gate IS NULL"
        rows = pm_conn.execute(
            f"SELECT id, content, decided_at, rationale, source_context, trade_off"
            f" FROM decisions WHERE {where} ORDER BY id",
        ).fetchall()

    targets = [dict(r) for r in rows]
    log(f"[INFO] 台帳再判定 対象: {len(targets)}件"
        + ("（--regrade-all: 判定済み含む）" if redo_all else "（ledger_gate未設定のみ）"))
    if not targets:
        return

    ledger_goals = _fetch_ledger_goals_for_prompt(pm_conn)
    ledger_constraints = _fetch_ledger_constraints_for_prompt(pm_conn)
    ledger_assumptions = _fetch_ledger_assumptions_for_prompt(pm_conn)
    ledger_issues = _fetch_ledger_issues_for_prompt(pm_conn)

    n_ok = n_err = 0
    for i, d in enumerate(targets, 1):
        log(f"\n[d:{d['id']}] ({i}/{len(targets)}) {(d['content'] or '')[:60]}...")
        prompt = REGRADE_LEDGER_PROMPT.format(
            id=d["id"],
            content=d["content"],
            decided_at=d.get("decided_at") or "不明",
            rationale=d.get("rationale") or "なし",
            source_context=d.get("source_context") or "なし",
            trade_off=d.get("trade_off") or "なし",
            ledger_rules=LEDGER_JUDGMENT_RULES,
            ledger_goals=ledger_goals,
            ledger_constraints=ledger_constraints,
            ledger_assumptions=ledger_assumptions,
            ledger_issues=ledger_issues,
        )
        try:
            raw = call_argus_llm(prompt, timeout=180, max_tokens=2048)
            result = _validate_ledger_judgment(_extract_json(raw), pm_conn)
        except Exception as e:
            log(f"  [WARN] 判定失敗（スキップ、再実行で再試行される）: {e}")
            n_err += 1
            continue

        _print_enrichment(result, "decision", log)
        if not dry_run:
            _apply_ledger_judgment(pm_conn, d["id"], result)
            pm_conn.commit()  # 1件ごとにコミット（中断＝ここまで確定、再実行で続きから）
        n_ok += 1

    log(f"\n[INFO] 台帳再判定 完了: 成功={n_ok}件, 失敗={n_err}件")


def main():
    parser = argparse.ArgumentParser(
        description="pm.db の既存アイテムにナレッジ文脈を付与する（Pass 2 エンリッチメント）"
    )
    parser.add_argument("--db", default="data/pm.db", help="pm.db パス")
    parser.add_argument("--since", help="この日付以降のアイテムのみ対象 (YYYY-MM-DD)")
    parser.add_argument("--id", nargs="+", dest="item_ids",
                        help="特定IDのみ (d:42 a:15 形式)")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB更新なし、結果を標準出力のみ")
    parser.add_argument("--output", help="結果をファイルにも保存")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--config", default="data/argus_config.yaml",
                        help="FTS5インデックス設定ファイル")
    parser.add_argument("--ledger-regrade", action="store_true",
                        help="台帳判定（選別ゲート+辺）のみを再実施する。decided_by等には触れない")
    parser.add_argument("--regrade-all", action="store_true",
                        help="--ledger-regrade で判定済み（ledger_gate設定済み）も含めて全件やり直す")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = _REPO_ROOT / db_path
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _REPO_ROOT / config_path

    output_file = None
    if args.output:
        output_file = open(args.output, "w", encoding="utf-8")

    def log(msg: str):
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    if db_path.exists():
        pm_conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    else:
        pm_conn = init_pm_db(db_path, no_encrypt=args.no_encrypt)

    if args.ledger_regrade:
        if args.dry_run:
            log("[INFO] --dry-run モード（DB更新なし）")
        ledger_regrade(
            pm_conn,
            item_ids=args.item_ids,
            redo_all=args.regrade_all,
            dry_run=args.dry_run,
            log=log,
        )
        pm_conn.close()
        if output_file:
            output_file.close()
            print(f"\n[INFO] 結果を {args.output} に保存しました")
        return

    decisions, action_items = _fetch_target_items(
        pm_conn, since=args.since, item_ids=args.item_ids,
    )

    log(f"[INFO] 対象: decisions={len(decisions)}件, action_items={len(action_items)}件")
    if not decisions and not action_items:
        log("[INFO] エンリッチ対象なし")
        pm_conn.close()
        return

    project_context = load_claude_md_context()

    if args.dry_run:
        log("[INFO] --dry-run モード（DB更新なし）")

    enrich_batch(
        pm_conn,
        decisions,
        action_items,
        project_context=project_context,
        config_path=config_path,
        dry_run=args.dry_run,
        log=log,
    )

    pm_conn.close()

    if output_file:
        output_file.close()
        log(f"\n[INFO] 結果を {args.output} に保存しました")


if __name__ == "__main__":
    main()
