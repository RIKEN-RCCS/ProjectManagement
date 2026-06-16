#!/usr/bin/env python3
"""
pm_screen.py

pm.db のアクションアイテムと決定事項をスクリーニングし、
重複・類似・曖昧なアイテムを検出する。

検出カテゴリ:
  1. exact_dup   — 正規化後に完全一致する重複
  2. near_dup    — 先頭N文字が一致し内容が微妙に異なる類似重複
  3. ambiguous   — 短すぎて文脈なしでは意味が類推できないもの

結果は pm_relink.py 互換の CSV で出力する。
deleted 列に 1 をセットしてから pm_relink.py --import で一括削除できる。

Usage:
    # スクリーニング結果を表示
    python3 scripts/pm_screen.py

    # CSV にエクスポート（pm_relink.py --import で編集可能）
    python3 scripts/pm_screen.py --export

    # 出力先を指定
    python3 scripts/pm_screen.py --export --output screen.csv

    # 閾値調整
    python3 scripts/pm_screen.py --short-threshold 25 --prefix-len 20

    # 決定事項も対象に含める
    python3 scripts/pm_screen.py --include-decisions
"""

import argparse
import csv
import io
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import open_db
from cli_utils import (
    add_no_encrypt_arg, add_dry_run_arg, add_since_arg,
    add_db_arg, add_output_arg, make_logger, resolve_db_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_SECTION_ACTIONS   = "# === アクションアイテム ==="
_SECTION_DECISIONS = "# === 決定事項 ==="


def normalize(s: str) -> str:
    s = re.sub(r"[。、．，\.\s　　]", "", s)
    s = re.sub(r"を行う$|する$|を進める$|すること$|こと$", "", s)
    return s


def fetch_active_action_items(conn, since: str | None = None) -> list[dict]:
    conds = ["COALESCE(a.deleted,0)=0"]
    params: list[str] = []
    if since:
        conds.append("a.extracted_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conds)
    rows = conn.execute(f"""
        SELECT a.id, a.content, a.assignee, a.due_date, a.milestone_id,
               a.status, a.extracted_at, a.source, a.source_ref, a.note,
               COALESCE(a.deleted,0) AS deleted
        FROM action_items a
        {where}
        ORDER BY a.id
    """, params).fetchall()
    return [dict(r) for r in rows]


def fetch_active_decisions(conn, since: str | None = None) -> list[dict]:
    conds = ["COALESCE(deleted,0)=0"]
    params: list[str] = []
    if since:
        conds.append("extracted_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conds)
    rows = conn.execute(f"""
        SELECT id, content, decided_at, source, source_ref,
               COALESCE(deleted,0) AS deleted
        FROM decisions
        {where}
        ORDER BY id
    """, params).fetchall()
    return [dict(r) for r in rows]


def detect_exact_duplicates(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        key = normalize(item["content"])
        groups[key].append(item)
    return [
        ("exact_dup", group)
        for group in groups.values()
        if len(group) > 1
    ]


def detect_near_duplicates(items: list[dict], prefix_len: int) -> list[tuple[str, list[dict]]]:
    norm_map = {item["id"]: normalize(item["content"]) for item in items}

    prefix_groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        n = norm_map[item["id"]]
        if len(n) < prefix_len // 2:
            continue
        prefix = n[:prefix_len]
        prefix_groups[prefix].append(item)

    results = []
    for group in prefix_groups.values():
        if len(group) < 2:
            continue
        norms = set(norm_map[it["id"]] for it in group)
        if len(norms) > 1:
            results.append(("near_dup", group))
    return results


def detect_ambiguous(items: list[dict], threshold: int) -> list[tuple[str, list[dict]]]:
    results = []
    for item in items:
        if len(item["content"]) <= threshold:
            results.append(("ambiguous", [item]))
    return results


def print_report(findings: list[tuple[str, list[dict]]], table: str, log) -> None:
    by_cat: dict[str, list[list[dict]]] = defaultdict(list)
    for cat, group in findings:
        by_cat[cat].append(group)

    flagged_ids: set[int] = set()
    for _cat, groups in by_cat.items():
        for group in groups:
            for item in group:
                flagged_ids.add(item["id"])

    log(f"\n{'='*60}")
    log(f"  {table}: {len(flagged_ids)} 件にフラグ")
    log(f"{'='*60}")

    labels = {
        "exact_dup": "完全重複（正規化後一致）",
        "near_dup":  "類似重複（先頭一致・表現違い）",
        "ambiguous": "曖昧・短すぎ（文脈なしで意味不明）",
    }

    for cat in ["exact_dup", "near_dup", "ambiguous"]:
        groups = by_cat.get(cat, [])
        if not groups:
            continue
        count = sum(len(g) for g in groups)
        log(f"\n--- {labels[cat]}: {len(groups)} グループ, {count} 件 ---")

        for i, group in enumerate(groups, 1):
            if cat == "ambiguous":
                item = group[0]
                log(f"  ID={item['id']:3d} | {item.get('extracted_at','?'):10s}"
                    f" | {item.get('source','?'):7s} | \"{item['content']}\"")
            else:
                log(f"\n  グループ {i} ({len(group)} 件):")
                for item in group:
                    log(f"    ID={item['id']:3d} | {item.get('extracted_at','?'):10s}"
                        f" | {item.get('source','?'):7s}"
                        f" | assignee={item.get('assignee') or '-':8s}"
                        f" | {item['content'][:80]}")


def export_csv(
    ai_findings: list[tuple[str, list[dict]]],
    dec_findings: list[tuple[str, list[dict]]],
    all_ais: list[dict],
    all_decs: list[dict],
    output_path: str,
    log,
) -> None:
    ai_flagged: dict[int, str] = {}
    for cat, group in ai_findings:
        for item in group:
            existing = ai_flagged.get(item["id"], "")
            if existing:
                ai_flagged[item["id"]] = existing + "+" + cat
            else:
                ai_flagged[item["id"]] = cat

    dec_flagged: dict[int, str] = {}
    for cat, group in dec_findings:
        for item in group:
            existing = dec_flagged.get(item["id"], "")
            if existing:
                dec_flagged[item["id"]] = existing + "+" + cat
            else:
                dec_flagged[item["id"]] = cat

    flagged_ais = [a for a in all_ais if a["id"] in ai_flagged]
    flagged_decs = [d for d in all_decs if d["id"] in dec_flagged]

    buf = io.StringIO()

    buf.write(_SECTION_ACTIONS + "\n")
    ai_cols = ["id", "flag", "assignee", "due_date", "milestone_id",
               "status", "content", "source", "extracted_at", "note", "deleted"]
    writer = csv.DictWriter(buf, fieldnames=ai_cols, extrasaction="ignore")
    writer.writeheader()
    for a in flagged_ais:
        row = {
            "id": a["id"],
            "flag": ai_flagged[a["id"]],
            "assignee": a.get("assignee") or "",
            "due_date": a.get("due_date") or "",
            "milestone_id": a.get("milestone_id") or "",
            "status": a.get("status") or "",
            "content": a["content"],
            "source": a.get("source") or "",
            "extracted_at": a.get("extracted_at") or "",
            "note": a.get("note") or "",
            "deleted": "",
        }
        writer.writerow(row)

    buf.write("\n" + _SECTION_DECISIONS + "\n")
    dec_cols = ["id", "flag", "content", "decided_at", "source", "deleted"]
    writer2 = csv.DictWriter(buf, fieldnames=dec_cols, extrasaction="ignore")
    writer2.writeheader()
    for d in flagged_decs:
        row = {
            "id": d["id"],
            "flag": dec_flagged[d["id"]],
            "content": d["content"],
            "decided_at": d.get("decided_at") or "",
            "source": d.get("source") or "",
            "deleted": "",
        }
        writer2.writerow(row)

    text = buf.getvalue()
    Path(output_path).write_text(text, encoding="utf-8")
    log(f"\nCSV 出力: {output_path}")
    log(f"  アクションアイテム: {len(flagged_ais)} 件")
    log(f"  決定事項: {len(flagged_decs)} 件")
    log()
    log("使い方:")
    log("  1. CSV の deleted 列に 1 を入れて削除対象をマーク")
    log("  2. flag 列・source 列・extracted_at 列は参考情報（インポート時に無視される）")
    log("  3. pm_relink.py --import でDB反映:")
    log(f"     python3 scripts/pm_relink.py --import {output_path} --dry-run")
    log(f"     python3 scripts/pm_relink.py --import {output_path}")


def screen_for_web(
    conn,
    *,
    include_decisions: bool = False,
    short_threshold: int = 25,
    prefix_len: int = 20,
    since: str | None = None,
) -> dict:
    """Web UI 向けに重複・類似・曖昧グループを JSON 互換 dict で返す。

    返り値:
      {
        "action_items": {
          "groups": [{"category": "exact_dup"|"near_dup"|"ambiguous",
                       "items": [{id, content, assignee, due_date, source, ...}, ...]}],
          "total_flagged": int,
        },
        "decisions": {... 同上, include_decisions=True のときのみ},
      }
    各グループの先頭アイテムが「残す推奨」、それ以降が「削除候補」。
    """
    ais = fetch_active_action_items(conn, since=since)
    ai_groups: list[dict] = []
    ai_flagged: set[int] = set()
    for cat, group in detect_exact_duplicates(ais):
        ai_groups.append({"category": cat, "items": group})
        for it in group:
            ai_flagged.add(it["id"])
    for cat, group in detect_near_duplicates(ais, prefix_len):
        ai_groups.append({"category": cat, "items": group})
        for it in group:
            ai_flagged.add(it["id"])
    for cat, group in detect_ambiguous(ais, short_threshold):
        ai_groups.append({"category": cat, "items": group})
        for it in group:
            ai_flagged.add(it["id"])

    result: dict = {
        "action_items": {"groups": ai_groups, "total_flagged": len(ai_flagged)},
    }

    if include_decisions:
        decs = fetch_active_decisions(conn, since=since)
        dec_groups: list[dict] = []
        dec_flagged: set[int] = set()
        for cat, group in detect_exact_duplicates(decs):
            dec_groups.append({"category": cat, "items": group})
            for it in group:
                dec_flagged.add(it["id"])
        for cat, group in detect_near_duplicates(decs, prefix_len):
            dec_groups.append({"category": cat, "items": group})
            for it in group:
                dec_flagged.add(it["id"])
        for cat, group in detect_ambiguous(decs, short_threshold):
            dec_groups.append({"category": cat, "items": group})
            for it in group:
                dec_flagged.add(it["id"])
        result["decisions"] = {"groups": dec_groups, "total_flagged": len(dec_flagged)}

    return result


def main():
    parser = argparse.ArgumentParser(
        description="pm.db のアクションアイテム・決定事項をスクリーニング（重複・類似・曖昧を検出）"
    )
    add_db_arg(parser)
    add_no_encrypt_arg(parser)
    add_since_arg(parser)
    add_output_arg(parser)
    parser.add_argument("--export", action="store_true",
                        help="フラグ付きアイテムを pm_relink.py 互換CSVにエクスポート")
    parser.add_argument("--short-threshold", type=int, default=20,
                        help="この文字数以下を「曖昧・短すぎ」と判定（デフォルト: 20）")
    parser.add_argument("--prefix-len", type=int, default=15,
                        help="類似重複検出の先頭比較文字数（デフォルト: 15）")
    parser.add_argument("--include-decisions", action="store_true",
                        help="決定事項もスクリーニング対象に含める")

    args = parser.parse_args()
    db_path = resolve_db_path(args.db, REPO_ROOT / "data" / "pm.db")
    log, close = make_logger(args.output if not args.export else None)

    conn = open_db(db_path, encrypt=not args.no_encrypt)

    ais = fetch_active_action_items(conn, since=args.since)
    decs = fetch_active_decisions(conn, since=args.since) if args.include_decisions else []

    log(f"対象: アクションアイテム {len(ais)} 件"
        + (f", 決定事項 {len(decs)} 件" if decs else ""))

    ai_findings: list[tuple[str, list[dict]]] = []
    ai_findings.extend(detect_exact_duplicates(ais))
    ai_findings.extend(detect_near_duplicates(ais, prefix_len=args.prefix_len))
    ai_findings.extend(detect_ambiguous(ais, threshold=args.short_threshold))

    dec_findings: list[tuple[str, list[dict]]] = []
    if decs:
        dec_findings.extend(detect_exact_duplicates(decs))
        dec_findings.extend(detect_near_duplicates(decs, prefix_len=args.prefix_len))
        dec_findings.extend(detect_ambiguous(decs, threshold=args.short_threshold))

    print_report(ai_findings, "アクションアイテム", log)
    if dec_findings:
        print_report(dec_findings, "決定事項", log)

    if args.export:
        output = args.output or "screen.csv"
        export_csv(ai_findings, dec_findings, ais, decs, output, log)

    conn.close()
    close()


if __name__ == "__main__":
    main()
