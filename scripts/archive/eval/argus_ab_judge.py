#!/usr/bin/env python3
"""LLM-as-a-judge による A/B 採点。

Kimi-K2-Thinking を judge として呼び、各サンプルで 2 モデルの出力を
盲検で比較・採点する（モデル名はラベル A/B にマスク）。

- judges テーブルに採点結果を追記
- 採点軸: instruction_follow / factual / japanese / overall (各 1-5)
- prefer: A / B / tie
- judge bias 対策: 半分の試行で A/B 順序を入れ替える（逆順実行）

例:
    source ~/.secrets/rivault_tokens.sh
    python3 scripts/eval/argus_ab_judge.py --judge-model Kimi-K2-Thinking
    python3 scripts/eval/argus_ab_judge.py --report
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent.parent
EVAL_DB = REPO / "data" / "eval" / "v4flash_ab.db"

JUDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS judges (
    judge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL,
    judge_model     TEXT NOT NULL,
    swap            INTEGER NOT NULL,    -- 0=A=model_a /B=model_b, 1=swap
    model_a         TEXT NOT NULL,
    model_b         TEXT NOT NULL,
    instr_a         INTEGER, instr_b INTEGER,
    fact_a          INTEGER, fact_b  INTEGER,
    ja_a            INTEGER, ja_b    INTEGER,
    overall_a       INTEGER, overall_b INTEGER,
    prefer          TEXT,                -- 'A' / 'B' / 'tie'
    rationale       TEXT NOT NULL,
    raw_output      TEXT NOT NULL,
    latency_ms      INTEGER NOT NULL,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_judges_sample ON judges(sample_id);
"""

JUDGE_SYSTEM = (
    "あなたは厳格な LLM 出力評価者です。"
    "プロンプトに対する 2 つの出力 (A と B) を比較し、以下の 4 軸で 1-5 点で採点します。"
    "1) instruction_follow: 指示遵守 (フォーマット・字数・範囲)"
    "2) factual: 事実整合性 (入力情報からの逸脱がないか・捏造がないか)"
    "3) japanese: 日本語の自然さ・固有名詞の正規化"
    "4) overall: 総合"
    "そして prefer (A/B/tie) と短い rationale を付ける。"
    "出力は JSON オブジェクトのみ。コードフェンスは不要。スキーマ:"
    "{instr_a:int, instr_b:int, fact_a:int, fact_b:int, ja_a:int, ja_b:int, "
    "overall_a:int, overall_b:int, prefer:'A'|'B'|'tie', rationale:str}"
)


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(EVAL_DB)
    con.executescript(JUDGE_SCHEMA)
    return con


def call_judge(model: str, system: str, user: str, *, max_tokens: int, timeout: int) -> tuple[str, int, str]:
    """Returns (output, latency_ms, error)."""
    base_url = os.environ["RIVAULT_URL"]
    api_key = os.environ["RIVAULT_TOKEN"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if "kimi" in model.lower():
        payload["temperature"] = 0.3
    else:
        payload["temperature"] = 0.2
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.time()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta", {}) or {}
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                if delta.get("content"):
                    content_parts.append(delta["content"])
        latency_ms = int((time.time() - started) * 1000)
        content = "".join(content_parts).strip()
        if not content:
            content = "".join(reasoning_parts).strip()
        return content, latency_ms, ""
    except Exception as exc:
        return "", int((time.time() - started) * 1000), f"{type(exc).__name__}: {exc}"


def parse_judge_output(text: str) -> dict | None:
    # think タグや余計な前置きを削除
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    # 最後に出てきた {} を抽出
    m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, flags=re.S)
    candidates = []
    for m in re.finditer(r"\{.*?\}", text, flags=re.S):
        candidates.append(m.group(0))
    for c in reversed(candidates):
        try:
            obj = json.loads(c)
            if "prefer" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _trial_label(model: str, think: int) -> str:
    return f"{model}{'#think' if think else ''}"


def _split_label(label: str) -> tuple[str, int]:
    if label.endswith("#think"):
        return label[: -len("#think")], 1
    return label, 0


