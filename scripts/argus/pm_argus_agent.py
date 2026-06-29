#!/usr/bin/env python3
"""
pm_argus_agent.py — Argus Investigation Agent

LLM が自律的にツールを選択・呼び出して段階的にプロジェクトデータを分析する
マルチステップ Agent。/argus-investigate Slack コマンドおよび CLI から利用する。

Usage:
    # CLI モード（標準出力のみ）
    python3 scripts/pm_argus_agent.py --investigate "M3の遅延原因を調査" --dry-run
    python3 scripts/pm_argus_agent.py --investigate "先週の決定事項の実行状況" --max-steps 5

環境変数:
    LOCAL_LLM_URL / RIVAULT_URL — LLM バックエンド（pm_argus.py と同じ）
    SLACK_BOT_TOKEN — Slack 返信用（Slack コマンド時のみ）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

import yaml
from cli_utils import call_argus_llm, load_codesign_context
from db_utils import open_pm_db

logger = logging.getLogger("pm_argus_agent")

_DATA_DIR = _REPO_ROOT / "data"
_MINUTES_DIR = _DATA_DIR / "minutes"
_PM_DB = _DATA_DIR / "pm.db"
_ARGUS_CONFIG = _DATA_DIR / "argus_config.yaml"
_QA_CONFIG_LEGACY = _DATA_DIR / "qa_config.yaml"
_DEFAULT_SINCE_DAYS = 30
_DEFAULT_MAX_STEPS = 20
_DEFAULT_TIMEOUT = 480.0
_CONTEXT_CHAR_LIMIT = 100_000


# =========================================================================== #
#  argus_config.yaml からインデックスDB・チャンネルリスト解決
# =========================================================================== #

def _resolve_index_and_channels(
    channel_id: str | None = None,
) -> tuple[Path, list[str], list[Path], str]:
    """argus_config.yaml を読み、channel_id に対応する index_db (統合)・channels・pm_db_paths・index_name を返す。

    全インデックスは統合 data/qa_index.db を共有し、検索時に index_name でフィルタする。
    """
    qa_index = _DATA_DIR / "qa_index.db"
    config_path = _ARGUS_CONFIG if _ARGUS_CONFIG.exists() else _QA_CONFIG_LEGACY
    if not config_path.exists():
        return qa_index, [], [_PM_DB], "pm"

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    indices = cfg.get("indices") or {}
    channel_map = cfg.get("channel_map") or {}
    default_index = cfg.get("default_index", "pm")

    index_name = channel_map.get(channel_id, default_index) if channel_id else default_index
    index_cfg = indices.get(index_name, {})
    channels = index_cfg.get("channels", [])
    pm_db_list = index_cfg.get("pm_db", ["data/pm.db"])
    pm_db_paths = [_REPO_ROOT / p for p in pm_db_list]
    return qa_index, channels, pm_db_paths, index_name



from argus.agent_tools import (  # noqa: F401 — 後方互換のため再 export
    _TOOL_MAP,
    TOOLS,
    AgentContext,
    ToolDef,
    _build_tool_descriptions,
    # 従来の _tool_* 関数は agent_tools.py 内で mcp_tools 委譲となったため
    # 個別 export は不要（必要なのは AgentContext / TOOLS / _TOOL_MAP のみ）
)

# =========================================================================== #
#  Protocol Parser
# =========================================================================== #

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FINAL_ANSWER_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)
# Kimi-K2-Thinking が稀に <answer>...</answer> タグでラップして返すケースの救済
# 中身が JSON なら tool_call として扱い、それ以外は final_answer として扱う
_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.DOTALL)
_JSON_TOOL_CALL_RE = re.compile(r"\{[^{}]*?\"name\"\s*:\s*\"[^\"]+\"[^{}]*?\"args\"\s*:\s*\{[^{}]*\}[^{}]*\}", re.DOTALL)


def parse_tool_calls(response: str) -> list[dict]:
    results = []
    for m in _TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            results.append({"error": f"JSONパースエラー: {m.group(1)[:100]}"})
    if results:
        return results
    # フォールバック: <answer>{json}</answer> 形式や、生 JSON の混入を検出
    for m in _ANSWER_TAG_RE.finditer(response):
        body = m.group(1).strip()
        for jm in _JSON_TOOL_CALL_RE.finditer(body):
            try:
                obj = json.loads(jm.group(0))
                name = obj.get("name", "")
                args = obj.get("args", {})
                if isinstance(args, dict) and isinstance(name, str) and name:
                    results.append({"name": name, "args": args})
            except json.JSONDecodeError:
                pass
    if results:
        return results
    # <answer> タグなしで生 JSON だけ返ってくるケース
    for jm in _JSON_TOOL_CALL_RE.finditer(response):
        try:
            obj = json.loads(jm.group(0))
            name = obj.get("name", "")
            args = obj.get("args", {})
            if isinstance(args, dict) and isinstance(name, str) and name:
                results.append({"name": name, "args": args})
        except json.JSONDecodeError:
            pass
    return results


def parse_final_answer(response: str) -> str | None:
    m = _FINAL_ANSWER_RE.search(response)
    if m:
        return m.group(1).strip()
    # フォールバック: <answer> タグの中身が JSON でなければ最終回答とみなす
    a = _ANSWER_TAG_RE.search(response)
    if a:
        body = a.group(1).strip()
        # JSON tool_call っぽくない（"name":"..." を含まない）なら最終回答
        if "\"name\"" not in body or "\"args\"" not in body:
            return body
    return None


# =========================================================================== #
#  System Prompt
# =========================================================================== #

_AGENT_SYSTEM_PROMPT = """\
あなたは富岳NEXTプロジェクトのAIインテリジェンスシステム「Argus」です。
ユーザーの質問に対し、利用可能なツールを駆使して徹底的に調査し、
根拠に基づく詳細な回答を生成してください。

