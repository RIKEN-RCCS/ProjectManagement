#!/usr/bin/env python3
"""
pm_link_milestones.py — 既存 action_items に対する遡及的マイルストーン紐づけ

pm.db の milestone_id IS NULL なアクションアイテムをバッチで LLM に渡し、
goals.yaml 由来の milestones から最も関連の高いものを推定して milestone_id を更新する。

- 対象: COALESCE(deleted,0)=0 の AI（既存の Slack 抽出と異なり open/closed 両方）
- LLM 出力は厳密に JSON。判断できない場合は null（更新しない）
- 全更新は audit_log に source='auto_link' で記録
- 既に milestone_id が入っているレコードは触らない

使用例:
    python3 scripts/pm_link_milestones.py --dry-run --limit 20
    python3 scripts/pm_link_milestones.py --since 2026-01-01
    python3 scripts/pm_link_milestones.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_utils import add_dry_run_arg, add_no_encrypt_arg, add_since_arg, call_argus_llm
from db_utils import open_pm_db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "pm.db"

_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,
    source     TEXT
)"""


# --------------------------------------------------------------------------- #
# DB ヘルパ
# --------------------------------------------------------------------------- #
def fetch_milestones(conn) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT m.milestone_id, m.name, m.due_date, m.area, m.success_criteria,
                      g.name AS goal_name, g.description AS goal_description
               FROM milestones m
               LEFT JOIN goals g ON m.goal_id = g.goal_id
               WHERE m.status='active' ORDER BY m.due_date"""
        ).fetchall()
    except Exception:
        rows = conn.execute(
            "SELECT milestone_id, name, due_date, area, success_criteria"
            " FROM milestones WHERE status='active' ORDER BY due_date"
        ).fetchall()
    return [dict(r) for r in rows]


def format_milestones_for_prompt(milestones: list[dict]) -> str:
    if not milestones:
        return "（マイルストーン未登録）"
    lines = []
    for m in milestones:
        sc_raw = m.get("success_criteria") or ""
        sc_text = ""
        if sc_raw:
            try:
                sc = json.loads(sc_raw)
                if isinstance(sc, list):
                    sc_text = " / ".join(str(s) for s in sc)
                else:
                    sc_text = str(sc)
            except Exception:
                sc_text = str(sc_raw)
        lines.append(
            f"- **{m['milestone_id']}** (期限: {m.get('due_date') or '未定'}, エリア: {m.get('area') or '-'}): "
            f"{m['name']}"
        )
        if sc_text:
            lines.append(f"    達成条件: {sc_text}")
        if m.get("goal_name"):
            goal_desc = (m.get("goal_description") or "").strip()
            goal_line = f"    ↳ 親ゴール [{m['goal_name']}]"
            if goal_desc:
                goal_line += f": {goal_desc[:80]}"
            lines.append(goal_line)
    return "\n".join(lines)


def fetch_unlinked_items(
    conn,
    since: str | None,
    ids: list[int] | None,
    limit: int | None,
) -> list[dict]:
    conds = ["COALESCE(a.deleted,0)=0", "a.milestone_id IS NULL"]
    params: list = []
    if since:
        conds.append("a.extracted_at >= ?")
        params.append(since)
    if ids:
        placeholders = ",".join("?" * len(ids))
        conds.append(f"a.id IN ({placeholders})")
        params.extend(ids)
    where = "WHERE " + " AND ".join(conds)
    sql = f"""
        SELECT a.id, a.content, a.assignee, a.due_date, a.status,
               a.source, a.extracted_at, a.source_ref, a.source_context,
               m.kind AS meeting_kind, m.held_at AS meeting_held_at
        FROM action_items a
        LEFT JOIN meetings m ON a.meeting_id = m.meeting_id
        {where}
        ORDER BY a.extracted_at DESC, a.id DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def write_audit(conn, ai_id: int, old: str | None, new: str | None) -> None:
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "action_items",
            str(ai_id),
            "milestone_id",
            old,
            new,
            datetime.now(UTC).isoformat(),
            "auto_link",
        ),
    )


