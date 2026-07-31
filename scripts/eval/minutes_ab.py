#!/usr/bin/env python3
"""minutes_ab.py — 議事録生成への視覚入力（kimi-k3 マルチモーダル）A/B ベンチハーネス

docs/kimi-k3-migration.md 優先度2の実証実験。「文字起こし（combined）+ VTT 話者情報 +
スライド画像そのもの」を kimi-k3 に直接投入する構成 (C/D) を、現行 glm 経路 (A) と
K3 だがテキストOCRのみの経路 (B) に対して A/B 比較する。

| アーム | LLM | スライド入力 |
|---|---|---|
| A（本番基準） | glm（現行経路） | OCR テキスト（--slide-context） |
| B | K3（LOCAL_LLM_* 上書き） | OCR テキスト（--slide-context） |
| C | K3 視覚（MINUTES_VISION_*） | 画像のみ（--slide-images） |
| D | K3 視覚 | 画像 + OCR テキスト（--slide-images + --slide-context） |

knowledge_ab.py（run/report/judge、JSONL 追記、argus_ab_judge 流用、盲検 swap、
auto-tie）+ investigate_ab.py（アーム env override 辞書、${VAR} 展開の run 前検証、
_ARM_CONTROLLED_ENV_KEYS 方式の親 env 打ち消し、機密ホワイトリスト記録）を合成した
構成を取る。

- Slack/investigate 系と異なり一次データは data/processing の mp4+VTT+combined.txt
  キャッシュ（generate_minutes_local.py --multi-stage が過去に書き出したもの）。
  prep はこれらを stem 照合で紐づけ、スライドフレーム抽出・OCR・用語抽出を
  全アーム共有の前処理として一度だけ行う（交絡除去）
- run は data/eval/minutes_ab/manifest.json（prep の出力）を読み、会議×アームで
  scripts/recording/generate_minutes_local.py を subprocess 実行する。既定は
  --from-combined（Stage 2/3 のみ差分測定）。--full 指定時は data/processing 直下の
  {stem}.md/.txt（whisper_vad.py が過去に生成した生transcript）から --multi-stage で
  全段再実行する（無ければそのアームを arm_failed として記録しスキップする）
- judge は scripts/eval/argus_ab_judge.py の call_judge/parse_judge_output を
  import 流用する。judge モデルは比較対象に kimi 系が含まれるため中立性確保の目的で
  既定 DeepSeek-V4-Flash を明示指定する（RiVault 経由）
- アーム env の JSONL 記録はホワイトリスト方式（トークンは一切記録しない）

例:
    source ~/.secrets/rivault_tokens.sh   # + RIKYU_URL/TOKEN
    source ~/.secrets/localLLM.sh         # EMBED_API_BASE/KEY, EMBED_MODEL 用
    V=~/.venv_aarch64/bin/python3
    $V scripts/eval/minutes_ab.py prep --n 5
    $V scripts/eval/minutes_ab.py run --arms A,B,C,D
    $V scripts/eval/minutes_ab.py judge
    $V scripts/eval/minutes_ab.py report
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = REPO_ROOT / "scripts"
_EVAL_DIR = Path(__file__).resolve().parent
for _p in (_SCRIPTS_DIR, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argus_ab_judge  # noqa: E402 — scripts/eval 内の同居モジュールを流用
from recording.generate_minutes_local import (  # noqa: E402
    _split_action_rows,
    _split_decisions_list,
)
from recording.slide_ocr import (  # noqa: E402
    build_slide_context,
    extract_slide_frames,
    extract_terminology,
    ocr_slides,
)
from utils.llm import load_llm_secrets  # noqa: E402

DEFAULT_PROCESSING_DIR = REPO_ROOT / "data" / "processing"
DEFAULT_WORKSPACE = REPO_ROOT / "data" / "eval" / "minutes_ab"
DEFAULT_JSONL = DEFAULT_WORKSPACE / "results.jsonl"
DEFAULT_JUDGES_JSONL = DEFAULT_WORKSPACE / "judges.jsonl"

_WIN_TIE_THRESHOLD = 0.60
_MIN_FRAMES = 8

# --------------------------------------------------------------------------- #
# stem 照合（pm_from_recording.sh の VTT 検出ロジックを踏襲）
# --------------------------------------------------------------------------- #

_RES_SUFFIX_RE = re.compile(r"_\d+x\d+$")
_DUP_SUFFIX_RE = re.compile(r" ?\(\d+\)$")
_COMBINED_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-(.+)-combined\.txt$")


def _stem_variants(stem: str) -> list[str]:
    """解像度サフィックス（`_1920x1150` 等）・ブラウザ重複DLサフィックス（` (1)` 等）を
    剥がした表記のバリアントを列挙する（pm_from_recording.sh:370-412 の派生規則）。"""
    no_res = _RES_SUFFIX_RE.sub("", stem)
    no_dup = _DUP_SUFFIX_RE.sub("", stem)
    bare = _RES_SUFFIX_RE.sub("", no_dup)
    variants: list[str] = []
    for s in (stem, no_res, no_dup, bare):
        if s not in variants:
            variants.append(s)
    return variants


def _combined_basename(path: Path) -> str | None:
    """`YYYY-MM-DD-HHMMSS-{basename}-combined.txt` から basename 部分を取り出す。"""
    m = _COMBINED_NAME_RE.match(path.name)
    return m.group(1) if m else None


def find_combined_for_stem(stem: str, processing_dir: Path) -> Path | None:
    """mp4 の stem に対応する `*-combined.txt` を探す。

    複数の再実行キャッシュがヒットする場合はファイル名先頭の実行時刻が
    最新のものを採用する（文字列ソートで安全に比較できる形式のため）。
    """
    variants = set(_stem_variants(stem))
    candidates: list[Path] = []
    for path in processing_dir.glob("*-combined.txt"):
        basename = _combined_basename(path)
        if basename is None:
            continue
        if basename in variants or (set(_stem_variants(basename)) & variants):
            candidates.append(path)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.name)[-1]


def _find_vtt_for_stem(stem: str, processing_dir: Path) -> Path | None:
    """pm_from_recording.sh:370-412 の VTT 候補探索を踏襲する（1ディレクトリ版）。"""
    for s in _stem_variants(stem):
        for candidate in (f"{s}.transcript.vtt", f"{s}.vtt"):
            path = processing_dir / candidate
            if path.exists():
                return path
    m = re.match(r"^(.+) (\(\d+\))$", stem)
    if m:
        base, paren = m.group(1), m.group(2)
        for candidate in (f"{base}.transcript {paren}.vtt", f"{base}.transcript{paren}.vtt"):
            path = processing_dir / candidate
            if path.exists():
                return path
    return None


def _find_raw_transcript(stem: str, processing_dir: Path) -> Path | None:
    """--full 用の生transcript（whisper_vad.py 出力）を探す。"""
    for ext in (".md", ".txt"):
        candidate = processing_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# アーム定義
# --------------------------------------------------------------------------- #

# k3系アームが親から継承すると意図しない挙動になる env キー（罠対策）。
# MINUTES_VISION_* / ARGUS_ONESHOT* / ARGUS_LLM_TEMPERATURE は investigate_ab.py の
# _ARM_CONTROLLED_ENV_KEYS と同様の考え方で、evalシェルの残留設定がアームの
# env override（多くは A アームで空 = 親環境を継承）に混入するのを防ぐ。
_ARM_CONTROLLED_ENV_KEYS = frozenset({
    "MINUTES_VISION_LLM_URL", "MINUTES_VISION_LLM_MODEL",
    "MINUTES_VISION_LLM_TOKEN", "MINUTES_VISION_LLM_TEMPERATURE",
    "ARGUS_SKIP_LLM_SECRETS",
    "RIVAULT_URL", "RIVAULT_TOKEN",
    "LOCAL_LLM_URL", "LOCAL_LLM_TOKEN", "LOCAL_LLM_MODEL",
    "EMBED_API_BASE", "EMBED_API_KEY", "EMBED_MODEL",
    "ARGUS_LLM_TEMPERATURE",
    "ARGUS_ONESHOT", "ARGUS_ONESHOT_TOP_K",
    "ARGUS_ONESHOT_CHAR_BUDGET", "ARGUS_ONESHOT_MAX_TOKENS",
})

# JSONL に記録するアーム設定の非機密ホワイトリスト（トークンは絶対に含めない）。
_ARM_CONFIG_WHITELIST = (
    "LOCAL_LLM_MODEL", "MINUTES_VISION_LLM_MODEL",
    "ARGUS_LLM_TEMPERATURE", "MINUTES_VISION_LLM_TEMPERATURE",
)

ARM_PRESETS: dict[str, dict] = {
    "A": {
        "env": {},
        "slide_context": True,
        "slide_images": False,
    },
    "B": {
        "env": {
            "ARGUS_SKIP_LLM_SECRETS": "1",
            "RIVAULT_URL": "",
            "RIVAULT_TOKEN": "",
            "LOCAL_LLM_URL": "${RIKYU_URL}",
            "LOCAL_LLM_TOKEN": "${RIKYU_TOKEN}",
            "LOCAL_LLM_MODEL": "kimi-k3",
            "ARGUS_LLM_TEMPERATURE": "1.0",
            "EMBED_API_BASE": "${EMBED_API_BASE}",
            "EMBED_API_KEY": "${EMBED_API_KEY}",
            "EMBED_MODEL": "${EMBED_MODEL}",
        },
        "slide_context": True,
        "slide_images": False,
    },
    # C/D: _resolve_vision_config() が load_llm_secrets() を呼ぶようになった（M2/S2）ため、
    # ARGUS_SKIP_LLM_SECRETS=1 を明示しないと ~/.secrets/localLLM.sh の
    # MINUTES_VISION_LLM_* 定義がこのアーム env の kimi-k3 上書きを静かに潰しうる。
    # 前提: Stage 1（--full 時のみ実行、既定の --from-combined では不使用）や
    # vision 失敗時のテキストフォールバックが使う LOCAL_LLM_URL/RIVAULT_URL は、
    # ARGUS_SKIP_LLM_SECRETS=1 下では secrets ファイルから補充されないため、
    # 親シェル env で明示的に export 済みであることが前提になる
    # （_build_subprocess_env が _ARM_CONTROLLED_ENV_KEYS を親から剥がすため、
    # 単に ~/.secrets を source しただけでは伝播しない点に注意）。
    "C": {
        "env": {
            "MINUTES_VISION_LLM_URL": "${RIKYU_URL}",
            "MINUTES_VISION_LLM_TOKEN": "${RIKYU_TOKEN}",
            "MINUTES_VISION_LLM_MODEL": "kimi-k3",
            "ARGUS_SKIP_LLM_SECRETS": "1",
        },
        "slide_context": False,
        "slide_images": True,
    },
    "D": {
        "env": {
            "MINUTES_VISION_LLM_URL": "${RIKYU_URL}",
            "MINUTES_VISION_LLM_TOKEN": "${RIKYU_TOKEN}",
            "MINUTES_VISION_LLM_MODEL": "kimi-k3",
            "ARGUS_SKIP_LLM_SECRETS": "1",
        },
        "slide_context": True,
        "slide_images": True,
    },
}

_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")


def _expand_env_refs(env: dict[str, str]) -> dict[str, str]:
    """アーム env の `${NAME}` 形式の値を親環境変数で展開する（investigate_ab.py 同型）。

    `${NAME}` 参照先が未設定・空文字の場合は ValueError（run 開始前に検証するため）。
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


