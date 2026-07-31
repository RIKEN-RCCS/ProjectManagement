#!/usr/bin/env python3
"""investigate_ab.py — investigate 経路/モデル A/B 評価（アーム一般化）

gemma4（小型モデル）前提で固定されている investigate の検索パラメータ
（TOP_K_RERANK / re-rank プレビュー長 / search_text 抜粋長 / --file 全文QAの窓サイズ）を
glm-5.2（長コンテキスト・高推論）向けに拡大した場合の end-to-end 回答品質を検証した
baseline vs expanded の枠組みを一般化し、任意の 2 アーム（ARM_PRESETS のプリセット名）
を scripts/eval/investigate_gold.yaml のゴールド質問セットで A/B 比較する。

- 各エントリ×各アーム（--arm-a / --arm-b で指定。既定は baseline / expanded = 従来挙動）で
  scripts/argus/pm_argus_agent.py --investigate を本番同一コードパスで
  subprocess 実行する（--to-box/--to-slack/--to-canvas 等は付けない = 無副作用、stdout のみ）
- 両アームとも --agent-timeout（既定1200秒）を明示的に pm_argus_agent.py --timeout として
  渡し、同一の調査予算で比較する（未指定だと pm_argus_agent.py 内部既定の480秒予算で
  動き、片方のアームだけ予算切れの静かな劣化を受けて「アームの効果」でなく
  「480秒に収まるか」を測ってしまうため）。subprocess の kill タイムアウトは
  agent_timeout + 300秒に自動設定する（CLI からは指定しない）
- 回答本文に pm_argus_agent.py の予算切れ注記
  （"タイムアウト予算超過のため未読込の断片"）が含まれるかを検出し、JSONL に
  budget_truncated_a / budget_truncated_b として記録する
- judge は scripts/eval/argus_ab_judge.py の call_judge / parse_judge_output を
  import 流用する（判定ロジックの重複実装を避ける）
- 回答本文は既定では保存しない（judge には in-memory で渡す）。--save-answers
  指定時のみ data/eval/investigate_ab/{run開始時刻}_{id}_{アーム名}.txt に保存し、
  JSONL に answer_path_a / answer_path_b を記録する。保存した
  平文回答（機密の可能性がある調査結果）は確認後に削除する運用とする。
  JSONL 自体には --save-answers の有無によらず本文を入れず文字数のみ記録する
- returncode 非0・タイムアウトは answer=None として記録し、judge はスキップして
  prefer_arm="error" とする
- cmd_run は実行前に RIVAULT_URL / RIVAULT_TOKEN の設定（judge 用）と、選択した
  両アームの ${VAR} env 参照が展開できることを確認する（全問走らせた後に気付くのを防ぐ）

注意（env スコープ）: ARM_PRESETS の env（ARGUS_TOP_K_RERANK 等）はプロセス全体スコープの
環境変数であり、investigate の検索層だけでなく Slack 抽出のナレッジ検索
（cli_utils.retrieve_knowledge_for_extraction 経由）にも影響する。この評価用に
export したシェルで pm_ingest.py 等の本番パイプラインを実行しないこと。

例:
    source ~/.secrets/rivault_tokens.sh
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run --entry ENTRY_ID
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run --save-answers
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py report

k3 系アーム（k3-loop / k3-oneshot）実行時の前提（_K3_ENV の ${VAR} 展開に必要）:
    source ~/.secrets/rivault_tokens.sh   # RIVAULT_URL/TOKEN（judge 用。k3 経路自体は空文字で封鎖）
    source ~/.secrets/localLLM.sh         # EMBED_API_BASE/KEY, EMBED_MODEL 用
    export RIKYU_URL="..."                # RIKYU の LOCAL_LLM_URL 相当（手動 export、直書き禁止）
    export RIKYU_TOKEN="..."              # RIKYU の LOCAL_LLM_TOKEN 相当
    ~/.venv_aarch64/bin/python3 scripts/eval/investigate_ab.py run --arm-a glm-loop --arm-b k3-oneshot \\
        --jsonl data/eval/investigate_k3.jsonl

夜間の本走（4ペア×12〜14問、所要8〜13時間見込み）はログを確実に流すため
`python3 -u scripts/eval/investigate_ab.py run ...` を推奨する。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = REPO_ROOT / "scripts"
_EVAL_DIR = Path(__file__).resolve().parent
for _p in (_SCRIPTS_DIR, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argus_ab_judge  # noqa: E402 — scripts/eval 内の同居モジュールを流用（call_judge/parse_judge_output）

DEFAULT_GOLD = _EVAL_DIR / "investigate_gold.yaml"
DEFAULT_JSONL = REPO_ROOT / "data" / "eval" / "investigate_ab.jsonl"
DEFAULT_ANSWERS_DIR = REPO_ROOT / "data" / "eval" / "investigate_ab"

_WIN_TIE_THRESHOLD = 0.60
_REQUIRED_FIELDS = ("id", "question", "reference", "since")

# 最小回答長ガード（B-2）: mode=="search" の回答がこの文字数未満の場合、
# suspect_short_answer=True として記録する（error 扱いにはしない。judge は走らせる —
# 短い回答は自然に劣後判定されるため）
_SUSPECT_SHORT_ANSWER_MIN_CHARS = 200

# pm_argus_agent.py --timeout の既定値（investigate の本番既定は480秒だが、
# A/B の両アームには明示的にこの値を渡し予算を固定する）
_DEFAULT_AGENT_TIMEOUT = 1200
# subprocess の kill タイムアウトの、agent_timeout に対するマージン（秒）
_KILL_TIMEOUT_MARGIN = 300

# pm_argus_agent.py:1272 の予算切れ注記マーカー（run_document_qa の
# skipped_windows 由来の制限事項）。このテキストが答えに含まれる場合、
# 予算不足で窓の一部が読まれずに回答が生成されたことを示す。
_BUDGET_MARKER = "タイムアウト予算超過のため未読込の断片"

# k3 系プリセット共通 env（罠対策込み）:
# - ARGUS_SKIP_LLM_SECRETS=1
# - RIVAULT_URL/TOKEN を空文字にして rivault ルートを決定的に無効化
#   （llm.py の routing_priority available 判定は truthy チェックのため空文字で封鎖できる）
# - LOCAL_LLM_URL/TOKEN/MODEL を RIKYU 側に向ける
# - EMBED_API_BASE/KEY を明示（embed_utils.py が RIVAULT_URL にフォールバックして
#   vector 脚が静かに空になるのを防ぐ）
# - EMBED_MODEL を明示（ARGUS_SKIP_LLM_SECRETS=1 で localLLM.sh が source されず、
#   embed_utils.py のハードコード既定 "bge-m3:567m" にフォールバックしてモデル名が
#   索引構築時と不一致になり、vector 脚が warning だけで空になる罠を防ぐ）
# - ARGUS_LLM_TEMPERATURE=1.0 は HF モデルカード推奨値（kimi-k3-tuned 前回評価と整合）。
#   local ルート限定の反映（llm.py: _resolve_llm_temperature_env）。top_p は
#   local 経路の呼び出し引数に無いため env からは制御できない（既知の非対称性）
_K3_ENV: dict[str, str] = {
    "ARGUS_SKIP_LLM_SECRETS": "1",
    "RIVAULT_URL": "",
    "RIVAULT_TOKEN": "",
    "LOCAL_LLM_URL": "${RIKYU_URL}",
    "LOCAL_LLM_TOKEN": "${RIKYU_TOKEN}",
    "LOCAL_LLM_MODEL": "kimi-k3",
    "EMBED_API_BASE": "${EMBED_API_BASE}",
    "EMBED_API_KEY": "${EMBED_API_KEY}",
    "EMBED_MODEL": "${EMBED_MODEL}",
    "ARGUS_LLM_TEMPERATURE": "1.0",
}

# run_investigate_arm が subprocess env を組み立てる前に、親シェルから必ず除去する
# ARGUS_* キー集合。これらは investigate の経路（loop/one-shot）や推論設定に直接
# 影響するため、評価用に export されたシェルにこれらの env が残っていると、
# アームの env override（多くは baseline/glm-loop 等で {} = 空）が「親環境の値を
# そのまま継承」してしまい、意図しない one-shot 化・reasoning 設定の混入を静かに
# 引き起こす（アーム間の env 汚染）。ここで一律除去してから各アームの env を
# 重ねることで、アームの env に明示されたキーのみが有効になる。
# 旧互換の baseline/expanded も同様に除去する（従来この env が設定された状態での
# 実行は想定外のため。CI/評価専用シェルでの利用を前提とし、本番シェルでの
# investigate_ab.py 実行は避けること = 冒頭の「env スコープ」注意書き参照）。
_ARM_CONTROLLED_ENV_KEYS = frozenset({
    "ARGUS_ONESHOT",
    "ARGUS_ONESHOT_TOP_K",
    "ARGUS_ONESHOT_CHAR_BUDGET",
    "ARGUS_ONESHOT_MAX_TOKENS",
    "ARGUS_PRESERVE_REASONING",
    "ARGUS_REASONING_EFFORT",
    "ARGUS_LLM_TEMPERATURE",
    # gemma4 期固定値だった investigate 検索パラメータ（expanded アーム等が上書きする）。
    # LOCAL_LLM_* / RIVAULT_* / EMBED_* は glm アームが親から継承する前提のため
    # ここには加えない。
    "ARGUS_TOP_K_RERANK",
    "ARGUS_SEARCH_EXCERPT_CHARS",
    "ARGUS_RERANK_PREVIEW_CHARS",
    "ARGUS_DOC_QA_WINDOW",
    # one-shot K3 override（ARGUS_ONESHOT_LLM_URL 等）と investigate の総予算。
    "ARGUS_ONESHOT_LLM_URL",
    "ARGUS_ONESHOT_LLM_TOKEN",
    "ARGUS_ONESHOT_LLM_MODEL",
    "ARGUS_ONESHOT_LLM_TEMPERATURE",
    "ARGUS_INVESTIGATE_TIMEOUT",
})

# JSONL レコードに記録するアーム設定の非機密ホワイトリスト。arm_env 全体は
# LOCAL_LLM_TOKEN 等の平文シークレットを含むため絶対にダンプしない。
_ARM_CONFIG_WHITELIST = (
    "LOCAL_LLM_MODEL", "ARGUS_ONESHOT", "ARGUS_ONESHOT_TOP_K", "ARGUS_PRESERVE_REASONING",
)

# アーム定義: env override（プロセス全体スコープの環境変数）と、
# pm_argus_agent.py コマンド末尾に追加する extra_args（既定は空）。
# baseline/expanded は旧互換（従来の ARMS と同一の env）。
# 既定値はここでは変更しない（A/B 合格後に本体側の既定を見直す）。
ARM_PRESETS: dict[str, dict] = {
    "baseline": {"env": {}},
    "expanded": {
        "env": {
            "ARGUS_TOP_K_RERANK": "10",
            "ARGUS_SEARCH_EXCERPT_CHARS": "1200",
            "ARGUS_RERANK_PREVIEW_CHARS": "800",
            "ARGUS_DOC_QA_WINDOW": "150000",
        },
    },
    "glm-loop": {"env": {}},
    # one-shot の TOP_K=50 は 2026-07-29 の N スイープ実測による:
    # N=50 で gold reference を完全にカバーし（N=200/600 は詳細増のみで正答性向上なし）、
    # kimi-k3 は設問依存で生成が長引くと RIKYU 側 nginx の 600s gateway timeout に
    # 当たる（生成が長引く設問で N=200/600 で全損を再現）。client --timeout では回避不可。
    "glm-oneshot": {
        "env": {
            "ARGUS_ONESHOT": "1",
            "ARGUS_ONESHOT_TOP_K": "50",
        },
    },
    "k3-loop": {
        "env": {**_K3_ENV, "ARGUS_PRESERVE_REASONING": "1"},
    },
    "k3-oneshot": {
        "env": {**_K3_ENV, "ARGUS_ONESHOT": "1", "ARGUS_ONESHOT_TOP_K": "50"},
    },
}

JUDGE_SYSTEM = (
    "あなたは PM 調査アシスタントの回答品質を審査する厳格な評価者です。"
    "評価観点: (1) 参照事実のカバー — 参照事実に該当する内容を正確に含むか、"
    "(2) 根拠性 — 出典（会議・Slack・資料名・日付）を明示しているか、"
    "(3) 質問への直接性 — 問いに正面から答えているか、"
    "(4) 正確性 — 参照事実と矛盾する断定をしていないか。"
    "冗長さ自体は減点しない。"
    "prefer は 'A'/'B'/'tie'、短い rationale を付けてください。"
    "出力は JSON のみ。スキーマ: {prefer:'A'|'B'|'tie', rationale:str}"
)


# --------------------------------------------------------------------------- #
# ゴールドセット読み込み
# --------------------------------------------------------------------------- #

def load_gold(path: Path) -> list[dict]:
    """investigate_gold.yaml を読み込み、必須フィールドを検証する。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("entries") or []
    for entry in entries:
        missing = [field for field in _REQUIRED_FIELDS if not entry.get(field)]
        if missing:
            raise ValueError(
                f"gold entry '{entry.get('id', '?')}' に必須フィールドが欠落: {missing}"
            )
    return entries