# --------------------------------------------------------------------------- #
# LLM プロンプト
# --------------------------------------------------------------------------- #
PROMPT_TEMPLATE = """あなたは富岳NEXT アプリケーション開発エリアのプロジェクトマネージャーです。
以下のアクションアイテム一覧について、それぞれを下記のマイルストーンのいずれかに紐づけてください。

## マイルストーン一覧

{milestones}

## 紐づけのルール

1. **関連が認められたら積極的に紐づける**: アクションアイテムの内容・担当者・期限が、マイルストーンの名称・エリア・達成条件・親ゴールのいずれかと関連すると判断できる場合は milestone_id を記入する。完全一致でなくても「このマイルストーンの推進に直接または間接に貢献する作業」であれば紐づけてよい。
2. **null は真に無関係な場合のみ**: 複数のマイルストーンに同程度に当てはまり最良の一択が存在しない場合、またはプロジェクト外の一般事務である場合のみ null とする。迷ったときは最も関連の強いマイルストーンを 1 つ選ぶこと。
3. 1つのアクションアイテムは最大1つのマイルストーンに紐づける（最も関連が強いものを選択）。

## アクションアイテム

{items}

## 出力形式（厳守）

最初の行に「紐づけ結果:」と書いた直後の行から、以下の JSON ブロックを返すこと。

紐づけ結果:
```json
{{
  "links": [
    {{"id": <action_item_id>, "milestone_id": "M3" または null, "reason": "短い根拠 or 'unrelated'"}},
    ...
  ]
}}
```

入力された各 id について必ず1つずつ出力すること。
"""


def format_items_for_prompt(items: list[dict]) -> str:
    lines = []
    for it in items:
        meta_parts = []
        if it.get("source") == "meeting" and it.get("meeting_kind"):
            meta_parts.append(f"会議: {it['meeting_kind']} ({it.get('meeting_held_at') or ''})")
        else:
            meta_parts.append("出典: Slack")
        if it.get("assignee"):
            meta_parts.append(f"担当: {it['assignee']}")
        if it.get("due_date"):
            meta_parts.append(f"期限: {it['due_date']}")
        if it.get("extracted_at"):
            meta_parts.append(f"発生: {it['extracted_at']}")
        meta_parts.append(f"状態: {it.get('status') or 'open'}")
        meta = " | ".join(meta_parts)
        content = (it.get("content") or "").replace("\n", " ").strip()
        lines.append(f"- id={it['id']} [{meta}]\n  {content}")
        ctx = (it.get("source_context") or it.get("_fts_context") or "").strip()
        if ctx:
            lines.append(f"  文脈: {ctx[:200]}")
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output: {text[:300]}")


# --------------------------------------------------------------------------- #
# Phase 2: 埋め込みによる候補事前絞り込み
# --------------------------------------------------------------------------- #

def build_milestone_text(m: dict) -> str:
    parts = [m.get("name") or ""]
    if m.get("area"):
        parts.append(m["area"])
    if m.get("goal_name"):
        parts.append(m["goal_name"])
    if m.get("goal_description"):
        parts.append(m["goal_description"][:100])
    sc_raw = m.get("success_criteria") or ""
    if sc_raw:
        try:
            sc = json.loads(sc_raw)
            if isinstance(sc, list):
                parts.extend(str(s) for s in sc[:3])
            else:
                parts.append(str(sc)[:200])
        except Exception:
            parts.append(str(sc_raw)[:200])
    return " ".join(p for p in parts if p)


def build_item_text(item: dict) -> str:
    parts = [(item.get("content") or "").strip()]
    ctx = (item.get("source_context") or "").strip()
    if ctx:
        parts.append(ctx[:200])
    return " ".join(p for p in parts if p)


def compute_milestone_embeddings(milestones: list[dict], log) -> object | None:
    try:
        from embed_utils import embed_batch  # type: ignore
        texts = [build_milestone_text(m) for m in milestones]
        mat = embed_batch(texts, timeout=120)
        return mat
    except Exception as e:
        log(f"[WARN] マイルストーム埋め込み失敗: {e}")
        return None


def select_top_k_candidates(
    item_vec: object,
    ms_mat: object,
    milestones: list[dict],
    top_k: int,
) -> tuple[list[dict], object]:
    import numpy as np
    from embed_utils import cosine_similarity_matrix  # type: ignore
    sims = cosine_similarity_matrix(item_vec, ms_mat)
    top_idxs = np.argsort(sims)[::-1][:top_k]
    return [milestones[i] for i in top_idxs], sims[top_idxs]


# --------------------------------------------------------------------------- #
# Phase 3: FTS5 コンテキスト補完
# --------------------------------------------------------------------------- #

