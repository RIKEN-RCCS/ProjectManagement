"""reconcile_transcript.py — VTT × Whisper × LLM 三方突合による文字起こし修正

VTT と Whisper のトランスクリプトを時間軸で突合し、LLM が文脈・用語辞書・
スライドOCR を見ながら誤認識を能動的に修正する。

主な修正対象:
- 固有名詞の誤認識（カタカナ転写 → 正式綴り）
- 話者帰属（SPEAKER_XX → 実名、VTT ラベル優先）
- 片方にしかない発言セグメントの補完

出力: 統合・修正済みトランスクリプト（タイムスタンプ + 実名話者 + 修正済みテキスト）

使い方（スクリプト単体）:
  python reconcile_transcript.py TRANSCRIPT_PATH --vtt VTT_PATH [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from utils.transcript import _ts_to_sec, parse_vtt, parse_whisper_transcript

# セグメント突合の許容タイムずれ（秒）— VTT と Whisper のタイムスタンプ誤差を吸収
_TIME_TOLERANCE_SEC = 10

# 1 チャンクあたりの最大秒数（LLM のコンテキスト長を考慮）
_CHUNK_SEC = 300  # 5 分


_RECONCILE_PROMPT = """\
あなたは日本語の会議文字起こしの修正担当者です。
2つのASR（自動音声認識）システムの出力と補助情報を使って、正確な文字起こしを再構築してください。

【重要な原則】
- VTT も Whisper も誤認識を含む可能性があります。どちらが正しいかを文脈・用語辞書・スライドで判断してください
- 固有名詞は「用語辞書」と「スライドOCR」を ground truth として優先してください
- 話者帰属は VTT のラベルを優先し、VTT に対応セグメントがない場合は参加者リストから推測してください
- 情報を創作しないでください。両者の情報から推定できる範囲に留めてください

【修正対象の典型的な誤認識パターン】
- カタカナ転写: 「ファントホローブルー」→ FrontFlow/Blue, 「サーモン」→ SALMON, 「スケールエルティーケーエフ」→ SCALE-LETKF, 「ジェネシス」→ GENESIS
- 略語の部分転写: 「エルキューシーディー」→ LQCD-DWF-HMC
- 人名の聞き誤り: 参加者リストで確認してください

## 用語辞書
{terminology}

## スライドOCR（グランドトゥルース）
{slide_context}

## 参加者リスト（話者同定の参照）
{claude_md_context}

## Whisper 文字起こし（{time_range}）
{whisper_text}

## Zoom VTT 文字起こし（{time_range}）
{vtt_text}

---
上記の2つのASR出力を突合し、修正済みの文字起こしを出力してください。

出力形式（各発言を以下の形式で）:
[HH:MM:SS] 話者名: 発言内容

ルール:
1. タイムスタンプは Whisper のものを基準とし、VTT で補完する
2. 話者名は VTT に記載の実名を使用、VTT に対応なければ参加者リストから推測、不明なら「不明」
3. 発言内容は用語辞書・スライドを参照して誤認識を修正する
4. 両方のASRに発言があれば内容を統合（より詳細・正確な方を選ぶ）
5. 片方にしかない発言は、それが妥当なら採用する
6. SPEAKER_00, SPEAKER_01 等の匿名ラベルを出力に含めないこと
7. 出力は修正済み発言のみ。説明や補足は不要

