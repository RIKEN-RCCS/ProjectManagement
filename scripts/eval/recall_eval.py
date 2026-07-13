#!/usr/bin/env python3
"""recall_eval.py — Argus retrieval recall 回帰評価ハーネス。

「エンティティ×既知の在窓事実」のゴールドセット (recall_gold.yaml) に対し、
retrieval 各段 (fts / hybrid / hyde / rerank) の hit@k / recall@k / MRR を測る。

- scripts/argus/retrieval.py の既存関数をそのまま呼ぶ（検索ロジックは複製しない）
- 本番 qa_index.db は読み取り専用でアクセス
- 結果は data/eval/recall_eval.db (新規, 暗号化なし) に蓄積
- Slack/Canvas/Box への副作用なし

サブコマンド:
    resolve  ゴールドセットを現行 chunk id 集合へ解決し、curation ゲートとして検証する
    run      段階別に検索を実行し、hit@k / recall@k / MRR を測定して記録する
    report   直近 2 run を比較したレポートを出力する

例:
    # fts のみ（secrets 不要）
    ~/.venv_aarch64/bin/python3 scripts/eval/recall_eval.py resolve --verbose
    ~/.venv_aarch64/bin/python3 scripts/eval/recall_eval.py run --stages fts

    # hybrid/hyde/rerank は埋め込み/LLM 接続が必要
    source ~/.secrets/rivault_tokens.sh
    source ~/.secrets/localLLM.sh
    export ARGUS_SKIP_LLM_SECRETS=1
    ~/.venv_aarch64/bin/python3 scripts/eval/recall_eval.py run --stages fts,hybrid,hyde,rerank --repeat 1

    ~/.venv_aarch64/bin/python3 scripts/eval/recall_eval.py report
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "utils"))

from argus.retrieval import (  # noqa: E402
    rerank_chunks,
    retrieve_chunks,
    retrieve_chunks_hybrid,
    retrieve_chunks_hyde,
)

DEFAULT_INDEX_DB = Path(os.environ.get("RECALL_INDEX_DB") or (REPO / "data" / "qa_index.db"))
DEFAULT_EVAL_DB = Path(os.environ.get("RECALL_EVAL_DB") or (REPO / "data" / "eval" / "recall_eval.db"))
DEFAULT_GOLD = Path(os.environ.get("RECALL_GOLD") or (REPO / "scripts" / "eval" / "recall_gold.yaml"))

DEFAULT_K_LIST = [5, 10, 30, 60]

STAGE_NEEDS_EMBED = {"hybrid", "hyde", "rerank"}
STAGE_NEEDS_LLM = {"hyde", "rerank"}
STAGE_IS_LLM = {"hyde", "rerank"}  # --repeat が意味を持つ段

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    git_rev       TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    index_db      TEXT NOT NULL,
    index_mtime   REAL,
    chunks_count  INTEGER,
    gold_sha256   TEXT NOT NULL,
    stages        TEXT NOT NULL,
    k_list        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    run_id        INTEGER NOT NULL,
    entry_id      TEXT NOT NULL,
    chunk_id      INTEGER NOT NULL,
    held_at       TEXT,
    source_type   TEXT,
    ok            INTEGER NOT NULL,
    warnings      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS results (
    result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    entry_id        TEXT NOT NULL,
    query_idx       INTEGER NOT NULL,
    query_text      TEXT NOT NULL,
    query_style     TEXT NOT NULL,
    stage           TEXT NOT NULL,
    rep             INTEGER NOT NULL,
    ranked_ids      TEXT NOT NULL,
    n_returned      INTEGER NOT NULL,
    first_gold_rank INTEGER,
    hit_at          TEXT NOT NULL,
    recall_at       TEXT NOT NULL,
    mrr             REAL NOT NULL,
    hyde_queries    TEXT,
    elapsed_ms      INTEGER NOT NULL,
    error           TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_resolutions_run ON resolutions(run_id);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_run_stage ON results(run_id, stage);
"""


def open_eval_db(eval_db: Path) -> sqlite3.Connection:
    eval_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(eval_db))
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# ゴールドセット読み込み
# --------------------------------------------------------------------------- #

