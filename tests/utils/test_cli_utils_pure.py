"""Pure-function tests for cli_utils (Whisper parsing, CoT removal)."""
from utils.transcript import (
    _parse_timestamp,
    format_whisper_transcript,
    parse_whisper_transcript,
    prepare_transcript,
)
from utils.llm import strip_think_blocks


# --------------------------------------------------------------------------- #
# _parse_timestamp
# --------------------------------------------------------------------------- #


def test_parse_timestamp_hms():
    assert _parse_timestamp("01:02:03") == 1 * 3600 + 2 * 60 + 3


def test_parse_timestamp_zero():
    assert _parse_timestamp("00:00:00") == 0


def test_parse_timestamp_whitespace():
    assert _parse_timestamp("  00:01:00  ") == 60


# --------------------------------------------------------------------------- #
# parse_whisper_transcript
# --------------------------------------------------------------------------- #


SAMPLE_TRANSCRIPT = """#### [00:00:00 - 00:00:05] SPEAKER_00
こんにちは

#### [00:00:05 - 00:00:10] SPEAKER_01
こんばんは
"""


def test_parse_whisper_two_segments():
    segs = parse_whisper_transcript(SAMPLE_TRANSCRIPT)
    assert len(segs) == 2
    assert segs[0]["speaker"] == "SPEAKER_00"
    assert segs[0]["text"] == "こんにちは"
    assert segs[0]["start"] == 0
    assert segs[0]["end"] == 5
    assert segs[1]["speaker"] == "SPEAKER_01"


def test_parse_whisper_empty_returns_empty():
    assert parse_whisper_transcript("") == []
    assert parse_whisper_transcript("not whisper format") == []


def test_parse_whisper_skips_ellipsis_text():
    transcript = """#### [00:00:00 - 00:00:05] SPEAKER_00
...

#### [00:00:05 - 00:00:10] SPEAKER_01
本当のテキスト
"""
    segs = parse_whisper_transcript(transcript)
    # The "..." segment is skipped (text in ("...", "…"))
    assert len(segs) == 1
    assert segs[0]["text"] == "本当のテキスト"


# --------------------------------------------------------------------------- #
# format_whisper_transcript
# --------------------------------------------------------------------------- #


def test_format_whisper_transcript_basic():
    segs = [
        {"speaker": "SPEAKER_00", "start": 3723, "text": "test"},
    ]
    out = format_whisper_transcript(segs)
    # 3723 = 1:02:03
    assert out == "[01:02:03] SPEAKER_00: test"


def test_format_whisper_transcript_empty():
    assert format_whisper_transcript([]) == ""


def test_format_whisper_transcript_multi_segments():
    segs = [
        {"speaker": "SPEAKER_00", "start": 0, "text": "a"},
        {"speaker": "SPEAKER_01", "start": 60, "text": "b"},
    ]
    out = format_whisper_transcript(segs)
    assert "[00:00:00] SPEAKER_00: a" in out
    assert "[00:01:00] SPEAKER_01: b" in out


# --------------------------------------------------------------------------- #
# prepare_transcript
# --------------------------------------------------------------------------- #


def test_prepare_transcript_whisper_detected():
    text, is_whisper = prepare_transcript(SAMPLE_TRANSCRIPT)
    assert is_whisper is True
    assert "[00:00:00] SPEAKER_00: こんにちは" in text


def test_prepare_transcript_non_whisper_passthrough():
    raw = "This is a plain transcript without Whisper format markers."
    text, is_whisper = prepare_transcript(raw)
    assert is_whisper is False
    assert text == raw


# --------------------------------------------------------------------------- #
# strip_think_blocks
# --------------------------------------------------------------------------- #


def test_strip_think_blocks_removes_think_tag():
    text = "外の本文"
    assert strip_think_blocks(text) == "外の本文"


def test_strip_think_blocks_unclosed_returns_empty():
    # <think> present but </think> absent (max_tokens truncation) → return ""
    text = "<think>推論過程のみ"
    assert strip_think_blocks(text) == ""


def test_strip_think_blocks_japanese_passthrough():
    text = "これは日本語の本文です。"
    out = strip_think_blocks(text)
    assert "日本語の本文" in out


def test_strip_think_blocks_mixed():
    text = "thinking content here 結論はこれです"
    out = strip_think_blocks(text)
    assert "結論はこれです" in out
    # English CoT prefix "thinking content here " is NOT japanese →
    # the second pattern kicks in, keeps from first JP line
