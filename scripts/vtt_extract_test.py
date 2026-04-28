#!/usr/bin/env python3
"""
vtt_extract_test.py — Zoom VTT から決定事項・アクションアイテムを抽出するテスト

Zoom の文字起こし VTT（話者名が正確）を LLM に直接渡し、
Whisper 経由の generate_minutes_local.py と比較するための実験スクリプト。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_utils import load_claude_md_context, call_argus_llm, parse_vtt


def format_for_llm(segments: list[dict]) -> str:
    """セグメントを LLM 入力用テキストに整形。"""
    lines = []
    for seg in segments:
        ts = seg["start"][:8]  # HH:MM:SS
        lines.append(f"[{ts}] {seg['speaker']}: {seg['text']}")
    return "\n\n".join(lines)


def chunk_segments(segments: list[dict], chunk_minutes: int = 30) -> list[list[dict]]:
    """時間ウィンドウごとにチャンク分割。"""
    def ts_to_sec(ts: str) -> float:
        parts = ts.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s

    if not segments:
        return []
    start_time = ts_to_sec(segments[0]["start"])
    chunk_dur = chunk_minutes * 60
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for seg in segments:
        if ts_to_sec(seg["start"]) >= start_time + chunk_dur * (len(chunks) + 1):
            if current:
                chunks.append(current)
                current = []
        current.append(seg)
    if current:
        chunks.append(current)
    return chunks


CHUNK_PROMPT = """\
以下は日本語の会議の文字起こし（Zoom自動文字起こし）のパート{idx}/{total}（{time_range}）です。
話者名は正確ですが、音声認識の精度が低いため、文言に誤りや不自然な箇所があります。

タスク: このパートで議論された内容の詳細な日本語要約を書いてください。

ルール:
1. 技術用語の修正: 以下のプロジェクト用語リストを参照し、音声認識の誤りを修正してください
2. 話者の発言内容・提案・懸念・合意事項をすべてカバーしてください
3. 「誰が何を言ったか」を明記してください（話者名付き）
4. 文字起こしにない内容を追加しないでください
5. 自然な日本語の散文で出力（箇条書き・見出しなし）

## プロジェクト用語リスト
{context}

## 文字起こし（{time_range}）
{chunk_text}

詳細な日本語要約（800〜1200字）を書いてください:
"""


DECISIONS_PROMPT = """\
以下は日本語会議の要約です。ここから決定事項とアクションアイテムを抽出してください。

ルール:
1. すべて日本語で出力
2. 「## 決定事項」から開始（前置きなし）
3. 決定事項: プロジェクトのスコープ・期限・方針に影響する重要な決定のみ（3〜7件）
4. アクションアイテム: 特定の担当者に明示的に割り当てられたタスクのみ
5. 担当者名は以下の参加者リストで正規化。担当者不明なら「（未定）」
6. 期限は文中の表現そのまま使用（推測しない）。期限不明なら「（未定）」
7. タスク内容は2〜3文（何を・なぜ・期待される成果）

出力形式:
## 決定事項

- （決定事項）

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| （名前） | （2〜3文） | （期限） |

## 参加者リスト
{context}

## 会議要約
{summaries}
"""


def main():
    parser = argparse.ArgumentParser(description="Zoom VTT → 決定事項・AI 抽出テスト")
    parser.add_argument("vtt_file", help="Zoom VTT ファイルパス")
    parser.add_argument("--chunk-minutes", type=int, default=30, help="チャンク分割の分数")
    parser.add_argument("--output", "-o", help="出力ファイルパス")
    args = parser.parse_args()

    print(f"[INFO] VTT ファイル読み込み: {args.vtt_file}")
    segments = parse_vtt(args.vtt_file)
    print(f"[INFO] {len(segments)} セグメント検出")

    speakers = sorted(set(s["speaker"] for s in segments))
    print(f"[INFO] 話者: {', '.join(speakers)}")

    context = load_claude_md_context()

    chunks = chunk_segments(segments, args.chunk_minutes)
    print(f"[INFO] {len(chunks)} チャンクに分割")

    # Stage 1: チャンクごとの要約
    summaries = []
    for i, chunk in enumerate(chunks):
        chunk_text = format_for_llm(chunk)
        time_range = f"{chunk[0]['start'][:8]} - {chunk[-1]['end'][:8]}"
        print(f"\n[Stage 1] チャンク {i+1}/{len(chunks)} ({time_range}) を処理中...")

        prompt = CHUNK_PROMPT.format(
            idx=i + 1,
            total=len(chunks),
            time_range=time_range,
            context=context[:8000],
            chunk_text=chunk_text,
        )
        result = call_argus_llm(prompt, timeout=120, max_tokens=2048)
        summaries.append(result)
        print(f"  → {len(result)} 字")

    combined = "\n\n---\n\n".join(summaries)
    print(f"\n[INFO] 全チャンク要約: {len(combined)} 字")

    # Stage 2: 決定事項・AI 抽出
    print(f"\n[Stage 2] 決定事項・アクションアイテム抽出中...")
    prompt = DECISIONS_PROMPT.format(
        context=context[:8000],
        summaries=combined,
    )
    result = call_argus_llm(prompt, timeout=120, max_tokens=4096)

    output = f"# Zoom VTT 抽出結果\n\nソース: {args.vtt_file}\n話者: {', '.join(speakers)}\n\n{result}"

    print("\n" + "=" * 60)
    print(output)
    print("=" * 60)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\n[INFO] 出力を保存: {args.output}")


if __name__ == "__main__":
    main()