def load_gold(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def select_entries(gold: dict, entry_id: str | None) -> list[dict]:
    entries = gold.get("entries") or []
    if entry_id:
        entries = [e for e in entries if e["id"] == entry_id]
        if not entries:
            raise SystemExit(f"ERROR: entry id '{entry_id}' が見つかりません")
    return entries


# --------------------------------------------------------------------------- #
# resolve: ゴールド→現行 chunk id 集合への解決
# --------------------------------------------------------------------------- #

def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(entry: dict, index_name: str, with_window: bool) -> tuple[str, list]:
    resolve = entry.get("resolve") or {}
    conds = ["ci.index_name = ?"]
    params: list = [index_name]
    if with_window:
        window = resolve.get("window") or {}
        since = window.get("since")
        until = window.get("until")
        if since:
            conds.append("c.held_at >= ?")
            params.append(since)
        if until:
            conds.append("c.held_at <= ?")
            params.append(until)
    for mc in resolve.get("must_contain") or []:
        conds.append("c.content LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(mc)}%")
    source_type = resolve.get("source_type")
    if source_type:
        conds.append("c.source_type = ?")
        params.append(source_type)
    source_ref_like = resolve.get("source_ref_like")
    if source_ref_like:
        conds.append("c.source_ref LIKE ?")
        params.append(source_ref_like)
    return " AND ".join(conds), params


def _query_chunks(conn: sqlite3.Connection, entry: dict, index_name: str,
                  with_window: bool) -> list[dict]:
    where, params = _build_where(entry, index_name, with_window)
    sql = (
        "SELECT c.id, c.held_at, c.source_type, c.source_ref, c.content "
        "FROM chunks c JOIN chunk_indexes ci ON ci.chunk_id = c.id "
        "WHERE " + where
    )
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    regex = (entry.get("resolve") or {}).get("regex")
    if regex:
        pattern = re.compile(regex)
        rows = [r for r in rows if pattern.search(r["content"] or "")]
    return rows


@dataclass
class ResolveResult:
    entry_id: str
    ids: list[int]
    rows: list[dict]
    count: int
    expect_min: int
    expect_max: int
    status: str  # PASS / FAIL
    leak_rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_entry(conn: sqlite3.Connection, entry: dict, index_name: str) -> ResolveResult:
    rows = _query_chunks(conn, entry, index_name, with_window=True)
    ids = sorted(r["id"] for r in rows)
    expect = (entry.get("resolve") or {}).get("expect_count") or {}
    emin = expect.get("min", 1)
    emax = expect.get("max", 10**9)
    count = len(rows)
    status = "PASS" if emin <= count <= emax else "FAIL"
    warnings: list[str] = []

    windowless_rows = _query_chunks(conn, entry, index_name, with_window=False)
    leak_ids = {r["id"] for r in windowless_rows} - set(ids)
    leak_rows = [r for r in windowless_rows if r["id"] in leak_ids]
    if leak_rows:
        warnings.append(
            f"窓外リーク: {len(leak_rows)} 件が同一条件で held_at 窓の外にマッチ "
            f"(ids={sorted(leak_ids)})"
        )

    return ResolveResult(
        entry_id=entry["id"], ids=ids, rows=rows, count=count,
        expect_min=emin, expect_max=emax, status=status,
        leak_rows=leak_rows, warnings=warnings,
    )


def resolve_all(conn: sqlite3.Connection, entries: list[dict], index_name: str) -> list[ResolveResult]:
    results = [resolve_entry(conn, e, index_name) for e in entries]
    id_to_entries: dict[int, list[str]] = {}
    for res in results:
        for cid in res.ids:
            id_to_entries.setdefault(cid, []).append(res.entry_id)
    for res in results:
        for cid in res.ids:
            owners = id_to_entries[cid]
            if len(owners) > 1:
                msg = f"overlap: chunk id {cid} は複数エントリに解決 ({owners})"
                if msg not in res.warnings:
                    res.warnings.append(msg)
    return results


# --------------------------------------------------------------------------- #
# score: 純関数（DB / LLM 非依存）
# --------------------------------------------------------------------------- #

def score(ranked_ids: list[int], gold: set[int], hit_rule: str = "any",
         k_list: list[int] | None = None) -> dict:
    """検索結果の hit@k / recall@k / MRR / first_gold_rank を計算する。"""
    k_list = k_list or DEFAULT_K_LIST
    gold = set(gold)

    first_gold_rank: int | None = None
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in gold:
            first_gold_rank = i
            break
    mrr = (1.0 / first_gold_rank) if first_gold_rank else 0.0

    hit_at: dict[int, int] = {}
    recall_at: dict[int, float] = {}
    for k in k_list:
        top = set(ranked_ids[:k])
        found = gold & top
        if hit_rule == "all":
            hit_at[k] = 1 if gold and gold.issubset(top) else 0
        else:
            hit_at[k] = 1 if found else 0
        recall_at[k] = (len(found) / len(gold)) if gold else 0.0

    return {
        "hit_at": hit_at,
        "recall_at": recall_at,
        "mrr": mrr,
        "first_gold_rank": first_gold_rank,
    }


# --------------------------------------------------------------------------- #
# HyDE 生成クエリの捕捉（本番 logger 設定は変更しない）
# --------------------------------------------------------------------------- #

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


def _call_capturing_hyde(fn, *args, **kwargs):
    """retrieve_chunks_hyde 呼び出し中の "[HyDE] queries=" ログを捕捉する。"""
    target_logger = logging.getLogger("pm_qa_server")
    handler = _ListHandler()
    prev_level = target_logger.level
    target_logger.addHandler(handler)
    target_logger.setLevel(logging.INFO)
    try:
        result = fn(*args, **kwargs)
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(prev_level)

    hyde_queries = None
    prefix = "[HyDE] queries="
    for msg in handler.messages:
        if msg.startswith(prefix):
            raw = msg[len(prefix):]
            try:
                hyde_queries = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                hyde_queries = raw
            break
    return result, hyde_queries


# --------------------------------------------------------------------------- #
# stage 実行関数
# --------------------------------------------------------------------------- #

def _stage_fts(question, since_date, index_name, index_db, args):
    results = retrieve_chunks(question, index_db, k=60, since_date=since_date, index_name=index_name)
    return results, None


def _stage_hybrid(question, since_date, index_name, index_db, args):
    results = retrieve_chunks_hybrid(question, index_db, k=60, since_date=since_date, index_name=index_name)
    return results, None


def _stage_hyde(question, since_date, index_name, index_db, args):
    return _call_capturing_hyde(
        retrieve_chunks_hyde, question, index_db, k=30, since_date=since_date,
        max_merged=60, index_name=index_name,
    )


def _stage_rerank(question, since_date, index_name, index_db, args):
    merged, hyde_queries = _call_capturing_hyde(
        retrieve_chunks_hyde, question, index_db, k=30, since_date=since_date,
        max_merged=50, index_name=index_name,
    )
    ranked = rerank_chunks(question, merged, openai_base=args.rerank_llm_base, top_k=50)
    return ranked, hyde_queries


STAGE_FUNCS = {
    "fts": _stage_fts,
    "hybrid": _stage_hybrid,
    "hyde": _stage_hyde,
    "rerank": _stage_rerank,
}


def _preflight_env(stages: set[str]) -> None:
    if stages & STAGE_NEEDS_EMBED:
        if not (os.environ.get("EMBED_API_BASE") or os.environ.get("RIVAULT_URL")):
            print(
                "ERROR: EMBED_API_BASE または RIVAULT_URL が未設定です"
                "（source ~/.secrets/rivault_tokens.sh 等で埋め込みAPI接続を用意してください）",
                file=sys.stderr,
            )
            sys.exit(2)
    if stages & STAGE_NEEDS_LLM:
        if not os.environ.get("RIVAULT_TOKEN"):
            print(
                "ERROR: RIVAULT_TOKEN が未設定です（hyde/rerank 段は LLM 呼び出しに必要）。"
                "source ~/.secrets/rivault_tokens.sh を確認してください",
                file=sys.stderr,
            )
            sys.exit(2)


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# CLI: resolve
# --------------------------------------------------------------------------- #

def cmd_resolve(args: argparse.Namespace) -> int:
    gold = load_gold(args.gold)
    index_name = args.index_name or gold.get("default_index_name", "pm")
    entries = select_entries(gold, args.entry)

    if not args.index_db.exists():
        print(f"ERROR: index db が見つかりません: {args.index_db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.index_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        results = resolve_all(conn, entries, index_name)
    finally:
        conn.close()

    any_fail = False
    for res in results:
        print(f"\n=== {res.entry_id} ===")
        print(f"件数: {res.count} (expect {res.expect_min}-{res.expect_max}) -> {res.status}")
        preview_len = 200 if args.verbose else 60
        for r in res.rows:
            preview = (r["content"] or "").replace("\n", " ").strip()[:preview_len]
            print(f"  id={r['id']} held_at={r['held_at']} source_ref={r['source_ref']} content={preview!r}")
        for w in res.warnings:
            print(f"  WARN: {w}")
        if res.status == "FAIL":
            any_fail = True

    return 1 if any_fail else 0


# --------------------------------------------------------------------------- #
# CLI: run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    stage_set = set(args.stages)
    unknown = stage_set - set(STAGE_FUNCS)
    if unknown:
        print(f"ERROR: 未知の stage: {sorted(unknown)}", file=sys.stderr)
        return 2
    _preflight_env(stage_set)

    if not args.index_db.exists():
        print(f"ERROR: index db が見つかりません: {args.index_db}", file=sys.stderr)
        return 2

    gold_raw = args.gold.read_bytes()
    gold = yaml.safe_load(gold_raw)
    index_name = args.index_name or gold.get("default_index_name", "pm")
    k_list = args.k or gold.get("default_k_list", DEFAULT_K_LIST)

    all_entries = gold.get("entries") or []
    target_entries = select_entries(gold, args.entry)

    conn_idx = sqlite3.connect(f"file:{args.index_db}?mode=ro", uri=True)
    conn_idx.row_factory = sqlite3.Row
    try:
        # 全エントリを resolve してスナップショット保存（陳腐化検知用）
        resolutions = resolve_all(conn_idx, all_entries, index_name)
        chunks_count = conn_idx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn_idx.close()

    gold_by_entry = {r.entry_id: set(r.ids) for r in resolutions}
    status_by_entry = {r.entry_id: r.status for r in resolutions}
    warnings_by_entry = {r.entry_id: "; ".join(r.warnings) for r in resolutions}

    conn = open_eval_db(args.eval_db)
    started_at = datetime.now().isoformat(timespec="seconds")
    gold_sha256 = hashlib.sha256(gold_raw).hexdigest()

    cur = conn.execute(
        "INSERT INTO runs(started_at, git_rev, label, index_db, index_mtime, chunks_count,"
        " gold_sha256, stages, k_list) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            started_at, _git_rev(), args.label or "", str(args.index_db),
            args.index_db.stat().st_mtime, chunks_count, gold_sha256,
            ",".join(args.stages), ",".join(str(k) for k in k_list),
        ),
    )
    run_id = cur.lastrowid
    conn.commit()

    for res in resolutions:
        for r in res.rows:
            conn.execute(
                "INSERT INTO resolutions(run_id, entry_id, chunk_id, held_at, source_type, ok, warnings)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, res.entry_id, r["id"], r["held_at"], r["source_type"],
                 1 if res.status == "PASS" else 0, "; ".join(res.warnings)),
            )
    conn.commit()

    print(f"run_id={run_id} entries={len(target_entries)} stages={args.stages} k_list={k_list}",
          file=sys.stderr)

    for entry in target_entries:
        entry_id = entry["id"]
        gold_ids = gold_by_entry.get(entry_id, set())
        if status_by_entry.get(entry_id) == "FAIL" or warnings_by_entry.get(entry_id):
            print(f"  WARN: entry {entry_id} の resolve 状態に注意 "
                  f"(status={status_by_entry.get(entry_id)}, {warnings_by_entry.get(entry_id)})",
                  file=sys.stderr)
        hit_rule = entry.get("hit_rule", "any")
        since_date = entry.get("query_since")

        for qi, q in enumerate(entry.get("queries") or []):
            qtext = q["text"]
            qstyle = q.get("style", "topic")

            for stage in args.stages:
                reps = args.repeat if stage in STAGE_IS_LLM else 1
                for rep in range(reps):
                    print(f"  [{entry_id}] q#{qi} stage={stage} rep={rep} ...",
                          file=sys.stderr, flush=True)
                    started = time.time()
                    error = ""
                    ranked_ids: list[int] = []
                    hyde_queries = None
                    try:
                        results, hyde_queries = STAGE_FUNCS[stage](
                            qtext, since_date, index_name, args.index_db, args
                        )
                        ranked_ids = [c["id"] for c in results]
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    elapsed_ms = int((time.time() - started) * 1000)
                    sc = score(ranked_ids, gold_ids, hit_rule=hit_rule, k_list=k_list)

                    conn.execute(
                        "INSERT INTO results(run_id, entry_id, query_idx, query_text, query_style,"
                        " stage, rep, ranked_ids, n_returned, first_gold_rank, hit_at, recall_at,"
                        " mrr, hyde_queries, elapsed_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id, entry_id, qi, qtext, qstyle, stage, rep,
                            json.dumps(ranked_ids), len(ranked_ids), sc["first_gold_rank"],
                            json.dumps(sc["hit_at"]), json.dumps(sc["recall_at"]), sc["mrr"],
                            json.dumps(hyde_queries, ensure_ascii=False) if hyde_queries is not None else None,
                            elapsed_ms, error,
                        ),
                    )
                    conn.commit()
                    status = f"ERROR: {error}" if error else (
                        f"rank={sc['first_gold_rank']} mrr={sc['mrr']:.3f}"
                    )
                    print(f"    -> {status} n={len(ranked_ids)} {elapsed_ms}ms",
                          file=sys.stderr, flush=True)

    conn.close()
    print(f"done. run_id={run_id}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# CLI: report
# --------------------------------------------------------------------------- #

def _pick_run_ids(conn: sqlite3.Connection, run_arg: int | None,
                  baseline_arg: int | None) -> tuple[int, int | None]:
    if run_arg:
        run_id = run_arg
    else:
        row = conn.execute("SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("ERROR: runs テーブルが空です（先に run を実行してください）")
        run_id = row[0]
    if baseline_arg:
        baseline_id: int | None = baseline_arg
    else:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id != ? ORDER BY run_id DESC LIMIT 1", (run_id,)
        ).fetchone()
        baseline_id = row[0] if row else None
    return run_id, baseline_id


def _fetch_results(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_id, query_idx, query_text, query_style, stage, rep,"
        " first_gold_rank, hit_at, recall_at, mrr, error"
        " FROM results WHERE run_id=?", (run_id,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "entry_id": r[0], "query_idx": r[1], "query_text": r[2], "query_style": r[3],
            "stage": r[4], "rep": r[5], "first_gold_rank": r[6],
            "hit_at": json.loads(r[7]), "recall_at": json.loads(r[8]), "mrr": r[9],
            "error": r[10],
        })
    return out


def _aggregate(rows: list[dict], k_list: list[int]) -> dict:
    """stage -> {n, hit_at[k], recall_at[k], mrr_at[k]} の平均値。"""
    by_stage: dict[str, list[dict]] = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(r)

    agg: dict[str, dict] = {}
    for stage, rs in by_stage.items():
        n = len(rs)
        hit_at = {k: sum(r["hit_at"].get(str(k), r["hit_at"].get(k, 0)) for r in rs) / n for k in k_list}
        recall_at = {k: sum(r["recall_at"].get(str(k), r["recall_at"].get(k, 0.0)) for r in rs) / n
                     for k in k_list}
        mrr_at = {}
        for k in k_list:
            total = 0.0
            for r in rs:
                fgr = r["first_gold_rank"]
                total += (1.0 / fgr) if (fgr and fgr <= k) else 0.0
            mrr_at[k] = total / n
        mrr_full = sum(r["mrr"] for r in rs) / n
        agg[stage] = {"n": n, "hit_at": hit_at, "recall_at": recall_at, "mrr_at": mrr_at, "mrr": mrr_full}
    return agg


def _fmt_delta(cur: float, base: float | None) -> str:
    if base is None:
        return ""
    d = cur - base
    sign = "+" if d >= 0 else ""
    return f" (Δ{sign}{d:.3f})"


def _print_table1(run_agg: dict, base_agg: dict | None, k_list: list[int]) -> None:
    print("## 表1: stage × k — hit@k / recall@k / MRR@k\n")
    for stage in sorted(run_agg):
        print(f"### stage={stage} (n={run_agg[stage]['n']})\n")
        print("| k | hit@k | recall@k | MRR@k |")
        print("|---|---|---|---|")
        base_stage = (base_agg or {}).get(stage)
        for k in k_list:
            h = run_agg[stage]["hit_at"][k]
            rc = run_agg[stage]["recall_at"][k]
            m = run_agg[stage]["mrr_at"][k]
            bh = base_stage["hit_at"][k] if base_stage else None
            brc = base_stage["recall_at"][k] if base_stage else None
            bm = base_stage["mrr_at"][k] if base_stage else None
            print(f"| {k} | {h:.3f}{_fmt_delta(h, bh)} | {rc:.3f}{_fmt_delta(rc, brc)} |"
                  f" {m:.3f}{_fmt_delta(m, bm)} |")
        print()


def _print_table2(run_rows: list[dict], base_rows: list[dict] | None, k_list: list[int]) -> None:
    print("## 表2: style(topic/literal) 別\n")
    for style in ("topic", "literal"):
        r_sub = [r for r in run_rows if r["query_style"] == style]
        if not r_sub:
            continue
        run_agg = _aggregate(r_sub, k_list)
        base_agg = None
        if base_rows is not None:
            b_sub = [r for r in base_rows if r["query_style"] == style]
            if b_sub:
                base_agg = _aggregate(b_sub, k_list)
        print(f"### style={style}\n")
        _print_table1(run_agg, base_agg, k_list)


def _print_table3(run_rows: list[dict], base_rows: list[dict]) -> None:
    print("## 表3: エントリ×クエリ×stage — first_gold_rank (run / baseline)\n")

    def _key(r: dict) -> tuple:
        return (r["entry_id"], r["query_idx"], r["stage"])

    def _mean_rank(rows: list[dict]) -> float | None:
        ranks = [r["first_gold_rank"] for r in rows if r["first_gold_rank"]]
        if not ranks:
            return None
        return sum(ranks) / len(ranks)

    run_by_key: dict[tuple, list[dict]] = {}
    for r in run_rows:
        run_by_key.setdefault(_key(r), []).append(r)
    base_by_key: dict[tuple, list[dict]] = {}
    for r in base_rows:
        base_by_key.setdefault(_key(r), []).append(r)

    keys = sorted(set(run_by_key) | set(base_by_key))
    print("| entry | q# | query | stage | run rank | base rank | |")
    print("|---|---|---|---|---|---|---|")
    for key in keys:
        entry_id, qi, stage = key
        run_rank = _mean_rank(run_by_key.get(key, []))
        base_rank = _mean_rank(base_by_key.get(key, []))
        sample_rows = run_by_key.get(key) or base_by_key.get(key) or []
        qtext = sample_rows[0]["query_text"] if sample_rows else ""

        degraded = False
        if base_rank is not None:
            if run_rank is None:
                degraded = True
            elif run_rank > base_rank:
                degraded = True
        marker = "▼" if degraded else ""
        rr = f"{run_rank:.1f}" if run_rank is not None else "-"
        br = f"{base_rank:.1f}" if base_rank is not None else "-"
        print(f"| {entry_id} | {qi} | {qtext[:30]} | {stage} | {rr} | {br} | {marker} |")
    print()


def _print_consistency_warnings(conn: sqlite3.Connection, run_id: int, baseline_id: int | None) -> None:
    if baseline_id is None:
        print("(baseline run なし。比較は run 単体のみ)\n")
        return
    run_row = conn.execute(
        "SELECT gold_sha256, chunks_count, k_list FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    base_row = conn.execute(
        "SELECT gold_sha256, chunks_count, k_list FROM runs WHERE run_id=?", (baseline_id,)
    ).fetchone()
    if run_row[0] != base_row[0]:
        print(f"WARNING: gold sha256 が baseline と異なります (run={run_row[0][:12]} base={base_row[0][:12]})")
    if run_row[1] != base_row[1]:
        print(f"WARNING: index chunks_count が baseline と異なります (run={run_row[1]} base={base_row[1]})")
    if run_row[2] != base_row[2]:
        print(f"WARNING: k_list が baseline と異なります (run={run_row[2]} base={base_row[2]})。"
              f"baseline は run の k_list で再集計されるため、baseline 側に含まれない k は"
              f" hit/recall が黙って 0 表示になります")

    run_counts = dict(conn.execute(
        "SELECT entry_id, COUNT(*) FROM resolutions WHERE run_id=? GROUP BY entry_id", (run_id,)
    ).fetchall())
    base_counts = dict(conn.execute(
        "SELECT entry_id, COUNT(*) FROM resolutions WHERE run_id=? GROUP BY entry_id", (baseline_id,)
    ).fetchall())
    for entry_id in sorted(set(run_counts) | set(base_counts)):
        rc, bc = run_counts.get(entry_id, 0), base_counts.get(entry_id, 0)
        if rc != bc:
            print(f"WARNING: entry {entry_id} の resolve 件数が baseline と異なります (run={rc} base={bc})")
    print()


def cmd_report(args: argparse.Namespace) -> int:
    conn = open_eval_db(args.eval_db)
    run_id, baseline_id = _pick_run_ids(conn, args.run, args.baseline)

    run_meta = conn.execute("SELECT started_at, label, stages, k_list FROM runs WHERE run_id=?",
                            (run_id,)).fetchone()
    print(f"# Recall Eval Report — run_id={run_id} ({run_meta[0]}, label={run_meta[1]!r})")
    if baseline_id:
        base_meta = conn.execute("SELECT started_at, label FROM runs WHERE run_id=?",
                                 (baseline_id,)).fetchone()
        print(f"baseline: run_id={baseline_id} ({base_meta[0]}, label={base_meta[1]!r})\n")
    else:
        print("baseline: (none)\n")

    _print_consistency_warnings(conn, run_id, baseline_id)

    k_list = [int(k) for k in run_meta[3].split(",")]
    run_rows = _fetch_results(conn, run_id)
    base_rows = _fetch_results(conn, baseline_id) if baseline_id else None

    run_agg = _aggregate(run_rows, k_list)
    base_agg = _aggregate(base_rows, k_list) if base_rows else None
    _print_table1(run_agg, base_agg, k_list)
    _print_table2(run_rows, base_rows, k_list)

    if args.full and base_rows is not None:
        _print_table3(run_rows, base_rows)

    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def _csv_str_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _csv_int_list(value: str) -> list[int]:
    return [int(v.strip()) for v in value.split(",") if v.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description="Argus retrieval recall 回帰評価ハーネス")
    p.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="ゴールドセット YAML")
    p.add_argument("--index-db", type=Path, default=DEFAULT_INDEX_DB, help="qa_index.db パス")
    p.add_argument("--eval-db", type=Path, default=DEFAULT_EVAL_DB, help="結果 DB パス")
    p.add_argument("--index-name", default=None, help="chunk_indexes.index_name（既定: gold の default_index_name）")
    sub = p.add_subparsers(dest="cmd", required=True)

    rs = sub.add_parser("resolve", help="ゴールド→現行 chunk id 集合へ解決し検証する")
    rs.add_argument("--entry", default=None, help="単一エントリのみ検証")
    rs.add_argument("--verbose", action="store_true", help="本文プレビューを長めに表示")
    rs.set_defaults(func=cmd_resolve)

    rn = sub.add_parser("run", help="段階別に検索を実行し hit@k/MRR を記録する")
    rn.add_argument("--stages", type=_csv_str_list, default=["fts"],
                    help="カンマ区切り: fts,hybrid,hyde,rerank")
    rn.add_argument("--k", type=_csv_int_list, default=None, help="カンマ区切り k list (既定: gold の default_k_list)")
    rn.add_argument("--repeat", type=int, default=1, help="LLM 段 (hyde/rerank) の反復回数")
    rn.add_argument("--entry", default=None, help="単一エントリのみ実行")
    rn.add_argument("--label", default="", help="run のラベル")
    rn.add_argument("--rerank-llm-base", default="",
                    help="rerank 段の LLM base url（既定 空文字＝rerank_chunks が LLM をスキップ）")
    rn.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="直近 run と baseline を比較したレポートを出力する")
    rp.add_argument("--run", type=int, default=None, help="run_id（既定: 最新）")
    rp.add_argument("--baseline", type=int, default=None, help="baseline run_id（既定: 2番目に新しい）")
    rp.add_argument("--full", action="store_true", help="エントリ×クエリ×stage の詳細表も出力")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