def fetch_item_fts_context(
    item: dict,
    qa_index_path: Path,
    max_chars: int = 300,
) -> str | None:
    """action_items.content で qa_index.db の FTS5 を検索し周辺コンテキストを返す。"""
    if not qa_index_path.exists():
        return None
    content = (item.get("content") or "").strip()
    if not content:
        return None
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(qa_index_path))
        conn.row_factory = _sqlite3.Row
        try:
            fts_q = re.sub(r'["\'\*\^\(\)\[\]]', " ", content[:60]).strip()
            rows = conn.execute(
                "SELECT c.content FROM fts"
                " JOIN chunks c ON fts.rowid = c.id"
                " WHERE fts MATCH ? ORDER BY rank LIMIT 3",
                [fts_q],
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()
        if not rows:
            return None
        snippets = [dict(r)["content"][:120].replace("\n", " ") for r in rows]
        return (" … ".join(snippets))[:max_chars]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# バッチ処理
# --------------------------------------------------------------------------- #
def process_batch(
    items: list[dict],
    milestones: list[dict],
    valid_ms_ids: set[str],
    log,
) -> list[dict]:
    """LLM にバッチを渡し、結果を [{id, milestone_id, reason}] のリストで返す"""
    prompt = PROMPT_TEMPLATE.format(
        milestones=format_milestones_for_prompt(milestones),
        items=format_items_for_prompt(items),
    )
    try:
        raw = call_argus_llm(prompt, timeout=180)
    except Exception as e:
        log(f"  [WARN] LLM呼び出し失敗: {e}")
        return []
    try:
        parsed = extract_json(raw)
    except Exception as e:
        log(f"  [WARN] JSON パース失敗: {e}")
        log(f"  [DEBUG] raw 先頭: {raw[:300]}")
        return []
    links = parsed.get("links") or []
    cleaned = []
    for ln in links:
        try:
            ai_id = int(ln.get("id"))
        except Exception:
            continue
        ms_id = ln.get("milestone_id")
        if ms_id is not None and ms_id not in valid_ms_ids:
            log(f"  [WARN] id={ai_id}: 未知の milestone_id={ms_id} → null 扱い")
            ms_id = None
        cleaned.append({
            "id": ai_id,
            "milestone_id": ms_id,
            "reason": ln.get("reason") or "",
        })
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(
        description="既存 action_items に対する遡及的マイルストーン紐づけ（LLM 利用）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"pm.db のパス（デフォルト: {DEFAULT_DB}）")
    parser.add_argument("--batch-size", type=int, default=15,
                        help="1回の LLM 呼び出しで処理する AI 数（デフォルト: 15）")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理対象の最大件数")
    parser.add_argument("--id", type=int, nargs="+", default=None,
                        help="特定の action_item id のみ処理（複数指定可）")
    parser.add_argument("--output", type=Path, default=None,
                        help="ログをファイルにも保存")
    parser.add_argument("--preview", action="store_true",
                        help="LLM は呼ぶが DB には書き込まない（紐づけ率の A/B 計測用）")
    parser.add_argument("--no-embed", action="store_true",
                        help="埋め込み事前フィルタを無効化（全マイルストーンを LLM に提示）")
    parser.add_argument("--top-k", type=int, default=3, metavar="N",
                        help="埋め込みで絞り込むマイルストーン候補数（デフォルト: 3）")
    parser.add_argument("--auto-link-threshold", type=float, default=0.85, metavar="THRESH",
                        help="自動紐づけのコサイン類似度閾値（デフォルト: 0.85）")
    parser.add_argument("--with-qa-context", action="store_true",
                        help="qa_index.db FTS5 で source_context 空のアイテムに文脈を補完")
    parser.add_argument("--qa-index", type=Path, default=REPO_ROOT / "data" / "qa_index.db",
                        help="qa_index.db のパス（デフォルト: data/qa_index.db）")
    add_dry_run_arg(parser)
    add_since_arg(parser, " (action_items.extracted_at で絞り込み)")
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    output_lines: list[str] = []
    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    conn = open_pm_db(args.db, no_encrypt=args.no_encrypt)
    conn.execute(_AUDIT_LOG_DDL)
    conn.commit()

    milestones = fetch_milestones(conn)
    valid_ms_ids = {m["milestone_id"] for m in milestones}
    log(f"[INFO] マイルストーン: {len(milestones)} 件 ({', '.join(sorted(valid_ms_ids))})")
    if not milestones:
        log("[ERROR] active なマイルストーンが pm.db に存在しません。")
        log("        先に `python3 scripts/ingest/pm_ingest.py goals` を実行してください。")
        sys.exit(1)

    items = fetch_unlinked_items(conn, args.since, args.id, args.limit)
    log(f"[INFO] 未紐づけ AI: {len(items)} 件")
    if not items:
        log("[INFO] 対象なし。終了します。")
        return

    # 統計
    by_source: dict[str, int] = {}
    for it in items:
        by_source[it.get("source") or "unknown"] = by_source.get(it.get("source") or "unknown", 0) + 1
    for s, n in sorted(by_source.items()):
        log(f"  source={s}: {n} 件")

    if args.dry_run:
        log("[INFO] --dry-run のため LLM 呼び出し・DB更新はスキップします")
        if args.output:
            args.output.write_text("\n".join(output_lines), encoding="utf-8")
        return

    write_to_db = not (args.dry_run or args.preview)
    if args.preview:
        log("[INFO] --preview モード: LLM は呼ぶが DB には書き込みません")

    # Phase 3: FTS5 コンテキスト補完
    if args.with_qa_context:
        fts_enriched = 0
        for it in items:
            if it.get("source_context"):
                continue
            ctx = fetch_item_fts_context(it, args.qa_index)
            if ctx:
                it["_fts_context"] = ctx
                fts_enriched += 1
        log(f"[INFO] qa_index.db コンテキスト補完: {fts_enriched} 件")

    total = len(items)
    updated = 0
    auto_linked = 0
    null_count = 0
    failed = 0
    by_ms: dict[str, int] = {}

    # Phase 2: 埋め込みによる候補事前絞り込み
    item_candidates: dict[int, list[dict]] = {}
    ms_mat = None
    if not args.no_embed:
        ms_mat = compute_milestone_embeddings(milestones, log)
        if ms_mat is not None:
            try:
                from embed_utils import embed_batch  # type: ignore
                log(f"[INFO] アイテム埋め込み計算中 ({len(items)} 件)…")
                item_texts = [build_item_text(it) for it in items]
                item_mat = embed_batch(item_texts, timeout=180)
                auto_link_ids: set[int] = set()
                for it, item_vec in zip(items, item_mat, strict=True):
                    candidates, sims = select_top_k_candidates(
                        item_vec, ms_mat, milestones, args.top_k
                    )
                    item_candidates[it["id"]] = candidates
                    if float(sims[0]) >= args.auto_link_threshold:
                        ms_id = candidates[0]["milestone_id"]
                        if write_to_db:
                            conn.execute(
                                "UPDATE action_items SET milestone_id=? WHERE id=?",
                                (ms_id, it["id"]),
                            )
                            write_audit(conn, it["id"], None, ms_id)
                        by_ms[ms_id] = by_ms.get(ms_id, 0) + 1
                        auto_linked += 1
                        auto_link_ids.add(it["id"])
                        log(f"  id={it['id']} → {ms_id} [自動 sim={float(sims[0]):.3f}]")
                if write_to_db and auto_link_ids:
                    conn.commit()
                items = [it for it in items if it["id"] not in auto_link_ids]
                log(f"[INFO] 自動紐づけ: {auto_linked} 件, LLM 判定へ: {len(items)} 件")
            except Exception as e:
                log(f"[WARN] アイテム埋め込み失敗 ({e}): 全アイテムを LLM 判定します")
                ms_mat = None
                item_candidates = {}

    for offset in range(0, len(items), args.batch_size):
        batch = items[offset:offset + args.batch_size]
        batch_no = offset // args.batch_size + 1
        total_batches = (len(items) + args.batch_size - 1) // args.batch_size
        log(f"\n[{batch_no}/{total_batches}] バッチ {len(batch)} 件を LLM 判定中…")

        # 埋め込みで候補を絞った場合はそれだけを LLM に渡す
        if item_candidates:
            cand_ids = {
                m["milestone_id"]
                for it in batch
                for m in item_candidates.get(it["id"], milestones)
            }
            batch_milestones = [m for m in milestones if m["milestone_id"] in cand_ids] or milestones
        else:
            batch_milestones = milestones

        results = process_batch(batch, batch_milestones, valid_ms_ids, log)
        if not results:
            failed += len(batch)
            continue

        result_map = {r["id"]: r for r in results}
        for it in batch:
            r = result_map.get(it["id"])
            if not r:
                log(f"  [WARN] id={it['id']} は LLM 応答に含まれず（スキップ）")
                failed += 1
                continue
            ms_id = r["milestone_id"]
            if ms_id is None:
                null_count += 1
                log(f"  id={it['id']}: 紐づけなし ({r['reason'][:60]})")
                continue
            if write_to_db:
                conn.execute(
                    "UPDATE action_items SET milestone_id=? WHERE id=?",
                    (ms_id, it["id"]),
                )
                write_audit(conn, it["id"], None, ms_id)
            by_ms[ms_id] = by_ms.get(ms_id, 0) + 1
            updated += 1
            log(f"  id={it['id']} → {ms_id} ({r['reason'][:60]})")

        if write_to_db:
            conn.commit()

    log("")
    log("=" * 60)
    link_rate = (updated + auto_linked) / total if total > 0 else 0.0
    log(f"完了: 対象={total} 件, 紐づけ更新={updated} 件 (自動={auto_linked}), "
        f"紐づけなし={null_count} 件, 失敗={failed} 件")
    log(f"紐づけ率: {link_rate:.1%}")
    if by_ms:
        log("マイルストーン別:")
        for ms_id in sorted(by_ms):
            log(f"  {ms_id}: {by_ms[ms_id]} 件")

    if args.output:
        args.output.write_text("\n".join(output_lines), encoding="utf-8")
        log(f"[INFO] ログを {args.output} に保存しました")


if __name__ == "__main__":
    main()