## プロジェクト文脈（最重要 — 全回答の判断軸）

富岳NEXTは富岳の後継となる次世代スーパーコンピュータの研究開発プロジェクトであり、
**理研・富士通・NVIDIA の三者によるコデザイン（co-design）** で進行している。
本プロジェクトの最終目標は **次世代スパコンのシステム仕様の決定** であり、
以下のコデザイン項目（ハードウェア設計選択肢）を絞り込むための材料収集と評価が行われている。

### コデザイン項目（システム仕様選択肢）

{codesign_items}

**アプリケーション評価の位置づけ**: EEA (Early Evaluation Application) と
ベンチマーク WG で対象となる代表アプリの GPU 化と性能評価は、
**それ自体が目的ではなく、上記コデザイン項目を決定するための定量的根拠** を得る活動。
従って各アプリの調査では、単なる進捗報告に留まらず以下を必ず引き出すこと:

1. **コデザインへの含意** — そのアプリの結果が上記コデザイン項目の
   どの選択肢に影響するか
2. **ボトルネック特性** — 何が律速か（演算・帯域・レイテンシ・容量・同期 等）
3. **評価フェーズ** — Benchkit 対応状況、ベンチマーク再現性、評価成熟度
4. **ベンダー協業状況** — NVIDIA / 富士通 との議論・合意・未決事項
5. **仕様決定に向けて不足している情報** — 追加で必要な測定・議論・データ

**注意**: `pm-multi-agent` および `Argus` はあなた自身（プロジェクト管理 AI ツール）
の名称です。富岳NEXTのアプリケーションやコデザインの対象ではないため、
回答セクションの論点として扱ったり「pm-multi-agent との連携」のような節を作ったり
してはいけません。

## ツール呼び出し形式

<tool_call>
{{"name": "ツール名", "args": {{"引数名": "値"}}}}
</tool_call>

1ステップで複数の <tool_call> を並列に並べてよい。タグ名は厳密に:
ツール呼び出しは `<tool_call>`、最終回答は `<final_answer>`。

{tool_descriptions}

## 進め方

- 質問の内容に応じて適切なツールを選択すること
- `search_text` で全文検索（決定事項・議事録・Slack・BOX文書・外部Web記事を含む）。
  特に外部技術記事（AI/LLM/HPCの最新動向）を調べる際は積極的に使うこと。
  `search_decisions` で決定事項検索、
  `search_entity` で異なる視点（conservative/aggressive/objective/future_oriented）
  とデータ種別（pm_data/minutes/slack/box_docs）の組み合わせで多角的に分析すること
- **アプリ評価系の質問では search_entity を最低3組合せ並列実行すべき**:
  例: (objective × pm_data) + (objective × minutes) + (conservative × slack)
- **コデザイン論点を意識した検索クエリを組み立てる**: 単に「アプリ名 進捗」ではなく、
  「アプリ名 メモリ ボトルネック」「アプリ名 NVL4 NVL72」「アプリ名 アトミック L2」
  「アプリ名 EEA Level」「アプリ名 富士通 NVIDIA 議論」等で複数回検索する
- 複数の視点とデータ種別を掛け合わせて検索すると、より深い洞察が得られる
- 同じツールを異なる引数で呼ぶことは有用。遠慮せずに必要なだけツールを使うこと
- 得られた結果に対して `synthesize_answers` を使って複数 Explorer の分析を統合することも検討する
- 質問が意思決定や制約に関する場合は `search_decisions` と `search_text` を併用する
- 特定ユーザーのメンション状況は `search_mentions` を使う
- 調査結果を Box/Slack/Canvas に出力する必要がある場合は出力ツールを使うこと
  （出力前にユーザーに確認を取ること）

## 最終回答（深掘り必須）

回答は単なる事実の羅列ではなく、**プロジェクト文脈** で示した5つの観点を
可能な限り含む構造化された分析にすること。具体的には:

1. **結論サマリ** — 1〜2 文で現状の本質的な評価（順調/遅延/障害/重要転換点 等）
2. **詳細状況** — 表形式で「項目 / 状況 / 根拠（出典付き）」を提示
3. **コデザインへの含意** — このアプリ/論点がシステム仕様（ノード/メモリ/NW/GPU 世代）の
   どの選択肢にどう影響するかを必ず一段落で論じる
4. **ボトルネック/リスク** — 技術的・組織的・スケジュール的な阻害要因を箇条書き
5. **仕様決定に向けて不足している情報** — 追加で必要な測定・議論・データ、
   ステークホルダー（理研/富士通/NVIDIA）に確認すべき事項

その他の必須事項:

- 数値・日付・人名・会議名・決定事項IDなど具体的根拠を引用すること
- 推測ではなくツール結果に基づいて答えること。推測する場合は「（推測）」と明記
- 回答の長さに制限はない。必要なだけ詳しく説明すること
- 必ず `<final_answer>` タグで終わること

## 調査期間