def build_arm_env(arm_name: str) -> dict[str, str]:
    """アーム名から展開済み（${VAR} 参照解決後）の env dict を返す。"""
    return _expand_env_refs(ARM_PRESETS[arm_name]["env"])


def _build_subprocess_env(arm_env: dict[str, str]) -> dict[str, str]:
    """親シェルの env 汚染（_ARM_CONTROLLED_ENV_KEYS）を打ち消してからアーム env を重ねる。"""
    env = {k: v for k, v in os.environ.items() if k not in _ARM_CONTROLLED_ENV_KEYS}
    env.update(arm_env)
    return env


def _whitelisted_arm_config(arm_env: dict[str, str]) -> dict[str, str]:
    """JSONL 記録用のホワイトリスト適用（LOCAL_LLM_TOKEN 等のトークンは含めない）。"""
    return {k: arm_env[k] for k in _ARM_CONFIG_WHITELIST if k in arm_env}


def build_run_cmd(
    *,
    stem: str,
    workspace: Path,
    output_dir: Path,
    arm_name: str,
    combined_path: Path | None = None,
    vtt_path: Path,
    max_tokens: int = 16384,
    full: bool = False,
    raw_transcript_path: Path | None = None,
) -> list[str]:
    """会議×アームの generate_minutes_local.py コマンド行を組み立てる。

    既定（full=False）は --from-combined で Stage 2/3 のみ差分測定する。
    full=True は --multi-stage で raw_transcript_path から全段再実行する
    （呼び出し側が raw_transcript_path の存在を保証すること）。
    """
    preset = ARM_PRESETS[arm_name]
    script = REPO_ROOT / "scripts" / "recording" / "generate_minutes_local.py"

    if full:
        if raw_transcript_path is None:
            raise ValueError("full=True には raw_transcript_path が必要です")
        cmd = [
            sys.executable, str(script), str(raw_transcript_path),
            "--multi-stage",
            "--vtt", str(vtt_path),
            "--max-tokens", str(max_tokens),
            "--output", str(output_dir),
        ]
    else:
        if combined_path is None:
            raise ValueError("full=False には combined_path が必要です")
        cmd = [
            sys.executable, str(script), str(combined_path),
            "--from-combined", str(combined_path),
            "--vtt", str(vtt_path),
            "--max-tokens", str(max_tokens),
            "--output", str(output_dir),
        ]

    if preset["slide_context"]:
        cmd += ["--slide-context", str(workspace / "ocr" / f"{stem}.md")]
    if preset["slide_images"]:
        cmd += ["--slide-images", str(workspace / "frames" / stem)]
    return cmd