def cmd_run(args):
    if not os.environ.get("RIVAULT_URL"):
        print("ERROR: source ~/.secrets/rivault_tokens.sh が必要", file=sys.stderr)
        return 2
    if not args.pair or len(args.pair) != 2:
        print("ERROR: --pair MODEL_A MODEL_B を指定 (think モードは MODEL#think で指定)",
              file=sys.stderr)
        return 2
    pair_a_model, pair_a_think = _split_label(args.pair[0])
    pair_b_model, pair_b_think = _split_label(args.pair[1])
    pair_label = f"{args.pair[0]}|{args.pair[1]}"
    con = open_db()
    rows = list(con.execute(
        "SELECT s.sample_id, s.kind, s.title, s.system, s.user "
        "FROM samples s WHERE NOT EXISTS ("
        "  SELECT 1 FROM judges j WHERE j.sample_id=s.sample_id "
        "    AND j.judge_model=? AND j.rationale LIKE ?)",
        (args.judge_model, f"%[pair={pair_label}]%"),
    ))
    if args.limit:
        rows = rows[: args.limit]
    print(f"judging {len(rows)} sample(s) with {args.judge_model} pair={pair_label}",
          file=sys.stderr)

    rng = random.Random(args.seed)
    for sid, kind, title, sysp, userp in rows:
        # 指定ペアの trial を取得
        ta = con.execute(
            "SELECT output FROM trials WHERE sample_id=? AND model=? AND think=?",
            (sid, pair_a_model, pair_a_think),
        ).fetchone()
        tb = con.execute(
            "SELECT output FROM trials WHERE sample_id=? AND model=? AND think=?",
            (sid, pair_b_model, pair_b_think),
        ).fetchone()
        if not ta or not tb:
            print(f"  s#{sid}: skipped (missing trial: a={bool(ta)} b={bool(tb)})", file=sys.stderr)
            continue
        ma = args.pair[0]; oa = ta[0]
        mb = args.pair[1]; ob = tb[0]
        swap = rng.randint(0, 1)
        if swap:
            ma, mb = mb, ma
            oa, ob = ob, oa
        prompt = (
            f"# 対象プロンプト\n## system\n{sysp}\n\n## user\n{userp[:6000]}\n\n"
            f"# 出力 A\n{oa[:4000]}\n\n# 出力 B\n{ob[:4000]}\n\n"
            "上記のプロンプトに対する出力 A と B を 4 軸で採点し、JSON で答えてください。"
        )
        print(f"  s#{sid} [{kind}] judge swap={swap} ...", file=sys.stderr, flush=True)
        out, lat, err = call_judge(args.judge_model, JUDGE_SYSTEM, prompt,
                                    max_tokens=args.max_tokens, timeout=args.timeout)
        parsed = parse_judge_output(out) if out else None
        if not parsed:
            print(f"    -> parse_failed (err={err}, len={len(out)})", file=sys.stderr)
            con.execute(
                "INSERT INTO judges(sample_id,judge_model,swap,model_a,model_b,raw_output,latency_ms,error,rationale) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, args.judge_model, swap, ma, mb, out, lat, err or "parse_failed", ""),
            )
            con.commit()
            continue
        prefer = parsed.get("prefer", "tie")
        # swap した場合の prefer 反転は記録時に元のラベル基準でそのまま入れる（モデル名は model_a/_b で判別可能）
        con.execute(
            "INSERT INTO judges(sample_id,judge_model,swap,model_a,model_b,"
            "instr_a,instr_b,fact_a,fact_b,ja_a,ja_b,overall_a,overall_b,prefer,rationale,raw_output,latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, args.judge_model, swap, ma, mb,
             parsed.get("instr_a"), parsed.get("instr_b"),
             parsed.get("fact_a"), parsed.get("fact_b"),
             parsed.get("ja_a"), parsed.get("ja_b"),
             parsed.get("overall_a"), parsed.get("overall_b"),
             prefer, f"[pair={pair_label}] " + parsed.get("rationale", ""),
             out, lat),
        )
        con.commit()
        print(f"    -> prefer={prefer} ovr_a={parsed.get('overall_a')} ovr_b={parsed.get('overall_b')} ({lat}ms)",
              file=sys.stderr, flush=True)
    con.close()
    return 0