- 本日: {today}
- 対象期間: {since} 〜 {today}
- この期間外のデータはツール結果に含まれません。期間を限定したい場合は適切なキーワードで検索してください。
"""


_FORCED_SYNTHESIS_PROMPT = """\
あなたは富岳NEXTプロジェクトのAI「Argus」です。
富岳NEXTは理研・富士通・NVIDIA のコデザインで進む次世代スーパーコンピュータ研究開発で、
アプリケーション評価は **以下のコデザイン項目を決定するための材料収集** が目的。

### コデザイン項目（システム仕様選択肢）

{codesign_items}

以下のツール実行結果のみを根拠として、ユーザー質問への最終回答を生成してください。

## 回答構成（深掘り必須）
アプリ/論点に関する質問の場合は以下を含む構造化された分析にすること:
1. **結論サマリ** — 1〜2 文で本質評価
2. **詳細状況** — 表形式「項目 / 状況 / 根拠（出典付き）」
3. **コデザインへの含意** — 上記コデザイン項目のどの選択肢にどう影響するかを一段落
4. **ボトルネック/リスク** — 律速要因（演算/帯域/レイテンシ/容量/同期 等）
5. **仕様決定に向けて不足している情報** — 追加測定・議論・確認事項

## 厳守事項
- 新しいツール呼び出し (`<tool_call>`) は禁止。出力に含めてはならない。
- 必ず `<final_answer>...</final_answer>` タグで回答を囲む。
- ツール結果に含まれない情報は推測しない。「情報なし」と明記してよい。
- 数値・日付・人名・ID 等の根拠を明示する。

本日: {today} / 対象期間: {since} 〜 {today}
"""


# =========================================================================== #
#  Seed Data (Lean Start)
# =========================================================================== #

def build_seed_data(ctx: AgentContext) -> str:
    """シードデータを生成する。

    マイルストーン・アクションアイテム関連（期限超過件数・未確認決定事項件数・
    担当者別負荷）はメンテ状況が不安定なため LLM への参考材料から除外する。
    必要な場合は search_action_items / get_milestone_progress 等のツールで
    LLM が明示的に取得する。
    """
    parts = [
        "## プロジェクト概況\n",
        f"- 本日: {ctx.today}",
        f"- 調査対象期間: {ctx.since} 〜 {ctx.today}",
        "",
    ]
    return "\n".join(parts)


# =========================================================================== #
#  Progress Updater
# =========================================================================== #

def _make_progress_updater(respond: Callable | None, max_respond_calls: int = 2) -> Callable[[str], None]:
    steps: list[str] = []
    respond_count = 0

    def update(msg: str) -> None:
        nonlocal respond_count
        steps.append(msg)
        if respond is not None and respond_count < max_respond_calls:
            try:
                respond(
                    text=":mag: Argus 調査中...\n" + "\n".join(steps),
                    response_type="ephemeral",
                    replace_original=True,
                )
                respond_count += 1
            except Exception as e:
                logger.warning(f"進捗通知エラー: {e}")
        else:
            logger.info(f"[STEP] {msg}")

    return update


# =========================================================================== #
#  Agent Loop
# =========================================================================== #

def execute_tool(name: str, args: dict, ctx: AgentContext) -> str:
    tool = _TOOL_MAP.get(name)
    if tool is None:
        available = ", ".join(_TOOL_MAP.keys())
        return f"エラー: ツール「{name}」は存在しません。利用可能なツール: {available}"
    try:
        return tool.fn(args, ctx)
    except Exception as e:
        return f"エラー: {name} の実行に失敗しました — {e}"


_QUERY_REWRITE_PROMPT = """\
あなたは富岳NEXTプロジェクト（理研×富士通×NVIDIA、次世代スーパーコンピュータ開発）の AI アシスタントです。
ユーザーが Slack の `/argus-investigate` に投げた **短く曖昧な質問** を、社内ナレッジ検索エージェント向けに展開します。

質問:
{question}

このプロジェクトに登場する固有名詞の例（参考、すべて社内用語）:
- EEA (Early Evaluation Application): 評価対象アプリ群、EEA-1 / EEA-2 などのフェーズあり
- コデザイン: 富岳NEXT のハード・ソフト協調設計活動
- スケールアウトネットワーク: ノード間相互接続
- HBM, ノード構成, ベンチマーク, 性能予測, LQCD, GENESIS, NICAM, Petsy, PyTorch 等

以下を JSON で出力してください（コードブロック禁止、JSON のみ）:

{{
  "intent": "ユーザーが本当に知りたいこと（1〜2文、推測でよい）",
  "entities": ["質問に含まれる/想起される固有名詞や略語の正規形（最大6個）"],
  "search_queries": ["検索エンジンに投げる具体クエリ（2〜4個、日本語/英語混在可、固有名詞優先）"]
}}

