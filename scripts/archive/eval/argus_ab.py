#!/usr/bin/env python3
"""Argus 候補 LLM の A/B 比較（pm.db / knowledge.db から合成プロンプト）。

過去の Slack slash command ログはローテーションで残っていないため、実データを
材料にして brief / risk / investigate 相当のプロンプトを合成し、複数モデルに
同一入力を投げて出力・所要時間・token 数を SQLite に記録する。

- 本番 DB (pm.db / knowledge.db) は読み取り専用でアクセス
- 結果は data/eval/v4flash_ab.db (新規, 暗号化なし) に蓄積
- Slack 投稿・本番 argus_state 書き込み・Canvas 更新はしない
- 認証: RiVault 系は source ~/.secrets/rivault_tokens.sh が事前必要
- ローカル gemma4 等は LOCAL_LLM_URL=http://localhost:8000/v1 を共有

例:
    source ~/.secrets/rivault_tokens.sh
    python3 scripts/eval/argus_ab.py build --n 30
    python3 scripts/eval/argus_ab.py run --target rivault --models deepseek-ai/DeepSeek-V4-Flash zai-org/GLM-4.7-Flash
    python3 scripts/eval/argus_ab.py run --target local --models google/gemma-4-26B-A4B-it --think
    python3 scripts/eval/argus_ab.py run --target rivault --models deepseek-ai/DeepSeek-V4-Flash --think-on-v4
    python3 scripts/eval/argus_ab.py report
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from db_utils import open_pm_db, open_knowledge_db  # noqa: E402

EVAL_DB = REPO / "data" / "eval" / "v4flash_ab.db"
DEFAULT_MODELS = [
    "deepseek-ai/DeepSeek-V4-Flash",
    "zai-org/GLM-4.7-Flash",
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,           -- brief / risk / investigate
    title         TEXT NOT NULL,           -- 人間用の見出し
    system        TEXT NOT NULL,
    user          TEXT NOT NULL,
    input_chars   INTEGER NOT NULL,
    seed          INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id         INTEGER NOT NULL,
    model             TEXT NOT NULL,
    think             INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL,
    latency_ms        INTEGER NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    output            TEXT NOT NULL,
    reasoning         TEXT NOT NULL,
    error             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_trials_sample ON trials(sample_id);
CREATE INDEX IF NOT EXISTS idx_trials_model ON trials(model);
"""


def open_eval_db() -> sqlite3.Connection:
    EVAL_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(EVAL_DB)
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# プロンプト合成
# --------------------------------------------------------------------------- #

SYS_BRIEF = (
    "あなたは富岳NEXTプロジェクトの PM アシスタントです。"
    "提供される情報から、現在の状況・直近の決定事項・要注意なアクションを"
    "300〜500 字程度で日本語ブリーフィングしてください。冗長な前置きは省く。"
)

SYS_RISK = (
    "あなたはリスク・矛盾検出を行う PM アシスタントです。"
    "出力は JSON 配列のみ。各要素は {risk, severity(low|med|high), evidence}。"
    "前置き・コードフェンスは出さない。"
)

SYS_INVESTIGATE = (
    "あなたは社内ドキュメントを基に質問に答える調査アシスタントです。"
    "提供されるナレッジ抜粋を根拠に、200〜400 字で回答してください。"
    "未確定事項は「未確定」と明記し、推測には「推測」と書き添える。"
)


def fetch_decisions(con, k: int, rng: random.Random) -> list[dict]:
    rows = list(con.execute(
        "SELECT content, decided_at, decided_by, rationale, source_context "
        "FROM decisions WHERE deleted=0 AND content IS NOT NULL "
        "ORDER BY decided_at DESC LIMIT 200"
    ))
    rng.shuffle(rows)
    out = []
    for r in rows[:k]:
        out.append({
            "content": r[0] or "",
            "decided_at": r[1] or "",
            "decided_by": r[2] or "",
            "rationale": r[3] or "",
            "context": r[4] or "",
        })
    return out


def fetch_action_items(con, k: int, rng: random.Random) -> list[dict]:
    rows = list(con.execute(
        "SELECT content, assignee, due_date, status, note "
        "FROM action_items WHERE deleted=0 AND content IS NOT NULL "
        "ORDER BY due_date DESC LIMIT 200"
    ))
    rng.shuffle(rows)
    out = []
    for r in rows[:k]:
        out.append({
            "content": r[0] or "",
            "assignee": r[1] or "",
            "due": r[2] or "",
            "status": r[3] or "",
            "note": r[4] or "",
        })
    return out


