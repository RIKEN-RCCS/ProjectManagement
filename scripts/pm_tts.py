#!/usr/bin/env python3
"""Markdown を VOICEVOX で音声合成し、結合して MP3 にするスクリプト/ライブラリ。

VOICEVOX エンジン (http://localhost:50021) が起動済みであること、および
ffmpeg が PATH 上にあることを前提とする。

要約モード (--summarize / synthesize_markdown(summarize=True)):
    Markdown のセクション (見出し / 番号付きトピック) ごとに LLM で短縮し、
    URL や記号読み・冗長な表現を取り除いてから合成する。argus-today などの
    長いレポートを聴き流せる長さに圧縮するための既定経路。

CLI 例:
    python3 scripts/pm_tts.py data/sample.md -o /tmp/sample.mp3
    python3 scripts/pm_tts.py data/sample.md --speaker 30 --speed 1.2 --summarize
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import requests


VOICEVOX_HOST = "http://localhost:50021"
VOICEVOX_TEXT_LIMIT = 200
QUERY_TIMEOUT = 30
SYNTH_TIMEOUT = 120
RETRY_BACKOFF = (0.5, 1.0, 2.0)

DEFAULT_SPEAKER = 74   # 琴詠ニア ノーマル
DEFAULT_SPEED = 1.3

# ---------------------------------------------------------------------------
# fish-speech バックエンド設定
# TTS_BACKEND=fish で有効化。VOICEVOX がデフォルト。
# ---------------------------------------------------------------------------
FISH_HOST = os.environ.get("FISH_TTS_HOST", "http://localhost:8080")
FISH_TEXT_LIMIT = 400  # fish-speech は長文も安定しているが念のため分割
FISH_SYNTH_TIMEOUT = 600


def _get_tts_backend() -> str:
    # FISH_TTS_HOST が設定されていれば fish、未設定なら voicevox
    # TTS_BACKEND で明示的に上書きも可能
    explicit = os.environ.get("TTS_BACKEND", "").lower()
    if explicit:
        return explicit
    return "fish" if os.environ.get("FISH_TTS_HOST") else "voicevox"


# ---------------------------------------------------------------------------
# fish-speech 呼び出し
# ---------------------------------------------------------------------------

def _fish_synth_chunk(text: str, out_path: Path, speed: float = 1.0, reference_id: str | None = None) -> None:
    """fish-speech API でテキストを合成して WAV を out_path に書き出す。"""
    import json as _json

    if reference_id is None:
        reference_id = os.environ.get("FISH_REFERENCE_ID") or None
    # FISH_SEED で話者を固定する。同じ seed → 毎回同じ声が生成される
    # FISH_SEED=0 でランダム（seed なし）、それ以外の値で話者固定
    seed_str = os.environ.get("FISH_SEED", "42")
    seed = int(seed_str) if seed_str.isdigit() and seed_str != "0" else None
    # FISH_EMOTION: excited / happy / sad / angry / fearful / disgusted / surprised など
    emotion = os.environ.get("FISH_EMOTION", "").strip()
    synth_text = f"[{emotion}] {text}" if emotion else text
    payload: dict = {
        "text": synth_text,
        "format": "wav",
        "normalize": True,
        "streaming": False,
        "latency": "normal",
    }
    if reference_id:
        payload["reference_id"] = reference_id
    if seed is not None:
        payload["seed"] = seed

    # fish-speech は推論に数分かかるためリトライなし・長タイムアウトで1回だけ呼ぶ
    resp = requests.post(
        f"{FISH_HOST}/v1/tts",
        data=_json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=FISH_SYNTH_TIMEOUT,
    )
    resp.raise_for_status()
    out_path.write_bytes(resp.content)


def _is_fish_available() -> bool:
    """fish-speech サーバが疎通可能かを返す。"""
    try:
        r = requests.get(f"{FISH_HOST}/v1/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _is_voicevox_available() -> bool:
    """VOICEVOX エンジンが疎通可能かを返す。"""
    try:
        r = requests.get(f"{VOICEVOX_HOST}/version", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def check_fish_alive() -> str:
    try:
        r = requests.get(f"{FISH_HOST}/v1/health", timeout=5)
        r.raise_for_status()
        return r.json().get("status", "ok")
    except Exception as exc:
        raise RuntimeError(
            f"fish-speech サーバに接続できません ({FISH_HOST}): {exc}\n"
            "bash scripts/pm_daemon.sh start fish で起動してください。"
        )


# ---------------------------------------------------------------------------
# Markdown 整形
# ---------------------------------------------------------------------------

_RE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_BARE_URL = re.compile(r"https?://\S+")
_RE_HEADING = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_RE_LIST_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_RE_LIST_NUM = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_RE_HR = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_RE_BLANKLINES = re.compile(r"\n{3,}")
_RE_SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
_RE_SLACK_CHANNEL = re.compile(r"<#[CG][A-Z0-9]+\|([^>]+)>")


def strip_markdown(text: str) -> str:
    """Markdown の装飾を除去し、読み上げ向けのプレーンテキストに変換する。"""
    text = _RE_FENCE.sub("", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_LINK.sub(r"\1", text)
    text = _RE_BARE_URL.sub("", text)
    text = _RE_SLACK_MENTION.sub("", text)
    text = _RE_SLACK_CHANNEL.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    text = _RE_HR.sub("", text)
    text = _RE_LIST_BULLET.sub("", text)
    text = _RE_LIST_NUM.sub("", text)
    text = _RE_BLANKLINES.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# セクション分割（要約用）
# ---------------------------------------------------------------------------

# `## 主な議論トピック` `1. 主な議論トピック` などをセクション境界として扱う
_RE_SECTION_HEAD = re.compile(
    r"^\s*(?:#{1,6}\s+|\d+\.\s+)(?P<title>.+?)\s*$",
    re.MULTILINE,
)
_RE_HEADING_LEVEL = re.compile(r"^\s*(#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)


def split_into_sections(markdown: str) -> list[tuple[str, str]]:
    """Markdown を (タイトル, 本文) のリストに分解する。

    見出し (#, ##, ...) または `1. タイトル` 形式の番号付き項目をセクション境界とする。
    最初の見出しより前にテキストがあれば、それは ("", body) として先頭に挿入される。
    水平線 (---) は無視。
    """
    cleaned = _RE_HR.sub("", markdown)

    matches = list(_RE_SECTION_HEAD.finditer(cleaned))
    if not matches:
        return [("", cleaned.strip())]

    sections: list[tuple[str, str]] = []

    head_text = cleaned[: matches[0].start()].strip()
    if head_text:
        sections.append(("", head_text))

    for i, m in enumerate(matches):
        title = m.group("title").strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        body = cleaned[body_start:body_end].strip()
        sections.append((title, body))

    return [(t, b) for t, b in sections if t or b]


# `- **[優先度: 高]** タイトル` または `- **[高]** タイトル` を境界として扱う
# - 行頭に `-` か `*` の箇条書きマーカー
# - 続いて `**[優先度: 高/中/低]**` または `**[高/中/低]**`
# - 続いてタイトル
_RE_PRIORITY_ITEM = re.compile(
    r"^(?P<indent>\s*)[-*]\s+\*\*\[(?:優先度[::]\s*)?(?P<level>高|中|低|High|Medium|Low)\]\*\*\s*(?P<title>.+?)\s*$",
    re.MULTILINE,
)


def split_priority_sections(markdown: str) -> list[tuple[str, str]]:
    """argus-brief / argus-risk の出力を優先度項目単位に分解する。

    各 `- **[優先度: 高]** タイトル` ブロックを 1 セクションとし、
    次の優先度項目（または同等以上の見出し）までを本文として束ねる。
    """
    cleaned = _RE_HR.sub("", markdown)
    matches = list(_RE_PRIORITY_ITEM.finditer(cleaned))

    if not matches:
        # priority 形式が見つからなければデフォルトの分割にフォールバック
        return split_into_sections(markdown)

    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        level = m.group("level").strip()
        title = m.group("title").strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        body = cleaned[body_start:body_end].strip()
        # 次のトップレベル `## ` ヘッダがあればそこで本文を打ち切る（保険）
        h2 = _RE_HEADING_LEVEL.search(body)
        if h2 and len(h2.group(1)) <= 2:
            body = body[: h2.start()].strip()
        section_title = f"優先度{level} {title}"
        sections.append((section_title, body))

    return sections


def split_minutes_sections(markdown: str) -> list[tuple[str, str]]:
    """議事録 Markdown を音声化用のブロック単位に分解する。

    - `## 決定事項` ブロック全体 → 1 セクション
    - `## アクションアイテム` ブロック全体 → 1 セクション
    - `## 議事内容` の本体は捨て、配下の `### トピック` を各々独立セクション化
    - その他の `##` ブロックも 1 セクションとして残す（保険）
    """
    cleaned = _RE_HR.sub("", markdown)

    h2_matches: list[tuple[int, int, str, int]] = []  # (start, end, title, level)
    for m in _RE_HEADING_LEVEL.finditer(cleaned):
        h2_matches.append((m.start(), m.end(), m.group("title").strip(), len(m.group(1))))

    if not h2_matches:
        return split_into_sections(markdown)

    sections: list[tuple[str, str]] = []

    # 先頭の見出し前テキストは捨てる（議事録冒頭の preamble は読み上げ不要）

    # level==2 のブロックを切り出す
    h2_only = [(i, x) for i, x in enumerate(h2_matches) if x[3] == 2]
    for idx, (i, (start, end, title, _level)) in enumerate(h2_only):
        if idx + 1 < len(h2_only):
            block_end = h2_only[idx + 1][1][0]
        else:
            block_end = len(cleaned)
        body = cleaned[end:block_end].strip()

        if title.startswith("議事内容"):
            # 配下の ### を各セクションとして抽出。### がなければスキップ
            sub = list(_RE_HEADING_LEVEL.finditer(body))
            sub3 = [s for s in sub if len(s.group(1)) == 3]
            if not sub3:
                if body.strip():
                    sections.append((title, body))
                continue
            for j, sm in enumerate(sub3):
                sub_title = sm.group("title").strip()
                sub_start = sm.end()
                sub_end = sub3[j + 1].start() if j + 1 < len(sub3) else len(body)
                sub_body = body[sub_start:sub_end].strip()
                if sub_body:
                    sections.append((sub_title, sub_body))
            continue

        # 決定事項・アクションアイテム・その他 ## はブロック全体を 1 セクションに
        if body:
            sections.append((title, body))

    return sections


# ---------------------------------------------------------------------------
# LLM 要約
# ---------------------------------------------------------------------------

_SUMMARIZE_SYSTEM = (
    "あなたは音声読み上げ用の要約アシスタントです。"
    "入力テキストを、自然な日本語で短く読み上げやすい形に書き直してください。"
)

_SUMMARIZE_PROMPT_TMPL = """\
次の Markdown セクションを、音声読み上げ用に短くまとめてください。

# 制約
- 出力は{max_sentences}文以内、合計{max_chars}文字以内の日本語平文。
- URL・括弧書きの注釈・記号・箇条書き記号は読み上げないので削除する。
- 日付や担当者名など固有名詞は維持する。
- セクションタイトルがあれば最初に「{section_title}は、」のように主題を提示してから内容を述べる（タイトルが空なら省略）。
- 冗長な前置き「以下の通りです」「〜について」は不要。
- マークダウン記号や見出し記号は出力しない。プレーンな読み上げ文だけを返す。

# セクションタイトル
{title}

# セクション本文
{body}

要約:"""


def _summarize_with_llm(
    title: str,
    body: str,
    *,
    max_sentences: int = 2,
    max_chars: int = 120,
    timeout: int = 60,
) -> str:
    """1 セクションを LLM で要約する。失敗時は strip_markdown(body) を返す。"""
    body = body.strip()
    if not body:
        return ""

    # 既に短いセクションは要約せずそのまま返す（LLM ラウンドトリップ節約）
    plain_body = strip_markdown(body)
    if len(plain_body) <= max_chars:
        if title:
            return f"{title}は、{plain_body}"
        return plain_body

    try:
        from cli_utils import call_argus_llm, strip_think_blocks
    except ImportError as exc:
        print(f"[warn] cli_utils import failed: {exc}; 要約せず素のテキストを使用", file=sys.stderr)
        return f"{title}は、{plain_body}" if title else plain_body

    prompt = _SUMMARIZE_PROMPT_TMPL.format(
        title=title or "(なし)",
        body=plain_body,
        section_title=title or "本セクション",
        max_sentences=max_sentences,
        max_chars=max_chars,
    )

    try:
        raw = call_argus_llm(
            prompt,
            system=_SUMMARIZE_SYSTEM,
            max_tokens=512,
            timeout=timeout,
        )
        out = strip_think_blocks(raw).strip()
    except Exception as exc:
        print(f"[warn] LLM 要約失敗 (title={title!r}): {exc}; 素のテキストを使用", file=sys.stderr)
        return f"{title}は、{plain_body}" if title else plain_body

    # よくある余計な前置き行を 1 行だけ落とす
    out = re.sub(r"^(要約[::]\s*|出力[::]\s*)", "", out)
    return out.strip() or (f"{title}は、{plain_body}" if title else plain_body)


def summarize_markdown(
    markdown: str,
    *,
    max_sentences: int = 2,
    max_chars: int = 120,
    mode: str = "auto",
) -> str:
    """Markdown 全体をセクション単位で要約し、読み上げ用テキストを返す。

    mode:
      - "auto" / "default": 見出し or 番号付き行で素直に分割（argus-today 等）
      - "minutes": 議事録レイアウト
            (## 決定事項 / ## アクションアイテム / ## 議事内容→### サブ) で分割
      - "priority": argus-brief / argus-risk の優先度タグ付き項目単位で分割
    """
    if mode == "minutes":
        sections = split_minutes_sections(markdown)
    elif mode == "priority":
        sections = split_priority_sections(markdown)
    else:
        sections = split_into_sections(markdown)
    out_parts: list[str] = []
    for title, body in sections:
        s = _summarize_with_llm(
            title, body,
            max_sentences=max_sentences,
            max_chars=max_chars,
        )
        if s:
            if not s.endswith(("。", "！", "？", ".", "!", "?")):
                s += "。"
            out_parts.append(s)
    return "\n".join(out_parts).strip()


# ---------------------------------------------------------------------------
# チャンク化
# ---------------------------------------------------------------------------

_RE_SENTENCE_END = re.compile(r"(?<=[。！？!?])")


def _split_by_comma(sentence: str, limit: int) -> list[str]:
    parts = re.split(r"(?<=、)", sentence)
    out: list[str] = []
    buf = ""
    for p in parts:
        if len(p) > limit:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(p), limit):
                out.append(p[i:i + limit])
            continue
        if len(buf) + len(p) > limit:
            out.append(buf)
            buf = p
        else:
            buf += p
    if buf:
        out.append(buf)
    return out


def default_text_limit() -> int:
    backend = _get_tts_backend()
    if backend == "fish":
        # fish 優先でも利用不可なら VOICEVOX にフォールバックする可能性があるため、
        # 常に VOICEVOX の制限 (200) を使うのが安全
        if _is_fish_available():
            return FISH_TEXT_LIMIT
    return VOICEVOX_TEXT_LIMIT


def split_into_sentences(text: str, limit: int = VOICEVOX_TEXT_LIMIT) -> list[str]:
    chunks: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        flat = paragraph.replace("\n", " ")
        sentences = [s.strip() for s in _RE_SENTENCE_END.split(flat) if s.strip()]
        for s in sentences:
            if len(s) <= limit:
                chunks.append(s)
            else:
                chunks.extend(_split_by_comma(s, limit))
    return chunks


# ---------------------------------------------------------------------------
# VOICEVOX 呼び出し
# ---------------------------------------------------------------------------

def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for delay in (0.0,) + RETRY_BACKOFF:
        if delay:
            time.sleep(delay)
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def synth_chunk(text: str, speaker: int, out_path: Path, speed: float = 1.0, reference_id: str | None = None) -> None:
    """1 チャンクを合成して WAV を out_path に書き出す。

    TTS_BACKEND で優先バックエンドを決定し、fish 優先時に fish が利用不可なら
    VOICEVOX にフォールバックする。voicevox 優先時のフォールバックは行わない。
    """
    backend = _get_tts_backend()
    use_voicevox = backend == "voicevox"
    if backend == "fish":
        try:
            _fish_synth_chunk(text, out_path, speed=speed, reference_id=reference_id)
            return
        except Exception as exc:
            if _is_voicevox_available():
                print(f"[WARN] fish-speech 合成失敗 ({exc})。VOICEVOX にフォールバック",
                      file=sys.stderr)
                use_voicevox = True
            else:
                raise

    if use_voicevox:
        q = _request_with_retry(
            "POST",
            f"{VOICEVOX_HOST}/audio_query",
            params={"speaker": speaker, "text": text},
            timeout=QUERY_TIMEOUT,
        )
        audio_query = q.json()
        audio_query["speedScale"] = speed

        s = _request_with_retry(
            "POST",
            f"{VOICEVOX_HOST}/synthesis",
            params={"speaker": speaker},
            json=audio_query,
            headers={"Content-Type": "application/json"},
            timeout=SYNTH_TIMEOUT,
        )
        out_path.write_bytes(s.content)
        return


def check_tts_alive() -> str:
    """アクティブな TTS バックエンドの疎通確認。

    fish 優先時に fish が利用不可なら VOICEVOX の疎通を試み、
    利用可能ならフォールバック先として確認する。
    両方不可なら例外を投げる。
    """
    if _get_tts_backend() == "fish":
        try:
            return check_fish_alive()
        except RuntimeError:
            if _is_voicevox_available():
                print("[WARN] fish-speech が利用不可。VOICEVOX にフォールバックします",
                      file=sys.stderr)
                return check_voicevox_alive()
            raise
    return check_voicevox_alive()


def check_voicevox_alive() -> str:
    try:
        r = requests.get(f"{VOICEVOX_HOST}/version", timeout=3)
        r.raise_for_status()
        return r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r.text.strip()
    except Exception as exc:
        raise RuntimeError(
            f"VOICEVOX エンジンに接続できません ({VOICEVOX_HOST}): {exc}"
        )


_SPEAKER_NAME_CACHE: dict[int, str] = {}


def resolve_speaker_name(speaker_id: int) -> str:
    """speaker_id から「話者名（スタイル名）」形式のクレジット表記を返す。

    例: 74 → "琴詠ニア", 1 → "四国めたん（あまあま）"
    エンジン未起動時等で解決できない場合は "speaker:{id}" を返す。
    """
    if speaker_id in _SPEAKER_NAME_CACHE:
        return _SPEAKER_NAME_CACHE[speaker_id]
    try:
        resp = requests.get(f"{VOICEVOX_HOST}/speakers", timeout=5)
        resp.raise_for_status()
        for sp in resp.json():
            base = sp.get("name", "")
            for st in sp.get("styles", []):
                if st.get("id") == speaker_id:
                    style = st.get("name", "")
                    label = base if style in ("ノーマル", "", "ふつう") else f"{base}（{style}）"
                    _SPEAKER_NAME_CACHE[speaker_id] = label
                    return label
    except Exception:
        pass
    fallback = f"speaker:{speaker_id}"
    _SPEAKER_NAME_CACHE[speaker_id] = fallback
    return fallback


def credit_line(speaker_id: int) -> str:
    """アクティブな TTS バックエンドのクレジット文字列を返す。

    fish 優先時は fish が利用不可なら VOICEVOX のクレジットを返す。
    """
    backend = _get_tts_backend()
    if backend == "fish" and _is_fish_available():
        return ""
    # fish が利用不可 (フォールバック含む) または voicevox 優先
    return f"音声合成に『VOICEVOX:{resolve_speaker_name(speaker_id)}』を使用"


# ---------------------------------------------------------------------------
# WAV 結合 / MP3 変換
# ---------------------------------------------------------------------------

def concat_wavs(inputs: list[Path], output: Path) -> None:
    if not inputs:
        raise ValueError("結合対象の WAV がありません")
    with wave.open(str(inputs[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
    for p in inputs[1:]:
        with wave.open(str(p), "rb") as w:
            if w.getparams()[:3] != params[:3]:
                raise RuntimeError(
                    f"WAV パラメータが一致しません: {p} {w.getparams()} vs {params}"
                )
            frames.append(w.readframes(w.getnframes()))
    with wave.open(str(output), "wb") as out:
        out.setparams(params)
        for f in frames:
            out.writeframes(f)


def wav_to_mp3(wav: Path, mp3: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg が PATH に見つかりません。"
            " ~/.local_aarch64/bin を PATH に追加するか、ffmpeg をインストールしてください。"
        )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav),
            "-codec:a", "libmp3lame", "-q:a", "4",
            str(mp3),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def _iter_progress(items, total: int, desc: str, *, quiet: bool = False):
    if quiet:
        return iter(items)
    try:
        from tqdm import tqdm
        return tqdm(items, total=total, desc=desc)
    except ImportError:
        def _gen():
            for i, x in enumerate(items, 1):
                print(f"[{i}/{total}] {desc}", file=sys.stderr)
                yield x
        return _gen()


# ---------------------------------------------------------------------------
# 高レベル API（ライブラリ用）
# ---------------------------------------------------------------------------

def synthesize_markdown(
    markdown: str,
    output_mp3: Path,
    *,
    speaker: int = DEFAULT_SPEAKER,
    speed: float = DEFAULT_SPEED,
    summarize: bool = False,
    summarize_mode: str = "auto",
    max_sentences: int = 2,
    max_chars: int = 120,
    chunk_limit: int = VOICEVOX_TEXT_LIMIT,
    quiet: bool = False,
) -> Path:
    """Markdown 文字列から MP3 を生成する。

    Args:
        markdown: 入力 Markdown 文字列
        output_mp3: 出力 MP3 ファイルパス
        speaker: VOICEVOX speaker ID
        speed: 再生速度倍率
        summarize: True ならセクション単位で LLM 要約してから合成
        summarize_mode: "auto" or "minutes"。議事録なら "minutes"
        max_sentences/max_chars: 要約 1 セクションあたりの上限
    Returns:
        output_mp3 と同じ Path
    """
    if summarize:
        plain = summarize_markdown(
            markdown,
            max_sentences=max_sentences,
            max_chars=max_chars,
            mode=summarize_mode,
        )
    else:
        plain = strip_markdown(markdown)

    chunks = split_into_sentences(plain, limit=chunk_limit)
    if not chunks:
        raise ValueError("読み上げる本文がありません")

    check_tts_alive()

    output_mp3 = output_mp3.resolve()
    output_mp3.parent.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(tempfile.mkdtemp(prefix="voicevox_"))
    try:
        wav_paths: list[Path] = []
        for i, chunk in enumerate(_iter_progress(chunks, total=len(chunks), desc="synth", quiet=quiet), 1):
            wp = tmp_root / f"chunk_{i:04d}.wav"
            synth_chunk(chunk, speaker, wp, speed=speed)
            wav_paths.append(wp)
        merged_wav = tmp_root / "merged.wav"
        concat_wavs(wav_paths, merged_wav)
        wav_to_mp3(merged_wav, output_mp3)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return output_mp3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown を VOICEVOX で音声合成して MP3 を生成する")
    ap.add_argument("input", type=Path, help="入力 Markdown ファイル")
    ap.add_argument("-o", "--output", type=Path, default=None, help="出力 MP3 パス（既定: 入力と同じ basename .mp3）")
    ap.add_argument("--speaker", type=int, default=DEFAULT_SPEAKER, help=f"VOICEVOX speaker ID (default: {DEFAULT_SPEAKER}=琴詠ニア)")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help=f"再生速度倍率 (default: {DEFAULT_SPEED})")
    ap.add_argument("--limit", type=int, default=VOICEVOX_TEXT_LIMIT, help=f"1 チャンクの最大文字数 (default: {VOICEVOX_TEXT_LIMIT})")
    ap.add_argument("--summarize", action="store_true", help="セクション単位で LLM 要約してから合成（argus-today 等の長文向け）")
    ap.add_argument("--mode", choices=["auto", "minutes", "priority"], default="auto", help="要約モード。minutes=議事録 / priority=argus-brief・argus-risk の優先度項目単位")
    ap.add_argument("--max-sentences", type=int, default=2, help="--summarize 時、1 セクションあたりの最大文数 (default: 2)")
    ap.add_argument("--max-chars", type=int, default=120, help="--summarize 時、1 セクションあたりの最大文字数 (default: 120)")
    ap.add_argument("--keep-wav", action="store_true", help="中間 WAV を /tmp 配下に保持する")
    ap.add_argument("--dry-run", action="store_true", help="合成せず分割結果のみ表示")
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"入力が存在しません: {args.input}", file=sys.stderr)
        return 1

    raw = args.input.read_text(encoding="utf-8")

    if args.summarize:
        plain = summarize_markdown(
            raw,
            max_sentences=args.max_sentences,
            max_chars=args.max_chars,
            mode=args.mode,
        )
    else:
        plain = strip_markdown(raw)

    chunks = split_into_sentences(plain, limit=args.limit)
    if not chunks:
        print("読み上げる本文がありません", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"# {len(chunks)} chunks (limit={args.limit}, summarize={args.summarize})")
        for i, c in enumerate(chunks, 1):
            print(f"--- [{i}] ({len(c)} chars) ---")
            print(c)
        return 0

    try:
        version = check_tts_alive()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    backend = _get_tts_backend()
    print(f"TTS backend={backend}, version={version}, speaker={args.speaker}, speed={args.speed}, chunks={len(chunks)}", file=sys.stderr)

    output_mp3 = args.output or args.input.with_suffix(".mp3")
    output_mp3 = output_mp3.resolve()
    output_mp3.parent.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(tempfile.mkdtemp(prefix="voicevox_"))
    try:
        wav_paths: list[Path] = []
        for i, chunk in enumerate(_iter_progress(chunks, total=len(chunks), desc="synth"), 1):
            wp = tmp_root / f"chunk_{i:04d}.wav"
            synth_chunk(chunk, args.speaker, wp, speed=args.speed)
            wav_paths.append(wp)
        merged_wav = tmp_root / "merged.wav"
        concat_wavs(wav_paths, merged_wav)
        wav_to_mp3(merged_wav, output_mp3)
        size_kb = output_mp3.stat().st_size / 1024
        print(f"wrote {output_mp3} ({size_kb:.1f} KB)", file=sys.stderr)
    finally:
        if args.keep_wav:
            print(f"intermediate WAV kept at: {tmp_root}", file=sys.stderr)
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