注意:
- ユーザー語彙を一般用語に置き換えない（例: 「EEA」を「欧州経済領域」と展開してはいけない。これは社内略語）。
- タイプミス・省略形は元のまま entities に残し、正規形を**併記**する。
- 推測が不確実なら intent に「（推測）」を付ける。
"""


def _rewrite_query(question: str) -> dict | None:
    """ユーザー質問を意図 / 固有名詞 / 検索クエリに展開する。失敗時は None。"""
    prompt = _QUERY_REWRITE_PROMPT.format(question=question.strip())
    try:
        t0 = time.time()
        response = call_argus_llm(prompt, max_tokens=512, timeout=30, think=False)
        elapsed = time.time() - t0
        logger.info(f"[rewrite] LLM応答 {len(response)} chars, {elapsed:.1f}s")
        # JSON 抽出（前後の余計な文字を許容）
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if not m:
            logger.warning(f"[rewrite] JSON 抽出失敗: {response[:200]}")
            return None
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return None
        intent = str(data.get("intent", "")).strip()
        entities = [str(x) for x in data.get("entities", []) if x][:6]
        queries = [str(x) for x in data.get("search_queries", []) if x][:4]
        if not intent and not entities and not queries:
            return None
        return {"intent": intent, "entities": entities, "search_queries": queries}
    except Exception as e:
        logger.warning(f"[rewrite] 失敗: {e}")
        return None


def _format_rewrite_for_seed(rewrite: dict) -> str:
    """seed_data 冒頭に注入する形式に整形。"""
    parts = ["## 質問の解釈（自動展開）\n"]
    if rewrite.get("intent"):
        parts.append(f"- **意図**: {rewrite['intent']}")
    if rewrite.get("entities"):
        parts.append(f"- **関連語**: {', '.join(rewrite['entities'])}")
    if rewrite.get("search_queries"):
        parts.append(f"- **推奨検索クエリ**: {', '.join(rewrite['search_queries'])}")
    parts.append("")
    parts.append("（上記は LLM による自動展開。原質問の語を優先しつつ、固有名詞や言い換えで補完して検索すること）")
    parts.append("")
    return "\n".join(parts)


# =========================================================================== #
#  Agent Loop Helpers
# =========================================================================== #

def _summarize_args(args: dict) -> str:
    """Tool args を 1 行 ≤80 文字に圧縮（dedupe key / 履歴表示用）"""
    try:
        s = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:
        s = str(args)
    return s if len(s) <= 80 else s[:77] + "..."


def _dedupe_key(name: str, args: dict) -> str:
    try:
        return f"{name}::{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
    except Exception:
        return f"{name}::{args}"


def _format_tool_history(history: list[dict], keep_recent: int = 8) -> str:
    """履歴を <tool_result> ブロック列に整形。古いものは 1 行に圧縮。"""
    if not history:
        return ""
    parts: list[str] = []
    cutoff = max(0, len(history) - keep_recent)
    for i, h in enumerate(history):
        name = h["name"]
        step = h["step"]
        args_sum = _summarize_args(h.get("args", {}))
        if i < cutoff:
            body = h.get("result") or h.get("error") or ""
            parts.append(
                f"<tool_result name=\"{name}\" step=\"{step}\">\n"
                f"[Tool Result: {name}({args_sum})] （{len(body)}文字、圧縮済み）\n"
                f"</tool_result>"
            )
        elif "error" in h:
            parts.append(
                f"<tool_result name=\"{name}\" step=\"{step}\" error=\"true\">\n"
                f"[Tool Error: {name}({args_sum})]\n{h['error']}\n"
                f"</tool_result>"
            )
        else:
            parts.append(
                f"<tool_result name=\"{name}\" step=\"{step}\">\n"
                f"[Tool Result: {name}({args_sum})]\n{h['result']}\n"
                f"</tool_result>"
            )
    return "\n\n".join(parts)


def _execute_calls_parallel(
    calls: list[dict], ctx: AgentContext, step: int, timeout_s: int = 120
) -> list[dict]:
    """ツール呼び出しを並列実行して history エントリのリストを返す。"""
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_map = {
            pool.submit(execute_tool, tc["name"], tc["args"], ctx): tc
            for tc in calls
        }
        try:
            for future in concurrent.futures.as_completed(fut_map, timeout=timeout_s):
                tc = fut_map[future]
                try:
                    result = future.result()
                    out.append({
                        "step": step, "name": tc["name"], "args": tc["args"],
                        "result": result,
                    })
                except Exception as e:
                    out.append({
                        "step": step, "name": tc["name"], "args": tc["args"],
                        "error": f"{type(e).__name__}: {e}",
                    })
        except concurrent.futures.TimeoutError:
            for fut, tc in fut_map.items():
                if not fut.done():
                    out.append({
                        "step": step, "name": tc["name"], "args": tc["args"],
                        "error": "tool execution timeout",
                    })
    return out


def run_agent(
    question: str,
    seed_data: str,
    respond: Callable | None,
    ctx: AgentContext,
    *,
    max_steps: int = _DEFAULT_MAX_STEPS,
    timeout: float = _DEFAULT_TIMEOUT,
    include_intent_header: bool = True,
    context: str = "",
) -> str:
    """質問を bounded multi-step agent loop で処理する。

    最大 max_steps 回まで LLM 呼び出し→ツール実行を繰り返し、
    `<final_answer>` が得られた時点で終了。重複ツール呼び出しは自動スキップ。
    ループ終了後も final_answer がない場合は強制 synthesis を 1 回追加実行する。

    max_steps=0 の場合はツールなしモードで seed_data のみから回答を生成する。
    timeout は LLM 呼び出しの総予算（wall-clock）として消費される。
    context が指定された場合、調査依頼の前に背景情報として注入する（Pass 1 → Pass 2 の引き継ぎ用）。
    """
    tool_desc = _build_tool_descriptions()
    codesign_items = load_codesign_context() or "(コデザイン項目情報なし)"
    # terminology 動的用語辞書を追記
    try:
        from utils.terminology import build_terminology_reference
        dyn_terms = build_terminology_reference()
        if dyn_terms:
            codesign_items = codesign_items + "\n" + dyn_terms
    except Exception:
        pass
    # glossary 構造化テキストを追記
    try:
        from utils.glossary import build_reference as build_glossary_ref
        glossary_ref = build_glossary_ref()
        if glossary_ref:
            codesign_items = codesign_items + "\n" + glossary_ref
    except Exception:
        pass
    system_prompt = _AGENT_SYSTEM_PROMPT.format(
        tool_descriptions=tool_desc,
        max_steps=max_steps,
        today=ctx.today,
        since=ctx.since,
        codesign_items=codesign_items,
    )
    forced_system = _FORCED_SYNTHESIS_PROMPT.format(
        today=ctx.today, since=ctx.since, codesign_items=codesign_items,
    )

    progress = _make_progress_updater(None)
    progress("質問の意図を解釈中...")

    rewrite = _rewrite_query(question)
    rewrite_block = ""
    if rewrite:
        rewrite_block = _format_rewrite_for_seed(rewrite)
        logger.info(
            f"[rewrite] intent={rewrite.get('intent', '')[:80]!r}"
            f" entities={rewrite.get('entities')}"
            f" queries={rewrite.get('search_queries')}"
        )

    intent_header = ""
    if include_intent_header and rewrite and rewrite.get("intent"):
        intent_header = f"> **ご質問の解釈**: {rewrite['intent']}\n\n"

    context_block = f"## アプリケーション背景情報（事前調査済み）\n\n{context}\n\n" if context else ""
    base_prompt = (
        f"{context_block}"
        f"## 調査依頼\n\n{question}\n\n"
        f"{rewrite_block}"
        f"{seed_data}\n\n"
    )

    deadline = time.monotonic() + timeout

    def remaining_s() -> int:
        return max(10, int(deadline - time.monotonic()))

    # --- max_steps == 0: ツールなしで seed_data のみから単発生成
    if max_steps <= 0:
        progress("max_steps=0: ツールなしで seed_data から直接回答生成")
        no_tools_prompt = (
            base_prompt
            + "上記の seed_data のみを根拠に `<final_answer>` で回答してください。"
            + "ツールは使用しないでください。"
        )
        try:
            resp = call_argus_llm(
                no_tools_prompt, system=forced_system,
                max_tokens=8192, timeout=remaining_s(), think=False,
            )
        except Exception as e:
            logger.exception(f"[investigate][step 0] LLM error: {e}")
            return intent_header + _append_sources_section(
                f"調査中にエラーが発生しました: {e}", ctx)
        final = parse_final_answer(resp)
        if final:
            return intent_header + _append_sources_section(final, ctx)
        clean = re.sub(r"<[^>]+>", "", resp).strip()
        return intent_header + _append_sources_section(clean or resp, ctx)

    # --- Multi-step loop
    history: list[dict] = []
    executed_keys: set[str] = set()
    last_response = ""

    for step in range(1, max_steps + 1):
        if time.monotonic() >= deadline:
            logger.warning(f"[STEP {step}/{max_steps}] タイムアウト到達、ループ中断")
            break

        prompt = base_prompt
        if history:
            prompt += "## これまでのツール実行結果\n\n" + _format_tool_history(history) + "\n\n"
        prompt += (
            f"残りステップ: {max_steps - step + 1}/{max_steps}。"
            f"追加調査が必要なら `<tool_call>`、回答可能なら `<final_answer>` で出力してください。"
        )

        progress(f"[STEP {step}/{max_steps}] LLM 呼び出し中...")
        t0 = time.monotonic()
        try:
            response = call_argus_llm(
                prompt, system=system_prompt,
                max_tokens=32768, timeout=remaining_s(), think=True,
            )
        except Exception as e:
            logger.exception(f"[STEP {step}/{max_steps}] LLM error: {e}")
            break
        last_response = response
        elapsed = time.monotonic() - t0

        final = parse_final_answer(response)
        tool_calls = [] if final else parse_tool_calls(response)
        valid_calls = [tc for tc in tool_calls if "name" in tc]
        logger.info(
            f"[STEP {step}/{max_steps}] LLM 応答 {len(response)} chars, "
            f"{len(valid_calls)}件のツール呼び出し ({elapsed:.1f}s)"
        )

        if final:
            return intent_header + _append_sources_section(final, ctx)

        if not valid_calls:
            logger.info(f"[STEP {step}/{max_steps}] tool_call も final_answer もなし — break")
            break

        # 重複検出
        fresh: list[dict] = []
        skipped = 0
        for tc in valid_calls:
            key = _dedupe_key(tc["name"], tc.get("args", {}))
            if key in executed_keys:
                skipped += 1
                continue
            executed_keys.add(key)
            fresh.append(tc)
        if skipped:
            logger.info(f"[STEP {step}/{max_steps}] 重複ツール {skipped}件をスキップ")

        if not fresh:
            logger.info(f"[STEP {step}/{max_steps}] 新規ツール0件 — 強制 synthesis へ")
            break

        progress(f"[STEP {step}/{max_steps}] ツール{len(fresh)}件実行中...")
        logger.info(f"[STEP {step}/{max_steps}] ツール{len(fresh)}件を実行中...")
        new_entries = _execute_calls_parallel(
            fresh, ctx, step=step, timeout_s=min(120, remaining_s())
        )
        history.extend(new_entries)

    # --- 強制 synthesis fallback
    if history:
        progress("最終回答を強制生成中...")
        logger.info(f"[forced-synthesis] {len(history)}件のツール結果から最終回答生成")
        synth_prompt = (
            base_prompt
            + "## ツール実行結果（全件）\n\n"
            + _format_tool_history(history, keep_recent=8)
            + "\n\n以下のツール結果のみを根拠に `<final_answer>` で回答してください。"
            + "追加ツール呼び出しは禁止です。"
        )
        try:
            resp = call_argus_llm(
                synth_prompt, system=forced_system,
                max_tokens=8192, timeout=remaining_s(), think=False,
            )
        except Exception as e:
            logger.exception(f"[forced-synthesis] LLM error: {e}")
            dump = _format_tool_history(history, keep_recent=999)
            return intent_header + _append_sources_section(
                f"回答生成に失敗しました ({e})。\n\n## 収集データ\n\n{dump}", ctx)

        final = parse_final_answer(resp)
        if final:
            return intent_header + _append_sources_section(final, ctx)
        clean = re.sub(r"<[^>]+>", "", resp).strip()
        # ツール呼び出し JSON が漏れている場合は捨てて収集データを返す
        if parse_tool_calls(resp):
            logger.warning("[forced-synthesis] final_answer なし・ツール呼び出しのみ返却 — 収集データにフォールバック")
            dump = _format_tool_history(history, keep_recent=999)
            return intent_header + _append_sources_section(
                f"最終回答を生成できませんでした（LLM がツール呼び出しを繰り返しました）。\n\n## 収集データ\n\n{dump}", ctx)
        logger.warning("[forced-synthesis] final_answer タグなし、生応答を返却")
        return intent_header + _append_sources_section(clean or resp, ctx)

    # --- ツール実行も synthesis も発動しなかった場合
    if last_response:
        clean = re.sub(r"<[^>]+>", "", last_response).strip()
        return intent_header + _append_sources_section(
            clean or "調査できませんでした（最終回答を得られず）", ctx)
    return intent_header + _append_sources_section(
        "調査できませんでした（LLM 呼び出しに失敗しました）", ctx)


_SLACK_REF_CACHE: dict[str, list[str]] = {}


def _fetch_slack_references_for_box(box_file_id: str, limit: int = 2) -> list[str]:
    """box_file_id に紐づく Slack 共有パーマリンクを最大 limit 件返す（新しい順）。"""
    if not box_file_id:
        return []
    if box_file_id in _SLACK_REF_CACHE:
        return _SLACK_REF_CACHE[box_file_id]
    try:
        from pathlib import Path

        from db_utils import open_db
        path = Path(__file__).resolve().parent.parent.parent / "data" / "box_docs.db"
        if not path.exists():
            _SLACK_REF_CACHE[box_file_id] = []
            return []
        conn = open_db(path, encrypt=True)
        rows = conn.execute(
            "SELECT slack_permalink, shared_at, shared_by FROM slack_references"
            " WHERE box_file_id=? AND slack_permalink IS NOT NULL"
            " ORDER BY shared_at DESC LIMIT ?",
            (box_file_id, limit),
        ).fetchall()
        conn.close()
    except Exception:
        _SLACK_REF_CACHE[box_file_id] = []
        return []
    out = []
    for r in rows:
        link = r["slack_permalink"]
        date = (r["shared_at"] or "")[:10]
        by = r["shared_by"] or ""
        label_bits = " ".join(b for b in [date, by] if b)
        out.append(f"<{link}|{label_bits or 'Slack'}>")
    _SLACK_REF_CACHE[box_file_id] = out
    return out


def _append_sources_section(answer: str, ctx: AgentContext) -> str:
    """ctx.cited_chunks にあるチャンクから「## 出典」セクションを生成して回答末尾に付与する。"""
    from argus.pm_qa_server import _format_source_label

    extra_sections: list[str] = []

    # 出典セクション
    if ctx.cited_chunks:
        seen: set[tuple] = set()
        items: list[tuple[int, str]] = []
        for i, chunk in enumerate(ctx.cited_chunks, 1):
            key = (chunk.get("source_type"), chunk.get("source_ref"), chunk.get("held_at"))
            if key in seen:
                continue
            seen.add(key)
            label = _format_source_label(chunk)
            ref = chunk.get("source_ref") or ""
            source_type = chunk.get("source_type", "")
            if source_type == "slack_raw" and ref:
                link = f"<{ref}|スレッドを開く>"
            elif source_type == "web" and ref:
                link = f"<{ref}|リンク>"
            elif source_type == "box_document" and ref:
                link = f"<{ref}|Boxで開く>"
                slack_links = _fetch_slack_references_for_box(chunk.get("record_id") or "")
                if slack_links:
                    link = link + " / Slack共有: " + ", ".join(slack_links)
            elif source_type == "minutes_content" and ref:
                held_at = chunk.get("held_at") or ""
                link = f"{held_at} {ref}".strip()
            else:
                link = ref
            items.append((i, f"- [{i}] {label}" + (f" — {link}" if link else "")))
        if items:
            extra_sections.append("\n".join(["", "## 出典"] + [s for _, s in items]))

    if not extra_sections:
        return answer
    return answer.rstrip() + "\n" + "\n\n".join(extra_sections)


