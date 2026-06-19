"""transcript.py — 音声文字起こし（Whisper / VTT）のパース・整形ユーティリティ

cli_utils.py から分離。Whisper VAD 出力の段落形式と Zoom VTT 形式の両方を扱う。
"""
import re
from pathlib import Path


# --------------------------------------------------------------------------- #
# Whisper VAD 出力パース・整形
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
    """whisper_vad.py 出力の `#### [HH:MM:SS - HH:MM:SS] SPEAKER_N` 形式を
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
    """文字起こしテキストを LLM 入力用に整形する。
    Whisper 形式を検出した場合は [HH:MM:SS] SPEAKER_N: text 形式に変換。
    Returns: (transcript_text, is_whisper_format)
    """
    segments = parse_whisper_transcript(raw_text)
    if segments:
        return format_whisper_transcript(segments), True
    return raw_text, False
