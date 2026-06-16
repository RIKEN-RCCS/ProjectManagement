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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_utils import open_pm_db
from cli_utils import call_claude, add_no_encrypt_arg, add_dry_run_arg, add_since_arg


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
               a.source, a.extracted_at, a.source_ref,
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
            datetime.now(timezone.utc).isoformat(),
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

1. **明確に該当する場合のみ紐づける**: アクションアイテムの内容が、マイルストーンの名称・エリア・達成条件に対し直接寄与すると判断できる場合のみ milestone_id を記入する
2. **判断できない / どれにも該当しない場合は null**: 一般的な運営事項、複数MSにまたがる事項、情報不足の場合は null とする
3. 1つのアクションアイテムは最大1つのマイルストーンに紐づける

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
            meta_parts.append(f"出典: Slack")
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
        raw = call_claude(prompt, timeout=180)
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

    total = len(items)
    updated = 0
    null_count = 0
    failed = 0
    by_ms: dict[str, int] = {}

    for offset in range(0, total, args.batch_size):
        batch = items[offset:offset + args.batch_size]
        batch_no = offset // args.batch_size + 1
        total_batches = (total + args.batch_size - 1) // args.batch_size
        log(f"\n[{batch_no}/{total_batches}] バッチ {len(batch)} 件を LLM 判定中…")

        results = process_batch(batch, milestones, valid_ms_ids, log)
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
            # UPDATE
            conn.execute(
                "UPDATE action_items SET milestone_id=? WHERE id=?",
                (ms_id, it["id"]),
            )
            write_audit(conn, it["id"], None, ms_id)
            by_ms[ms_id] = by_ms.get(ms_id, 0) + 1
            updated += 1
            log(f"  id={it['id']} → {ms_id} ({r['reason'][:60]})")

        conn.commit()

    log("")
    log("=" * 60)
    log(f"完了: 対象={total} 件, 紐づけ更新={updated} 件, 紐づけなし={null_count} 件, 失敗={failed} 件")
    if by_ms:
        log("マイルストーン別:")
        for ms_id in sorted(by_ms):
            log(f"  {ms_id}: {by_ms[ms_id]} 件")

    if args.output:
        args.output.write_text("\n".join(output_lines), encoding="utf-8")
        log(f"[INFO] ログを {args.output} に保存しました")


if __name__ == "__main__":
    main()