def fetch_knowledge(con, k: int, rng: random.Random) -> list[dict]:
    rows = list(con.execute(
        "SELECT topic, current_state, rationale, kind FROM knowledge "
        "WHERE deleted=0 AND confidence='high' ORDER BY last_validated_at DESC LIMIT 300"
    ))
    rng.shuffle(rows)
    out = []
    for r in rows[:k]:
        out.append({
            "topic": r[0] or "",
            "state": r[1] or "",
            "rationale": r[2] or "",
            "kind": r[3] or "",
        })
    return out


def make_brief_prompt(decisions, actions) -> tuple[str, str]:
    body = ["## 直近の決定事項"]
    for d in decisions:
        body.append(f"- ({d['decided_at']}) {d['content']}")
        if d["rationale"]:
            body.append(f"  根拠: {d['rationale']}")
    body.append("")
    body.append("## 進行中のアクション")
    for a in actions:
        body.append(f"- [{a['status']}] {a['content']} (担当: {a['assignee']}, 期限: {a['due']})")
        if a["note"]:
            body.append(f"  メモ: {a['note']}")
    title = f"brief: 決定 {len(decisions)} 件 / アクション {len(actions)} 件"
    return title, "\n".join(body)


def make_risk_prompt(actions, knowledge) -> tuple[str, str]:
    body = ["## アクション一覧"]
    for a in actions:
        body.append(f"- [{a['status']}] {a['content']} (担当: {a['assignee']}, 期限: {a['due']})")
    body.append("")
    body.append("## 関連ナレッジ")
    for k in knowledge:
        body.append(f"- ({k['kind']}) {k['topic']}: {k['state'][:200]}")
    title = f"risk: action {len(actions)} 件 / knowledge {len(knowledge)} 件"
    return title, "\n".join(body)


def make_investigate_prompt(question_kw: str, knowledge: list[dict]) -> tuple[str, str]:
    body = [f"質問: {question_kw}", "", "## 参照ナレッジ"]
    for k in knowledge:
        body.append(f"- ({k['kind']}) {k['topic']}")
        body.append(f"  状況: {k['state'][:300]}")
        if k["rationale"]:
            body.append(f"  根拠: {k['rationale'][:200]}")
    title = f"investigate: {question_kw[:30]}"
    return title, "\n".join(body)


INVESTIGATE_QUESTIONS = [
    "現在のスケールアウトネットワークの設計方針は何で、未確定事項は何か。",
    "ストレージ階層について最新の決定はどうなっているか。",
    "アプリケーション WG の主要な懸案は何か。",
    "ベンチマーク評価の方針について最新の合意事項を教えて。",
    "電力・冷却の制約について現時点でのコンセンサスは何か。",
]


def build_samples(n: int, seed: int = 42) -> list[tuple[str, str, str, str]]:
    """Returns: list of (kind, title, system, user)."""
    rng = random.Random(seed)
    pm = open_pm_db(REPO / "data" / "pm.db")
    pm.row_factory = None
    kdb = open_knowledge_db(REPO / "data" / "knowledge.db")
    kdb.row_factory = None

    out: list[tuple[str, str, str, str]] = []
    # 配分: brief 1/3 / risk 1/3 / investigate 1/3
    n_brief = n // 3
    n_risk = n // 3
    n_inv = n - n_brief - n_risk

    for _ in range(n_brief):
        ds = fetch_decisions(pm, rng.randint(8, 16), rng)
        ai = fetch_action_items(pm, rng.randint(10, 20), rng)
        title, user = make_brief_prompt(ds, ai)
        out.append(("brief", title, SYS_BRIEF, user))

    for _ in range(n_risk):
        ai = fetch_action_items(pm, rng.randint(10, 20), rng)
        kn = fetch_knowledge(kdb, rng.randint(5, 12), rng)
        title, user = make_risk_prompt(ai, kn)
        out.append(("risk", title, SYS_RISK, user))

    for _ in range(n_inv):
        q = rng.choice(INVESTIGATE_QUESTIONS)
        kn = fetch_knowledge(kdb, rng.randint(8, 16), rng)
        title, user = make_investigate_prompt(q, kn)
        out.append(("investigate", title, SYS_INVESTIGATE, user))

    pm.close()
    kdb.close()
    return out


# --------------------------------------------------------------------------- #
# RiVault 呼び出し
# --------------------------------------------------------------------------- #

@dataclass
class TrialResult:
    ok: bool
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    output: str
    reasoning: str
    error: str = ""