# --------------------------------------------------------------------------- #
# vision usage 行のパース（utils/llm.py call_vision_llm の stderr 出力）
# --------------------------------------------------------------------------- #

_VISION_USAGE_RE = re.compile(
    r"\[INFO\] vision usage: prompt=(\d+|None) image=(\d+|None) completion=(\d+|None) "
    r"cached=(\d+|None) latency_ms=(\d+|None) images=(\d+|None)"
)


def _parsed_int_or_zero(value: str) -> int:
    """パース結果が \"None\" 文字列だった場合の防御的フォールバック（0扱い）。

    utils/llm.py call_vision_llm は usage 値を常に整数でログ出力する契約だが、
    万一 None 文字列が混入しても行ごと欠測させないための多層防御。
    """
    return 0 if value == "None" else int(value)


def parse_vision_usage(stderr: str) -> dict | None:
    """generate_minutes_local.py の stderr から vision usage 行を集計する。

    Stage 2 / Stage 3 でそれぞれ視覚呼び出しが発生しうるため、複数行あれば
    トークン数・レイテンシは合算し、calls に呼び出し回数を記録する。
    マッチ行が無い場合（視覚モード無効）は None を返す。
    """
    matches = _VISION_USAGE_RE.findall(stderr or "")
    if not matches:
        return None
    totals = {
        "prompt_tokens": 0, "image_tokens": 0, "completion_tokens": 0,
        "cached_tokens": 0, "latency_ms": 0, "images": 0,
    }
    keys = ("prompt_tokens", "image_tokens", "completion_tokens",
            "cached_tokens", "latency_ms", "images")
    for values in matches:
        for key, value in zip(keys, values, strict=True):
            totals[key] += _parsed_int_or_zero(value)
    totals["calls"] = len(matches)
    return totals


# --------------------------------------------------------------------------- #
# 実効モデルのパース（generate_minutes_local.py の "vision config:" / "route_order=" 行）
# --------------------------------------------------------------------------- #

_VISION_CONFIG_RE = re.compile(r"\[INFO\] vision config: model=(\S+)")
_ROUTE_ORDER_RE = re.compile(r"\[INFO\] call_argus_llm: route_order=(\S+)")


def parse_effective_model(stderr: str) -> dict | None:
    """generate_minutes_local.py の stderr から実効モデル情報を集計する。

    視覚モード有効時は "[INFO] vision config: model=..." から視覚モデル名を、
    call_argus_llm 経由の呼び出し（Stage 1・非vision呼び出し・vision失敗フォールバック）
    からは "[INFO] call_argus_llm: route_order=..." のユニーク集合を拾う。
    どちらも見つからなければ None を返す。
    """
    text = stderr or ""
    vision_models = sorted(set(_VISION_CONFIG_RE.findall(text)))
    route_orders = sorted(set(_ROUTE_ORDER_RE.findall(text)))
    if not vision_models and not route_orders:
        return None
    result: dict = {}
    if vision_models:
        result["vision_model"] = vision_models[0]
    if route_orders:
        result["route_order"] = route_orders
    return result


# --------------------------------------------------------------------------- #
# 自動メトリクス
# --------------------------------------------------------------------------- #

