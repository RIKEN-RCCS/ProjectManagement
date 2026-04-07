#!/usr/bin/env python3
"""
cli_utils.py

PM支援スクリプト共通の CLI ユーティリティ。
argparse ヘルパー関数・make_logger() を提供する。
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# argparse ヘルパー
# --------------------------------------------------------------------------- #

def add_output_arg(parser: argparse.ArgumentParser) -> None:
    """--output PATH を parser に追加する"""
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="出力をファイルにも保存")


def add_no_encrypt_arg(parser: argparse.ArgumentParser) -> None:
    """--no-encrypt を parser に追加する"""
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DBを暗号化しない（平文モード）")


def add_dry_run_arg(parser: argparse.ArgumentParser) -> None:
    """--dry-run を parser に追加する"""
    parser.add_argument("--dry-run", action="store_true",
                        help="DB保存なし・結果を標準出力のみ")


def add_since_arg(parser: argparse.ArgumentParser, help_suffix: str = "") -> None:
    """--since YYYY-MM-DD を parser に追加する"""
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help=f"この日付以降のデータのみ対象{help_suffix}")


def add_db_arg(parser: argparse.ArgumentParser, default: str = "data/pm.db") -> None:
    """--db PATH を parser に追加する"""
    parser.add_argument("--db", default=None, metavar="PATH",
                        help=f"pm.db のパス（デフォルト: {default}）")


# --------------------------------------------------------------------------- #
# ロガーユーティリティ
# --------------------------------------------------------------------------- #

def make_logger(output_path: str | None):
    """
    (log, close) のタプルを返す。

    Parameters
    ----------
    output_path : str | None
        ファイルに出力する場合はパス文字列。None なら標準出力のみ。

    Returns
    -------
    log : Callable[[str], None]
        print(msg) + output_file.write(msg + "\\n") を行う関数
    close : Callable[[], None]
        output_file を閉じる関数（output_path が None の場合は何もしない）
    """
    output_file = open(output_path, "w", encoding="utf-8") if output_path else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    def close() -> None:
        if output_file:
            output_file.close()

    return log, close


# --------------------------------------------------------------------------- #
# LLM 呼び出し
# --------------------------------------------------------------------------- #

def call_claude(prompt: str, *, model: str | None = None, timeout: int = 120) -> str:
    """
    LLM を呼び出す。OPENAI_API_BASE が設定されている場合は OpenAI 互換 API を使用し、
    未設定の場合は Claude CLI（subprocess）を使用する。

    Parameters
    ----------
    prompt : str
        LLM に渡すプロンプト
    model : str | None
        使用するモデル名。OpenAI互換モード時は OPENAI_MODEL 環境変数 → "gemma4" の順で
        フォールバックする。Claude CLI モード時は Claude CLI のデフォルトを使用する。
    timeout : int
        タイムアウト秒数（デフォルト: 120秒）

    Returns
    -------
    str
        LLM の出力（strip済み）

    Raises
    ------
    RuntimeError
        Claude CLI モードで returncode != 0 の場合
    requests.HTTPError
        OpenAI互換モードで HTTP エラーが発生した場合
    subprocess.TimeoutExpired / requests.Timeout
        タイムアウトした場合（呼び出し元でキャッチすること）
    """
    if os.environ.get("OPENAI_API_BASE"):
        return _call_openai_compat(prompt, model=model, timeout=timeout)
    # CLAUDECODE 環境変数が設定されているとネストセッション判定でエラーになるため除外する
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = ["claude"]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


def _call_openai_compat(prompt: str, *, model: str | None = None, timeout: int = 120) -> str:
    """
    OpenAI 互換 API を requests で直接呼び出す。
    環境変数:
        OPENAI_API_BASE   — エンドポイント URL（例: http://localhost:8000/v1）
        OPENAI_API_KEY    — API キー（省略時は "dummy"）
        OPENAI_MODEL      — モデル名（省略時は "gemma4"）
        OPENAI_MAX_TOKENS — 最大出力トークン数（省略時はサーバーデフォルト）
                            議事録など長い出力が必要な場合は 8192 以上を推奨
    """
    import requests  # slack-sdk の依存として既にインストール済み
    base_url = os.environ["OPENAI_API_BASE"].rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    model_name = model or os.environ.get("OPENAI_MODEL", "gemma4")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    max_tokens_env = os.environ.get("OPENAI_MAX_TOKENS")
    if max_tokens_env:
        payload["max_tokens"] = int(max_tokens_env)
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------------- #
# CLAUDE.md ローダー
# --------------------------------------------------------------------------- #

def load_claude_md(claude_md_path: Path) -> str:
    """
    CLAUDE.md を読み込み、`@path` 参照を再帰的に展開して返す。

    Claude Code は `@docs/project.md` のような行を自動的に展開するが、
    スクリプトがファイルを直接読む場合は展開されない。
    本関数はその差異を吸収し、参照先ファイルの内容をインラインに結合する。
    """
    if not claude_md_path.exists():
        return ""
    base_dir = claude_md_path.parent
    return _expand_at_refs(claude_md_path.read_text(encoding="utf-8"), base_dir, depth=0)


def _expand_at_refs(text: str, base_dir: Path, depth: int) -> str:
    if depth > 5:  # 循環参照ガード
        return text
    lines = []
    for line in text.splitlines():
        m = re.match(r"^@(.+)$", line.strip())
        if m:
            ref_path = base_dir / m.group(1).strip()
            if ref_path.exists():
                included = _expand_at_refs(
                    ref_path.read_text(encoding="utf-8"), ref_path.parent, depth + 1
                )
                lines.append(included)
            # 参照先が存在しない場合はその行をスキップ
        else:
            lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Whisper 出力パース・整形
# --------------------------------------------------------------------------- #
_WHISPER_SEGMENT_RE = re.compile(
    r"####\s*\[([0-9:]+)\s*-\s*([0-9:]+)\]\s+(SPEAKER_\d+)\n(.*?)(?=\n####|\Z)",
    re.DOTALL,
)


def _parse_timestamp(time_str: str) -> int:
    """HH:MM:SS → 秒数"""
    parts = time_str.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + int(s)
    return 0


def parse_whisper_transcript(text: str) -> list[dict]:
    """
    whisper_vad.py 出力の `#### [HH:MM:SS - HH:MM:SS] SPEAKER_N` 形式を
    セグメントリストに変換する。形式に合わない場合は空リストを返す。
    """
    segments = []
    for m in _WHISPER_SEGMENT_RE.finditer(text):
        start_str, end_str, speaker, seg_text = m.groups()
        seg_text = seg_text.strip()
        if not seg_text or seg_text in ("...", "…"):
            continue
        segments.append({
            "speaker": speaker,
            "start": _parse_timestamp(start_str),
            "end": _parse_timestamp(end_str),
            "text": seg_text,
        })
    return segments


def format_whisper_transcript(segments: list[dict]) -> str:
    """セグメントリストを `[HH:MM:SS] SPEAKER_N: text` 形式に整形する"""
    lines = []
    for seg in segments:
        h, rem = divmod(seg["start"], 3600)
        m, s = divmod(rem, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}"
        lines.append(f"[{ts}] {seg['speaker']}: {seg['text']}")
    return "\n\n".join(lines)


def prepare_transcript(raw_text: str) -> tuple[str, bool]:
    """
    文字起こしテキストを LLM 入力用に整形する。
    Whisper形式を検出した場合は [HH:MM:SS] SPEAKER_N: text 形式に変換。
    Returns: (transcript_text, is_whisper_format)
    """
    segments = parse_whisper_transcript(raw_text)
    if segments:
        return format_whisper_transcript(segments), True
    return raw_text, False


# --------------------------------------------------------------------------- #
# パスユーティリティ
# --------------------------------------------------------------------------- #

def resolve_db_path(arg_db: str | None, default: Path) -> Path:
    """--db 引数からパスを解決する"""
    return Path(arg_db) if arg_db else default