def call_local(model: str, system: str, user: str, *, max_tokens: int, think: bool, timeout: int) -> TrialResult:
    """ローカル vLLM (gemma4 等) を直接叩く。本番 cli_utils.call_local_llm と同等の think 処理。"""
    base_url = os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")
    api_key = os.environ.get("LOCAL_LLM_TOKEN", "dummy")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.6 if think else 0.8,
        "stream": True,
    }
    if think:
        payload["chat_template_kwargs"] = {"enable_thinking": True}
        payload["top_p"] = 0.95
        payload["skip_special_tokens"] = False
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.time()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
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
            if chunk.get("usage"):
                usage = chunk["usage"]
        latency_ms = int((time.time() - started) * 1000)
        content = "".join(content_parts).strip()
        reasoning = "".join(reasoning_parts).strip()
        # gemma4 で reasoning が混じった場合の <think> タグ除去
        import re as _re
        content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.S).strip()
        if not content and reasoning:
            content = reasoning
            reasoning = ""
        return TrialResult(
            ok=True, latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            output=content, reasoning=reasoning,
        )
    except Exception as exc:
        return TrialResult(
            ok=False, latency_ms=int((time.time() - started) * 1000),
            prompt_tokens=None, completion_tokens=None, total_tokens=None,
            output="", reasoning="",
            error=f"{type(exc).__name__}: {exc}",
        )


