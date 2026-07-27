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
"""

import argparse
import csv
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli_utils import (
    add_db_arg,
    add_no_encrypt_arg,
    add_output_arg,
    add_since_arg,
    make_logger,
    resolve_db_path,
)
from db_utils import open_db

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

    args = parser.parse_args()
    db_path = resolve_db_path(args.db, REPO_ROOT / "data" / "pm.db")
    # --triage / --export 時は --output が出力CSVのパスとして使われるため、
    # ログファイルとしては開かない（make_logger の close() で CSV が
    # 上書きされてしまうバグを防ぐ）。
    log, close = make_logger(args.output if not (args.export or args.triage) else None)

    conn = open_db(db_path, encrypt=not args.no_encrypt)

    if args.triage:
        output = args.output or "triage.csv"
        run_triage(conn, args.since, args.triage_include_closed, output, log)
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