# --------------------------------------------------------------------------- #
# RIVAULT プリフライト
# --------------------------------------------------------------------------- #

def rivault_configured() -> bool:
    """RIVAULT_URL / RIVAULT_TOKEN が両方設定されているか確認する。"""
    return bool(os.environ.get("RIVAULT_URL")) and bool(os.environ.get("RIVAULT_TOKEN"))


# --------------------------------------------------------------------------- #
# アーム env の ${VAR} 参照展開
# --------------------------------------------------------------------------- #

_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")


def _expand_env_refs(env: dict[str, str]) -> dict[str, str]:
    """アーム env の ${NAME} 形式の値を親環境変数 os.environ[NAME] で展開する。

    ${NAME} 形式でない値（空文字列リテラル含む）はそのまま通す。
    ${NAME} 参照先が未設定または空文字列の場合は ValueError
    （RIKYU_URL 未 export のまま走って全問エラーになる事故を run 開始前に防ぐ）。
    """
    expanded: dict[str, str] = {}
    for key, value in env.items():
        m = _ENV_REF_RE.match(value)
        if not m:
            expanded[key] = value
            continue
        var_name = m.group(1)
        resolved = os.environ.get(var_name)
        if not resolved:
            raise ValueError(
                f"env var '{var_name}'（{key}=${{{var_name}}} が参照）が未設定または空です"
            )
        expanded[key] = resolved
    return expanded