_REQUIRED_HEADERS = ("決定事項", "アクションアイテム", "議事内容")
_SPEAKER_LEAK_RE = re.compile(r"SPEAKER_\d+")
_UNFILLED_VALUES = {"", "（未定）", "未定", "-", "ー", "TBD"}


def _is_filled(value: str) -> bool:
    return (value or "").strip() not in _UNFILLED_VALUES


def _section_present(text: str, header: str) -> bool:
    return bool(re.search(rf"^##\s*{re.escape(header)}\s*$", text or "", flags=re.MULTILINE))


def _actions_table_parseable(text: str) -> bool:
    """`## アクションアイテム` 配下が表形式で崩れずパースできるかを判定する。

    K3 の構造化出力懸念（テーブル崩壊）の定量化用。自由文が混入している場合は False。
    """
    m = re.search(r"^##\s*アクションアイテム\s*$", text or "", flags=re.MULTILINE)
    if not m:
        return False
    body = text[m.end():]
    m2 = re.search(r"^##\s+\S", body, flags=re.MULTILINE)
    if m2:
        body = body[: m2.start()]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    if lines == ["（なし）"]:
        return True
    return all(ln.startswith("|") for ln in lines)


def compute_auto_metrics(minutes_text: str, terminology: list[str] | None = None) -> dict:
    """generate_minutes_local.py の出力 Markdown から自動メトリクスを算出する。"""
    text = minutes_text or ""
    decisions = _split_decisions_list(text)
    action_rows = _split_action_rows(text)
    n_actions = len(action_rows)

    assignee_filled = sum(1 for a, _t, _d in action_rows if _is_filled(a))
    due_filled = sum(1 for _a, _t, d in action_rows if _is_filled(d))

    sections_present = {h: _section_present(text, h) for h in _REQUIRED_HEADERS}
    table_parseable = _actions_table_parseable(text)
    speaker_leak = bool(_SPEAKER_LEAK_RE.search(text))
    format_ok = all(sections_present.values()) and table_parseable and not speaker_leak

    terms = terminology or []
    hits = sum(1 for t in terms if t in text)

    return {
        "n_decisions": len(decisions),
        "n_actions": n_actions,
        "assignee_fill_rate": (assignee_filled / n_actions) if n_actions else None,
        "due_fill_rate": (due_filled / n_actions) if n_actions else None,
        "sections_present": sections_present,
        "table_parseable": table_parseable,
        "speaker_leak": speaker_leak,
        "format_ok": format_ok,
        "terminology_hits": hits,
        "terminology_total": len(terms),
        "terminology_hit_rate": (hits / len(terms)) if terms else None,
        "char_count": len(text),
    }


# --------------------------------------------------------------------------- #
# auto-tie 判定（knowledge_ab.py 同型）
# --------------------------------------------------------------------------- #

def _is_effectively_empty_minutes(text: str) -> bool:
    t = (text or "").strip()
    return t == "" or t == "（なし）"


def is_auto_tie(text_a: str, text_b: str) -> tuple[bool, str]:
    """両出力が同一、または両方とも実質空の場合に auto-tie とする。(is_tie, reason) を返す。"""
    if text_a == text_b:
        return True, "identical_output"
    if _is_effectively_empty_minutes(text_a) and _is_effectively_empty_minutes(text_b):
        return True, "both_empty"
    return False, ""


JUDGE_SYSTEM_MINUTES = (
    "あなたは会議議事録の品質を審査する厳格な評価者です。評価観点は次の3点です: "
    "(1) 固有名詞・数値の正確性 — 文字起こし・スライド参照テキストと矛盾する誤変換・誤記がないか、"
    "(2) 決定事項・アクションアイテムの網羅と実在性 — 入力に根拠のある項目を漏れなく拾えているか。"
    "入力に無い項目の捏造や、些末な事項の過剰抽出は減点してください、"
    "(3) 構成・可読性 — 議事録の定型フォーマット（## 決定事項 / ## アクションアイテム / "
    "## 議事内容の3見出し、アクションアイテムは3列テーブル）からの逸脱は減点してください。"
    "スライド由来の参照テキストは OCR 誤読があり得るため、参照テキストに無い表記でも"
    "文字起こしと整合する正しい固有名詞であれば減点しないでください。"
    "prefer は 'A' / 'B' / 'tie' のいずれかとし、短い rationale を付けてください。"
    "出力は JSON オブジェクトのみ。コードフェンス不要。スキーマ: "
    "{prefer:'A'|'B'|'tie', rationale:str}"
)

_JUDGE_PAIRS_DEFAULT = ("B:A", "C:B", "D:C")


# --------------------------------------------------------------------------- #
# prep
# --------------------------------------------------------------------------- #

def _discover_meetings(processing_dir: Path) -> list[dict]:
    """mp4+VTT ペアを列挙し、対応する combined.txt を stem 照合で紐づける。

    combined.txt が見つからない mp4 は候補から除外する（Stage2/3 差分測定の
    ベンチには combined キャッシュが必須のため）。
    """
    meetings = []
    for mp4_path in sorted(processing_dir.glob("*.mp4")):
        stem = mp4_path.stem
        vtt_path = _find_vtt_for_stem(stem, processing_dir)
        if vtt_path is None:
            continue
        combined_path = find_combined_for_stem(stem, processing_dir)
        if combined_path is None:
            continue
        combined_text = combined_path.read_text(encoding="utf-8", errors="replace")
        meetings.append({
            "stem": stem, "mp4": mp4_path, "vtt": vtt_path, "combined": combined_path,
            "combined_chars": len(combined_text),
        })
    return meetings


