#!/usr/bin/env python3
"""
pm_screen.py

pm.db のアクションアイテムと決定事項をスクリーニングし、
重複・類似・曖昧なアイテムを検出する。

検出カテゴリ:
  1. exact_dup    — 正規化後に完全一致する重複
  2. near_dup     — 先頭N文字が一致し内容が微妙に異なる類似重複
  3. ambiguous    — 短すぎて文脈なしでは意味が類推できないもの
  4. semantic_dup — embedding コサイン類似度＋境界帯のローカルLLM審査による意味的重複
                    （--semantic 指定時のみ、アクションアイテムのみ対象）

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

    # 意味的重複検出（embedding + ローカルLLM境界審査）も含める
    python3 scripts/pm_screen.py --semantic --export

    # 既存データの一括トリアージ（3ゲート審査。重複検出とは独立したモード）
    python3 scripts/pm_screen.py --triage --output triage.csv

    # closed の action_items も対象に含める（非推奨。完了実績を誤って抹消しうる）
    python3 scripts/pm_screen.py --triage --triage-include-closed --output triage.csv

    # 議事録経路への第2系統差分検査（--reader で読み手を切り替え。既定: second=R8対策）
    python3 scripts/pm_screen.py --second-opinion-minutes --reader second
    python3 scripts/pm_screen.py --second-opinion-minutes --reader k3 --dry-run
    python3 scripts/pm_screen.py --second-opinion-minutes --reader both

    # 特定の会議1件だけを検査（--limit は無視される。録音経路からの即時検査用）
    python3 scripts/pm_screen.py --second-opinion-minutes --reader both \
        --meeting-stem 2026-07-01-120000-Leader_Meeting-minutes

    # 第2系統トリアージの所見レビュー（triage_second_opinion。誰も見ない検査は意味がない）
    python3 scripts/pm_screen.py --list-findings --unreviewed-only
    python3 scripts/pm_screen.py --list-findings --kind minutes_extraction_recall
    python3 scripts/pm_screen.py --mark-reviewed 12,13,14
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli_utils import (
    add_db_arg,
    add_dry_run_arg,
    add_no_encrypt_arg,
    add_output_arg,
    add_since_arg,
    make_logger,
    resolve_db_path,
)
from db_utils import (
    list_second_opinion_findings,
    mark_second_opinion_reviewed,
    open_db,
    table_exists,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"
DEFAULT_PROCESSING_DIR = REPO_ROOT / "data" / "processing"

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
               a.rationale, a.requested_by, a.source_context, a.related_ids,
               a.meeting_id,
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
        SELECT id, content, decided_at, source, source_ref, meeting_id,
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


_ENRICH_COLUMNS = (
    "milestone_id", "rationale", "requested_by", "source_context",
    "related_ids", "note", "due_date", "assignee",
)


def _enrichment_score(item: dict) -> int:
    score = 0
    for col in _ENRICH_COLUMNS:
        v = item.get(col)
        if v is not None and str(v).strip() != "":
            score += 1
    return score


def rank_cluster(items: list[dict]) -> tuple[dict, list[dict]]:
    """クラスタ内で「残す1件」と「削除候補リスト」を決める。

    ソートキー: (充実度スコア 降順, extracted_at 降順[Noneは最小扱い], id 降順)。
    先頭を keep、残りを delete 候補として返す。
    """
    def sort_key(item: dict) -> tuple[int, str, int]:
        return (
            _enrichment_score(item),
            item.get("extracted_at") or "",
            item.get("id") or 0,
        )

    ordered = sorted(items, key=sort_key, reverse=True)
    return ordered[0], ordered[1:]


def _union_find_clusters(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, j in edges:
        union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        clusters[find(idx)].append(idx)
    return [members for members in clusters.values() if len(members) >= 2]


_MAX_LLM_REVIEW_PAIRS = 200


def _llm_review_borderline_pairs(
    items: list[dict],
    borderline: list[tuple[int, int, float]],
) -> set[tuple[int, int]]:
    """境界帯ペアをローカルLLMに1回のバッチコールで審査させる。

    パース失敗・件数不一致等の異常時は保守的に全ペア same=false 扱いとする。
    件数が _MAX_LLM_REVIEW_PAIRS を超える場合は類似度上位のみ審査し、
    超過分は審査せず same=false（別物）扱いとする。
    """
    if len(borderline) > _MAX_LLM_REVIEW_PAIRS:
        print(f"[WARN] detect_semantic_duplicates: 境界帯ペアが多すぎるため"
              f" (件数={len(borderline)}) 類似度上位 {_MAX_LLM_REVIEW_PAIRS} 件のみ審査、"
              f"残りは別物扱い", file=sys.stderr)
        borderline = sorted(borderline, key=lambda t: t[2], reverse=True)[:_MAX_LLM_REVIEW_PAIRS]

    from cli_utils import call_argus_llm

    lines = []
    for n, (i, j, _sim) in enumerate(borderline, 1):
        lines.append(
            f"ペア{n}:\n"
            f"  A: {items[i]['content']}\n"
            f"  B: {items[j]['content']}"
        )
    prompt = (
        "以下のペアそれぞれについて、A と B が同じ意図・目的のアクションアイテムか判定してください。\n"
        "表現・語順が違っても意図が同じなら same=true、異なる作業・対象なら same=false としてください。\n\n"
        + "\n\n".join(lines)
        + "\n\n出力は JSON 配列のみ: "
        '[{"pair": 1, "same": true}, {"pair": 2, "same": false}, ...]'
    )
    max_tokens = min(max(1024, len(borderline) * 64), 8192)
    try:
        resp = call_argus_llm(
            prompt,
            system="あなたは重複判定器。出力は JSON 配列のみ。",
            think=False,
            max_tokens=max_tokens,
        )
        parsed = json.loads(_extract_json_array(resp))
        if not isinstance(parsed, list) or len(parsed) != len(borderline):
            raise ValueError(
                f"LLM応答の件数不一致: expected={len(borderline)} got="
                f"{len(parsed) if isinstance(parsed, list) else type(parsed)}"
            )
        confirmed: set[tuple[int, int]] = set()
        for n, (i, j, _sim) in enumerate(borderline, 1):
            entry = parsed[n - 1]
            if not isinstance(entry, dict) or "pair" not in entry or "same" not in entry:
                raise ValueError(f"LLM応答のペア{n}にキー欠落: {entry!r}")
            if bool(entry["same"]):
                confirmed.add((i, j))
        return confirmed
    except Exception as e:
        print(f"[WARN] detect_semantic_duplicates: LLM境界審査に失敗、全ペアを別物扱い"
              f" ({type(e).__name__}: {e})", file=sys.stderr)
        return set()


def _extract_json_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("応答にJSON配列が見つからない")
    return text[start:end + 1]


def detect_semantic_duplicates(
    items: list[dict],
    *,
    merge_threshold: float = 0.92,
    review_threshold: float = 0.85,
    use_llm: bool = True,
) -> list[tuple[str, list[dict]]]:
    """embedding コサイン類似度＋境界帯のローカルLLM審査で意味的重複を検出する。

    embedding サーバ未起動・API エラー時は警告を出して [] を返す（既存検出への影響なし）。
    """
    if len(items) < 2:
        return []

    from embed_utils import cosine_similarity_matrix, embed_batch, healthcheck

    if not healthcheck():
        print("[WARN] detect_semantic_duplicates: embedding サーバに接続できないためスキップ",
              file=sys.stderr)
        return []

    contents = [it["content"] for it in items]
    try:
        mat = embed_batch(contents)
    except Exception as e:
        print(f"[WARN] detect_semantic_duplicates: embedding 取得に失敗しスキップ"
              f" ({type(e).__name__}: {e})", file=sys.stderr)
        return []

    n = len(items)
    edges: list[tuple[int, int]] = []
    borderline: list[tuple[int, int, float]] = []
    for i in range(n):
        if i + 1 >= n:
            continue
        sims = cosine_similarity_matrix(mat[i], mat[i + 1:])
        for offset, sim in enumerate(sims):
            j = i + 1 + offset
            sim = float(sim)
            if sim >= merge_threshold:
                edges.append((i, j))
            elif sim >= review_threshold:
                borderline.append((i, j, sim))

    if use_llm and borderline:
        confirmed = _llm_review_borderline_pairs(items, borderline)
        for i, j in confirmed:
            edges.append((i, j))

    clusters = _union_find_clusters(n, edges)
    return [
        ("semantic_dup", [items[idx] for idx in members])
        for members in clusters
    ]


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
        "exact_dup":    "完全重複（正規化後一致）",
        "near_dup":     "類似重複（先頭一致・表現違い）",
        "semantic_dup": "意味的重複（同義・表現違い）",
        "ambiguous":    "曖昧・短すぎ（文脈なしで意味不明）",
    }

    for cat in ["exact_dup", "near_dup", "semantic_dup", "ambiguous"]:
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


def _rank_ai_findings_for_export(
    ai_findings: list[tuple[str, list[dict]]],
) -> tuple[dict[int, str], dict[int, list[int]]]:
    """exact_dup/semantic_dup グループに rank_cluster を適用し、
    (id → 事前 deleted 値, id → その id が属するグループの出力順 id リスト) を返す。

    近似重複(near_dup)・曖昧(ambiguous) のみでフラグされた id は対象外
    （呼び出し側で従来通り空欄・id 昇順のまま扱う）。
    """
    deleted_prefill: dict[int, str] = {}
    order_hint: dict[int, list[int]] = {}
    for cat, group in ai_findings:
        if cat not in ("exact_dup", "semantic_dup"):
            continue
        keep, deletes = rank_cluster(group)
        ordered_ids = [keep["id"]] + [d["id"] for d in deletes]
        deleted_prefill[keep["id"]] = ""
        for d in deletes:
            deleted_prefill[d["id"]] = "1"
        for i in ordered_ids:
            order_hint[i] = ordered_ids
    return deleted_prefill, order_hint


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

    deleted_prefill, order_hint = _rank_ai_findings_for_export(ai_findings)
    by_id = {a["id"]: a for a in all_ais}

    emitted: set[int] = set()
    flagged_ais: list[dict] = []
    for a in all_ais:
        aid = a["id"]
        if aid not in ai_flagged or aid in emitted:
            continue
        if aid in order_hint:
            for gid in order_hint[aid]:
                if gid in by_id and gid not in emitted:
                    flagged_ais.append(by_id[gid])
                    emitted.add(gid)
        else:
            flagged_ais.append(a)
            emitted.add(aid)

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
            "deleted": deleted_prefill.get(a["id"], ""),
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


def _ai_group_with_keep(cat: str, group: list[dict]) -> list[dict]:
    """アクションアイテムのグループを keep 先頭順に並べ替え、各 item に keep を付与する。

    元の ais/decs リストの dict オブジェクトは他カテゴリのグループとも共有されて
    いるため、in-place 変異せず各 item を浅コピーしてからグループローカルに keep を
    設定する（複数カテゴリに跨る item の keep が後勝ちで上書きされるのを防ぐ）。
    """
    if cat == "ambiguous" or len(group) < 2:
        return [dict(it, keep=True) for it in group]
    keep, deletes = rank_cluster(group)
    result = [dict(keep, keep=True)]
    result.extend(dict(d, keep=False) for d in deletes)
    return result


def _dec_group_with_keep(group: list[dict]) -> list[dict]:
    """決定事項のグループは先頭 = 残す推奨のまま keep を付与する（並べ替えなし）。

    _ai_group_with_keep と同様、元の dict は浅コピーしてから keep を設定する。
    """
    return [dict(it, keep=(i == 0)) for i, it in enumerate(group)]


def screen_for_web(
    conn,
    *,
    include_decisions: bool = False,
    short_threshold: int = 25,
    prefix_len: int = 20,
    since: str | None = None,
    semantic: bool = True,
    merge_threshold: float = 0.92,
    review_threshold: float = 0.85,
    use_llm: bool = True,
) -> dict:
    """Web UI 向けに重複・類似・曖昧グループを JSON 互換 dict で返す。

    返り値:
      {
        "action_items": {
          "groups": [{"category": "exact_dup"|"near_dup"|"semantic_dup"|"ambiguous",
                       "items": [{id, content, assignee, due_date, source, ..., keep}, ...]}],
          "total_flagged": int,
        },
        "decisions": {... 同上, include_decisions=True のときのみ},
      }
    各グループの items は keep=True の推奨行が先頭。
    """
    ais = fetch_active_action_items(conn, since=since)
    ai_groups: list[dict] = []
    ai_flagged: set[int] = set()
    for cat, group in detect_exact_duplicates(ais):
        ai_groups.append({"category": cat, "items": _ai_group_with_keep(cat, group)})
        for it in group:
            ai_flagged.add(it["id"])
    for cat, group in detect_near_duplicates(ais, prefix_len):
        ai_groups.append({"category": cat, "items": _ai_group_with_keep(cat, group)})
        for it in group:
            ai_flagged.add(it["id"])
    if semantic:
        for cat, group in detect_semantic_duplicates(
            ais,
            merge_threshold=merge_threshold,
            review_threshold=review_threshold,
            use_llm=use_llm,
        ):
            ai_groups.append({"category": cat, "items": _ai_group_with_keep(cat, group)})
            for it in group:
                ai_flagged.add(it["id"])
    for cat, group in detect_ambiguous(ais, short_threshold):
        ai_groups.append({"category": cat, "items": _ai_group_with_keep(cat, group)})
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
            dec_groups.append({"category": cat, "items": _dec_group_with_keep(group)})
            for it in group:
                dec_flagged.add(it["id"])
        for cat, group in detect_near_duplicates(decs, prefix_len):
            dec_groups.append({"category": cat, "items": _dec_group_with_keep(group)})
            for it in group:
                dec_flagged.add(it["id"])
        for cat, group in detect_ambiguous(decs, short_threshold):
            dec_groups.append({"category": cat, "items": _dec_group_with_keep(group)})
            for it in group:
                dec_flagged.add(it["id"])
        result["decisions"] = {"groups": dec_groups, "total_flagged": len(dec_flagged)}

    return result


# --------------------------------------------------------------------------- #
# --triage: 既存データの一括トリアージ（3ゲート審査、重複検出とは独立したモード）
# --------------------------------------------------------------------------- #
def _fetch_meeting_context(conn, meeting_id: str) -> tuple[str, str, str]:
    row = conn.execute(
        "SELECT kind, held_at, summary FROM meetings WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    if not row:
        return "不明", "不明", ""
    return row["kind"] or "不明", row["held_at"] or "不明", row["summary"] or ""


def run_triage(conn, since: str | None, include_closed: bool, output_path: str, log) -> None:
    """既存の action_items / decisions を meeting_id 単位（+ slack由来はバッチ）で
    ingest.slack.triage_items_batched にかけ、DROP判定の項目のみを CSV に出力する。

    バッチ分割・チャンク単位の障害フェイルオープン（missing_verdict="KEEP"）は
    ingest.slack.triage_items_batched に委譲する（minutes 転記時トリアージと共用）。
    """
    from ingest.slack import fetch_milestones, triage_items_batched

    ais = fetch_active_action_items(conn, since=since)
    if not include_closed:
        ais = [a for a in ais if (a.get("status") or "open") == "open"]
    decs = fetch_active_decisions(conn, since=since)

    log(f"対象: アクションアイテム {len(ais)} 件, 決定事項 {len(decs)} 件")

    milestones = fetch_milestones(conn)
    if not milestones:
        log("[WARN] マイルストーン未登録のためトリアージをスキップします（全件 KEEP 扱い）")
        log(f"合計: アクションアイテム KEEP={len(ais)} / DROP=0")
        log(f"合計: 決定事項       KEEP={len(decs)} / DROP=0")
        export_triage_csv([], [], output_path, log)
        return

    ai_by_meeting: dict[str, list[dict]] = defaultdict(list)
    ai_slack: list[dict] = []
    for a in ais:
        mid = a.get("meeting_id")
        if mid:
            ai_by_meeting[mid].append(a)
        else:
            ai_slack.append(a)

    dec_by_meeting: dict[str, list[dict]] = defaultdict(list)
    dec_slack: list[dict] = []
    for d in decs:
        mid = d.get("meeting_id")
        if mid:
            dec_by_meeting[mid].append(d)
        else:
            dec_slack.append(d)

    meeting_ids = sorted(set(ai_by_meeting) | set(dec_by_meeting))
    n_groups = len(meeting_ids) + (1 if (ai_slack or dec_slack) else 0)

    dropped_ai: list[tuple[dict, str]] = []
    dropped_dec: list[tuple[dict, str]] = []
    n_processed = 0
    n_chunks_total = n_chunks_skipped = 0

    def _apply(batched: dict) -> None:
        nonlocal n_chunks_total, n_chunks_skipped
        n_chunks_total += batched["n_chunks"]
        n_chunks_skipped += batched["n_skipped_chunks"]
        for item, verdict, reason in batched["action_items"]:
            if verdict == "DROP":
                dropped_ai.append((item, reason))
        for item, verdict, reason in batched["decisions"]:
            if verdict == "DROP":
                dropped_dec.append((item, reason))

    def _skip_note(batched: dict) -> str:
        if not batched["n_skipped_chunks"]:
            return ""
        return f"（スキップ {batched['n_skipped_chunks']}/{batched['n_chunks']} チャンク）"

    for mid in meeting_ids:
        n_processed += 1
        kind, held_at, summary = _fetch_meeting_context(conn, mid)
        context_note = (
            "### 会議コンテキスト\n"
            f"会議種別: {kind} / 開催日: {held_at}\n"
            f"議事概要: {summary[:1500]}"
        )
        batched = triage_items_batched(
            ai_by_meeting.get(mid, []), dec_by_meeting.get(mid, []), milestones,
            context_note=context_note, missing_verdict="KEEP", log=log,
            group_label=f"meeting={mid}",
        )
        _apply(batched)
        log(f"[{n_processed}/{n_groups}] meeting={mid}: 完了"
            f"（AI {len(batched['action_items'])}件, 決定 {len(batched['decisions'])}件）"
            f"{_skip_note(batched)}")

    if ai_slack or dec_slack:
        n_processed += 1
        context_note = (
            "### 会議コンテキスト\n"
            "Slackスレッド由来の抽出項目（特定の会議に紐づかない候補のバッチ審査）\n"
        )
        batched = triage_items_batched(
            ai_slack, dec_slack, milestones,
            context_note=context_note, missing_verdict="KEEP", log=log,
            group_label="slackバッチ",
        )
        _apply(batched)
        log(f"[{n_processed}/{n_groups}] slackバッチ: 完了"
            f"（AI {len(batched['action_items'])}件, 決定 {len(batched['decisions'])}件）"
            f"{_skip_note(batched)}")

    # KEEP 件数は「対象件数 - DROP件数」で計算する（チャンク障害でスキップされた
    # 項目は KEEP/DROP いずれの判定もされないが、既存レコードに変更を加えない
    # という意味で実質 KEEP と同義のため、この引き算で正しく数えられる）。
    n_keep_ai = len(ais) - len(dropped_ai)
    n_keep_dec = len(decs) - len(dropped_dec)

    log("")
    log(f"合計: アクションアイテム KEEP={n_keep_ai} / DROP={len(dropped_ai)}")
    log(f"合計: 決定事項       KEEP={n_keep_dec} / DROP={len(dropped_dec)}")
    if n_chunks_skipped:
        log(f"[WARN] {n_chunks_skipped}/{n_chunks_total} チャンクがLLM障害等でスキップされました"
            "（該当項目は KEEP 扱い）")

    export_triage_csv(dropped_ai, dropped_dec, output_path, log)


def _flatten_reason(reason: str) -> str:
    """reason 内の改行を空白に正規化する（pm_relink.py の splitlines 行パーサが
    改行入りセルで壊れるのを防ぐ。S4）。"""
    return (reason or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def export_triage_csv(
    dropped_ai: list[tuple[dict, str]],
    dropped_dec: list[tuple[dict, str]],
    output_path: str,
    log,
) -> None:
    buf = io.StringIO()
    buf.write("# LLM 判定の一括審査結果。pm_relink.py --import --dry-run で確認してから適用すること\n")

    buf.write(_SECTION_ACTIONS + "\n")
    ai_cols = ["id", "assignee", "due_date", "milestone_id", "status", "content",
               "source", "extracted_at", "note", "deleted", "reason"]
    writer = csv.DictWriter(buf, fieldnames=ai_cols, extrasaction="ignore")
    writer.writeheader()
    for a, reason in dropped_ai:
        writer.writerow({
            "id": a["id"],
            "assignee": a.get("assignee") or "",
            "due_date": a.get("due_date") or "",
            "milestone_id": a.get("milestone_id") or "",
            "status": a.get("status") or "",
            "content": a["content"],
            "source": a.get("source") or "",
            "extracted_at": a.get("extracted_at") or "",
            "note": a.get("note") or "",
            "deleted": "1",
            "reason": _flatten_reason(reason),
        })

    buf.write("\n" + _SECTION_DECISIONS + "\n")
    dec_cols = ["id", "content", "decided_at", "source", "deleted", "reason"]
    writer2 = csv.DictWriter(buf, fieldnames=dec_cols, extrasaction="ignore")
    writer2.writeheader()
    for d, reason in dropped_dec:
        writer2.writerow({
            "id": d["id"],
            "content": d["content"],
            "decided_at": d.get("decided_at") or "",
            "source": d.get("source") or "",
            "deleted": "1",
            "reason": _flatten_reason(reason),
        })

    text = buf.getvalue()
    Path(output_path).write_text(text, encoding="utf-8")
    log(f"\nCSV 出力: {output_path}")
    log(f"  DROP判定 アクションアイテム: {len(dropped_ai)} 件")
    log(f"  DROP判定 決定事項: {len(dropped_dec)} 件")
    log()
    log("使い方:")
    log("  1. LLM 判定の一括審査結果。内容を確認してから適用すること")
    log("  2. pm_relink.py --import でDB反映:")
    log(f"     python3 scripts/pm_relink.py --import {output_path} --dry-run")
    log(f"     python3 scripts/pm_relink.py --import {output_path}")


# --------------------------------------------------------------------------- #
# --second-opinion-minutes: 議事録経路への第2系統（独立系統）差分検査
#   docs/security-architecture.md §4.9 対策3+5 / R8 の第2系統を議事録経路へ拡張したもの。
#
# 議事録生成 LLM（主系統: glm / DeepSeek / Qwen、いずれも同系統）が文字起こしにあった
# 決定事項・アクションアイテムを静かに落としても、現状それを検出する手段がない。
# Slack Pass 1 抽出（ingest/slack.py）・Box relevance 判定（pm_box_relevance.py）に続く
# 3経路目として、議事録の文字起こし原文（data/processing/ に保存済み）を後から・
# まとめて・過去に遡って第2系統（Llama-4-Scout, RiVault配信）に独立抽出させ、
# 保存済みの決定事項・アクションアイテムと突合する。
#
# 何を捕まえないか（重要）:
#   - 捕まえるのは欠落だけ。議事録に載っている内容が正しいかは検証しない
#     （それは引用スパン照合という別の仕組み）
#   - 両方の系統が同じように見落とした場合は検出できない
#   - 第2系統は Llama-4-Scout（RiVault配信）で、主系統（glm/DeepSeek/Qwen、いずれも
#     同系統）に対する唯一の独立系統である。K3 を主系統に足しても独立系統は1本のまま
#   - **入力の階層によって検出できる欠落の範囲が変わる**（_resolve_transcript_for_meeting
#     の優先順位参照）。vtt / whisper_raw は主系統の議事録生成パイプライン（Stage 1/2/3）
#     より前段の独立入力のため Stage 1〜3 いずれの欠落も検出しうるが、combined.txt は
#     主系統 Stage 1 の出力そのものであり、**Stage 1 で既に落ちた項目は combined.txt にも
#     現れないため原理的に検出できない**（Stage 2/3 の欠落のみ検出対象になる）。
#     どの階層を使ったかは record_second_opinion の content プレフィックスに残す
#     （vtt / whisper_raw / combined_degraded）。
#
# --reader オプション: 「読み手」を切り替える（2026-08 追加）。
#   second（既定） — 上記の第2系統（Llama-4-Scout, RiVault配信）。R8（提供元レベルの
#     集中リスク）対策そのもの。kind="minutes_extraction"（従来どおり）。
#   k3            — kimi-k3（RIKYU配信、call_local_llm）を read-only の recall チェック役
#     として追加する。kind="minutes_extraction_recall" で別集計する。
#     **K3 は R8 の独立系統ではない** — Moonshot は主系統（glm/DeepSeek/Qwen）と同じく
#     「本番経路が一系統に寄っている」構図の外に出られない。K3 を議事録生成そのものに
#     使わない（書かせない）理由は、実測で判明した用語一致率の低さ・所要時間・失敗率
#     （docs/kimi-k3-migration.md「実測との突合」節）。ここでは K3 に議事録を書かせず、
#     欠落を指摘させるだけなので、その弱点は問題にならない：
#       - 形式・用語を要求しない（項目を挙げるだけで指示追従の弱さが出ない）
#       - クリティカルパス外の後追いバッチなので所要時間が問題にならない
#       - 失敗しても議事録は既に完成済み
#       - 出力は triage_second_opinion に記録するだけで、議事録DB・pm.dbの
#         action_items/decisions には一切入らない
#     kimi-k3 は 2026-08-03 に PM 判断で production: true になった（読み手専用）。
#     ただし declared_trust_remote_code は依然 null（未確認）であり、
#     model_pin.yaml の risk_accepted に受容の経緯が記録されている。
#     ★pin は役割を区別しない: production: true は「議事録生成に使ってよい」ことを
#     意味しない。生成に使わないのは role の記述と運用規律による制約であって、
#     pin が機械的に防いでいるわけではない。
#   both          — 両方を順に実行し、それぞれの kind で記録する。
#
# 議事録DB・pm.db の action_items/decisions は一切書き換えない。記録は
# pm.db の triage_second_opinion（record_second_opinion 経由）のみ。
# --------------------------------------------------------------------------- #

# --reader の選択肢と、各読み手の record_second_opinion kind の対応。
_READER_KIND = {
    "second": "minutes_extraction",
    "k3": "minutes_extraction_recall",
}


def _resolve_readers(reader: str) -> list[str]:
    """--reader の値（second/k3/both）から実行する読み手のリストを返す。"""
    if reader == "both":
        return ["second", "k3"]
    return [reader]

# generate_minutes_local.py の出力命名規則（同一 `now` から生成されるため ts が一致する）:
#   {ts}-{basename}-minutes.md   … pm_minutes_import.py --no-llm がインポートする議事録
#   {ts}-{basename}-combined.txt … Stage 1 抽出結果のキャッシュ（デバッグ・再実行用）
# instances.file_path の stem からこの ts / basename を逆算し、data/processing/ 内の
# 対応する文字起こし原文を探す。
_MINUTES_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-(.+)-minutes$")


def _stem_of_file_path(file_path: str | None) -> str:
    """instances.file_path の拡張子抜きファイル名（Path(file_path).stem）を返す。

    --meeting-stem による絞り込みで使う識別子。file_path が空なら空文字を返す。
    """
    return Path(file_path).stem if file_path else ""

_DEFAULT_SECOND_OPINION_SINCE_DAYS = 30
_DEFAULT_SECOND_OPINION_LIMIT = 10
# 1会議（かつ1読み手）あたりの記録件数の上限。読み手の粒度が主系統より細かい場合
# （例: K3が日程調整・事務連絡まで拾う）、突合の閾値の粗さと相まって大量の
# 「欠落」誤検出を作ってしまうため、上位N件で打ち切りWARNを出す（2026-08 追加）。
#
# 10 → 25 → 100 と引き上げた（いずれも 2026-08-04）。**上限に張り付いている限り
# 「全部見た」と言えない**というのが引き上げの理由で、25 でも足りなかった
# （同一会議で K3 が 26 件、Scout が 26 件を検出。10 の時点では 44 件中 24 件を
# 捨てていた）。打ち切りは件数しかログに残らず**本文はどこにも保存されない**ため、
# 後から「何を捨てたか」を検証できない。
#
# **100 は「妥当な件数」ではなく事故防止の last resort** — 突合が壊れて全件が
# 「欠落」判定になったときに台帳を埋め尽くすのを止めるためだけの値。量の制御は
# 記録が残る側（レビューで落とす・読み手のプロンプトを直す・重複排除）で行う。
# 1 会議で 100 件に達したら、それは読み手か突合のどちらかが壊れている兆候として
# WARN を読むべきで、上限をさらに上げて対処してよいものではない。
_DEFAULT_MAX_FINDINGS_PER_MEETING = 100

# 入力階層の表示名（ログ・レポート用）
_TIER_LABELS = {
    "vtt": "VTT（Zoom生成、主系統から独立）",
    "whisper_raw": "生Whisper文字起こし（主系統の議事録生成LLMより前段）",
    "combined_degraded": "combined.txt（主系統Stage1出力、降格）",
}

# VTT ファイル名は mp4/combined.txt と共有する {basename} に解像度サフィックス
# （`_1280x948` 等）やブラウザ重複DLサフィックス（` (1)` 等）が付くと、Zoom が実際に
# 書き出す `{stem}.transcript.vtt` / `{stem}.vtt` と一致しなくなる。
# pm_from_recording.sh（VTT自動検出）・scripts/recording/transcribe_pipeline.py の
# download_vtt() と同じ stem 派生規則を踏襲する（新しい照合規則は作らない）。
_RES_SUFFIX_RE = re.compile(r"_\d+x\d+$")
_DUP_SUFFIX_RE = re.compile(r" ?\(\d+\)$")


def _vtt_candidates_for_basename(basename: str) -> list[str]:
    """basename から Zoom VTT のファイル名候補（解像度・重複DLサフィックス剥がし後の
    バリアント × `.transcript.vtt`/`.vtt`）を列挙する。"""
    stem_nores = _RES_SUFFIX_RE.sub("", basename)
    stem_nodup = _DUP_SUFFIX_RE.sub("", basename)
    stem_bare = _RES_SUFFIX_RE.sub("", stem_nodup)
    variants: list[str] = []
    for s in (basename, stem_nores, stem_nodup, stem_bare):
        if s and s not in variants:
            variants.append(s)
    candidates: list[str] = []
    for s in variants:
        candidates.extend([f"{s}.transcript.vtt", f"{s}.vtt"])
    return candidates

# combined.txt は発話ごとのタイムスタンプを保持しないため generate_minutes_local.py の
# chunk_transcript（セグメントの start/end 時刻が必要）が使えず、文字数分割にフォールバック
# する。1800 は generate_minutes_local.py の target_chars 算出式
# `min(6000, max(800, chunk_minutes * 60))` に chunk_minutes=30 を当てはめた値を踏襲する
# （Stage 1 のチャンク要約1件がおおよそこの文字数になるよう生成されているため、
# combined.txt 全体を同程度の大きさで割ることで擬似的に「30分チャンク」に近づける）。
_COMBINED_CHUNK_CHARS = 1800


def _default_second_opinion_since(days: int = _DEFAULT_SECOND_OPINION_SINCE_DAYS) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _resolve_transcript_for_meeting(
    file_path: str | None, processing_dir: Path,
) -> tuple[Path | None, str]:
    """instances.file_path から対応する文字起こし原文を data/processing/ で探す。

    優先順位（独立性が高い順）:
      1. **VTT**（`{basename}.transcript.vtt` / `{basename}.vtt`、解像度・重複DL
         サフィックス違いを含む。`_vtt_candidates_for_basename` 参照）— Zoom が
         生成した生の文字起こし。Argus のパイプラインの外で作られたものであり、
         主系統から完全に独立している。最優先で採用する
      2. 生の文字起こし（{basename}.md / {basename}.txt）— 自前 Whisper の出力だが、
         議事録生成 LLM（主系統）より前段のため独立性は保たれる
      3. **降格扱い**: Stage 1 の combined.txt キャッシュ（{ts}-{basename}-combined.txt）
         — 主系統自身の Stage 1 出力そのものであり、Stage 1 で落ちた項目はここにも
         現れない（Stage 2/3 の欠落のみ検出できる）。生の文字起こしが通常クリーンアップ
         済みのため、実データではこちらが主な経路になる

    file_path が `{ts}-{basename}-minutes` の命名規則に一致しない場合
    （pm_minutes_import.py への直接インポート等）は、stem そのものを basename として
    VTT・生の文字起こしのみを探す（combined.txt は同じ ts 前提の対応関係が成立しないため探さない）。

    戻り値: (見つかったパス, "vtt"|"whisper_raw"|"combined_degraded")。
    見つからなければ (None, "")。
    """
    if not file_path:
        return None, ""
    stem = Path(file_path).stem
    m = _MINUTES_STEM_RE.match(stem)
    basename = m.group(2) if m else stem
    ts = m.group(1) if m else None

    for vtt_name in _vtt_candidates_for_basename(basename):
        vtt_cand = processing_dir / vtt_name
        if vtt_cand.is_file():
            return vtt_cand, "vtt"

    for ext in (".md", ".txt"):
        cand = processing_dir / f"{basename}{ext}"
        if cand.is_file():
            return cand, "whisper_raw"

    if ts:
        cand = processing_dir / f"{ts}-{basename}-combined.txt"
        if cand.is_file():
            return cand, "combined_degraded"

    return None, ""


def _split_by_chars(text: str, size: int = _COMBINED_CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def _chunk_meeting_text(path: Path, source_kind: str) -> list[str]:
    """会議の文字起こし原文を主系統と同じ30分チャンクに分割する。

    source_kind == "vtt": scripts/utils/transcript.py の parse_vtt をそのまま再利用する。
    parse_vtt が返す start/end は "HH:MM:SS" 文字列のため、_ts_to_sec で秒数へ変換して
    generate_minutes_local.py の chunk_transcript（デフォルト1800秒=30分）に渡す。

    source_kind == "whisper_raw": generate_minutes_local.py の parse_transcript +
    chunk_transcript をそのまま再利用する（Whisper VAD 形式・reconcile_transcript.py
    出力形式の両方に対応済み）。

    source_kind == "combined_degraded": Stage 1 の combined.txt キャッシュは発話ごとの
    タイムスタンプを持たない要約テキストのため chunk_transcript が使えず、
    文字数で分割する（_COMBINED_CHUNK_CHARS 参照）。
    """
    from recording.generate_minutes_local import chunk_transcript, format_transcript

    if source_kind == "vtt":
        from utils.transcript import _ts_to_sec, parse_vtt
        vtt_segments = parse_vtt(str(path))
        segments = [
            {"speaker": s["speaker"], "start": _ts_to_sec(s["start"]),
             "end": _ts_to_sec(s["end"]), "text": s["text"]}
            for s in vtt_segments
        ]
        if segments:
            return [format_transcript(c) for c in chunk_transcript(segments)]
        # VTT形式に一致しない（空・壊れたファイル等）場合は文字数分割にフォールバック
        return _split_by_chars(path.read_text(encoding="utf-8"))

    if source_kind == "whisper_raw":
        from recording.generate_minutes_local import parse_transcript
        segments = parse_transcript(str(path))
        if segments:
            return [format_transcript(c) for c in chunk_transcript(segments)]
        # 既知の2形式（Whisper VAD / reconcile_transcript.py 出力）に一致しない場合は
        # 文字数分割にフォールバックする
        return _split_by_chars(path.read_text(encoding="utf-8"))

    return _split_by_chars(path.read_text(encoding="utf-8"))


def _collect_meeting_candidates(
    minutes_dir: Path, since: str, no_encrypt: bool, log,
) -> list[dict]:
    """data/minutes/*.db から since 以降の会議を列挙する。

    各要素: {"kind", "meeting_id", "held_at", "file_path",
             "decisions": [content...], "action_items": [content...],
             "minutes_content": 議事録本文（無ければ空文字）}
    """
    candidates: list[dict] = []
    if not minutes_dir.is_dir():
        return candidates

    for db_path in sorted(minutes_dir.glob("*.db")):
        kind = db_path.stem
        try:
            conn = open_db(db_path, encrypt=not no_encrypt)
        except Exception as e:
            log(f"[WARN] 議事録DBを開けませんでした ({db_path.name}): {e}")
            continue
        try:
            if not table_exists(conn, "instances"):
                continue
            has_content = table_exists(conn, "minutes_content")
            rows = conn.execute(
                "SELECT meeting_id, held_at, file_path FROM instances"
                " WHERE held_at >= ? ORDER BY held_at DESC",
                (since,),
            ).fetchall()
            for r in rows:
                meeting_id = r["meeting_id"]
                decs = conn.execute(
                    "SELECT content FROM decisions WHERE meeting_id=?", (meeting_id,)
                ).fetchall()
                ais = conn.execute(
                    "SELECT content FROM action_items WHERE meeting_id=?", (meeting_id,)
                ).fetchall()
                minutes_content = ""
                if has_content:
                    mc_row = conn.execute(
                        "SELECT content FROM minutes_content WHERE meeting_id=? ORDER BY id",
                        (meeting_id,),
                    ).fetchall()
                    minutes_content = "\n".join(mc["content"] for mc in mc_row if mc["content"])
                candidates.append({
                    "kind": kind,
                    "meeting_id": meeting_id,
                    "held_at": r["held_at"],
                    "file_path": r["file_path"],
                    "decisions": [d["content"] for d in decs],
                    "action_items": [a["content"] for a in ais],
                    "minutes_content": minutes_content,
                })
        finally:
            conn.close()
    return candidates


_TRIAGE_REF_TAG_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")


def _existing_finding_bodies(pm_conn, *, kind: str, meeting_id: str) -> list[tuple[int, str]]:
    """同じ会議・同じ読み手（kind）について既に記録済みの所見本文を返す。

    `record_second_opinion` は content を `"[kind/meeting_id][tier][reader=..] 本文"`
    の形で保存するため、先頭の `[...]` タグ列を取り除いて本文だけを取り出す。
    tier タグ（vtt / combined_degraded）は照合条件に入れない — 入力階層が変わっても
    同じ項目は同じ項目なので、階層違いで二重に記録したくない。

    戻り値: [(id, 本文), ...]。テーブル未作成なら空リスト。
    """
    from db_utils import table_exists

    if not table_exists(pm_conn, "triage_second_opinion"):
        return []
    rows = pm_conn.execute(
        "SELECT id, content_head FROM triage_second_opinion"
        " WHERE kind = ? AND content_head LIKE ?",
        (kind, f"%{meeting_id}%"),
    ).fetchall()
    out: list[tuple[int, str]] = []
    for r in rows:
        head = (r[1] if not hasattr(r, "keys") else r["content_head"]) or ""
        rid = r[0] if not hasattr(r, "keys") else r["id"]
        out.append((rid, _TRIAGE_REF_TAG_RE.sub("", head).strip()))
    return out


def run_second_opinion_minutes(
    pm_conn,
    *,
    since: str | None,
    limit: int,
    dry_run: bool,
    minutes_dir: Path,
    processing_dir: Path,
    no_encrypt: bool,
    log,
    reader: str = "second",
    max_findings_per_meeting: int = _DEFAULT_MAX_FINDINGS_PER_MEETING,
    meeting_stem: str | None = None,
    dedup_existing: bool = True,
) -> None:
    """議事録経路への第2系統差分検査バッチ本体。

    議事録DB・pm.db の action_items/decisions は一切変更しない。記録先は
    pm.db の triage_second_opinion（record_second_opinion 経由）のみ。

    何を捕まえないか（重要）:
      - 捕まえるのは欠落だけ。議事録に載っている内容が正しいかは検証しない
        （それは引用スパン照合という別の仕組み）
      - 両方の系統が同じように見落とした場合は検出できない
      - 第2系統は Llama-4-Scout（RiVault配信）で、主系統（glm/DeepSeek/Qwen、
        いずれも同系統）に対する唯一の独立系統である。K3 を主系統に足しても
        独立系統は1本のまま
      - **入力の階層で検出できる範囲が変わる**（_resolve_transcript_for_meeting 参照）。
        vtt / whisper_raw は主系統の議事録生成（Stage 1〜3）より前段の独立入力なので
        いずれの段の欠落も検出しうるが、combined.txt（"combined_degraded"）は主系統
        Stage 1 自身の出力であり、**Stage 1 で落ちた項目はそこにも現れないため
        原理的に検出できない**（Stage 2/3 の欠落のみ検出対象）。この会議は処理対象から
        除外しない（Stage2/3はK3が入る段のため検出価値がある）が、その旨をWARNで
        明示し、record_second_opinion の content にも階層タグを残す

    reader: "second"（既定）/ "k3" / "both"。上のファイルヘッダコメント参照。
        second は R8 対策の第2系統（変更なし）、k3 は kimi-k3 による recall チェック
        （R8 の独立系統ではない。品質目的のみ）。

    max_findings_per_meeting: 1会議・1読み手あたりに記録する件数の上限
        （既定 _DEFAULT_MAX_FINDINGS_PER_MEETING）。超えた場合はチャンク処理順で
        上位N件のみ記録し、切り捨てた件数をWARNに残す（黙って打ち切らない）。
        **既定値は「妥当な件数」ではなく事故防止の last resort**（定数のコメント参照）。

    dedup_existing: 既に記録済みの所見（同じ会議・同じ読み手 kind）と重複する項目を
        記録しない（既定 True）。**読み手の抽出は再現しない**ため同じ会議を複数回
        読ませて検出を積み増す使い方が有効で、その際に台帳が重複で埋まるのを防ぐ。
        照合は `_extraction_texts_match`（表現の揺れを吸収する）で行う。
        再現性そのものを測りたい場合は False にする（`--no-dedup-existing`）。

    meeting_stem: 指定時は instances.file_path の拡張子抜きファイル名
        （Path(file_path).stem。例: "{ts}-{basename}-minutes"）がこれと一致する
        会議1件だけを検査する。録音経路（pm_from_recording.sh）が処理直後に
        自分自身の会議だけを即時検査するための絞り込み。
        --since によるウィンドウは無視し（過去分も含めて全件から探す）、
        --limit も無視する（対象は常に高々1件のため）。一致する会議が見つから
        ない場合は例外にせず警告を出して何もせず正常終了する（録音ジョブ全体を
        落とさないため）。
    """
    if os.environ.get("ARGUS_SECOND_OPINION", "1").strip() not in ("1", "true", "yes"):
        log("[INFO] ARGUS_SECOND_OPINION が無効のため第2系統検査をスキップします")
        return

    readers = _resolve_readers(reader)
    if "k3" in readers:
        log("[INFO] --reader k3: kimi-k3 は読み手（recall 確認）専用として "
            "production: true（PM 判断 2026-08-03）。declared_trust_remote_code は"
            "未確認のままで、受容の経緯は config/model_pin.yaml の risk_accepted にある。"
            "R8 対策の第2系統（Llama-4-Scout）の代わりではない — 出自が主系統と同系統のため。")

    if meeting_stem:
        since_effective = "1970-01-01"
        log(f"[INFO] --meeting-stem 指定: {meeting_stem}"
            "（--since ウィンドウ・--limit は無視し、過去分も含めて全件から探します）")
    else:
        since_effective = since or _default_second_opinion_since()
    log(f"対象期間: {since_effective} 以降")

    candidates = _collect_meeting_candidates(minutes_dir, since_effective, no_encrypt, log)

    if meeting_stem:
        candidates = [c for c in candidates if _stem_of_file_path(c["file_path"]) == meeting_stem]
        if not candidates:
            log(f"[WARN] --meeting-stem に一致する会議が見つかりません: {meeting_stem}"
                "（第2系統検査をスキップします。録音・議事録処理自体には影響ありません）")
            return

    candidates.sort(key=lambda c: c["held_at"] or "", reverse=True)
    log(f"対象会議数: {len(candidates)} 件")

    resolved: list[tuple[dict, Path, str]] = []
    tier_counts: dict[str, int] = {"vtt": 0, "whisper_raw": 0, "combined_degraded": 0}
    n_not_found = 0
    for c in candidates:
        path, source_kind = _resolve_transcript_for_meeting(c["file_path"], processing_dir)
        if path is None:
            n_not_found += 1
            continue
        tier_counts[source_kind] += 1
        resolved.append((c, path, source_kind))

    log(f"文字起こしが見つかった会議: {len(resolved)} 件"
        f" / 見つからなかった会議: {n_not_found} 件（黙って飛ばさず件数を報告）")
    log("入力階層の内訳: "
        + " / ".join(
            f"{_TIER_LABELS[t]} {tier_counts[t]} 件" for t in ("vtt", "whisper_raw", "combined_degraded")
        ))
    if tier_counts["combined_degraded"]:
        log(f"[WARN] うち {tier_counts['combined_degraded']} 件は"
            " combined.txt（主系統のStage1出力）が入力です。"
            "Stage1で落ちた項目は原理的に検出できません"
            "（Stage2/3の欠落のみ検出対象。『全部見た』とは読めません）")

    if meeting_stem:
        pass  # --meeting-stem 指定時は絞り込み済みのため --limit を適用しない
    elif len(resolved) > limit:
        log(f"[WARN] 処理対象 {len(resolved)} 件が --limit {limit} を超えています。"
            f"先頭（開催日が新しい順）{limit} 件のみ処理します"
            "（黙って打ち切ると「全部見た」と誤読されるため明示します）")
        resolved = resolved[:limit]

    processed: list[tuple[dict, Path, str, list[str]]] = []
    for c, path, source_kind in resolved:
        try:
            chunks = _chunk_meeting_text(path, source_kind)
        except Exception as e:
            log(f"[WARN] 文字起こしの読み込み・分割に失敗しました"
                f"（この会議はスキップ）: {c['kind']}/{c['meeting_id']}: {e}")
            continue
        processed.append((c, path, source_kind, chunks))

    total_calls = sum(len(chunks) for _c, _p, _k, chunks in processed) * len(readers)
    log(f"推定LLM呼び出し回数: 対象会議 {len(processed)} 件 × チャンク数 × 読み手{len(readers)}種"
        f" 合計 {total_calls} 回（reader={reader}）")

    if dry_run:
        log("[INFO] --dry-run のためLLM呼び出し・DB書き込みは行いません")
        return

    from db_utils import record_second_opinion
    from ingest.slack import (
        _call_second_opinion_extraction,
        _extraction_texts_match,
        _load_second_opinion_config,
        compare_extractions,
        flag_sensitive_terms,
    )

    cfg_root = _load_second_opinion_config()
    # 読み手ごとの (route, model, kind, 出力タグ) を解決する。second は従来どおり
    # タグなし（既存の record_second_opinion content 互換のため）。
    reader_specs = []
    for r in readers:
        if r == "k3":
            reader_specs.append({
                "route": "k3",
                "model": (cfg_root.get("quality_reader") or {}).get("model") or "",
                "kind": _READER_KIND["k3"],
                "tag": "reader=k3",
            })
        else:
            reader_specs.append({
                "route": "rivault",
                "model": (cfg_root.get("second_opinion") or {}).get("model") or "",
                "kind": _READER_KIND["second"],
                "tag": None,
            })

    n_recorded = 0
    # ② 本文にはあるが抽出表に無い（除外） / ③ 抽出表にもある（除外）の内訳集計。
    # ①（真の欠落候補）は n_recorded に一致する。件数は「効きすぎて真の欠落まで
    # 落としていないか」を後から検証できるよう、除外した件数も必ずログに出す。
    n_excluded_in_haystack = 0
    n_excluded_in_table = 0
    # スキップしたチャンク数。**0 件でないなら「全部見た」と言ってはいけない**ため、
    # 最後の要約で必ず出す（LLM 呼び出しの失敗と突合の失敗を分けて数える）。
    n_call_errors = 0
    n_chunk_errors = 0
    n_chunks_total = 0
    n_dup_existing = 0
    for c, _path, source_kind, chunks in processed:
        base_ref = f"[{c['kind']}/{c['meeting_id']}][{source_kind}]"
        if source_kind == "combined_degraded":
            log(f"[WARN] {base_ref} は combined.txt（主系統のStage1出力）を入力にしています。"
                "Stage1で落ちた項目は検出できません（Stage2/3の欠落のみ検出対象）")

        primary = {
            "decisions": [{"content": x} for x in c["decisions"]],
            "action_items": [{"content": x} for x in c["action_items"]],
        }
        extra_haystack = c.get("minutes_content") or ""
        for spec in reader_specs:
            meeting_ref = base_ref + (f"[{spec['tag']}]" if spec["tag"] else "")
            seen: set[str] = set()
            # 上限判定のため、DB記録はチャンク処理が終わってから行う
            # （切り捨てた件数を正しくWARNに出すため、実際に見つかった総数が必要）。
            findings: list[dict] = []
            for chunk_text in chunks:
                n_chunks_total += 1
                try:
                    second, raw = _call_second_opinion_extraction(
                        chunk_text, model=spec["model"], route=spec["route"],
                    )
                except Exception as e:
                    n_call_errors += 1
                    log(f"[WARN] 第2系統(minutes, route={spec['route']}) の呼び出しに失敗"
                        f"（このチャンクはスキップ）: {e}")
                    continue
                # 突合・記録側の失敗もチャンク単位で閉じ込める。LLM 呼び出しだけを
                # try で囲っていた結果、2026-08-04 に第2系統の応答形式の逸脱
                # （素の文字列配列）が AttributeError になり、**会議1件の検査が
                # 丸ごと落ちて後続の読み手も走らなかった**。1チャンクの異常で
                # 全体を失わないようにする。**黙って飛ばさず件数を最後に必ず報告する**。
                try:
                    diff = compare_extractions(primary, second,
                                               extra_haystack=extra_haystack)
                    terms = flag_sensitive_terms(chunk_text)
                    for k in ("decisions", "action_items"):
                        n_excluded_in_table += diff.get(
                            "matched_in_table_counts", {}).get(k, 0)
                        n_excluded_in_haystack += diff.get(
                            "matched_in_haystack_counts", {}).get(k, 0)
                        for content in diff.get(k, []):
                            if not content.strip() or content in seen:
                                continue
                            seen.add(content)
                            findings.append({"kind": spec["kind"], "content": content,
                                             "terms": terms, "raw": raw})
                except Exception as e:
                    n_chunk_errors += 1
                    log(f"[ERROR] {base_ref} の突合に失敗（このチャンクはスキップ）: "
                        f"{type(e).__name__}: {e}")
                    continue

            # 既に記録済みの所見と重複するものを落とす。**同じ会議を2回検査すると
            # 所見が二重に並ぶ**問題への対策（2026-08-04 実測: 同一会議・同一VTT・
            # 同一モデルで検出数が 18 → 26 に変わり、共通は 3 件だけだった。
            # K3 の抽出は再現しないため、重要な会議を複数回読ませて検出を積み増す
            # 使い方が有効で、その前提として重複排除が要る）。
            # 突合は content_sha256 ではなく `_extraction_texts_match`（ratio または
            # 12文字以上の共通部分文字列）を使う — 表現が揺れるので完全一致では
            # 重複を捕まえられない。
            if dedup_existing and findings:
                existing = _existing_finding_bodies(
                    pm_conn, kind=spec["kind"], meeting_id=c["meeting_id"],
                )
                if existing:
                    kept: list[dict] = []
                    for f in findings:
                        hit = next(
                            (rid for rid, body in existing
                             if _extraction_texts_match(f["content"], body)), None,
                        )
                        if hit is None:
                            kept.append(f)
                            continue
                        n_dup_existing += 1
                        log(f"  [重複] {meeting_ref} 既存 id={hit} と一致するため"
                            f"記録しません: {f['content'][:60]}")
                    log(f"  {meeting_ref} 既存所見 {len(existing)} 件と突合: "
                        f"重複 {len(findings) - len(kept)} 件を除外、"
                        f"新規 {len(kept)} 件")
                    findings = kept

            if len(findings) > max_findings_per_meeting:
                n_over = len(findings) - max_findings_per_meeting
                log(f"[WARN] {meeting_ref} で {max_findings_per_meeting} 件を超えました"
                    f"（実際 {len(findings)} 件）。**上限は妥当な件数ではなく事故防止の"
                    "last resort** です（読み手か突合のどちらかが壊れている兆候として"
                    "読むこと。上限を上げて対処するものではありません）。"
                    f"上位 {max_findings_per_meeting} 件のみ記録し、残り {n_over} 件は記録しません"
                    "（黙って打ち切ると『全部見た』と誤読されるため明示します）")

            for f in findings[:max_findings_per_meeting]:
                record_second_opinion(
                    pm_conn, kind=f["kind"],
                    content=f"{meeting_ref} {f['content']}",
                    primary_verdict="MISSING", second_verdict="PRESENT",
                    flagged_terms=f["terms"], model=spec["model"], raw=f["raw"],
                )
                n_recorded += 1
            log(f"  {meeting_ref} チャンク{len(chunks)}件 処理完了")

    log("")
    log(f"所見: 真の欠落候補 {n_recorded} 件 / 本文にあり（除外） {n_excluded_in_haystack} 件"
        f" / 抽出表にあり（除外） {n_excluded_in_table} 件"
        + (f" / 既存所見と重複（除外） {n_dup_existing} 件" if dedup_existing else
           " / 既存所見との重複排除は無効（--no-dedup-existing）"))
    n_skipped = n_call_errors + n_chunk_errors
    if n_skipped:
        log(f"[WARN] 検査したチャンク {n_chunks_total} 件のうち {n_skipped} 件を"
            f"スキップしました（LLM呼び出し失敗 {n_call_errors} 件 / 突合失敗 "
            f"{n_chunk_errors} 件）。**この会議の検査は網羅的ではありません** — "
            "スキップしたチャンクにあった欠落は検出できていません")
    else:
        log(f"検査したチャンク {n_chunks_total} 件すべてを処理しました（スキップ 0 件）")
    log(f"完了: 読み手が保存済みに無いと判定した項目 {n_recorded} 件を"
        " triage_second_opinion に記録しました"
        "（議事録DB・pm.dbのaction_items/decisionsは変更していません）")


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
    parser.add_argument("--semantic", action="store_true",
                        help="embedding + ローカルLLM審査による意味的重複検出を実行"
                             "（アクションアイテムのみ、デフォルト: off）")
    parser.add_argument("--merge-threshold", type=float, default=0.92,
                        help="意味的重複の確定判定コサイン類似度閾値（デフォルト: 0.92）")
    parser.add_argument("--review-threshold", type=float, default=0.85,
                        help="意味的重複の境界帯（LLM審査対象）下限コサイン類似度閾値（デフォルト: 0.85）")
    parser.add_argument("--no-llm", action="store_true",
                        help="境界帯のローカルLLM審査を行わない（merge-threshold 以上のみ確定）")
    parser.add_argument("--triage", action="store_true",
                        help="既存データの一括トリアージ（3ゲート審査）を実行。"
                             "重複検出とは独立したモードで、指定時は重複検出をスキップする。"
                             "この場合 --output は進捗ログではなく出力CSVのパスとして使われる"
                             "（デフォルト: triage.csv）")
    parser.add_argument("--triage-include-closed", action="store_true",
                        help="--triage 時に status='open' 以外（closed 含む）の action_items も"
                             "対象に含める（デフォルト: open のみ）。"
                             "警告: closed 項目はゲート3（影響範囲）でほぼ DROP 判定になるため、"
                             "完了実績を誤って抹消対象にしてしまう恐れがある。通常は指定しないこと")
    parser.add_argument("--second-opinion-minutes", action="store_true",
                        help="議事録経路への第2系統（独立系統）差分検査バッチを実行。"
                             "重複検出・--triage とは独立したモード。"
                             "ARGUS_SECOND_OPINION=1/true/yes のときのみ動作する")
    parser.add_argument("--reader", choices=("second", "k3", "both"), default="second",
                        help="--second-opinion-minutes の読み手を切り替える"
                             "（デフォルト: second）。second=R8対策の第2系統"
                             "（Llama-4-Scout、変更なし）。k3=kimi-k3による recall"
                             "チェック（R8の独立系統ではない。品質目的のみ、"
                             "kind=minutes_extraction_recall で別集計）。"
                             "both=両方を実行")
    parser.add_argument("--limit", type=int, default=_DEFAULT_SECOND_OPINION_LIMIT,
                        help="--second-opinion-minutes 時に処理する会議数の上限"
                             f"（デフォルト: {_DEFAULT_SECOND_OPINION_LIMIT}）。"
                             "--meeting-stem 指定時は無視される")
    parser.add_argument("--meeting-stem", default=None, metavar="STEM",
                        help="--second-opinion-minutes の処理対象を1会議に絞り込む。"
                             "instances.file_path の拡張子抜きファイル名"
                             "（Path(file_path).stem。例: "
                             "2026-07-01-120000-Leader_Meeting-minutes）と一致する"
                             "会議のみを検査する。録音経路（pm_from_recording.sh）が"
                             "処理直後にその会議だけを即時検査するためのオプション。"
                             "指定時は --since ウィンドウ・--limit を無視する"
                             "（対象は常に高々1件）。一致する会議が無い場合は"
                             "エラーにせず警告を出して exit 0 で終了する"
                             "（録音ジョブ全体を落とさないため）")
    parser.add_argument("--max-findings-per-meeting", type=int,
                        default=_DEFAULT_MAX_FINDINGS_PER_MEETING,
                        help="--second-opinion-minutes 時に1会議・1読み手あたり記録する"
                             "件数の上限。超えた場合は上位N件のみ記録しWARNを出す"
                             f"（デフォルト: {_DEFAULT_MAX_FINDINGS_PER_MEETING}）。"
                             "既定値は妥当な件数ではなく事故防止の last resort であり、"
                             "到達したら読み手か突合が壊れている兆候として扱う")
    parser.add_argument("--no-dedup-existing", action="store_true",
                        help="--second-opinion-minutes 時に、既に記録済みの所見"
                             "（同じ会議・同じ読み手）との重複排除を行わない。"
                             "既定では重複を記録しない（読み手の抽出は再現しないため、"
                             "同じ会議を複数回読ませて検出を積み増せるようにするための"
                             "前提）。読み手の再現性そのものを測るときに指定する")
    parser.add_argument("--minutes-dir", default=str(DEFAULT_MINUTES_DIR),
                        help=f"議事録DBディレクトリ（デフォルト: {DEFAULT_MINUTES_DIR}）")
    parser.add_argument("--processing-dir", default=str(DEFAULT_PROCESSING_DIR),
                        help=f"文字起こし原文ディレクトリ（デフォルト: {DEFAULT_PROCESSING_DIR}）")
    add_dry_run_arg(parser)
    parser.add_argument("--list-findings", action="store_true",
                        help="第2系統トリアージの所見（triage_second_opinion）を一覧表示する"
                             "（id/ts/kind/model/content_head先頭120文字）")
    parser.add_argument("--kind", default=None, metavar="KIND",
                        help="--list-findings で kind を絞り込む"
                             "（例: minutes_extraction_recall）")
    parser.add_argument("--unreviewed-only", action="store_true",
                        help="--list-findings で reviewed_at が未設定の行のみ表示する")
    parser.add_argument("--mark-reviewed", default=None, metavar="ID[,ID...]",
                        help="指定した triage_second_opinion.id の reviewed_at を"
                             "現在時刻で埋める（カンマ区切りで複数指定可）")

    args = parser.parse_args()
    db_path = resolve_db_path(args.db, REPO_ROOT / "data" / "pm.db")
    # --triage / --export 時は --output が出力CSVのパスとして使われるため、
    # ログファイルとしては開かない（make_logger の close() で CSV が
    # 上書きされてしまうバグを防ぐ）。
    log, close = make_logger(args.output if not (args.export or args.triage) else None)

    conn = open_db(db_path, encrypt=not args.no_encrypt)

    if args.list_findings:
        rows = list_second_opinion_findings(
            conn, kind=args.kind, unreviewed_only=args.unreviewed_only,
        )
        if not rows:
            log("所見はありません")
        else:
            for r in rows:
                head = (r["content_head"] or "")[:120]
                log(f"id={r['id']} ts={r['ts']} kind={r['kind']} model={r['model']}"
                    f" reviewed_at={r['reviewed_at']} content_head={head!r}")
        conn.close()
        close()
        return

    if args.mark_reviewed:
        ids = [int(x) for x in args.mark_reviewed.split(",") if x.strip()]
        n = mark_second_opinion_reviewed(conn, ids)
        log(f"reviewed_at を設定しました: {n} 件（指定 {len(ids)} 件中）")
        conn.close()
        close()
        return

    if args.triage:
        output = args.output or "triage.csv"
        run_triage(conn, args.since, args.triage_include_closed, output, log)
        conn.close()
        close()
        return

    if args.second_opinion_minutes:
        run_second_opinion_minutes(
            conn,
            since=args.since,
            limit=args.limit,
            dry_run=args.dry_run,
            minutes_dir=Path(args.minutes_dir),
            processing_dir=Path(args.processing_dir),
            no_encrypt=args.no_encrypt,
            log=log,
            reader=args.reader,
            max_findings_per_meeting=args.max_findings_per_meeting,
            meeting_stem=args.meeting_stem,
            dedup_existing=not args.no_dedup_existing,
        )
        conn.close()
        close()
        return

    ais = fetch_active_action_items(conn, since=args.since)
    decs = fetch_active_decisions(conn, since=args.since) if args.include_decisions else []

    log(f"対象: アクションアイテム {len(ais)} 件"
        + (f", 決定事項 {len(decs)} 件" if decs else ""))

    ai_findings: list[tuple[str, list[dict]]] = []
    ai_findings.extend(detect_exact_duplicates(ais))
    ai_findings.extend(detect_near_duplicates(ais, prefix_len=args.prefix_len))
    if args.semantic:
        ai_findings.extend(detect_semantic_duplicates(
            ais,
            merge_threshold=args.merge_threshold,
            review_threshold=args.review_threshold,
            use_llm=not args.no_llm,
        ))
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
