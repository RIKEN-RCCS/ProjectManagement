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


# --------------------------------------------------------------------------- #
# Zoom VTT パース・話者分析
# --------------------------------------------------------------------------- #

def parse_vtt(vtt_path: str) -> list[dict]:
    """Zoom VTT ファイルをパースして発言セグメントのリストを返す。"""
    content = Path(vtt_path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"(\d+)\n"
        r"(\d{2}:\d{2}:\d{2})\.\d{3}\s*-->\s*(\d{2}:\d{2}:\d{2})\.\d{3}\n"
        r"(.+?)(?=\n\n|\n\d+\n|\Z)",
        re.DOTALL,
    )
    segments = []
    for m in pattern.finditer(content):
        _, start, end, text = m.groups()
        text = text.strip()
        if not text:
            continue
        speaker_match = re.match(r"^(.+?):\s*(.*)$", text, re.DOTALL)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            utterance = speaker_match.group(2).strip()
        else:
            speaker = "Unknown"
            utterance = text
        segments.append({
            "speaker": speaker,
            "start": start,
            "end": end,
            "text": utterance,
        })
    return segments


def _ts_to_sec(ts: str) -> int:
    """HH:MM:SS → 秒数"""
    parts = ts.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def get_speaker_timeline(vtt_segments: list[dict], start_ts: str, end_ts: str) -> str:
    """指定時間帯の話者発言を時系列で要約（誰がどの順番で話したか）。"""
    start_sec = _ts_to_sec(start_ts)
    end_sec = _ts_to_sec(end_ts)
    timeline = []
    prev_speaker = None
    for seg in vtt_segments:
        seg_start = _ts_to_sec(seg["start"])
        if start_sec <= seg_start < end_sec:
            if seg["speaker"] != prev_speaker:
                timeline.append(f"[{seg['start']}] {seg['speaker']}")
                prev_speaker = seg["speaker"]
    return "\n".join(timeline)


def get_speaker_summary(vtt_segments: list[dict], start_ts: str, end_ts: str) -> str:
    """指定時間帯の VTT セグメントから、話者ごとの発言概要を生成。"""
    start_sec = _ts_to_sec(start_ts)
    end_sec = _ts_to_sec(end_ts)
    speaker_utterances: dict[str, list[str]] = {}
    for seg in vtt_segments:
        seg_start = _ts_to_sec(seg["start"])
        if start_sec <= seg_start < end_sec:
            speaker = seg["speaker"]
            if speaker not in speaker_utterances:
                speaker_utterances[speaker] = []
            speaker_utterances[speaker].append(seg["text"])
    if not speaker_utterances:
        return ""
    lines = []
    for speaker, utterances in speaker_utterances.items():
        combined = " ".join(utterances)
        if len(combined) > 500:
            combined = combined[:500] + "..."
        lines.append(f"- {speaker}: {combined}")
    return "\n".join(lines)


def build_speaker_map(speakers: list[str], context: str) -> str:
    """VTT英語名 → 参加者リスト日本語名の対応表を生成。"""
    import unicodedata

    def normalize(s: str) -> str:
        return unicodedata.normalize("NFKC", s).lower().strip()

    lines = context.splitlines()
    name_entries: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip().lstrip("- ")
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            for i, p in enumerate(parts):
                if re.match(r"[A-Z][a-z]", p) and i > 0:
                    ja_name = " ".join(parts[:i])
                    en_tokens = []
                    for j in range(i, len(parts)):
                        if re.match(r"[a-zA-Z]", parts[j]):
                            en_tokens.append(parts[j])
                        else:
                            break
                    en_name = " ".join(en_tokens)
                    if en_name and ja_name:
                        name_entries.append((en_name, ja_name))
                    break

    mapping = []
    for vtt_speaker in speakers:
        vtt_norm = normalize(vtt_speaker)
        vtt_norm = re.sub(r"\s*\(.*?\)\s*", " ", vtt_norm).strip()
        best_match = None
        for en_name, ja_name in name_entries:
            en_norm = normalize(en_name)
            en_parts = en_norm.split()
            vtt_parts = vtt_norm.split()
            if any(ep in vtt_parts for ep in en_parts):
                best_match = ja_name
                break
        if best_match:
            mapping.append(f"- {vtt_speaker} → {best_match}")
        else:
            mapping.append(f"- {vtt_speaker} → （参加者リストから特定してください）")
    return "\n".join(mapping)


_COMBINED_PART_RE = re.compile(
    r"===\s*第(\d+)部（(\d{2}:\d{2}:\d{2})〜(\d{2}:\d{2}:\d{2})）\s*===\n(.*?)(?====\s*第|\Z)",
    re.DOTALL,
)


def enrich_combined_with_vtt(
    combined_text: str, vtt_path: str, claude_md_context: str
) -> tuple[str, str]:
    """combined テキストに VTT 話者情報を付加する。

    Returns:
        (enriched_text, speaker_map_text)
    """
    vtt_segments = parse_vtt(vtt_path)
    if not vtt_segments:
        return combined_text, ""

    speakers = sorted(set(s["speaker"] for s in vtt_segments))
    speaker_map = build_speaker_map(speakers, claude_md_context)

    parts = list(_COMBINED_PART_RE.finditer(combined_text))
    if not parts:
        all_starts = [_ts_to_sec(s["start"]) for s in vtt_segments]
        all_ends = [_ts_to_sec(s["end"]) for s in vtt_segments]
        global_start = f"{min(all_starts)//3600:02d}:{(min(all_starts)%3600)//60:02d}:{min(all_starts)%60:02d}"
        global_end = f"{max(all_ends)//3600:02d}:{(max(all_ends)%3600)//60:02d}:{max(all_ends)%60:02d}"
        timeline = get_speaker_timeline(vtt_segments, global_start, global_end)
        summary = get_speaker_summary(vtt_segments, global_start, global_end)
        header = ""
        if timeline:
            header += f"【発言者（時系列）】\n{timeline}\n\n"
        if summary:
            header += f"【話者別の発言概要（Zoom文字起こしより）】\n{summary}\n\n"
        return header + combined_text, speaker_map

    enriched_parts = []
    last_end = 0
    for m in parts:
        if m.start() > last_end:
            enriched_parts.append(combined_text[last_end:m.start()])
        idx, start_ts, end_ts, text = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        timeline = get_speaker_timeline(vtt_segments, start_ts, end_ts)
        summary = get_speaker_summary(vtt_segments, start_ts, end_ts)
        enriched = f"=== 第{idx}部（{start_ts}〜{end_ts}）===\n"
        if timeline:
            enriched += f"\n【この時間帯の発言者（時系列）】\n{timeline}\n"
        if summary:
            enriched += f"\n【話者別の発言概要（Zoom文字起こしより）】\n{summary}\n"
        enriched += f"\n{text}"
        enriched_parts.append(enriched)
        last_end = m.end()

    if last_end < len(combined_text):
        enriched_parts.append(combined_text[last_end:])

    return "".join(enriched_parts), speaker_map