# --------------------------------------------------------------------------- #
# investigate subprocess 実行
# --------------------------------------------------------------------------- #

def build_investigate_cmd(entry: dict, agent_timeout: float, extra_args=()) -> list[str]:
    """gold エントリから pm_argus_agent.py --investigate のコマンド行を組み立てる。

    agent_timeout は pm_argus_agent.py --timeout（内部の調査予算）に渡す。
    mode=docqa かつ file フィールドがあれば --file を追加する。
    extra_args はコマンド末尾にそのまま追加する（アームプリセットの汎用拡張点）。
    """
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "argus" / "pm_argus_agent.py"),
        "--investigate", entry["question"], "--since", entry["since"], "--no-intent-header",
        "--timeout", str(agent_timeout),
    ]
    if entry.get("mode") == "docqa" and entry.get("file"):
        cmd += ["--file", entry["file"]]
    cmd += list(extra_args)
    return cmd


def _stderr_text(stderr) -> str:
    if not stderr:
        return ""
    return stderr if isinstance(stderr, str) else stderr.decode("utf-8", "replace")


def _stderr_tail(stderr, n: int = 20) -> str:
    text = _stderr_text(stderr)
    if not text:
        return ""
    return "\n".join(text.strip().splitlines()[-n:])


# --------------------------------------------------------------------------- #
# stderr メトリクス抽出
# --------------------------------------------------------------------------- #