def call_rivault(model: str, system: str, user: str, *, max_tokens: int, think: bool, timeout: int) -> TrialResult:
    base_url = os.environ["RIVAULT_URL"]
    api_key = os.environ["RIVAULT_TOKEN"]
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": True,
    }
    m = model.lower()
    if "kimi" in m:
        payload["temperature"] = 0.3
    elif "deepseek-v4" in m or "v4-flash" in m:
        payload["temperature"] = 0.3
        if think:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["thinking"] = {"type": "disabled"}
    else:
        payload["temperature"] = 0.3
        payload["thinking"] = {"type": "disabled"}
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.time()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
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
            if chunk.get("usage"):
                usage = chunk["usage"]
        latency_ms = int((time.time() - started) * 1000)
        content = "".join(content_parts).strip()
        reasoning = "".join(reasoning_parts).strip()
        if not content and reasoning:
            content = reasoning
            reasoning = ""
        return TrialResult(
            ok=True,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            output=content,
            reasoning=reasoning,
        )
    except Exception as exc:
        return TrialResult(
            ok=False,
            latency_ms=int((time.time() - started) * 1000),
            prompt_tokens=None, completion_tokens=None, total_tokens=None,
            output="", reasoning="",
            error=f"{type(exc).__name__}: {exc}",
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_build(args: argparse.Namespace) -> int:
    samples = build_samples(args.n, seed=args.seed)
    con = open_eval_db()
    con.execute("DELETE FROM samples")
    con.execute("DELETE FROM trials")
    for kind, title, sys_p, user_p in samples:
        con.execute(
            "INSERT INTO samples(kind,title,system,user,input_chars,seed) VALUES(?,?,?,?,?,?)",
            (kind, title, sys_p, user_p, len(sys_p) + len(user_p), args.seed),
        )
    con.commit()
    print(f"built {len(samples)} samples in {EVAL_DB}", file=sys.stderr)
    for row in con.execute("SELECT kind, COUNT(*), AVG(input_chars) FROM samples GROUP BY kind"):
        print(f"  {row[0]:12s} n={row[1]} avg_chars={int(row[2])}", file=sys.stderr)
    con.close()
    return 0


def _resolve_think(model: str, args: argparse.Namespace) -> bool:
    """モデル名と CLI フラグから think モードを決定。"""
    m = model.lower()
    if args.think:
        return True
    if args.think_on_v4 and ("deepseek-v4" in m or "v4-flash" in m):
        return True
    return False


def cmd_run(args: argparse.Namespace) -> int:
    if args.target == "rivault":
        if not os.environ.get("RIVAULT_URL") or not os.environ.get("RIVAULT_TOKEN"):
            print("ERROR: source ~/.secrets/rivault_tokens.sh が必要", file=sys.stderr)
            return 2
        backend = call_rivault
    elif args.target == "local":
        base = os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")
        try:
            health_url = base.removesuffix("/v1").rstrip("/") + "/health"
            requests.get(health_url, timeout=3).raise_for_status()
        except Exception as exc:
            print(f"ERROR: ローカル vLLM ({base}) が応答しません: {exc}", file=sys.stderr)
            return 2
        backend = call_local
    else:
        print(f"ERROR: 未知の target {args.target}", file=sys.stderr)
        return 2

    con = open_eval_db()
    samples = list(con.execute(
        "SELECT s.sample_id, s.kind, s.title, s.system, s.user FROM samples s ORDER BY s.sample_id"
    ))
    if args.limit:
        samples = samples[: args.limit]
    print(f"running {len(samples)} sample(s) x {len(args.models)} model(s) target={args.target}",
          file=sys.stderr)
    rate_sec = max(0, args.rate_limit_ms) / 1000.0
    for sid, kind, title, sysp, userp in samples:
        for model in args.models:
            think_flag = _resolve_think(model, args)
            already = con.execute(
                "SELECT 1 FROM trials WHERE sample_id=? AND model=? AND think=?",
                (sid, model, int(think_flag)),
            ).fetchone()
            if already:
                continue
            print(f"  [{kind}] s#{sid} {model} think={think_flag} ...",
                  file=sys.stderr, flush=True)
            r = backend(model, sysp, userp,
                        max_tokens=args.max_tokens, think=think_flag, timeout=args.timeout)
            con.execute(
                "INSERT INTO trials(sample_id,model,think,ok,latency_ms,prompt_tokens,"
                "completion_tokens,total_tokens,output,reasoning,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, model, int(think_flag), int(r.ok), r.latency_ms,
                 r.prompt_tokens, r.completion_tokens, r.total_tokens,
                 r.output, r.reasoning, r.error),
            )
            con.commit()
            print(f"    -> ok={r.ok} latency={r.latency_ms}ms ctok={r.completion_tokens}",
                  file=sys.stderr, flush=True)
            if rate_sec > 0:
                time.sleep(rate_sec)
    con.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    con = open_eval_db()
    print("# A/B Report\n")
    print("## サマリ (model x kind)\n")
    print("| model | kind | n | ok | avg_latency_ms | avg_completion_tokens |")
    print("|---|---|---|---|---|---|")
    for row in con.execute(
        "SELECT t.model, s.kind, COUNT(*) AS n, "
        "       SUM(t.ok) AS ok_n, AVG(t.latency_ms) AS lat, AVG(t.completion_tokens) AS ctok "
        "FROM trials t JOIN samples s USING(sample_id) "
        "GROUP BY t.model, s.kind ORDER BY s.kind, t.model"
    ):
        m, k, n, okn, lat, ctok = row
        print(f"| `{m}` | {k} | {n} | {okn}/{n} | {int(lat or 0)} | {int(ctok or 0)} |")
    if args.full:
        print("\n## サンプル別出力\n")
        for srow in con.execute("SELECT sample_id, kind, title, input_chars FROM samples ORDER BY sample_id"):
            sid, kind, title, ichars = srow
            print(f"\n### s#{sid} [{kind}] {title} (input {ichars} chars)\n")
            for trow in con.execute(
                "SELECT model, ok, latency_ms, completion_tokens, output, reasoning, error "
                "FROM trials WHERE sample_id=? ORDER BY model", (sid,)
            ):
                m, ok, lat, ctok, out, rsn, err = trow
                print(f"#### `{m}` (ok={ok}, {lat}ms, {ctok} ctok)\n")
                if not ok:
                    print(f"```\nERROR: {err}\n```\n"); continue
                if rsn:
                    print(f"<details><summary>reasoning ({len(rsn)} chars)</summary>\n\n```\n{rsn[:1500]}\n```\n</details>\n")
                print("```\n" + (out[:2000] if not args.long else out) + "\n```\n")
    con.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="サンプルを合成して samples テーブルに書き込む")
    b.add_argument("--n", type=int, default=30)
    b.add_argument("--seed", type=int, default=42)
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="モデルを呼び出して trials に追記")
    r.add_argument("--target", choices=["rivault", "local"], default="rivault",
                   help="呼び出し先 backend (rivault=GB200 NVL4 / local=本番 vLLM:8000)")
    r.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    r.add_argument("--max-tokens", type=int, default=1500)
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--think", action="store_true",
                   help="全モデルで think モードを有効化")
    r.add_argument("--think-on-v4", action="store_true",
                   help="DeepSeek-V4 系のみ think モードを有効化")
    r.add_argument("--rate-limit-ms", type=int, default=500,
                   help="連投間 sleep。本番デーモンとの輻輳緩和用")
    r.add_argument("--limit", type=int, default=0, help="先頭 N サンプルだけ実行")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="集計レポートを Markdown で出力")
    rp.add_argument("--full", action="store_true", help="サンプル別の出力も全部出す")
    rp.add_argument("--long", action="store_true", help="出力を切り詰めず全文")
    rp.set_defaults(func=cmd_report)

    # 古い --build-samples / --run / --report も受ける糖衣
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