def cmd_report(args):
    con = open_db()
    print("# Judge Report\n")
    if args.by_judge:
        print("## judge × model 集計\n")
        print("| judge | pair | n | A_overall | B_overall | A_wins | B_wins | tie |")
        print("|---|---|---|---|---|---|---|---|")
        for row in con.execute(
            "SELECT judge_model, model_a, model_b, COUNT(*) AS n, "
            "       AVG(overall_a) AS oa, AVG(overall_b) AS ob, "
            "       SUM(CASE WHEN prefer='A' THEN 1 ELSE 0 END) AS aw, "
            "       SUM(CASE WHEN prefer='B' THEN 1 ELSE 0 END) AS bw, "
            "       SUM(CASE WHEN prefer='tie' THEN 1 ELSE 0 END) AS tw "
            "FROM judges WHERE prefer IS NOT NULL "
            "GROUP BY judge_model, model_a, model_b ORDER BY judge_model"
        ):
            jm, ma, mb, n, oa, ob, aw, bw, tw = row
            print(f"| `{jm}` | A=`{ma}` / B=`{mb}` | {n} | {oa:.2f} | {ob:.2f} | {aw} | {bw} | {tw} |")
        print()
    print("## 全体集計 (judge 横断、A/B ラベルを実モデルに戻して集計)\n")
    print("| model | n | avg_overall | avg_instr | avg_fact | avg_ja | wins | ties |")
    print("|---|---|---|---|---|---|---|---|")
    # model_a / model_b のスコアを model 単位に展開
    rows = list(con.execute(
        "SELECT model_a, model_b, instr_a, instr_b, fact_a, fact_b, ja_a, ja_b, "
        "       overall_a, overall_b, prefer FROM judges WHERE prefer IS NOT NULL"
    ))
    agg: dict[str, dict] = {}
    for ma, mb, ia, ib, fa, fb, ja_, jb, oa, ob, pref in rows:
        for m, instr, fact, ja_s, ovr, label in (
            (ma, ia, fa, ja_, oa, "A"),
            (mb, ib, fb, jb, ob, "B"),
        ):
            if instr is None:
                continue
            d = agg.setdefault(m, {"n": 0, "instr": 0, "fact": 0, "ja": 0, "ovr": 0, "win": 0, "tie": 0})
            d["n"] += 1
            d["instr"] += instr or 0
            d["fact"] += fact or 0
            d["ja"] += ja_s or 0
            d["ovr"] += ovr or 0
            if pref == label:
                d["win"] += 1
            elif pref == "tie":
                d["tie"] += 1
    # tie は両モデル+1するので半分にして二重計上を避ける
    for m, d in agg.items():
        n = d["n"] or 1
        ties_real = d["tie"] // 2 if d["tie"] else 0
        print(f"| `{m}` | {d['n']} | {d['ovr']/n:.2f} | {d['instr']/n:.2f} | {d['fact']/n:.2f} | "
              f"{d['ja']/n:.2f} | {d['win']} | {ties_real} |")

    print("\n## kind 別 prefer 集計\n")
    print("| kind | model | wins |")
    print("|---|---|---|")
    for row in con.execute(
        "SELECT s.kind, "
        "       SUM(CASE WHEN j.prefer='A' THEN 1 ELSE 0 END) AS a_win, "
        "       SUM(CASE WHEN j.prefer='B' THEN 1 ELSE 0 END) AS b_win, "
        "       SUM(CASE WHEN j.prefer='tie' THEN 1 ELSE 0 END) AS tie_n, "
        "       j.model_a, j.model_b "
        "FROM judges j JOIN samples s USING(sample_id) "
        "WHERE j.prefer IS NOT NULL "
        "GROUP BY s.kind, j.model_a, j.model_b ORDER BY s.kind"
    ):
        kind, aw, bw, tw, ma, mb = row
        print(f"| {kind} | A=`{ma}` ({aw}勝) / B=`{mb}` ({bw}勝) / tie {tw} | |")

    if args.full:
        print("\n## 全 judge エントリ\n")
        for row in con.execute(
            "SELECT j.sample_id, s.kind, s.title, j.model_a, j.model_b, j.swap, "
            "       j.instr_a, j.instr_b, j.fact_a, j.fact_b, j.ja_a, j.ja_b, "
            "       j.overall_a, j.overall_b, j.prefer, j.rationale "
            "FROM judges j JOIN samples s USING(sample_id) "
            "ORDER BY j.sample_id"
        ):
            print(f"\n### s#{row[0]} [{row[1]}] {row[2]}")
            print(f"A=`{row[3]}` B=`{row[4]}` swap={row[5]}")
            print(f"instr A/B={row[6]}/{row[7]} fact={row[8]}/{row[9]} ja={row[10]}/{row[11]} "
                  f"overall={row[12]}/{row[13]} prefer={row[14]}")
            print(f"rationale: {row[15]}")
    con.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--judge-model", default="Kimi-K2-Thinking")
    r.add_argument("--pair", nargs=2, metavar=("MODEL_A", "MODEL_B"),
                   help="比較するペア。think モードは MODEL#think 表記。"
                        "未指定時は (deepseek-ai/DeepSeek-V4-Flash, zai-org/GLM-4.7-Flash)")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--timeout", type=int, default=300)
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report")
    rp.add_argument("--full", action="store_true")
    rp.add_argument("--by-judge", action="store_true",
                    help="judge × pair 別に集計を先頭に追加")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