修正済み文字起こし:
"""


def _sec_to_hms(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_whisper_to_timed(text: str) -> list[dict]:
    """Whisper 出力を [{start_sec, end_sec, speaker, text}] に変換。"""
    segments = parse_whisper_transcript(text)
    return segments  # already has start/end as int seconds


def _vtt_in_range(vtt_segments: list[dict], start_sec: int, end_sec: int) -> list[dict]:
    """VTT セグメントのうち [start_sec, end_sec) に属するものを返す。"""
    result = []
    for seg in vtt_segments:
        seg_start = _ts_to_sec(seg["start"])
        if start_sec <= seg_start < end_sec:
            result.append(seg)
    return result


def _whisper_in_range(whisper_segs: list[dict], start_sec: int, end_sec: int) -> list[dict]:
    return [s for s in whisper_segs if start_sec <= s["start"] < end_sec]


def _format_whisper_chunk(segs: list[dict]) -> str:
    lines = []
    for s in segs:
        ts = _sec_to_hms(s["start"])
        lines.append(f"[{ts}] {s['speaker']}: {s['text']}")
    return "\n".join(lines) if lines else "（この時間帯の Whisper セグメントなし）"


def _format_vtt_chunk(segs: list[dict]) -> str:
    lines = []
    for s in segs:
        lines.append(f"[{s['start']}] {s['speaker']}: {s['text']}")
    return "\n".join(lines) if lines else "（この時間帯の VTT セグメントなし）"


def _build_terminology_text(meeting_kind: str | None = None) -> str:
    """terminology.py から用語辞書テキストを生成。DB が無ければ空文字。"""
    try:
        from utils.terminology import load_all_terms
        terms = load_all_terms()
        if not terms:
            return "（用語辞書なし）"
        lines = []
        for t in terms[:60]:  # LLM コンテキスト節約
            line = f"- 正式形: {t['term']}"
            if t.get("aliases"):
                try:
                    aliases = json.loads(t["aliases"]) if isinstance(t["aliases"], str) else t["aliases"]
                    if aliases:
                        line += f"  ← 誤認識例: {', '.join(aliases)}"
                except Exception:
                    pass
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return "（用語辞書読み込み失敗）"


def reconcile_chunk(
    whisper_segs: list[dict],
    vtt_segs: list[dict],
    start_sec: int,
    end_sec: int,
    claude_md_context: str,
    terminology_text: str,
    slide_context: str,
    timeout: int = 480,
) -> str:
    """1 チャンクを LLM で突合修正し、修正済みテキストを返す。"""
    time_range = f"{_sec_to_hms(start_sec)}〜{_sec_to_hms(end_sec)}"
    whisper_chunk = _whisper_in_range(whisper_segs, start_sec, end_sec)
    vtt_chunk = _vtt_in_range(vtt_segs, start_sec, end_sec)

    # 両方空のチャンクはスキップ
    if not whisper_chunk and not vtt_chunk:
        return ""

    whisper_text = _format_whisper_chunk(whisper_chunk)
    vtt_text = _format_vtt_chunk(vtt_chunk)

    prompt = _RECONCILE_PROMPT.format(
        terminology=terminology_text,
        slide_context=slide_context or "（スライドOCR なし）",
        claude_md_context=claude_md_context,
        time_range=time_range,
        whisper_text=whisper_text,
        vtt_text=vtt_text,
    )

    result = call_argus_llm(
        prompt,
        timeout=timeout,
        max_tokens=2048,
        system="あなたは日本語会議の文字起こし修正専門家です。指定された出力形式を厳守してください。",
    )
    return result.strip()


def reconcile_transcript(
    transcript_path: str | Path,
    vtt_path: str | Path,
    output_path: str | Path | None = None,
    slide_context: str = "",
    meeting_kind: str | None = None,
    chunk_sec: int = _CHUNK_SEC,
    timeout: int = 480,
    verbose: bool = True,
) -> Path:
    """Whisper トランスクリプト全体を VTT と突合修正して保存する。

    Parameters
    ----------
    transcript_path : Whisper 出力の MD ファイルパス
    vtt_path        : Zoom VTT ファイルパス
    output_path     : 出力パス（省略時: transcript_path の stem + _reconciled.txt）
    slide_context   : スライドOCR テキスト
    meeting_kind    : 用語辞書フィルタ用の会議種別
    chunk_sec       : 1 チャンクあたりの秒数（デフォルト 300 = 5 分）
    timeout         : LLM 呼び出しタイムアウト（秒）

    Returns
    -------
    Path: 出力ファイルのパス
    """
    transcript_path = Path(transcript_path)
    vtt_path = Path(vtt_path)

    if not transcript_path.exists():
        raise FileNotFoundError(f"Whisper トランスクリプトが見つかりません: {transcript_path}")
    if not vtt_path.exists():
        raise FileNotFoundError(f"VTT ファイルが見つかりません: {vtt_path}")

    if output_path is None:
        output_path = transcript_path.parent / (transcript_path.stem + "_reconciled.txt")
    output_path = Path(output_path)

    raw_text = transcript_path.read_text(encoding="utf-8")
    whisper_segs = _parse_whisper_to_timed(raw_text)
    vtt_segs = parse_vtt(str(vtt_path))

    if not whisper_segs:
        if verbose:
            print("[WARN] reconcile: Whisper セグメントが検出されませんでした。元のファイルをそのまま使用します。")
        import shutil
        shutil.copy(transcript_path, output_path)
        return output_path

    claude_md_context = load_claude_md_context()
    # glossary 構造化テキストを追記
    try:
        from utils.glossary import build_reference as build_glossary_ref
        glossary_ref = build_glossary_ref()
        if glossary_ref:
            claude_md_context = claude_md_context + glossary_ref
    except Exception:
        pass
    terminology_text = _build_terminology_text(meeting_kind)

    # 全体の時間範囲
    total_start = whisper_segs[0]["start"]
    total_end = max(s["end"] for s in whisper_segs)
    if vtt_segs:
        vtt_end = _ts_to_sec(vtt_segs[-1]["end"])
        total_end = max(total_end, vtt_end)

    # チャンク分割して並列 LLM 修正
    chunks_output: list[str] = []
    current = total_start
    chunk_idx = 0
    while current < total_end:
        chunk_end = min(current + chunk_sec, total_end + 1)
        chunk_idx += 1
        time_range = f"{_sec_to_hms(current)}〜{_sec_to_hms(chunk_end)}"
        if verbose:
            print(f"[INFO] reconcile チャンク {chunk_idx}: {time_range}")

        result = reconcile_chunk(
            whisper_segs=whisper_segs,
            vtt_segs=vtt_segs,
            start_sec=current,
            end_sec=chunk_end,
            claude_md_context=claude_md_context,
            terminology_text=terminology_text,
            slide_context=slide_context,
            timeout=timeout,
        )
        if result:
            chunks_output.append(result)
        current = chunk_end

    final_text = "\n\n".join(chunks_output)
    if not final_text.strip():
        if verbose:
            print("[WARN] reconcile: 全チャンクが空文字を返しました。元のファイルをそのまま使用します。")
        import shutil
        shutil.copy(transcript_path, output_path)
        return output_path
    output_path.write_text(final_text, encoding="utf-8")
    if verbose:
        print(f"[INFO] reconcile 完了: {output_path} ({len(final_text)} 字)")
    return output_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VTT × Whisper 三方突合修正")
    parser.add_argument("transcript", help="Whisper 出力の MD ファイルパス")
    parser.add_argument("--vtt", required=True, help="Zoom VTT ファイルパス")
    parser.add_argument("--output", help="出力ファイルパス（省略時: {stem}_reconciled.txt）")
    parser.add_argument("--slide-context", help="スライドOCR テキストファイルパス")
    parser.add_argument("--meeting-kind", help="会議種別（用語辞書フィルタ用）")
    parser.add_argument("--chunk-sec", type=int, default=_CHUNK_SEC, help=f"チャンク秒数（デフォルト: {_CHUNK_SEC}）")
    parser.add_argument("--timeout", type=int, default=480, help="LLM タイムアウト秒数")
    args = parser.parse_args()

    slide_ctx = ""
    if args.slide_context and Path(args.slide_context).exists():
        slide_ctx = Path(args.slide_context).read_text(encoding="utf-8")

    out = reconcile_transcript(
        transcript_path=args.transcript,
        vtt_path=args.vtt,
        output_path=args.output,
        slide_context=slide_ctx,
        meeting_kind=args.meeting_kind,
        chunk_sec=args.chunk_sec,
        timeout=args.timeout,
    )
    print(f"[完了] {out}")
