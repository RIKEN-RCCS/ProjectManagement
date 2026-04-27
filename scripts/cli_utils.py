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


def strip_think_blocks(text: str) -> str:
    """CoT を除去して日本語本文のみを返す。

    generate_minutes_local.py より移植。対応パターン:
    1. <think>...</think> タグ付きブロック（Qwen3/ELYZA 系）
    2. タグなし英語 CoT の前置き（Nemotron 系）— 日本語文字が最初に現れる段落から抽出する
    """
    # パターン1: <think>...</think> タグ除去
    # 閉じタグが無い場合（max_tokens 打ち切り）は空文字を返してリトライを促す
    if "<think>" in text and "</think>" not in text:
        return ""
    text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()

    # パターン2: 先頭が英語 CoT（ASCII主体）の場合、最初の日本語段落から開始
    if text and not re.search(r"[^\x00-\x7F]", text[:200]):
        # 日本語文字（ひらがな・カタカナ・漢字）を含む最初の行を探す
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"[\u3000-\u9FFF\uF900-\uFAFF]", line):
                text = "\n".join(lines[i:]).strip()
                break

    return text


def call_local_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    think: bool = False,
    max_tokens: int = 8192,
    no_stream: bool = False,
    system: str = "",
    no_chat_template_kwargs: bool = False,
    temperature: float | None = None,
) -> str:
    """OpenAI 互換 API を requests で直接呼び出す。generate_minutes_local.py より移植。

    ストリーミングをデフォルトとし、CoT ブロック（<think>タグ・英語前置き）を自動除去する。
    """
    import requests
    import json as _json
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    # temperature: 明示指定があればそれを使用、なければ think モードに応じたデフォルト
    effective_temp = temperature if temperature is not None else (0.6 if think else 0.8)
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": effective_temp,
    }
    # thinking モードを有効化（enable_thinking のみ送信; clear_thinking は Qwen3 専用のため除外）
    # thinking 時は top_p=0.95 を追加（ELYZA/Nemotron の推奨設定、反復ループ防止）
    # no_chat_template_kwargs=True の場合は chat_template_kwargs を送信しない
    # （Qwen3-Swallow 等の常時 reasoning モデル向け: toggle 不要、送信すると 400 エラーの可能性）
    if think:
        if not no_chat_template_kwargs:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        payload["top_p"] = 0.95
    # Qwen3-Swallow 推奨サンプリングパラメータ（HF公式サンプルより）
    if no_chat_template_kwargs:
        payload["top_k"] = 20
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"

    if no_stream:
        # 非ストリーミング（LiteLLM プロキシ経由で streaming が動作しない場合等）
        payload["stream"] = False
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        # reasoning_content は reasoning parser が有効な場合に thinking が分離される領域。
        # content のみを使用し、thinking トークンが出力に混入するのを防ぐ。
        content = msg.get("content") or ""
        print(f"[INFO] 生成トークン数（strip前）: {len(content)} chars, think={think}", file=sys.stderr)
        stripped = strip_think_blocks(content)
        print(f"[INFO] 生成トークン数（strip後）: {len(stripped)} chars", file=sys.stderr)
        return stripped

    # ストリーミング（デフォルト）
    payload["stream"] = True
    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
    resp.raise_for_status()

    content_parts: list[str] = []
    print("[INFO] 生成中 ", end="", flush=True, file=sys.stderr)
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = _json.loads(data_str)
        except _json.JSONDecodeError:
            continue
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        # reasoning parser 有効時は reasoning_content に thinking が流れる。
        # content のみを取得し、thinking トークンが出力に混入するのを防ぐ。
        token = delta.get("content") or ""
        if token:
            content_parts.append(token)
            print(".", end="", flush=True, file=sys.stderr)
    print(" 完了", flush=True, file=sys.stderr)

    content = "".join(content_parts)
    print(f"[INFO] 生成トークン数（strip前）: {len(content)} chars, think={think}", file=sys.stderr)
    stripped = strip_think_blocks(content)
    print(f"[INFO] 生成トークン数（strip後）: {len(stripped)} chars", file=sys.stderr)
    return stripped


def detect_vllm_model(base_url: str) -> str:
    """vLLM の /v1/models エンドポイントからモデル名を自動取得する。"""
    import urllib.request, json  # noqa: E401
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        models = [m["id"] for m in data.get("data", [])]
        if not models:
            raise RuntimeError(f"vLLM にモデルが見つかりません: {url}")
        return models[0]
    except Exception as e:
        raise RuntimeError(f"vLLM モデル自動取得に失敗: {url} — {e}") from e