def _select_by_length_distribution(meetings: list[dict], n: int) -> list[dict]:
    """combined.txt の文字数で会議をソートし、短/中/長にまたがるよう均等な
    インデックスで n 件選ぶ（自動選定。manifest.json に記録し人が差し替え可能）。"""
    ordered = sorted(meetings, key=lambda m: m["combined_chars"])
    if n <= 0 or n >= len(ordered):
        return ordered
    if n == 1:
        return [ordered[len(ordered) // 2]]
    step = (len(ordered) - 1) / (n - 1)
    idx_set = {round(i * step) for i in range(n)}
    i = 0
    while len(idx_set) < n and i < len(ordered):
        idx_set.add(i)
        i += 1
    return [ordered[i] for i in sorted(idx_set)[:n]]


def _probe_image_tokens(frames_dir: Path, selected: list[dict]) -> dict | None:
    """採用会議の代表1枚を MINUTES_VISION_LLM_URL の kimi-k3 に投げ image_tokens/枚を実測する。"""
    if not selected:
        return None
    url = os.environ.get("MINUTES_VISION_LLM_URL") or os.environ.get("RIKYU_URL")
    if not url:
        print("[WARN] MINUTES_VISION_LLM_URL/RIKYU_URL 未設定のため image_tokens プローブをスキップ",
              file=sys.stderr)
        return None
    token = os.environ.get("MINUTES_VISION_LLM_TOKEN") or os.environ.get("RIKYU_TOKEN") or "dummy"
    model = os.environ.get("MINUTES_VISION_LLM_MODEL", "kimi-k3")

    stem = selected[0]["stem"]
    frame_paths = sorted((frames_dir / stem).glob("*.png"))
    if not frame_paths:
        return None
    probe_image = frame_paths[len(frame_paths) // 2]

    try:
        from utils.llm import call_vision_llm
    except ImportError:
        print("[WARN] call_vision_llm 未実装のため image_tokens プローブをスキップ", file=sys.stderr)
        return None

    try:
        result = call_vision_llm(
            "この画像に写っているテキストを簡潔に説明してください。",
            [str(probe_image)], model=model, base_url=url, api_key=token,
            max_tokens=512, return_usage=True,
        )
    except Exception as e:
        print(f"[WARN] image_tokens プローブ失敗: {e}", file=sys.stderr)
        return None

    usage = result[1] if isinstance(result, tuple) else {}
    return {
        "stem": stem, "probe_image": str(probe_image),
        "image_tokens_per_frame": usage.get("image_tokens"), "usage": usage,
    }


def cmd_prep(args: argparse.Namespace) -> int:
    load_llm_secrets()
    processing_dir = Path(args.processing_dir)
    workspace = Path(args.workspace)
    frames_dir = workspace / "frames"
    ocr_dir = workspace / "ocr"
    terminology_dir = workspace / "terminology"
    for d in (frames_dir, ocr_dir, terminology_dir):
        d.mkdir(parents=True, exist_ok=True)

    meetings = _discover_meetings(processing_dir)
    if args.meetings:
        wanted = set(args.meetings.split(","))
        meetings = [m for m in meetings if m["stem"] in wanted]
        missing = wanted - {m["stem"] for m in meetings}
        if missing:
            print(f"[WARN] --meetings で指定された会議が見つかりません: {missing}", file=sys.stderr)
    print(f"[INFO] 候補会議: {len(meetings)} 件（mp4+VTT+combined.txt 揃い）", file=sys.stderr)

    qualified = []
    for m in meetings:
        stem = m["stem"]
        out_dir = frames_dir / stem
        frames = extract_slide_frames(m["mp4"], out_dir, scene_threshold=0.25, max_frames=200)
        if len(frames) < _MIN_FRAMES:
            print(f"[INFO] 除外（フレーム{len(frames)}枚 < {_MIN_FRAMES}）: {stem}", file=sys.stderr)
            continue
        m["frame_count"] = len(frames)
        qualified.append(m)

    if not qualified:
        print("[ERROR] フレーム8枚以上の会議が見つかりませんでした", file=sys.stderr)
        return 1

    selected = _select_by_length_distribution(qualified, args.n)
    print(f"[INFO] 選定: {len(selected)}/{len(qualified)} 件 (--n {args.n})", file=sys.stderr)

    manifest: dict = {"selected": [], "created_at": datetime.now().isoformat(timespec="seconds")}
    included_meetings: list[dict] = []
    for m in selected:
        stem = m["stem"]
        print(f"[INFO] OCR実行中: {stem} ({m['frame_count']}枚)...", file=sys.stderr)
        frame_paths = sorted((frames_dir / stem).glob("*.png"))
        slide_mds = ocr_slides(frame_paths)
        slide_context = build_slide_context(slide_mds)
        if not slide_context.strip():
            # OCR全滅（LOCAL_LLM_URL 未設定・エンドポイント不通等）を静かに通すと
            # A/B/D（--slide-context 併用アーム）が空文脈のまま実行され、
            # 何を比較しているのか分からなくなる。manifest から除外して明示的に警告する。
            print(
                f"[WARN] OCR結果が空のため除外します（LOCAL_LLM_URL/OCRエンドポイント"
                f"未設定または全滅の可能性）: {stem}",
                file=sys.stderr,
            )
            continue
        (ocr_dir / f"{stem}.md").write_text(slide_context, encoding="utf-8")
        # prep は会議ごとに1回きりのためコストは無視できる。use_llm_filter=False（正規表現
        # のみ）は一般語・OCR誤認識まで大量に拾いノイズが支配するため既定の True を使う。
        terms = extract_terminology(slide_mds, use_llm_filter=True)
        (terminology_dir / f"{stem}.txt").write_text(
            "\n".join(terms) + ("\n" if terms else ""), encoding="utf-8",
        )
        manifest["selected"].append({
            "stem": stem, "mp4": str(m["mp4"]), "vtt": str(m["vtt"]),
            "combined": str(m["combined"]), "frame_count": m["frame_count"],
            "combined_chars": m["combined_chars"], "terminology_count": len(terms),
        })
        included_meetings.append(m)

    if not manifest["selected"]:
        print("[ERROR] OCR結果が全会議で空でした。LOCAL_LLM_URL 設定を確認してください", file=sys.stderr)
        return 1

    probe = _probe_image_tokens(frames_dir, included_meetings)
    if probe is not None:
        manifest["image_tokens_probe"] = probe

    manifest_path = workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] manifest 保存: {manifest_path}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def _load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


_NOISY_STDERR_PREFIXES = ("[INFO] LLM call:", "[INFO] vision config:")


def _filtered_stderr_tail(stderr: str, n: int = 20) -> str:
    """err_tail 用に、毎回大量に出るルーチンINFOノイズ（LLM call: / vision config:）を
    除外してから末尾 n 行を取る（本当のエラー原因が埋もれるのを防ぐ）。"""
    lines = [
        ln for ln in (stderr or "").strip().splitlines()
        if not ln.startswith(_NOISY_STDERR_PREFIXES)
    ]
    return "\n".join(lines[-n:])


def _build_error_record(stem: str, arm_name: str, arm_env: dict, error: str, wall_s: float) -> dict:
    return {
        "stem": stem, "arm": arm_name, "output_path": None, "wall_s": wall_s,
        "arm_config": _whitelisted_arm_config(arm_env),
        "metrics": None, "vision_usage": None, "effective_model": None, "error": error,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def cmd_run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest が見つかりません。先に prep を実行してください: {manifest_path}",
              file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = manifest.get("selected") or []
    if args.meetings:
        wanted = set(args.meetings.split(","))
        selected = [m for m in selected if m["stem"] in wanted]
    if not selected:
        print("ERROR: 実行対象の会議がありません", file=sys.stderr)
        return 2

    arm_names = args.arms.split(",")
    try:
        arm_envs = {name: build_arm_env(name) for name in arm_names}
    except ValueError as exc:
        print(f"ERROR: アーム env の展開に失敗しました: {exc}", file=sys.stderr)
        return 2

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir = workspace / "out"
    processing_dir = Path(args.processing_dir)

    with open(jsonl_path, "a", encoding="utf-8") as out_f:
        for meeting in selected:
            stem = meeting["stem"]
            combined_path = Path(meeting["combined"])
            vtt_path = Path(meeting["vtt"])

            raw_transcript_path = None
            if args.full:
                raw_transcript_path = _find_raw_transcript(stem, processing_dir)
                if raw_transcript_path is None:
                    print(f"[WARN] --full 指定ですが生transcriptが見つかりません（{stem}）。"
                          f"{processing_dir}/{stem}.md(.txt) を用意してください。全アームをスキップ",
                          file=sys.stderr)
                    for arm_name in arm_names:
                        record = _build_error_record(
                            stem, arm_name, arm_envs[arm_name],
                            f"raw transcript not found for --full ({processing_dir}/{stem}.md/.txt)",
                            0.0,
                        )
                        out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_f.flush()
                    continue

            for arm_name in arm_names:
                print(f"[{stem}] arm={arm_name} 実行中...", file=sys.stderr, flush=True)
                arm_out_dir = out_dir / stem / arm_name
                arm_out_dir.mkdir(parents=True, exist_ok=True)
                cmd = build_run_cmd(
                    stem=stem, workspace=workspace, output_dir=arm_out_dir, arm_name=arm_name,
                    combined_path=combined_path, vtt_path=vtt_path, max_tokens=args.max_tokens,
                    full=args.full, raw_transcript_path=raw_transcript_path,
                )
                env = _build_subprocess_env(arm_envs[arm_name])
                started = time.time()
                try:
                    proc = subprocess.run(
                        cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                        timeout=args.timeout,
                    )
                except subprocess.TimeoutExpired:
                    wall_s = time.time() - started
                    record = _build_error_record(
                        stem, arm_name, arm_envs[arm_name], f"TimeoutExpired: {args.timeout}s", wall_s,
                    )
                    out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_f.flush()
                    print("    -> arm_failed (timeout)", file=sys.stderr)
                    continue

                wall_s = time.time() - started
                if proc.returncode != 0:
                    err_tail = _filtered_stderr_tail(proc.stderr or "")
                    record = _build_error_record(
                        stem, arm_name, arm_envs[arm_name],
                        f"returncode={proc.returncode}: {err_tail}", wall_s,
                    )
                    out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_f.flush()
                    print(f"    -> arm_failed (returncode={proc.returncode})", file=sys.stderr)
                    continue

                minutes_files = sorted(arm_out_dir.glob("*-minutes.md"))
                if not minutes_files:
                    record = _build_error_record(
                        stem, arm_name, arm_envs[arm_name], "no output file produced", wall_s,
                    )
                    out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_f.flush()
                    print("    -> arm_failed (no output)", file=sys.stderr)
                    continue

                minutes_path = minutes_files[-1]
                minutes_text = minutes_path.read_text(encoding="utf-8")

                terminology_path = workspace / "terminology" / f"{stem}.txt"
                terminology = []
                if terminology_path.exists():
                    terminology = [
                        ln for ln in terminology_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]

                metrics = compute_auto_metrics(minutes_text, terminology)
                vision_usage = parse_vision_usage(proc.stderr or "")
                effective_model = parse_effective_model(proc.stderr or "")

                record = {
                    "stem": stem, "arm": arm_name, "output_path": str(minutes_path),
                    "wall_s": wall_s, "arm_config": _whitelisted_arm_config(arm_envs[arm_name]),
                    "metrics": metrics, "vision_usage": vision_usage,
                    "effective_model": effective_model, "error": "",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_f.flush()
                print(f"    -> OK ({wall_s:.1f}s, decisions={metrics['n_decisions']}, "
                      f"actions={metrics['n_actions']}, format_ok={metrics['format_ok']})",
                      file=sys.stderr)

    print(f"完了: {jsonl_path} に追記", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# judge
# --------------------------------------------------------------------------- #

def _rivault_configured() -> bool:
    """RIVAULT_URL / RIVAULT_TOKEN が両方設定されているか確認する（investigate_ab.py 同型）。"""
    return bool(os.environ.get("RIVAULT_URL")) and bool(os.environ.get("RIVAULT_TOKEN"))


def cmd_judge(args: argparse.Namespace) -> int:
    if not _rivault_configured():
        print("ERROR: RIVAULT_URL / RIVAULT_TOKEN が未設定です。"
              "source ~/.secrets/rivault_tokens.sh を先に実行してください。", file=sys.stderr)
        return 2

    workspace = Path(args.workspace)
    manifest_path = workspace / "manifest.json"
    results_path = Path(args.jsonl)
    if not manifest_path.exists() or not results_path.exists():
        print("ERROR: manifest/results が見つかりません。先に prep/run を実行してください",
              file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    combined_by_stem = {m["stem"]: Path(m["combined"]) for m in manifest.get("selected") or []}

    records = _load_jsonl(results_path)
    by_stem_arm = {
        (r["stem"], r["arm"]): r for r in records if r.get("output_path")
    }
    pairs = [tuple(p.split(":")) for p in args.pairs.split(",")]

    judges_path = Path(args.judges_jsonl)
    judges_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    stems = sorted({stem for stem, _arm in by_stem_arm})

    with open(judges_path, "a", encoding="utf-8") as out_f:
        for stem in stems:
            ocr_path = workspace / "ocr" / f"{stem}.md"
            ocr_text = ocr_path.read_text(encoding="utf-8") if ocr_path.exists() else ""
            combined_path = combined_by_stem.get(stem)
            combined_excerpt = (
                combined_path.read_text(encoding="utf-8", errors="replace")[:8000]
                if combined_path and combined_path.exists() else ""
            )

            for new_name, base_name in pairs:
                rec_new = by_stem_arm.get((stem, new_name))
                rec_base = by_stem_arm.get((stem, base_name))
                if not rec_new or not rec_base:
                    print(f"[{stem}] {new_name}:{base_name} skipped (missing output)",
                          file=sys.stderr)
                    continue

                text_new = Path(rec_new["output_path"]).read_text(encoding="utf-8")
                text_base = Path(rec_base["output_path"]).read_text(encoding="utf-8")

                result = {
                    "stem": stem, "pair": f"{new_name}:{base_name}",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                is_tie, reason = is_auto_tie(text_new, text_base)
                if is_tie:
                    result.update(swap=None, prefer="tie", rationale=reason, auto_tie=True)
                else:
                    swap = rng.randint(0, 1)
                    arms = [(new_name, text_new), (base_name, text_base)]
                    if swap:
                        arms = arms[::-1]
                    (label_a, text_a), (label_b, text_b) = arms
                    prompt = (
                        f"# 文字起こし抜粋\n{combined_excerpt}\n\n"
                        "# スライド由来の参照テキスト（OCR。誤読があり得るため、参照に無い"
                        "正しい表記を採用している場合は減点しないでください）\n"
                        f"{ocr_text[:15000]}\n\n"
                        f"# 議事録候補 A\n{text_a}\n\n"
                        f"# 議事録候補 B\n{text_b}\n\n"
                        "候補 A と B のどちらが議事録としてより優れているか判定し、JSON で答えてください。"
                    )
                    raw, _latency_ms, err = argus_ab_judge.call_judge(
                        args.judge_model, JUDGE_SYSTEM_MINUTES, prompt,
                        max_tokens=args.judge_max_tokens, timeout=args.judge_timeout,
                    )
                    parsed = argus_ab_judge.parse_judge_output(raw) if raw else None
                    if not parsed:
                        result.update(swap=bool(swap), prefer="parse_failed",
                                      rationale=err or "parse_failed", auto_tie=False)
                    else:
                        prefer = parsed.get("prefer", "tie")
                        if prefer == "A":
                            winner = label_a
                        elif prefer == "B":
                            winner = label_b
                        else:
                            winner = "tie"
                        result.update(swap=bool(swap), prefer=winner,
                                      rationale=parsed.get("rationale", ""), auto_tie=False)

                out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                out_f.flush()
                print(f"[{stem}] {new_name}:{base_name} -> prefer={result['prefer']}",
                      file=sys.stderr)

    print(f"完了: {judges_path} に追記", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def cmd_report(args: argparse.Namespace) -> int:
    results_path = Path(args.jsonl)
    if not results_path.exists():
        print(f"results JSONL が見つかりません: {results_path}", file=sys.stderr)
        return 2
    records = _load_jsonl(results_path)
    if not records:
        print("レコードなし", file=sys.stderr)
        return 1

    print("# Minutes A/B Report\n")

    by_arm: dict[str, list[dict]] = {}
    arm_failed = 0
    for r in records:
        if r.get("error"):
            arm_failed += 1
            continue
        by_arm.setdefault(r["arm"], []).append(r)

    print(f"## アーム別自動メトリクス（arm_failed={arm_failed}件）\n")
    print("| arm | n | 決定件数(平均) | AI件数(平均) | 担当者埋まり率 | 期限埋まり率 | "
          "format_ok率 | terminology率 | wall秒(平均) | prompt/image/completion/cached(合計) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for arm, recs in sorted(by_arm.items()):
        n = len(recs)
        n_decisions_avg = _avg([r["metrics"]["n_decisions"] for r in recs]) or 0.0
        n_actions_avg = _avg([r["metrics"]["n_actions"] for r in recs]) or 0.0
        assignee_rate = _avg([
            r["metrics"]["assignee_fill_rate"] for r in recs
            if r["metrics"]["assignee_fill_rate"] is not None
        ])
        due_rate = _avg([
            r["metrics"]["due_fill_rate"] for r in recs
            if r["metrics"]["due_fill_rate"] is not None
        ])
        format_ok_rate = sum(1 for r in recs if r["metrics"]["format_ok"]) / n
        term_rate = _avg([
            r["metrics"]["terminology_hit_rate"] for r in recs
            if r["metrics"]["terminology_hit_rate"] is not None
        ])
        wall_avg = _avg([r["wall_s"] for r in recs]) or 0.0
        vision_totals = {"prompt_tokens": 0, "image_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        for r in recs:
            usage = r.get("vision_usage") or {}
            for k in vision_totals:
                vision_totals[k] += usage.get(k) or 0

        assignee_label = f"{assignee_rate:.1%}" if assignee_rate is not None else "N/A"
        due_label = f"{due_rate:.1%}" if due_rate is not None else "N/A"
        term_label = f"{term_rate:.1%}" if term_rate is not None else "N/A"
        print(f"| {arm} | {n} | {n_decisions_avg:.1f} | {n_actions_avg:.1f} | "
              f"{assignee_label} | {due_label} | {format_ok_rate:.1%} | {term_label} | "
              f"{wall_avg:.1f}s | "
              f"{vision_totals['prompt_tokens']}/{vision_totals['image_tokens']}/"
              f"{vision_totals['completion_tokens']}/{vision_totals['cached_tokens']} |")

    judges_path = Path(args.judges_jsonl)
    if not judges_path.exists():
        print("\n[INFO] judges JSONL が見つかりません。judge を先に実行してください", file=sys.stderr)
        return 0

    judge_records = _load_jsonl(judges_path)
    by_pair: dict[str, dict[str, int]] = {}
    for r in judge_records:
        counts = by_pair.setdefault(r["pair"], {})
        prefer = r.get("prefer") or "parse_failed"
        counts[prefer] = counts.get(prefer, 0) + 1

    print("\n## ペア別勝敗\n")
    for pair, counts in sorted(by_pair.items()):
        new_name, base_name = pair.split(":")
        new_wins = counts.get(new_name, 0)
        base_wins = counts.get(base_name, 0)
        ties = counts.get("tie", 0)
        valid = new_wins + base_wins + ties
        total = sum(counts.values())
        print(f"### pair={pair} (n={total})")
        for name, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {cnt}")
        if valid == 0:
            print("  有効サンプルなし\n")
            continue
        win_tie_rate = (new_wins + ties) / valid
        print(f"  {new_name} 勝ち+引き分け率: {win_tie_rate:.1%} ({new_wins + ties}/{valid})")
        verdict = "合格" if win_tie_rate >= _WIN_TIE_THRESHOLD else "未達"
        print(f"  合否判定: {verdict}（閾値{_WIN_TIE_THRESHOLD:.0%}）\n")

    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="議事録生成への視覚入力（kimi-k3）A/Bベンチハーネス "
                     "(A: glm+OCR / B: k3+OCR / C: k3視覚のみ / D: k3視覚+OCR)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prep", help="会議選定・スライドフレーム抽出・OCR・用語抽出・image_tokensプローブ")
    pp.add_argument("--processing-dir", default=str(DEFAULT_PROCESSING_DIR), metavar="PATH")
    pp.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), metavar="PATH")
    pp.add_argument("--n", type=int, default=5, help="選定する会議数（既定5）")
    pp.add_argument("--meetings", default=None,
                     help="stem をカンマ区切りで指定して選定対象を固定する（省略時は自動選定）")
    pp.set_defaults(func=cmd_prep)

    r = sub.add_parser("run", help="会議×アームで generate_minutes_local.py を実行")
    r.add_argument("--arms", default="A,B,C,D")
    r.add_argument("--meetings", default=None, help="stem をカンマ区切りで指定して実行対象を絞り込む")
    r.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), metavar="PATH")
    r.add_argument("--processing-dir", default=str(DEFAULT_PROCESSING_DIR), metavar="PATH",
                   help="--full 時の生transcript（{stem}.md/.txt）探索先")
    r.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    r.add_argument("--max-tokens", type=int, default=16384)
    r.add_argument("--timeout", type=int, default=3600,
                   help="generate_minutes_local.py subprocess の kill タイムアウト秒（既定3600。"
                        "vision 呼び出しの timeout 引き上げ（max(timeout, 900)）に対して"
                        "十分な余裕を持たせる）")
    r.add_argument("--full", action="store_true",
                   help="--from-combined を使わず生transcriptから全段（--multi-stage）再実行する。"
                        "processing-dir に {stem}.md/.txt が無い会議はそのアームをスキップする")
    r.set_defaults(func=cmd_run)

    j = sub.add_parser("judge", help="ペアごとに盲検比較してLLM-as-a-judgeで採点")
    j.add_argument("--pairs", default=",".join(_JUDGE_PAIRS_DEFAULT),
                   help="挑戦者:ベースライン のペアをカンマ区切りで指定"
                        f"（既定 {','.join(_JUDGE_PAIRS_DEFAULT)}）")
    j.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), metavar="PATH")
    j.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    j.add_argument("--judges-jsonl", default=str(DEFAULT_JUDGES_JSONL), metavar="PATH")
    j.add_argument("--judge-model", default="DeepSeek-V4-Flash",
                   help="比較対象に kimi 系を含むため中立性確保のため既定 DeepSeek-V4-Flash")
    j.add_argument("--judge-max-tokens", type=int, default=4096)
    j.add_argument("--judge-timeout", type=int, default=300)
    j.add_argument("--seed", type=int, default=7)
    j.set_defaults(func=cmd_judge)

    rp = sub.add_parser("report", help="集計・合否判定（≥60%%）")
    rp.add_argument("--jsonl", default=str(DEFAULT_JSONL), metavar="PATH")
    rp.add_argument("--judges-jsonl", default=str(DEFAULT_JUDGES_JSONL), metavar="PATH")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
