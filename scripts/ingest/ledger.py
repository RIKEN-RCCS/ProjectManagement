#!/usr/bin/env python3
"""
ledger.py

前提・意思決定台帳（有向グラフ）の初期シード投入プラグイン。
pm_ingest.py ledger 経由で呼び出される。

設計: data/FugakuNEXT_Argus_designsheet.docx §8「初期投入シード」。
台帳エントリ型は目標・制約（ledger_goals）／前提（ledger_assumptions）／
論点（ledger_issues）の3つ + 型付き辺（ledger_edges）。決定（decisions）は
既存 pm.db テーブルを流用するため本プラグインでは扱わない。

投入元 JSON のフィールドは出所主義に基づき、未確定の重み・出所は
weight_status="provisional" / source_status="needs_source" のまま保存し、
値を推測で補わない（設計書の三原則: 出所主義・スキーマ最小化）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.ingest_plugin import IngestContext

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SEED_PATH = REPO_ROOT / "data" / "ledger_seed.json"


# --------------------------------------------------------------------------- #
# 投入コア
# --------------------------------------------------------------------------- #
def load_seed(seed_path: Path) -> dict:
    if not seed_path.exists():
        raise FileNotFoundError(f"シードファイルが見つかりません: {seed_path}")
    with seed_path.open(encoding="utf-8") as f:
        return json.load(f)


def upsert_goals(pm_conn, goals: list[dict], *, force: bool, dry_run: bool, log=print) -> int:
    """ledger_goals へ INSERT OR REPLACE。既存かつ --force 無指定ならスキップ。"""
    now = datetime.now().isoformat()
    count = 0
    for g in goals:
        existing = pm_conn.execute(
            "SELECT goal_id FROM ledger_goals WHERE goal_id = ?", (g["goal_id"],)
        ).fetchone()
        if existing and not force:
            log(f"  [SKIP] {g['goal_id']} は既に台帳に存在します（--ledger-force で上書き可能）")
            continue
        log(f"  [{'DRY' if dry_run else 'OK'}] goal {g['goal_id']}: {g['name']}")
        if dry_run:
            continue
        pm_conn.execute(
            """
            INSERT INTO ledger_goals
                (goal_id, kind, layer, is_top_goal, name, identification_test,
                 weight, weight_status, source, source_status, state,
                 created_at, last_reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(goal_id) DO UPDATE SET
                kind=excluded.kind, layer=excluded.layer, is_top_goal=excluded.is_top_goal,
                name=excluded.name, identification_test=excluded.identification_test,
                weight=excluded.weight, weight_status=excluded.weight_status,
                source=excluded.source, source_status=excluded.source_status,
                last_reviewed_at=excluded.last_reviewed_at
            """,
            (
                g["goal_id"], g.get("kind"), g.get("layer"), int(g.get("is_top_goal") or 0),
                g.get("name"), g.get("identification_test"),
                g.get("weight"), g.get("weight_status"),
                g.get("source"), g.get("source_status"),
                now, now,
            ),
        )
        count += 1
    return count


def upsert_issues(pm_conn, issues: list[dict], *, force: bool, dry_run: bool, log=print) -> int:
    now = datetime.now().isoformat()
    count = 0
    for it in issues:
        existing = pm_conn.execute(
            "SELECT issue_id FROM ledger_issues WHERE issue_id = ?", (it["issue_id"],)
        ).fetchone()
        if existing and not force:
            log(f"  [SKIP] {it['issue_id']} は既に台帳に存在します（--ledger-force で上書き可能）")
            continue
        log(f"  [{'DRY' if dry_run else 'OK'}] issue {it['issue_id']}: {it['content']}")
        if dry_run:
            continue
        pm_conn.execute(
            """
            INSERT INTO ledger_issues (issue_id, content, owner, due_date, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                content=excluded.content, owner=excluded.owner, due_date=excluded.due_date
            """,
            (it["issue_id"], it.get("content"), it.get("owner"), it.get("due_date"),
             it.get("state") or "open", now),
        )
        count += 1
    return count


def upsert_assumptions(
    pm_conn, assumptions: list[dict], *, force: bool, dry_run: bool, log=print
) -> int:
    """ledger_assumptions へ投入する。goal_id/issue_id のような自然キーが無いため、
    content の完全一致を既存判定キーとして使う（--ledger-force で上書き）。
    """
    now = datetime.now().isoformat()
    count = 0
    for a in assumptions:
        content = a.get("content")
        existing = pm_conn.execute(
            "SELECT id FROM ledger_assumptions WHERE content = ?", (content,)
        ).fetchone()
        if existing and not force:
            log(f"  [SKIP] 前提（内容一致）は既に台帳に存在します（--ledger-force で上書き可能）: {content}")
            continue
        log(f"  [{'DRY' if dry_run else 'OK'}] assumption: {content}")
        if dry_run:
            continue
        if existing:
            pm_conn.execute(
                """
                UPDATE ledger_assumptions SET
                    confidence=?, evidence=?, monitor_target=?, source=?,
                    last_reviewed_at=?
                WHERE id=?
                """,
                (a.get("confidence"), a.get("evidence"), a.get("monitor_target"),
                 a.get("source"), now, existing["id"]),
            )
        else:
            pm_conn.execute(
                """
                INSERT INTO ledger_assumptions
                    (content, confidence, evidence, monitor_target, source, state,
                     created_at, last_reviewed_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (content, a.get("confidence"), a.get("evidence"), a.get("monitor_target"),
                 a.get("source"), now, now),
            )
        count += 1
    return count


_SUGGEST_ASSUMPTIONS_PROMPT = """\
以下はプロジェクトの決定事項（根拠付き）と現在の目標一覧です。
これらの決定・目標が暗黙のうちに前提としている事柄——後で外部要因により
変化しうる仮定——を抽出してください。設計書の原則に従い、以下を満たすこと:

- 前提は「今は真だが、将来変わりうること」に限る（恒久的な事実は前提ではない）
- 監視できるもの（何らかの外部シグナルで肯定・否定を確認できるもの）を優先する
- 決定・目標に明記されていない内容を推測で補わない（出所主義）
- 重要なものだけ最大5件に絞る

## 決定事項（根拠付き、直近{n_decisions}件）
{decisions_text}

## 目標一覧
{goals_text}

## 出力フォーマット（JSON配列のみ、前置き・説明は書かない）
[
  {{
    "content": "前提の内容（簡潔な一文）",
    "confidence": "high または medium または low",
    "evidence": "この前提を裏付ける根拠（元の決定・目標の内容から）",
    "monitor_target": "何の外部シグナルが肯定/否定するか（具体的に。無ければ空文字）",
    "source": "示唆元の決定ID/目標ID（例: d:1234, G-PHYS）"
  }}
]
"""


def _extract_json_array(text: str) -> list[dict]:
    import re

    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\[[\s\S]+\]", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON配列が見つかりません:\n{text[:300]}")


def suggest_assumptions(pm_conn, *, limit_decisions: int = 30, log=print) -> list[dict]:
    """pm.db の decisions（rationale付き）・ledger_goals から、LLM に前提候補を
    提案させる。ledger_assumptions へは書き込まない（人の承認を経て
    --ledger-seed から投入する運用、設計書の「LLMが提案し人が承認する」原則）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utils"))
    from llm import call_argus_llm

    decisions = pm_conn.execute(
        "SELECT id, content, rationale, trade_off FROM decisions"
        " WHERE COALESCE(deleted,0) = 0 AND rationale IS NOT NULL AND TRIM(rationale) != ''"
        " ORDER BY COALESCE(decided_at, '') DESC LIMIT ?",
        (limit_decisions,),
    ).fetchall()
    goals = pm_conn.execute(
        "SELECT goal_id, name, layer FROM ledger_goals ORDER BY layer, goal_id"
    ).fetchall()

    if not decisions and not goals:
        log("[WARN] decisions/ledger_goals が空のため前提候補を生成できません")
        return []

    decisions_text = "\n".join(
        f"- [d:{d['id']}] {d['content']}\n  根拠: {d['rationale']}"
        + (f" / 捨てた案: {d['trade_off']}" if d["trade_off"] else "")
        for d in decisions
    ) or "(該当なし)"
    goals_text = "\n".join(
        f"- {g['goal_id']} ({g['layer']}): {g['name']}" for g in goals
    ) or "(該当なし)"

    prompt = _SUGGEST_ASSUMPTIONS_PROMPT.format(
        n_decisions=len(decisions), decisions_text=decisions_text, goals_text=goals_text
    )
    log(f"[INFO] LLM に前提候補を提案させています（decisions={len(decisions)}件, goals={len(goals)}件を入力）...")
    raw = call_argus_llm(prompt, timeout=120, max_tokens=2000)
    candidates = _extract_json_array(raw)
    log(f"[INFO] {len(candidates)} 件の前提候補を取得しました")
    return candidates


def upsert_edges(pm_conn, edges: list[dict], *, dry_run: bool, log=print) -> int:
    """ledger_edges へ INSERT OR REPLACE。UNIQUE(edge_type,from_kind,from_id,to_kind,to_id) で冪等。"""
    now = datetime.now().isoformat()
    count = 0
    for e in edges:
        log(
            f"  [{'DRY' if dry_run else 'OK'}] edge {e['edge_type']}: "
            f"{e['from_kind']}/{e['from_id']} -> {e['to_kind']}/{e['to_id']}"
        )
        if dry_run:
            continue
        pm_conn.execute(
            """
            INSERT INTO ledger_edges
                (edge_type, from_kind, from_id, to_kind, to_id, weight, source, rationale,
                 state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(edge_type, from_kind, from_id, to_kind, to_id) DO UPDATE SET
                weight=excluded.weight, source=excluded.source, rationale=excluded.rationale
            """,
            (
                e["edge_type"], e["from_kind"], e["from_id"], e["to_kind"], e["to_id"],
                e.get("weight"), e.get("source"), e.get("rationale"), now,
            ),
        )
        count += 1
    return count


def list_ledger(pm_conn, log=print) -> None:
    log("== ledger_goals ==")
    for row in pm_conn.execute(
        "SELECT goal_id, kind, layer, weight, weight_status, name FROM ledger_goals ORDER BY layer, goal_id"
    ):
        log(f"  {row['goal_id']:12} [{row['layer']:>11}] weight={row['weight'] or '-':4} "
            f"({row['weight_status'] or '-'}) {row['name']}")
    log("== ledger_issues ==")
    for row in pm_conn.execute("SELECT issue_id, content, state FROM ledger_issues ORDER BY issue_id"):
        log(f"  {row['issue_id']:12} [{row['state']}] {row['content']}")
    log("== ledger_assumptions ==")
    for row in pm_conn.execute(
        "SELECT id, content, confidence, monitor_target, state FROM ledger_assumptions ORDER BY id"
    ):
        log(f"  #{row['id']:<4} [{row['state']}] confidence={row['confidence'] or '-'} "
            f"monitor_target={row['monitor_target'] or '-'}: {row['content']}")
    log("== ledger_edges ==")
    for row in pm_conn.execute(
        "SELECT edge_type, from_kind, from_id, to_kind, to_id FROM ledger_edges ORDER BY edge_type, from_id"
    ):
        log(f"  {row['edge_type']:12} {row['from_kind']}/{row['from_id']} -> {row['to_kind']}/{row['to_id']}")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class LedgerIngestPlugin:
    source_name = "ledger"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--ledger-seed", default=None,
            metavar="PATH",
            help="台帳シード JSON のパス（ledger ソース用、デフォルト: data/ledger_seed.json）",
        )
        parser.add_argument(
            "--ledger-force", action="store_true",
            help="既存の台帳エントリを上書き（ledger ソース用。辺は常に冪等 UPSERT）",
        )
        parser.add_argument(
            "--ledger-list", action="store_true",
            help="pm.db の台帳エントリ一覧を表示して終了（ledger ソース用）",
        )
        parser.add_argument(
            "--ledger-suggest-assumptions", action="store_true",
            help="decisions/ledger_goals から LLM に前提候補を提案させ、"
                 "下書き JSON に出力して終了（pm.db へは書き込まない。ledger ソース用）",
        )
        parser.add_argument(
            "--ledger-suggest-output", default=None,
            metavar="PATH",
            help="前提候補の出力先（デフォルト: data/ledger_assumptions_draft.json）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        if getattr(args, "ledger_list", False):
            list_ledger(ctx.pm_conn, log=ctx.log)
            return

        if getattr(args, "ledger_suggest_assumptions", False):
            candidates = suggest_assumptions(ctx.pm_conn, log=ctx.log)
            output_path = (
                Path(args.ledger_suggest_output) if getattr(args, "ledger_suggest_output", None)
                else REPO_ROOT / "data" / "ledger_assumptions_draft.json"
            )
            output_path.write_text(
                json.dumps({"assumptions": candidates}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            ctx.log(f"[INFO] 前提候補（未承認）を出力しました: {output_path}")
            ctx.log("[INFO] 内容を確認・編集の上、承認したものだけを残してから以下で投入してください:")
            ctx.log(f"  python3 scripts/ingest/pm_ingest.py ledger --ledger-seed {output_path}")
            return

        seed_path = Path(args.ledger_seed) if getattr(args, "ledger_seed", None) else DEFAULT_SEED_PATH
        seed = load_seed(seed_path)

        ctx.log(f"[INFO] シードファイル: {seed_path}")
        if seed.get("_meta", {}).get("reconstructed_note"):
            ctx.log(f"[INFO] {seed['_meta']['reconstructed_note']}")
        if ctx.dry_run:
            ctx.log("[INFO] --dry-run モード（DB保存なし）")

        force = ctx.force or getattr(args, "ledger_force", False)
        if force:
            ctx.log("[INFO] --ledger-force モード（既存エントリを上書き）")

        n_goals = upsert_goals(ctx.pm_conn, seed.get("goals", []), force=force, dry_run=ctx.dry_run, log=ctx.log)
        n_issues = upsert_issues(ctx.pm_conn, seed.get("issues", []), force=force, dry_run=ctx.dry_run, log=ctx.log)
        n_assumptions = upsert_assumptions(
            ctx.pm_conn, seed.get("assumptions", []), force=force, dry_run=ctx.dry_run, log=ctx.log
        )
        n_edges = upsert_edges(ctx.pm_conn, seed.get("edges", []), dry_run=ctx.dry_run, log=ctx.log)

        if not ctx.dry_run:
            ctx.pm_conn.commit()

        ctx.log(
            f"[INFO] 完了: goals={n_goals}件, issues={n_issues}件, "
            f"assumptions={n_assumptions}件, edges={n_edges}件 投入"
            f"{'（dry-run のため未保存）' if ctx.dry_run else ''}"
        )