# pm_argus_agent.py の STEP ループログ:
#   "[STEP {step}/{max_steps}] LLM 応答 {n} chars, {m}件のツール呼び出し ({elapsed}s)"
# 注意: このログ・正規表現は STEP ループ内で LLM が新規に発行したツール呼び出しのみを
# 数える。STEP ループに入る前の事前検索（[initial-search]、下記 _INITIAL_SEARCH_RE）で
# 実行された search_text はここには含まれない（別メトリクス initial_search_calls）。
# 実測（data/eval/investigate_k3.jsonl の glm-loop 側 metrics_a、51件全数）では
# tool_calls_total=0 が 51/51、steps_used はほぼ 3 で forced_synthesis=True が大半
# （zero-tool nudge を2回使い切って強制 synthesis に落ちる = STEP ループ内では
# 追加のツール呼び出しを一度も発行していない）。これは initial-search 側の実行が
# tool_calls_total に反映されず「ツールを何も使っていない」ように見える表示上の
# 問題であり、実際には initial-search で search_text が複数件実行されている
# （pm_argus_agent.py 内の "[initial-search] rewrite クエリN件を事前検索" /
# "[initial-search] 完了 (X.Xs, M件)" ログ参照）。steps_used 自体はパース漏れではなく
# 実際に STEP ループが (ナッジ含め) 進んだ回数を正しく反映している。
_TOOL_CALLS_RE = re.compile(r"(\d+)件のツール呼び出し")
_STEP_RE = re.compile(r"\[STEP (\d+)/")
# pm_argus_agent.py の initial-search 事前検索ログ:
#   "[initial-search] 完了 ({elapsed}s, {n}件)"
_INITIAL_SEARCH_RE = re.compile(r"\[initial-search\]\s*完了\s*\([\d.]+s,\s*(\d+)件\)")
# pm_argus_agent.py の強制 synthesis ログ: "[forced-synthesis] ..."
_FORCED_SYNTHESIS_MARKER = "[forced-synthesis]"
# retrieval.py:684 の re-rank エラーフォールバックログ（例外→日付降順フォールバック）
_RERANK_FALLBACK_MARKER = "日付降順フォールバック"
# retrieval.py:682 の re-rank 番号パース失敗フォールバックログ（先頭N件で代替）
_RERANK_HEAD_FALLBACK_MARKER = "先頭件数で代替"
# retrieval.py:491 の embedding 取得エラーログ
_EMBEDDING_ERROR_MARKER = "embedding 取得エラー"
# pm_argus_agent.py の one-shot 経路ログ（prompt_chars は追加分、旧形式ログとの
# 互換のため任意マッチにする）:
#   "[oneshot] retrieved={n} packed={m} context_chars={c} prompt_chars={p}"
_ONESHOT_RE = re.compile(
    r"\[oneshot\]\s*retrieved=(\d+)\s*packed=(\d+)\s*context_chars=(\d+)"
    r"(?:\s*prompt_chars=(\d+))?"
)
# pm_argus_agent.py の one-shot 劣化 WARN
_VECTOR_LEG_EMPTY_MARKER = "[oneshot][DEGRADED] vector leg empty"
_SOURCES_MISSING_MARKER = "sources section missing"
# llm.py の max_tokens 縮小リトライログ:
#   "[WARN] コンテキスト長超過。max_tokens {a} → {b} に縮小再試行"
_CTX_SHRINK_MARKER = "に縮小再試行"
# llm.py:757 のルーティングログ: "[INFO] call_argus_llm: route_order={...} think={...} fallback={...}"
_ROUTE_ORDER_MARKER = "route_order="
# pm_argus_agent.py:_call_oneshot_llm の override LLM フォールバック WARN:
#   "[oneshot][FALLBACK] override LLM failed (...), falling back to default route"
#   （残り時間不足でフォールバックせず raise した場合はこのマーカーは出ない）
_ONESHOT_LLM_FALLBACK_MARKER = "[oneshot][FALLBACK]"
# retrieval.py:retrieve_chunks_hybrid の RRF 遮断ログ（S1: date_fallback / like 共通の
# 固定部分文字列で判定。stage 名部分は "[hybrid] FTS {stage} excluded from RRF (vector-only)"）
_HYBRID_FTS_EXCLUDED_MARKER = "excluded from RRF (vector-only)"


