#!/usr/bin/env python3
"""
vtt_speaker_merge_test.py — Whisper combined + Zoom VTT 話者マッピング統合テスト

Whisper の高品質な要約テキスト（combined）に、Zoom VTT の正確な話者帰属情報を
付加して LLM に渡し、担当者の特定精度を向上させる。

アプローチ:
  1. VTT をパースし、時間帯ごとの話者発言マップを構築
  2. combined テキストのパート（=== 第N部 ===）に対応する時間帯の話者情報を付加
  3. 話者帰属付き combined を Stage 2（決定事項・AI抽出）に渡す
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_utils import (
    load_claude_md_context, call_argus_llm,
    parse_vtt, get_speaker_summary, get_speaker_timeline,
    build_speaker_map, _COMBINED_PART_RE,
)


def parse_combined_parts(combined_path: str) -> list[dict]:
    """combined テキストを === 第N部（HH:MM:SS〜HH:MM:SS）=== で分割。"""
    content = Path(combined_path).read_text(encoding="utf-8")
    parts = []
    for m in _COMBINED_PART_RE.finditer(content):
        idx, start, end, text = m.groups()
        parts.append({
            "idx": int(idx),
            "start": start,
            "end": end,
            "text": text.strip(),
        })
    return parts


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
MERGE_PROMPT = """\
以下は日本語会議の要約です。各パートには Zoom 自動文字起こしから取得した **正確な話者名** が付加されています。

## あなたのタスク
決定事項とアクションアイテムを抽出してください。

## 担当者特定の手順（重要）
アクションアイテムの担当者を決定する際は、以下の手がかりを **すべて** 活用してください:

1. **直接的な指名**: 要約中で「〜さんがやる」「〜が担当」と明記されている場合
2. **発言者＝引き受け**: 話者情報で特定の人物が「私がやります」「対応します」「入力します」「作成します」と発言している場合、その人が担当者
3. **依頼＋応答パターン**: Aさんが「〜をお願い」→ Bさんが応答している場合、Bが担当者
4. **議題の提案者**: 特定の話者がある議題について詳しく説明・主導している場合、その人が担当者である可能性が高い
5. **役職からの推定**: 参加者リストの役職・責任範囲から担当者を推定できる場合

「（未定）」は上記すべてを検討しても担当者が特定できない場合のみ使用してください。

## 話者名の正規化
VTT の話者名（英語表記）は以下のように参加者リストの日本語名に変換してください:
{speaker_map}

## ルール
1. すべて日本語で出力
2. 「## 決定事項」から開始（前置きなし）
3. 決定事項: プロジェクトのスコープ・期限・方針に影響する重要な決定のみ（3〜7件）
4. アクションアイテム: 特定の担当者に明示的に割り当てられたタスクのみ
5. 担当者名は参加者リストの日本語表記に正規化すること
6. 期限は文中の表現そのまま使用（推測しない）。期限不明なら「（未定）」
7. タスク内容は2〜3文（何を・なぜ・期待される成果）

## 出力形式（厳密に従うこと。列数は3列のみ）

## 決定事項

- （決定事項）

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| 山田 太郎 | タスクの説明。2〜3文で記載。 | 5月7日 |

## 参加者リスト
{context}

## 会議要約（話者情報付き）
{summaries}
"""


def main():
    parser = argparse.ArgumentParser(
        description="Whisper combined + Zoom VTT 話者マッピング統合テスト"
    )
    parser.add_argument("combined_file", help="Whisper combined テキストファイル")
    parser.add_argument("vtt_file", help="Zoom VTT ファイル")
    parser.add_argument("--output", "-o", help="出力ファイルパス")
    args = parser.parse_args()

    print(f"[INFO] combined 読み込み: {args.combined_file}")
    parts = parse_combined_parts(args.combined_file)
    print(f"[INFO] {len(parts)} パート検出")

    print(f"[INFO] VTT 読み込み: {args.vtt_file}")
    vtt_segments = parse_vtt(args.vtt_file)
    speakers = sorted(set(s["speaker"] for s in vtt_segments))
    print(f"[INFO] {len(vtt_segments)} セグメント, 話者: {', '.join(speakers)}")

    context = load_claude_md_context()

    speaker_map = build_speaker_map(speakers, context)
    print(f"[INFO] 話者マッピング:\n{speaker_map}")

    # 各パートに話者情報を付加
    enriched_parts = []
    for part in parts:
        speaker_summary = get_speaker_summary(
            vtt_segments, part["start"], part["end"]
        )
        timeline = get_speaker_timeline(
            vtt_segments, part["start"], part["end"]
        )

        enriched = f"=== 第{part['idx']}部（{part['start']}〜{part['end']}）===\n"
        if timeline:
            enriched += f"\n【この時間帯の発言者（時系列）】\n{timeline}\n"
        if speaker_summary:
            enriched += f"\n【話者別の発言概要（Zoom文字起こしより）】\n{speaker_summary}\n"
        enriched += f"\n【要約】\n{part['text']}"
        enriched_parts.append(enriched)

        print(f"  パート {part['idx']}: {part['start']}〜{part['end']}, "
              f"話者数={len(speaker_summary.splitlines()) if speaker_summary else 0}")

    merged = "\n\n---\n\n".join(enriched_parts)
    print(f"\n[INFO] 統合テキスト: {len(merged)} 字")

    # LLM に渡して抽出
    print(f"\n[Stage 2] 決定事項・アクションアイテム抽出中...")
    prompt = MERGE_PROMPT.format(
        speaker_map=speaker_map,
        context=context[:8000],
        summaries=merged,
    )
    result = call_argus_llm(prompt, timeout=120, max_tokens=4096)

    # 比較用: VTTなし（combined のみ）でも抽出
    print(f"\n[比較] VTTなし（combined のみ）で抽出中...")
    baseline_prompt = MERGE_PROMPT.format(
        speaker_map="（VTT話者情報なし）",
        context=context[:8000],
        summaries="\n\n---\n\n".join(
            f"=== 第{p['idx']}部（{p['start']}〜{p['end']}）===\n{p['text']}"
            for p in parts
        ),
    )
    baseline_result = call_argus_llm(baseline_prompt, timeout=120, max_tokens=4096)

    output = (
        f"# Whisper + Zoom VTT 統合抽出テスト\n\n"
        f"combined: {args.combined_file}\n"
        f"VTT: {args.vtt_file}\n"
        f"話者: {', '.join(speakers)}\n\n"
        f"---\n\n"
        f"# A. VTT話者情報あり（統合版）\n\n{result}\n\n"
        f"---\n\n"
        f"# B. VTTなし（Whisper combinedのみ、ベースライン）\n\n{baseline_result}\n"
    )

    print("\n" + "=" * 60)
    print(output)
    print("=" * 60)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\n[INFO] 出力を保存: {args.output}")


if __name__ == "__main__":
    main()