# =========================================================================== #
#  Slack Handler
# =========================================================================== #

_ID_REF_RE = re.compile(
    r"(?P<full>"
    r"(?P<kind>a|d|AI|決定|ID)"      # 種別
    r"\s*[:： ]\s*"
    r"(?P<id>\d{1,6})"
    r")"
)


def _expand_id_references(text: str, conns: list) -> str:
    """出力中の `a:670` / `AI:670` / `決定:42` / `ID:670` 等を content[:60] で展開する。

    参照先が action_items なら `a:670 "xxxxx..."`、decisions なら `d:42 "xxxxx..."`。
    pm.db に見つからないIDは元のまま残す。
    """
    cache: dict[tuple[str, int], str | None] = {}

    def _lookup(table: str, item_id: int) -> str | None:
        key = (table, item_id)
        if key in cache:
            return cache[key]
        snippet: str | None = None
        for conn in conns:
            try:
                row = conn.execute(
                    f"SELECT content FROM {table} WHERE id = ? AND COALESCE(deleted,0)=0",
                    (item_id,),
                ).fetchone()
            except Exception:
                continue
            if row and row["content"]:
                s = row["content"].replace("\n", " ").strip()
                snippet = s[:60] + ("…" if len(s) > 60 else "")
                break
        cache[key] = snippet
        return snippet

    def _replace(m: re.Match) -> str:
        kind = m.group("kind")
        item_id = int(m.group("id"))
        # 種別から対象テーブルを推定。a/AI → action_items、d/決定 → decisions、
        # ID は両方試す（action_items 優先）
        if kind in ("a", "AI"):
            tables = ["action_items"]
            norm = f"a:{item_id}"
        elif kind in ("d", "決定"):
            tables = ["decisions"]
            norm = f"d:{item_id}"
        else:  # ID
            tables = ["action_items", "decisions"]
            norm = f"ID:{item_id}"

        for t in tables:
            snippet = _lookup(t, item_id)
            if snippet:
                prefix = "a" if t == "action_items" else "d"
                if kind == "ID":
                    return f"{prefix}:{item_id} “{snippet}”"
                return f"{norm} “{snippet}”"
        return m.group("full")

    return _ID_REF_RE.sub(_replace, text)