def _extract_run_metrics(stderr: str) -> dict:
    """subprocess の stderr ログから investigate 実行のメトリクスを抽出する。

    ログ文言は pm_argus_agent.py / retrieval.py / llm.py の実際の出力に合わせている。
    劣化イベントは種別ごとに分けて記録する（vector_leg_empty / sources_missing /
    ctx_shrink_retries を合算した旧 degraded_events は廃止。原因の切り分けができない
    合算値は N スイープ・本走の劣化原因調査に使えないため）。
    """
    text = stderr or ""
    # STEP ループ内で LLM が新規発行したツール呼び出しのみ（initial-search 分は含まない）
    tool_calls_total = sum(int(m) for m in _TOOL_CALLS_RE.findall(text))
    # STEP ループ前の事前検索（rewrite クエリの search_text 並列実行）本数。独立メトリクス
    initial_search_calls = sum(int(m) for m in _INITIAL_SEARCH_RE.findall(text))
    steps = [int(m) for m in _STEP_RE.findall(text)]
    steps_used = max(steps) if steps else 0
    forced_synthesis = _FORCED_SYNTHESIS_MARKER in text
    rerank_fallbacks = text.count(_RERANK_FALLBACK_MARKER)
    rerank_head_fallbacks = text.count(_RERANK_HEAD_FALLBACK_MARKER)
    embedding_errors = text.count(_EMBEDDING_ERROR_MARKER)

    oneshot_matches = _ONESHOT_RE.findall(text)
    if oneshot_matches:
        _retrieved, packed, context_chars, prompt_chars = oneshot_matches[-1]
        oneshot_chunks = int(packed)
        oneshot_context_chars = int(context_chars)
        oneshot_prompt_chars = int(prompt_chars) if prompt_chars else None
    else:
        oneshot_chunks = None
        oneshot_context_chars = None
        oneshot_prompt_chars = None

    vector_leg_empty = text.count(_VECTOR_LEG_EMPTY_MARKER)
    sources_missing = text.count(_SOURCES_MISSING_MARKER)
    ctx_shrink_retries = text.count(_CTX_SHRINK_MARKER)
    route_orders = text.count(_ROUTE_ORDER_MARKER)
    oneshot_llm_fallbacks = text.count(_ONESHOT_LLM_FALLBACK_MARKER)
    hybrid_fts_excluded = text.count(_HYBRID_FTS_EXCLUDED_MARKER)

    return {
        "tool_calls_total": tool_calls_total,
        "initial_search_calls": initial_search_calls,
        "steps_used": steps_used,
        "forced_synthesis": forced_synthesis,
        "rerank_fallbacks": rerank_fallbacks,
        "rerank_head_fallbacks": rerank_head_fallbacks,
        "embedding_errors": embedding_errors,
        "oneshot_chunks": oneshot_chunks,
        "oneshot_context_chars": oneshot_context_chars,
        "oneshot_prompt_chars": oneshot_prompt_chars,
        "vector_leg_empty": vector_leg_empty,
        "sources_missing": sources_missing,
        "ctx_shrink_retries": ctx_shrink_retries,
        "route_orders": route_orders,
        "oneshot_llm_fallbacks": oneshot_llm_fallbacks,
        "hybrid_fts_excluded": hybrid_fts_excluded,
    }