def _call_openai_compat(prompt: str, *, model: str | None = None, timeout: int = 120) -> str:
    """
    call_local_llm() を環境変数経由で呼び出すラッパー。
    環境変数:
        OPENAI_API_BASE   — エンドポイント URL（例: http://localhost:8000/v1）
        OPENAI_API_KEY    — API キー（省略時は "dummy"）
        OPENAI_MODEL      — モデル名（省略時は "gemma4"）
        OPENAI_MAX_TOKENS — 最大出力トークン数（省略時: 8192）
    """
    base_url = os.environ["OPENAI_API_BASE"]
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    model_name = model or os.environ.get("OPENAI_MODEL", "gemma4")
    max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))
    return call_local_llm(
        prompt,
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def call_argus_llm(
    prompt: str,
    *,
    timeout: int = 300,
    max_tokens: int = 4096,
    system: str = "",
) -> str:
    """
    Argus 用 LLM 呼び出し。gemma4（localhost）優先、未起動なら RiVault にフォールバック。

    優先順位:
        1. OPENAI_API_BASE（gemma4 等のローカル vLLM）— 128K 対応済みなら十分
        2. RIVAULT_URL（RiVault GLM-4.7-Flash）— ローカルが使えない場合のフォールバック
    """
    local_base = os.environ.get("OPENAI_API_BASE", "http://localhost:8000/v1")
    model = os.environ.get("OPENAI_MODEL", "google/gemma-4-26B-A4B-it")

    # ローカル vLLM（gemma4）が起動しているか確認
    import requests as _req
    try:
        _req.get(local_base.rstrip("/v1").rstrip("/") + "/health", timeout=3)
        local_ok = True
    except Exception:
        local_ok = False

    if local_ok:
        return call_local_llm(
            prompt,
            model=model,
            base_url=local_base,
            api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
            timeout=timeout,
            max_tokens=max_tokens,
            system=system,
            no_stream=True,
        )
    # フォールバック: RiVault
    print("[INFO] ローカル LLM に接続できません。RiVault にフォールバックします。", file=sys.stderr)
    return call_rivault(prompt, timeout=timeout, max_tokens=max_tokens, system=system)


def call_rivault(
    prompt: str,
    *,
    model: str = "zai-org/GLM-4.7-Flash",
    timeout: int = 300,
    max_tokens: int = 8192,
    system: str = "",
) -> str:
    """
    RiVault (GLM-4.7-Flash, 200k context) を呼び出す。
    環境変数:
        RIVAULT_URL   — エンドポイント URL（末尾に /v1 を含む形式。例: https://rivault.example/v1）
        RIVAULT_TOKEN — API トークン
    call_local_llm() が base_url + "/chat/completions" でURLを組み立てるため、
    RIVAULT_URL に /v1 が含まれていれば正しく /v1/chat/completions になる。
    """
    base_url = os.environ.get("RIVAULT_URL")
    if not base_url:
        raise RuntimeError(
            "RIVAULT_URL が未設定。source ~/.secrets/rivault_tokens.sh を実行してください"
        )
    api_key = os.environ.get("RIVAULT_TOKEN", "dummy")
    # GLM-4.7-Flash は thinking モデルのため enable_thinking=False で thinking を無効化する。
    # 非ストリーミングはゲートウェイタイムアウト(504)が発生するためストリーミングを使用する。
    import requests as _requests
    import json as _json
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"
    resp = _requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
    resp.raise_for_status()
    parts: list[str] = []
    print("[INFO] Argus 生成中 ", end="", flush=True, file=sys.stderr)
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = _json.loads(data_str)
            token_text = (chunk.get("choices", [{}])[0].get("delta", {}).get("content") or "")
            if token_text:
                parts.append(token_text)
                print(".", end="", flush=True, file=sys.stderr)
        except _json.JSONDecodeError:
            continue
    print(" 完了", flush=True, file=sys.stderr)
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
# CLAUDE.md ローダー
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_claude_md_context() -> str:
    """ローカルLLM向けプロジェクト文脈を返す。generate_minutes_local.py より移植。

    docs/project.md から「ステークホルダー・主なプロジェクト参加者・プロジェクト固有の用語・
    会議の種類」の各セクションを抽出する。docs/project.md が存在しない場合は CLAUDE.md に
    フォールバックする。Claude CLI は CLAUDE.md を自動ロードするが、ローカルLLMはしないため
    このコンテキストをプロンプトに明示的に埋め込む必要がある。
    """
    _SECTION_PAT = re.compile(
        r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)"
    )
    project_md = _REPO_ROOT / "docs" / "project.md"
    claude_md  = _REPO_ROOT / "CLAUDE.md"

    if project_md.exists():
        content = project_md.read_text(encoding="utf-8")
        sections, capture = [], False
        for line in content.splitlines():
            if _SECTION_PAT.match(line):
                capture = True
            if capture:
                sections.append(line)
        return "\n".join(sections) if sections else content

    # フォールバック: CLAUDE.md から抽出
    if not claude_md.exists():
        return ""
    content = claude_md.read_text(encoding="utf-8")
    sections, capture = [], False
    for line in content.splitlines():
        if _SECTION_PAT.match(line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else content[:3000]


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