# =========================================================================== #
#  調査結果の出力先フラグパース
# =========================================================================== #

_OUTPUT_FLAG_RE = re.compile(r"\b(--to-box|--to-slack|--to-canvas)\b")


def _parse_output_flags(text: str) -> dict[str, bool]:
    """コマンドテキストから出力先フラグを抽出する。"""
    flags = {"box": False, "slack": False, "canvas": False}
    for m in _OUTPUT_FLAG_RE.finditer(text):
        flag = m.group(1)
        if flag == "--to-box":
            flags["box"] = True
        elif flag == "--to-slack":
            flags["slack"] = True
        elif flag == "--to-canvas":
            flags["canvas"] = True
    return flags


def _strip_output_flags(text: str) -> str:
    """出力先フラグを取り除いた純粋な質問文を返す。"""
    return _OUTPUT_FLAG_RE.sub("", text).strip()


def _output_to_box(result: str, today: str) -> str:
    """調査結果を一時ファイルに書き出して Box にアップロードする。"""
    import tempfile

    from argus.output_tools import box_upload_file

    result_md = f"# Argus 調査結果 ({today})\n\n{result}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f"argus_investigate_{today}_",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(result_md)
        tmp_path = f.name
    try:
        fname = f"argus_investigate_{today}.md"
        out = box_upload_file(tmp_path, filename=fname)
        return out
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _output_to_slack(result: str, today: str, channel_id: str) -> str:
    """調査結果をチャンネルに公開投稿する。"""
    from argus.output_tools import slack_post_message

    text = f"*Argus 調査結果* ({today})\n\n{result}"
    return slack_post_message(channel_id, text)