def run_investigate_arm(entry: dict, arm_env: dict[str, str], *, agent_timeout: float,
                         extra_args=()) -> dict:
    """1 エントリ × 1 アームを subprocess 実行する。

    pm_argus_agent.py --timeout に agent_timeout を明示的に渡し（両アーム同一予算）、
    subprocess の kill タイムアウトは agent_timeout + _KILL_TIMEOUT_MARGIN 秒に自動設定する。
    arm_env は展開済み（${VAR} 参照解決後）の env dict を想定する。

    subprocess env は os.environ から _ARM_CONTROLLED_ENV_KEYS を除去したものに
    arm_env を重ねて作る（親シェルの env 汚染を打ち消す。二重防御として
    cmd_report 側にもアーム名と metrics の矛盾チェックがある）。

    戻り値: {"answer": str|None, "latency_s": float, "error": str, "budget_truncated": bool,
             "metrics": dict, "has_sources_section": bool, "suspect_short_answer": bool}。
    returncode 非0・タイムアウト・その他例外は answer=None、error にメッセージを入れる
    （この場合 budget_truncated / has_sources_section / suspect_short_answer は False）。
    suspect_short_answer は mode=="search" かつ回答が _SUSPECT_SHORT_ANSWER_MIN_CHARS
    文字未満の場合に True になる（error 扱いにはしない。judge は走らせる）。
    """
    cmd = build_investigate_cmd(entry, agent_timeout, extra_args)
    kill_timeout = agent_timeout + _KILL_TIMEOUT_MARGIN
    # 親シェルからの env 汚染を打ち消してからアーム env を重ねる（モジュール定数
    # _ARM_CONTROLLED_ENV_KEYS の説明を参照）。
    env = {k: v for k, v in os.environ.items() if k not in _ARM_CONTROLLED_ENV_KEYS}
    env.update(arm_env)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=kill_timeout,
        )
        latency_s = time.time() - started
        metrics = _extract_run_metrics(_stderr_text(proc.stderr))
        if proc.returncode != 0:
            return {
                "answer": None, "latency_s": latency_s, "budget_truncated": False,
                "error": f"returncode={proc.returncode}: {_stderr_tail(proc.stderr)}",
                "metrics": metrics, "has_sources_section": False, "suspect_short_answer": False,
            }
        answer = proc.stdout.strip()
        suspect_short_answer = (
            entry.get("mode", "search") == "search" and len(answer) < _SUSPECT_SHORT_ANSWER_MIN_CHARS
        )
        if suspect_short_answer:
            print(f"[WARN] suspiciously short answer ({len(answer)} chars)", file=sys.stderr)
        return {
            "answer": answer, "latency_s": latency_s, "error": "",
            "budget_truncated": _BUDGET_MARKER in answer,
            "metrics": metrics, "has_sources_section": "## 出典" in answer,
            "suspect_short_answer": suspect_short_answer,
        }
    except subprocess.TimeoutExpired as exc:
        latency_s = time.time() - started
        err_tail = _stderr_tail(exc.stderr)
        metrics = _extract_run_metrics(_stderr_text(exc.stderr))
        return {
            "answer": None, "latency_s": latency_s, "budget_truncated": False,
            "error": f"TimeoutExpired: {kill_timeout}s" + (f" stderr_tail={err_tail}" if err_tail else ""),
            "metrics": metrics, "has_sources_section": False, "suspect_short_answer": False,
        }
    except Exception as exc:
        return {
            "answer": None, "latency_s": time.time() - started, "budget_truncated": False,
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": {}, "has_sources_section": False, "suspect_short_answer": False,
        }


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    if not rivault_configured():
        print("ERROR: RIVAULT_URL / RIVAULT_TOKEN が未設定です。"
              "source ~/.secrets/rivault_tokens.sh してください。", file=sys.stderr)
        return 2

    arm_a_name = getattr(args, "arm_a", "baseline")
    arm_b_name = getattr(args, "arm_b", "expanded")
    try:
        arm_a_env = _expand_env_refs(ARM_PRESETS[arm_a_name].get("env", {}))
        arm_b_env = _expand_env_refs(ARM_PRESETS[arm_b_name].get("env", {}))
    except ValueError as exc:
        print(f"ERROR: アーム env の展開に失敗しました: {exc}", file=sys.stderr)
        return 2
    arm_a_extra = ARM_PRESETS[arm_a_name].get("extra_args", [])
    arm_b_extra = ARM_PRESETS[arm_b_name].get("extra_args", [])

    gold_path = Path(args.gold)
    entries = load_gold(gold_path)
    if args.entry:
        entries = [e for e in entries if e["id"] == args.entry]
        if not entries:
            print(f"ERROR: entry id '{args.entry}' が gold に見つかりません: {gold_path}",
                  file=sys.stderr)
            return 2

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    answers_dir = Path(args.answers_dir)
    if args.save_answers:
        answers_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    with open(jsonl_path, "a", encoding="utf-8") as out_f:
        for i, entry in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] id={entry['id']} (mode={entry.get('mode', 'search')}) ...",
                  file=sys.stderr, flush=True)

            results: dict[str, dict] = {}
            answer_paths: dict[str, str | None] = {}
            arm_specs = (
                ("a", arm_a_name, arm_a_env, arm_a_extra),
                ("b", arm_b_name, arm_b_env, arm_b_extra),
            )
            for label, arm_name, arm_env, arm_extra in arm_specs:
                print(f"  arm={arm_name} 実行中...", file=sys.stderr, flush=True)
                result = run_investigate_arm(
                    entry, arm_env, agent_timeout=args.agent_timeout, extra_args=arm_extra,
                )
                results[label] = result
                if args.save_answers:
                    answer_path = answers_dir / f"{run_ts}_{entry['id']}_{arm_name}.txt"
                    answer_path.write_text(result["answer"] or "", encoding="utf-8")
                    answer_paths[label] = str(answer_path)
                else:
                    answer_paths[label] = None
                status = "OK" if result["answer"] is not None else f"ERROR ({result['error']})"
                print(f"    -> {status} ({result['latency_s']:.1f}s)", file=sys.stderr)

            record = {
                "id": entry["id"],
                "mode": entry.get("mode", "search"),
                "compare": f"{arm_a_name}_vs_{arm_b_name}",
                "arm_a": arm_a_name,
                "arm_b": arm_b_name,
                "model_a": arm_a_env.get("LOCAL_LLM_MODEL", "(inherited)"),
                "model_b": arm_b_env.get("LOCAL_LLM_MODEL", "(inherited)"),
                "arm_config_a": {k: arm_a_env[k] for k in _ARM_CONFIG_WHITELIST if k in arm_a_env},
                "arm_config_b": {k: arm_b_env[k] for k in _ARM_CONFIG_WHITELIST if k in arm_b_env},
                "sampling": "gold",
                "latency_a_s": results["a"]["latency_s"],
                "latency_b_s": results["b"]["latency_s"],
                "chars_a": len(results["a"]["answer"] or ""),
                "chars_b": len(results["b"]["answer"] or ""),
                "budget_truncated_a": results["a"]["budget_truncated"],
                "budget_truncated_b": results["b"]["budget_truncated"],
                "answer_path_a": answer_paths["a"],
                "answer_path_b": answer_paths["b"],
                "metrics_a": results["a"].get("metrics", {}),
                "metrics_b": results["b"].get("metrics", {}),
                "has_sources_section_a": results["a"].get("has_sources_section", False),
                "has_sources_section_b": results["b"].get("has_sources_section", False),
                "suspect_short_answer_a": results["a"].get("suspect_short_answer", False),
                "suspect_short_answer_b": results["b"].get("suspect_short_answer", False),
                "swap": None,
                "prefer_arm": None,
                "rationale": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

            if results["a"]["answer"] is None or results["b"]["answer"] is None:
                record["prefer_arm"] = "error"
                error_labels = {"a": arm_a_name, "b": arm_b_name}
                record["rationale"] = "; ".join(
                    f"{error_labels[label]}: {r['error']}"
                    for label, r in results.items() if r["answer"] is None
                )
            else:
                swap = rng.randint(0, 1)
                record["swap"] = bool(swap)
                arms_order = [(arm_a_name, results["a"]["answer"]), (arm_b_name, results["b"]["answer"])]
                if swap:
                    arms_order = arms_order[::-1]
                (name_a, ans_a), (name_b, ans_b) = arms_order
                prompt = (
                    f"# 質問\n{entry['question']}\n\n"
                    f"# 参照事実\n{entry['reference']}\n\n"
                    f"# 回答 A\n{ans_a[:12000]}\n\n"
                    f"# 回答 B\n{ans_b[:12000]}\n\n"
                    "候補 A と B のどちらが調査回答としてより優れているか判定し、JSON で答えてください。"
                )
                raw, _latency_ms, err = argus_ab_judge.call_judge(
                    args.judge_model, JUDGE_SYSTEM, prompt,
                    max_tokens=args.judge_max_tokens, timeout=args.judge_timeout,
                )
                parsed = argus_ab_judge.parse_judge_output(raw) if raw else None
                if not parsed:
                    record["prefer_arm"] = "parse_failed"
                    record["rationale"] = err or "parse_failed"
                else:
                    prefer = parsed.get("prefer", "tie")
                    if prefer == "A":
                        record["prefer_arm"] = name_a
                    elif prefer == "B":
                        record["prefer_arm"] = name_b
                    else:
                        record["prefer_arm"] = "tie"
                    record["rationale"] = parsed.get("rationale", "")

            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_f.flush()
            print(f"    -> prefer_arm={record['prefer_arm']}", file=sys.stderr)

    print(f"完了: {jsonl_path} に追記", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

# 旧 JSONL レコード（arm_a/arm_b 導入前、baseline/expanded 固定）のキー写像
_LEGACY_KEY_MAP = {
    "latency_baseline_s": "latency_a_s",
    "latency_expanded_s": "latency_b_s",
    "chars_baseline": "chars_a",
    "chars_expanded": "chars_b",
    "budget_truncated_baseline": "budget_truncated_a",
    "budget_truncated_expanded": "budget_truncated_b",
    "answer_path_baseline": "answer_path_a",
    "answer_path_expanded": "answer_path_b",
}


def _normalize_record(rec: dict) -> dict:
    """旧形式（arm_a/arm_b 導入前）の JSONL レコードを新キー体系へ写像する。

    新形式のレコード（既に arm_a/arm_b を持つ）はそのまま返す（冪等）。
    """
    normalized = dict(rec)
    normalized.setdefault("arm_a", "baseline")
    normalized.setdefault("arm_b", "expanded")
    for old_key, new_key in _LEGACY_KEY_MAP.items():
        if old_key in normalized and new_key not in normalized:
            normalized[new_key] = normalized[old_key]
    return normalized


def _check_oneshot_metrics_consistency(records: list[dict]) -> list[str]:
    """アーム名（命名規約: "oneshot" を含むか）と metrics.oneshot_chunks の有無の
    矛盾を検出する（二重防御）。

    run_investigate_arm 側の env 汚染除去（_ARM_CONTROLLED_ENV_KEYS）が漏れて
    ループ系アームが静かに one-shot 化した場合、またはその逆の場合に、
    report 実行時点で気付けるようにするための警告のみ（自動修正はしない）。
    metrics_a/metrics_b が無い（旧形式・実行エラー）レコードはスキップする。
    """
    warnings: list[str] = []
    for rec in records:
        for label, arm_key, metrics_key in (("a", "arm_a", "metrics_a"), ("b", "arm_b", "metrics_b")):
            arm_name = rec.get(arm_key)
            metrics = rec.get(metrics_key)
            if not arm_name or not isinstance(metrics, dict) or "oneshot_chunks" not in metrics:
                continue
            is_oneshot_name = "oneshot" in arm_name
            has_oneshot_metrics = metrics.get("oneshot_chunks") is not None
            if is_oneshot_name != has_oneshot_metrics:
                warnings.append(
                    f"id={rec.get('id', '?')} arm_{label}={arm_name}: "
                    f"oneshot_chunks={metrics.get('oneshot_chunks')!r} は"
                    f"{'one-shot' if is_oneshot_name else 'ループ'}系アーム名と矛盾します"
                    "（env 汚染の疑い）"
                )
    return warnings


def aggregate_report(records: list[dict]) -> dict[tuple[str, str], dict]:
    """(compare, mode) 別に prefer_arm を集計し、勝率・合否・平均レイテンシを算出する。

    records は _normalize_record 済み（新キー体系）を前提とする。
    error/parse_failed は win_tie_rate の分母（valid）から除外する。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        arm_a = rec.get("arm_a", "baseline")
        arm_b = rec.get("arm_b", "expanded")
        compare = rec.get("compare") or f"{arm_a}_vs_{arm_b}"
        mode = rec.get("mode", "search")
        groups.setdefault((compare, mode), []).append(rec)

    report: dict[tuple[str, str], dict] = {}
    for (compare, mode), recs in groups.items():
        arm_a = recs[0].get("arm_a", "baseline")
        arm_b = recs[0].get("arm_b", "expanded")
        counts: dict[str, int] = {}
        lat_a: list[float] = []
        lat_b: list[float] = []
        for r in recs:
            prefer_arm = r.get("prefer_arm") or "parse_failed"
            counts[prefer_arm] = counts.get(prefer_arm, 0) + 1
            if r.get("latency_a_s") is not None:
                lat_a.append(r["latency_a_s"])
            if r.get("latency_b_s") is not None:
                lat_b.append(r["latency_b_s"])

        b_wins = counts.get(arm_b, 0)
        a_wins = counts.get(arm_a, 0)
        ties = counts.get("tie", 0)
        valid = b_wins + a_wins + ties
        win_tie_rate = (b_wins + ties) / valid if valid else None

        report[(compare, mode)] = {
            "arm_a": arm_a,
            "arm_b": arm_b,
            "counts": counts,
            "total": len(recs),
            "valid": valid,
            "win_tie_rate": win_tie_rate,
            "passed": bool(win_tie_rate is not None and win_tie_rate >= _WIN_TIE_THRESHOLD),
            "avg_latency_a_s": (sum(lat_a) / len(lat_a)) if lat_a else None,
            "avg_latency_b_s": (sum(lat_b) / len(lat_b)) if lat_b else None,
        }
    return report


def cmd_report(args: argparse.Namespace) -> int:
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL が見つかりません: {jsonl_path}", file=sys.stderr)
        return 2

    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    if not records:
        print("レコードなし", file=sys.stderr)
        return 1

    normalized = [_normalize_record(r) for r in records]
    for warning in _check_oneshot_metrics_consistency(normalized):
        print(f"[WARN] {warning}", file=sys.stderr)

    report = aggregate_report(normalized)
    print("# Investigate A/B Report\n")
    any_valid = False
    for (compare, mode), stats in report.items():
        arm_a = stats["arm_a"]
        arm_b = stats["arm_b"]
        print(f"## compare={compare} mode={mode} (n={stats['total']})\n")
        for name, n in sorted(stats["counts"].items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {n}")

        if stats["valid"] == 0:
            print("  有効サンプルなし（judge解析失敗/実行エラーのみ）。判定不能\n")
            continue

        any_valid = True
        wins_ties = stats["counts"].get(arm_b, 0) + stats["counts"].get("tie", 0)
        print(f"\n  {arm_b} 勝ち+引き分け率: {stats['win_tie_rate']:.1%} ({wins_ties}/{stats['valid']})")
        if stats["passed"]:
            print(f"  合否判定: 合格（≥{_WIN_TIE_THRESHOLD:.0%}）→ {arm_b} の既定採用可\n")
        else:
            print(f"  合否判定: 未達（<{_WIN_TIE_THRESHOLD:.0%}）→ {arm_a} のまま据え置き\n")

        avg_a = stats["avg_latency_a_s"]
        avg_b = stats["avg_latency_b_s"]
        if avg_a is not None or avg_b is not None:
            a_label = f"{avg_a:.1f}s" if avg_a is not None else "N/A"
            b_label = f"{avg_b:.1f}s" if avg_b is not None else "N/A"
            print(f"  平均レイテンシ: {arm_a}={a_label} {arm_b}={b_label}\n")

    return 0 if any_valid else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="investigate 経路/モデルの A/B 評価（ARM_PRESETS の任意 2 アームを比較。"
                     "既定は baseline=現行既定値 / expanded=検索パラメータ拡大値）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="ゴールド質問セット実行→judge→JSONL追記")
    r.add_argument("--gold", default=str(DEFAULT_GOLD), metavar="PATH")
    r.add_argument("--entry", default=None, help="指定した id のエントリのみ実行")
    r.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    r.add_argument("--answers-dir", default=str(DEFAULT_ANSWERS_DIR), metavar="PATH")
    r.add_argument("--arm-a", default="baseline", choices=list(ARM_PRESETS),
                   help="比較アーム A（既定: baseline）")
    r.add_argument("--arm-b", default="expanded", choices=list(ARM_PRESETS),
                   help="比較アーム B（既定: expanded）")
    r.add_argument("--agent-timeout", type=float, default=_DEFAULT_AGENT_TIMEOUT,
                   help="pm_argus_agent.py --timeout に渡す調査予算（秒）。両アーム共通"
                        f"（既定{_DEFAULT_AGENT_TIMEOUT}秒）。subprocess の kill タイムアウトは"
                        f"この値+{_KILL_TIMEOUT_MARGIN}秒に自動設定される")
    r.add_argument("--save-answers", action="store_true",
                   help="回答本文をファイル保存する（既定では保存しない）。"
                        "保存した平文回答は確認後に削除する運用とする")
    r.add_argument("--judge-model", default="Kimi-K2-Thinking")
    # Kimi-K2-Thinking は thinking 無効化不可で think に 2-3k トークンを使うため
    # 4096 未満だと本文が出る前に切れて parse_failed になる
    r.add_argument("--judge-max-tokens", type=int, default=4096)
    r.add_argument("--judge-timeout", type=int, default=600)
    r.add_argument("--seed", type=int, default=7)
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="JSONL集計・合否判定（≥60%%）")
    rp.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
