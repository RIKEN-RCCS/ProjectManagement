#!/usr/bin/env python3
"""
achievements.py

実績台帳（achievements ledger）の populator プラグイン。
pm_ingest.py achievements 経由で呼び出される。

アプリ別に enrich.achievements_extract.extract_achievements() で完了実績を
LLM抽出し、重複排除（正規化キー + embedding類似度）を経て pm.db の
achievements テーブルへ投入する。人間が確定（confirmed）・却下（rejected）
した行は、再実行時に本文を上書きしない（丸ごとスキップして保護する）。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrich.achievements_extract import extract_achievements

from ingest.ingest_plugin import IngestContext

_DEFAULT_APPS = "GENESIS,LQCD-DWF-HMC,SCALE-LETKF,E-Wave,SALMON,FrontFlow/blue"
# skill embedding-similarity-dedup の 0.85=審査ラインに合わせる（0.88 だと
# LLM言い換えブレ「（再報告）」等の近似重複が閾値未満で漏れやすかったため）。
_DEFAULT_DEDUP_THRESHOLD = 0.85

# 「公開（再報告）」のような付帯注記は exact dedup_key でも吸収できるよう、
# 括弧の中身ごと正規化前に取り除く。
_BRACKET_CONTENT_RE = re.compile(r"[（(][^）)]*[）)]")
_NORMALIZE_RE = re.compile(r"[\s　\-_/,、。.:：;；()（）\[\]【】]")


def _normalize_title(title: str) -> str:
    t = _BRACKET_CONTENT_RE.sub("", title.strip().lower())
    return _NORMALIZE_RE.sub("", t)


def _dedup_key(app: str, title: str) -> str:
    return f"{app}|{_normalize_title(title)}"


def _semantic_max_similarity(title: str, existing_vecs, log=print) -> float:
    """既存実績タイトル群の embedding との最大コサイン類似度を返す。

    embedding API が使えない場合は 0.0（意味的重複なしとみなす、正規化キー
    のみで dedup を続行する）。
    """
    try:
        from embed_utils import cosine_similarity_matrix, embed_one
        vec = embed_one(title)
        sims = cosine_similarity_matrix(vec, existing_vecs)
        return float(sims.max()) if sims.size else 0.0
    except Exception as e:  # noqa: BLE001 — embedding失敗時は正規化キーのみで続行
        log(f"  [WARN] embedding比較失敗、内容一致のみでdedupします: {e}")
        return 0.0


def self_dedup_candidates(candidates: list[dict], threshold: float, log=print) -> list[dict]:
    """同一run内の候補どうしを embedding で貪欲クラスタリングし、各クラスタから
    代表1件（confidence が high のものを優先、無ければ最初）に畳む。

    scripts/ingest/slack.py の _consensus_decisions/_consensus_action_items と
    同じ貪欲クラスタリング方式（クラスタ中心への逐次追加・平均更新）を使う。
    """
    if len(candidates) <= 1:
        return candidates

    titles = [c["title"] for c in candidates]
    try:
        import numpy as np
        from embed_utils import cosine_similarity_matrix, embed_batch
        vecs = embed_batch(titles)
        clusters: list[list[int]] = []
        centers = []
        for i, v in enumerate(vecs):
            if not clusters:
                clusters.append([i])
                centers.append(v.copy())
                continue
            sims = cosine_similarity_matrix(v, np.stack(centers))
            best = int(np.argmax(sims))
            if float(sims[best]) >= threshold:
                clusters[best].append(i)
                n_old = len(clusters[best]) - 1
                centers[best] = (centers[best] * n_old + v) / (n_old + 1)
            else:
                clusters.append([i])
                centers.append(v.copy())
    except Exception as e:  # noqa: BLE001 — embedding失敗時は素通し（既存DB比較のみでdedup）
        log(f"  [WARN] run内 self-dedup の embedding 失敗、素通しします: {e}")
        return candidates

    collapsed: list[dict] = []
    for cl in clusters:
        cl_items = [candidates[i] for i in cl]
        if len(cl_items) > 1:
            titles_str = " / ".join(c["title"] for c in cl_items)
            log(f"  [DEDUP] run内近似重複を統合: {titles_str}")
        rep = next((c for c in cl_items if c["confidence"] == "high"), cl_items[0])
        collapsed.append(rep)
    return collapsed


def upsert_achievements(
    pm_conn, app: str, candidates: list[dict], *, threshold: float, dry_run: bool, log=print
) -> dict:
    """1アプリ分の実績候補を dedup + upsert する。件数を分類別に返す。"""
    existing_rows = pm_conn.execute(
        "SELECT id, title, status, dedup_key FROM achievements "
        "WHERE app = ? AND COALESCE(deleted,0) = 0",
        (app,),
    ).fetchall()
    existing_titles = [r["title"] for r in existing_rows]
    existing_by_key = {r["dedup_key"]: r for r in existing_rows}

    existing_vecs = None
    if existing_titles:
        try:
            from embed_utils import embed_batch
            existing_vecs = embed_batch(existing_titles)
        except Exception as e:  # noqa: BLE001
            log(f"  [WARN] {app}: 既存実績の embedding 取得失敗、内容一致のみでdedupします: {e}")
            existing_vecs = None

    counts = {"confirmed": 0, "proposed": 0, "skip": 0}
    now = datetime.now().isoformat()

    for cand in candidates:
        title = cand["title"]
        dedup_key = _dedup_key(app, title)

        if existing_vecs is not None and existing_vecs.size:
            max_sim = _semantic_max_similarity(title, existing_vecs, log=log)
            if max_sim >= threshold:
                counts["skip"] += 1
                log(f"  [SKIP] {app}: 既存実績と類似のため見送り (sim={max_sim:.2f}): {title}")
                continue

        existing = existing_by_key.get(dedup_key)
        # 既存行が人間により確定/却下済みなら丸ごとスキップし、本文・状態を保護する。
        if existing and existing["status"] in ("confirmed", "rejected"):
            counts["skip"] += 1
            log(f"  [SKIP] {app}: 既存レコード（status={existing['status']}）を保護: {title}")
            continue

        status = "confirmed" if cand["confidence"] == "high" else "proposed"
        counts[status] += 1
        log(f"  [{'DRY' if dry_run else 'OK'}] {app} ({status}): {title}")
        if dry_run:
            continue

        # status は既存 proposed 行の人間編集を上書きしないよう UPDATE 対象から除外する
        # （新規行のみ status の初期値として使われる）。
        pm_conn.execute(
            """
            INSERT INTO achievements
                (app, title, category, achieved_on, evidence_ref, evidence_quote,
                 confidence, status, source, dedup_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'argus_auto', ?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                title=excluded.title, category=excluded.category, achieved_on=excluded.achieved_on,
                evidence_ref=excluded.evidence_ref, evidence_quote=excluded.evidence_quote,
                confidence=excluded.confidence, updated_at=excluded.updated_at
            """,
            (
                app, title, cand["category"], cand["achieved_on"], cand["evidence_ref"],
                cand["evidence_quote"], cand["confidence"], status, dedup_key, now, now,
            ),
        )

    return counts


class AchievementsIngestPlugin:
    source_name = "achievements"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--achievements-apps", default=_DEFAULT_APPS,
            metavar="APP1,APP2,...",
            help=f"実績を抽出する対象アプリ（カンマ区切り、デフォルト: {_DEFAULT_APPS}）",
        )
        parser.add_argument(
            "--achievements-dedup-threshold", type=float, default=_DEFAULT_DEDUP_THRESHOLD,
            metavar="FLOAT",
            help=f"既存実績との重複判定コサイン類似度閾値（デフォルト: {_DEFAULT_DEDUP_THRESHOLD}）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        apps_arg = getattr(args, "achievements_apps", None) or _DEFAULT_APPS
        apps = [a.strip() for a in apps_arg.split(",") if a.strip()]
        threshold = getattr(args, "achievements_dedup_threshold", _DEFAULT_DEDUP_THRESHOLD)

        if ctx.dry_run:
            ctx.log("[INFO] --dry-run モード（DB保存なし）")

        total = {"confirmed": 0, "proposed": 0, "skip": 0}
        for app in apps:
            ctx.log(f"[INFO] 実績抽出中: {app}")
            known_titles = [
                r["title"] for r in ctx.pm_conn.execute(
                    "SELECT title FROM achievements WHERE app = ? AND COALESCE(deleted,0) = 0 "
                    "AND status IN ('confirmed', 'proposed')",
                    (app,),
                ).fetchall()
            ]
            candidates = extract_achievements(app, known_titles=known_titles or None)
            if not candidates:
                ctx.log(f"  [INFO] {app}: 候補なし")
                continue
            candidates = self_dedup_candidates(candidates, threshold, log=ctx.log)
            counts = upsert_achievements(
                ctx.pm_conn, app, candidates,
                threshold=threshold, dry_run=ctx.dry_run, log=ctx.log,
            )
            for k in total:
                total[k] += counts[k]
            ctx.log(
                f"  [INFO] {app}: confirmed={counts['confirmed']}件, "
                f"proposed={counts['proposed']}件, skip={counts['skip']}件"
            )

        if not ctx.dry_run:
            ctx.pm_conn.commit()

        ctx.log(
            f"[INFO] 完了: confirmed={total['confirmed']}件, proposed={total['proposed']}件, "
            f"skip={total['skip']}件"
            f"{'（dry-run のため未保存）' if ctx.dry_run else ''}"
        )