def _output_to_canvas(result: str, today: str) -> str:
    """調査結果を Canvas に投稿する（要: SLACK_USER_TOKEN）。"""
    from cli_utils import resolve_report_canvas_id

    from argus.output_tools import canvas_post_content

    canvas_id = resolve_report_canvas_id()
    if not canvas_id:
        return "Canvas ID が設定されていません（PM_REPORT_CANVAS_ID 環境変数または argus_config.yaml report.canvas_id）"
    content = f"# Argus 調査結果 ({today})\n\n{result}"
    return canvas_post_content(canvas_id, content)


def _run_investigate(respond, command, *, no_encrypt: bool = False):
    """Slack /argus-investigate のバックグラウンド処理"""
    try:
        cmd_text = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")

        today = date.today().isoformat()
        since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()

        index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels(channel_id)
        conns = [open_pm_db(p, no_encrypt=no_encrypt) for p in pm_db_paths]

        ctx = AgentContext(
            conns=conns,
            today=today,
            since=since_date,
            no_encrypt=no_encrypt,
            data_dir=_DATA_DIR,
            minutes_dir=_MINUTES_DIR,
            index_db=index_db,
            index_name=index_name,
            channels=channels,
        )

        seed_data = build_seed_data(ctx)
        result = run_agent(
            question=cmd_text,
            seed_data=seed_data,
            respond=respond,
            ctx=ctx,
        )

        # ID 参照 (a:670 / d:42 / AI:670 / 決定:42 / ID:670) を content[:60] で展開
        result = _expand_id_references(result, conns)

        for c in conns:
            c.close()

        # --- 出力ツールフラグのパース ---
        # コマンド末尾に --to-box / --to-slack / --to-canvas が付いていたら
        # 調査結果を各出力先にも送信する（ユーザー確認はコマンド入力時に済んでいる想定）
        _output_flags = _parse_output_flags(cmd_text)
        _cmd_cleaned = _strip_output_flags(cmd_text)

        _output_result_lines = []

        if _output_flags.get("box"):
            out = _output_to_box(result, today)
            _output_result_lines.append(f"> 📦 Box: {out}")
            logger.info("[investigate] output_to_box: %s", out)

        if _output_flags.get("slack"):
            out = _output_to_slack(result, today, channel_id)
            _output_result_lines.append(f"> 💬 Slack: {out}")
            logger.info("[investigate] output_to_slack: %s", out)

        if _output_flags.get("canvas"):
            out = _output_to_canvas(result, today)
            _output_result_lines.append(f"> 📋 Canvas: {out}")
            logger.info("[investigate] output_to_canvas: %s", out)

        # --- 元の Slack ephemeral 応答 ---
        # ephemeral は section block 3000 文字上限。超過時は分割して複数ブロックにする
        header = f"*Argus 調査結果* ({today})\n\n"
        output_footer = ""
        if _output_result_lines:
            output_footer = "\n\n" + "\n".join(_output_result_lines)
        body_raw = header + result + output_footer

        from utils.slack_post import _split_mrkdwn_to_blocks, _to_slack_mrkdwn

        # Section block の制限内に分割
        body = _to_slack_mrkdwn(body_raw)
        blocks = _split_mrkdwn_to_blocks(body)

        try:
            respond(
                blocks=blocks,
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception as e:
            logger.error(f"[investigate] 最終結果の Slack 送信エラー: {e}")
            logger.info(f"[investigate] 結果テキスト:\n{result[:500]}")
        logger.info("[investigate] 完了")

    except Exception as e:
        logger.exception("[investigate] エラー")
        try:
            respond(
                text=f":warning: Argus 調査エラー: {e}",
                response_type="ephemeral",
                replace_original=True,
            )
        except Exception:
            pass


# =========================================================================== #
#  CLI Mode
# =========================================================================== #

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Argus Investigation Agent")
    parser.add_argument("--investigate", required=True, help="調査内容")
    parser.add_argument("--max-steps", type=int, default=_DEFAULT_MAX_STEPS, help="最大ステップ数")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT, help="タイムアウト（秒）")
    parser.add_argument("--days", type=int, default=_DEFAULT_SINCE_DAYS, help="直近何日分を対象にするか（--since 未指定時のみ有効）")
    parser.add_argument("--since", type=str, default="", help="調査対象期間の開始日（YYYY-MM-DD）。指定時は --days より優先")
    parser.add_argument("--db", default=str(_PM_DB), help="pm.db のパス")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--dry-run", action="store_true", help="LLM呼び出しなし（シードデータ確認用）")
    parser.add_argument("--to-box", action="store_true", help="調査結果を Box にアップロード")
    parser.add_argument("--to-slack", type=str, default="", help="調査結果を指定チャンネルに Slack 投稿（channel_id）")
    parser.add_argument("--to-canvas", action="store_true", help="調査結果を Canvas に投稿")
    parser.add_argument("--no-intent-header", action="store_true", help="ご質問の解釈ヘッダを出力しない（レポートファイル用）")
    parser.add_argument("--context-file", help="事前調査結果等の背景情報ファイル（Pass 1 結果を Pass 2 に渡す際に使用）")
    args = parser.parse_args()

    today = date.today().isoformat()
    if args.since:
        try:
            date.fromisoformat(args.since)
        except ValueError:
            parser.error(f"--since の日付形式が不正です（YYYY-MM-DD）: {args.since}")
        since_date = args.since
    else:
        since_date = (date.today() - timedelta(days=args.days)).isoformat()

    index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels()
    if args.db != str(_PM_DB):
        pm_db_paths = [Path(args.db)]
    conns = [open_pm_db(p, no_encrypt=args.no_encrypt) for p in pm_db_paths]

    ctx = AgentContext(
        conns=conns,
        today=today,
        since=since_date,
        no_encrypt=args.no_encrypt,
        data_dir=_DATA_DIR,
        minutes_dir=_MINUTES_DIR,
        index_db=index_db,
        index_name=index_name,
        channels=channels,
    )

    seed_data = build_seed_data(ctx)

    if args.dry_run:
        print("=== シードデータ ===")
        print(seed_data)
        print(f"\n=== 調査質問 ===\n{args.investigate}")
        print(f"\n=== ツール一覧 ===\n{_build_tool_descriptions()}")
        for c in conns:
            c.close()
        return

    context_text = ""
    if args.context_file:
        try:
            context_text = Path(args.context_file).read_text(encoding="utf-8")
        except Exception as e:
            print(f"[WARN] --context-file 読み込み失敗: {e}", file=sys.stderr)

    result = run_agent(
        question=args.investigate,
        seed_data=seed_data,
        respond=None,
        ctx=ctx,
        max_steps=args.max_steps,
        timeout=args.timeout,
        include_intent_header=not args.no_intent_header,
        context=context_text,
    )

    # ID 参照 (a:670 / d:42 / AI:670 / 決定:42 / ID:670) を content[:60] で展開
    result = _expand_id_references(result, conns)

    for c in conns:
        c.close()

    # --- 出力先オプション ---
    _output_summary = []
    if result:
        if args.to_box:
            out = _output_to_box(result, today)
            _output_summary.append(f"[Box] {out}")
            print(f"[output] {out}")
        if args.to_slack:
            out = _output_to_slack(result, today, args.to_slack)
            _output_summary.append(f"[Slack] {out}")
            print(f"[output] {out}")
        if args.to_canvas:
            out = _output_to_canvas(result, today)
            _output_summary.append(f"[Canvas] {out}")
            print(f"[output] {out}")

    print("\n=== Argus 調査結果 ===\n", file=sys.stderr)
    print(result)


if __name__ == "__main__":
    main()
