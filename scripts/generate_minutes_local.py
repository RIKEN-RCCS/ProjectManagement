#!/usr/bin/env python3
"""
generate_minutes_local.py

Whisper生成の文字起こしMarkdownからローカルLLMを使って議事録を生成する。

generate_minutes.py のローカルLLM版。
Claude CLIが自動で読み込むCLAUDE.mdのプロジェクト文脈を明示的にプロンプトに埋め込む。

使い方:
  python generate_minutes_local.py TRANSCRIPT_FILE --model MODEL [options]

Options:
  --model MODEL       使用するモデル名（必須）
  --think             思考モードを有効化（デフォルト: 無効）
  --output DIR        議事録の出力ディレクトリ（デフォルト: minutes）
  --url URL           ローカルLLMのURL（RIVAULT_URL 環境変数でも可）
  --token TOKEN       APIトークン（RIVAULT_TOKEN 環境変数でも可）
  --timeout SEC       LLM呼び出しタイムアウト秒数（デフォルト: 600）

認証情報の読み込み順序:
  1. --url / --token 引数
  2. RIVAULT_URL / RIVAULT_TOKEN 環境変数
  3. ~/.secrets/rivault_tokens.sh の内容をパース
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
PROJECT_MD = REPO_ROOT / "docs" / "project.md"

sys.path.insert(0, str(SCRIPT_DIR))
from cli_utils import (
    strip_think_blocks, call_local_llm, load_claude_md_context, detect_vllm_model,
    enrich_combined_with_vtt,
)


# --------------------------------------------------------------------------- #
# プロンプトテンプレート
# --------------------------------------------------------------------------- #
PROMPT_TEMPLATE = """\
You are writing the 議事内容 (meeting content) section of Japanese meeting minutes.

Below are sequential summaries of each segment of the meeting.
Organize them into 6-8 thematic sections.

RULES:
1. Output must be entirely in Japanese
2. Focus on WHAT was discussed — topics, proposals, numbers, conclusions
3. Each section: 3-5 paragraphs, each paragraph 3-4 sentences (approx. 120-180 chars)
4. Separate paragraphs with a blank line
5. Avoid padding and repetition, but include background context, raised concerns, and rationale behind decisions
6. Section titles: concise Japanese noun phrases reflecting the actual topic discussed (15 characters or less)
7. NO speaker attribution ("〜さんが言った", "SPEAKER_XX が" etc.)
8. Do NOT insert horizontal rules (---) between sections
9. Actively correct speech recognition errors: cross-reference ALL technical terms, application names, and project-specific words against the Project Terminology Reference. Replace phonetic approximations or garbled terms with the correct form (e.g. 「ファントホローブルー」→ FrontFlow/Blue, 「サーモン」→ SALMON, 「スケールTKF」→ SCALE-LETKF).
10. Preserve exact numbers and dates as written in the summaries
11. Begin output immediately with "## 議事内容" — no preamble, no other sections
CRITICAL: The strings "SPEAKER_00", "SPEAKER_01", "SPEAKER_02", etc. must NEVER appear in your output.

Output format:
## 議事内容

### （タイトル）

（1つ目の段落。2〜3文。）

（2つ目の段落。2〜3文。）

### （タイトル）

（1つ目の段落。2〜3文。）

（2つ目の段落。2〜3文。）

(6-8 sections total, covering the entire meeting)

## Project Terminology Reference
{claude_md_context}

## Meeting Segment Summaries
{transcript}
"""


DECISIONS_TEMPLATE = """\
You are extracting decisions and action items from Japanese meeting summaries.

RULES:
1. Output entirely in Japanese
2. Begin immediately with "## 決定事項" — no preamble, no thinking text
3. Dates/deadlines: use exactly as written in the summaries (e.g. "26日", "来週月曜日") — do NOT expand or infer months/years
4. Person names: normalize using the Participant List below; if no clear assignee, write "（未定）"
5. Actively correct speech recognition errors: replace phonetic approximations with correct terms from the Participant List and Project Terminology Reference (e.g. 「ファントホローブルー」→ FrontFlow/Blue, 「サーモン」→ SALMON).
CRITICAL: "SPEAKER_00", "SPEAKER_01", "SPEAKER_02", etc. must NEVER appear in output.
{vtt_speaker_instructions}
## 決定事項 rules:
- **Core decisions only (3-7 items)**: Include ONLY decisions that materially affect the project — cancelled/rescheduled meetings, agreed policy or methodology, committed deliverables with scope/deadline.
- Ask yourself: "Would this item appear in a formal board-level summary?" If not, omit it.
- Definitely omit: routine procedural confirmations ("資料を配布する", "Slackで共有する", "アジェンダを反映する", "議事録を作成する"), minor operational steps, and restatements of deadlines that are already obvious from context.
- Markers for explicit agreement: 〜で進める / 〜に決定 / 〜することが合意 / 〜することになった / 〜が決定した
- Vary the ending naturally in Japanese (e.g. 〜することになった / 〜と合意した / 〜する方針となった / 〜をキャンセルした). Do NOT end every item with 〜が決定された.
- If no significant decisions found: write a single line "（なし）"

## アクションアイテム rules:
- List only specific tasks explicitly assigned or delegated to an identifiable person
- Do NOT infer tasks that were not explicitly assigned in the summaries
- タスク内容: Write 2-3 sentences (40-80 chars each) covering (1) what to do, (2) why it matters / background, (3) expected output. Do NOT include deadline expressions (e.g. 「26日までに」「27日までに」) — those belong in the 期限 column only.

Example — follow this style:
BAD:  「各アプリの測定結果を提出する。」
GOOD: 「未測定アプリのベンチマークを実行し性能数値を取得する。これらの数値は最終報告書に反映するため、測定結果をまとめて共有する。」

- If no clear action items found: write a single line "（なし）"

Output format (EXACTLY 3 columns — do NOT add extra columns):
## 決定事項

- （決定事項）

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| （名前または未定） | （タスクの内容・背景・成果物を2〜3文で） | （期限または未定） |

## Participant List
{claude_md_context}

## Meeting Segment Summaries
{transcript}
"""


VTT_SPEAKER_INSTRUCTIONS = """
## IMPORTANT: Speaker attribution from Zoom VTT
Each meeting segment below includes ACCURATE speaker names from Zoom's automatic transcription.
Use these speaker names to determine who is responsible for each action item.

Assignee identification heuristics (apply ALL of these):
1. **Direct assignment**: The summary says "〜さんがやる" or "〜が担当"
2. **Speaker = volunteer**: A speaker says "私がやります", "対応します", "入力します", "作成します" → that speaker is the assignee
3. **Request + response**: Person A asks "〜をお願い" → Person B responds → B is the assignee
4. **Topic lead**: A speaker explains a topic in detail or drives the discussion → likely the assignee for related tasks
5. **Role-based**: Use the Participant List roles to infer responsibility when other signals are ambiguous

Write "（未定）" ONLY when none of the above heuristics yield a name.

## Speaker name mapping (VTT English → Japanese)
{speaker_map}
"""


# --------------------------------------------------------------------------- #
# 認証情報の読み込み
# --------------------------------------------------------------------------- #
def load_rivault_tokens() -> tuple[str, str]:
    """RIVAULT_URL と RIVAULT_TOKEN を返す。読み込み順: 環境変数 → トークンファイル"""
    url = os.environ.get("RIVAULT_URL")
    token = os.environ.get("RIVAULT_TOKEN")

    if url and token:
        return url, token

    token_file = Path.home() / ".secrets" / "rivault_tokens.sh"
    if token_file.exists():
        content = token_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            m = re.match(r'^\s*export\s+RIVAULT_URL=["\']?(.*?)["\']?\s*$', line)
            if m:
                url = url or m.group(1).strip()
            m = re.match(r'^\s*export\s+RIVAULT_TOKEN=["\']?(.*?)["\']?\s*$', line)
            if m:
                token = token or m.group(1).strip()

    if not url:
        print("ERROR: RIVAULT_URL が設定されていません。", file=sys.stderr)
        print("  環境変数 RIVAULT_URL を設定するか --url で指定してください。", file=sys.stderr)
        sys.exit(1)
    if not token:
        print("ERROR: RIVAULT_TOKEN が設定されていません。", file=sys.stderr)
        print("  環境変数 RIVAULT_TOKEN を設定するか --token で指定してください。", file=sys.stderr)
        sys.exit(1)

    return url, token


# --------------------------------------------------------------------------- #
# 文字起こし解析（generate_minutes.py と同一）
# --------------------------------------------------------------------------- #
def parse_transcript(file_path: str) -> list[dict]:
    """文字起こしファイルを解析して発言セグメントのリストを返す"""
    content = Path(file_path).read_text(encoding="utf-8")

    pattern = re.compile(
        r"####\s*\[([0-9:]+)\s*-\s*([0-9:]+)\]\s+(SPEAKER_\d+)\n(.*?)(?=\n####|\Z)",
        re.DOTALL,
    )

    segments = []
    for m in pattern.finditer(content):
        start_str, end_str, speaker, text = m.groups()
        text = text.strip()
        if not text or text in ("...", "…"):
            continue
        segments.append({
            "speaker": speaker,
            "start": _parse_timestamp(start_str.strip()),
            "end": _parse_timestamp(end_str.strip()),
            "text": text,
        })
    return segments


def _parse_timestamp(time_str: str) -> int:
    """HH:MM:SS形式のタイムスタンプを秒数に変換する"""
    parts = time_str.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + int(s)
    return 0


def format_transcript(segments: list[dict]) -> str:
    """セグメントリストをLLMへの入力テキストに整形する"""
    lines = []
    for seg in segments:
        h, rem = divmod(seg["start"], 3600)
        m, s = divmod(rem, 60)
        timestamp = f"{h:02d}:{m:02d}:{s:02d}"
        lines.append(f"[{timestamp}] {seg['speaker']}: {seg['text']}")
    return "\n\n".join(lines)


def chunk_transcript(segments: list[dict], chunk_duration_sec: int = 1800) -> list[list[dict]]:
    """セグメントリストを時間ウィンドウごとに分割する"""
    if not segments:
        return []
    start_time = segments[0]["start"]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for seg in segments:
        if seg["start"] >= start_time + chunk_duration_sec * (len(chunks) + 1):
            if current:
                chunks.append(current)
                current = []
        current.append(seg)
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# チャンク抽出プロンプト（マルチステージ Stage 2 用）
# --------------------------------------------------------------------------- #
CHUNK_EXTRACTION_TEMPLATE = """\
You are processing part {chunk_idx} of {total_chunks} of a Japanese meeting transcript (time: {time_range}).

The transcript was produced by Whisper ASR and may contain misrecognized words,
unnatural phrasing, and broken sentences, especially for technical terms and proper nouns.

Your task: Write a detailed Japanese prose summary of everything discussed in this segment.

RULES:
1. CRITICAL - ASR correction: Before writing, scan the transcript for misrecognized words and replace them with the correct form from the Project Terminology below. Common ASR error patterns:
   - Phonetic katakana rendering of English terms (e.g. 「フロントフロー」「ファントホローブルー」→ FrontFlow/Blue, 「スケールTKF」「スケールエルティーケーエフ」→ SCALE-LETKF, 「サーモン」→ SALMON, 「ジェネシス」→ GENESIS, 「エルキューシーディー」→ LQCD-DWF-HMC)
   - Partial or garbled technical acronyms (e.g. 「エフエフブイ」→ FFVHC-ACE, 「ユワバミ」→ UWABAMI)
   - Person names misheard as similar-sounding names — check against the Participant List and use the correct name
2. Cover ALL topics mentioned: decisions, numbers, proposals, concerns, and action items
3. Write in natural Japanese WITHOUT speaker attribution or "SPEAKER_XX" references
4. Do NOT add content not present in the transcript
5. Output Japanese prose only (no bullet points, no headers)
CRITICAL: Never write "SPEAKER_00", "SPEAKER_01", "SPEAKER_02" or any "SPEAKER_" token in the output.

## Project Terminology
{claude_md_context}

## Transcript ({time_range})
{chunk_text}

Write a thorough Japanese prose summary (600-800 characters):
"""


def extract_from_chunk(
    chunk_text: str,
    chunk_idx: int,
    total_chunks: int,
    time_range: str,
    claude_md_context: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    think: bool = False,
    no_stream: bool = False,
    no_chat_template_kwargs: bool = False,
    temperature: Optional[float] = None,
    max_tokens: int = 4096,
) -> str:
    """1チャンクから事実を抽出する（Stage 1）"""
    prompt = CHUNK_EXTRACTION_TEMPLATE.format(
        chunk_idx=chunk_idx,
        total_chunks=total_chunks,
        time_range=time_range,
        claude_md_context=claude_md_context,
        chunk_text=chunk_text,
    )
    system = "You are a Japanese meeting minutes assistant. Output Japanese prose only, no bullet points."
    # thinking モデルは思考トークン分を考慮して max_tokens をそのまま使用
    # 非 thinking モデルは 1024 で十分
    chunk_max_tokens = max_tokens if (think or no_chat_template_kwargs) else 1024
    result = call_local_llm(
        prompt, model, base_url, api_key, timeout,
        think=think, max_tokens=chunk_max_tokens, no_stream=no_stream, system=system,
        no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
    )
    return result


# --------------------------------------------------------------------------- #
# 議事録生成
# --------------------------------------------------------------------------- #
def generate_minutes(
    transcript_path: str,
    output_dir: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    think: bool = False,
    max_tokens: int = 8192,
    multi_stage: bool = False,
    chunk_minutes: int = 30,
    no_stream: bool = False,
    no_chat_template_kwargs: bool = False,
    from_combined: Optional[str] = None,
    temperature: Optional[float] = None,
    vtt_path: Optional[str] = None,
) -> str:
    """文字起こしファイルから議事録を生成してファイルに保存する。

    from_combined が指定された場合は Stage 1+2（チャンク抽出・統合）をスキップし、
    指定ファイルから combined テキストを読み込んで Stage 3（決定事項抽出）のみ実行する。

    vtt_path が指定された場合は Stage 3 で Zoom VTT の話者帰属情報を付加して
    担当者特定の精度を向上させる。
    """
    print(f"[INFO] 文字起こしファイルを読み込み中: {transcript_path}")
    segments = parse_transcript(transcript_path)
    if not segments and from_combined is None:
        raise ValueError(f"文字起こしセグメントが見つかりません: {transcript_path}")
    print(f"[INFO] {len(segments)} セグメントを検出")

    claude_md_context = load_claude_md_context()

    # 出力パスの命名に必要な now/basename は早めに確定する
    now = datetime.now()
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)
    basename = Path(transcript_path).stem

    if from_combined is not None:
        # ------------------------------------------------------------------ #
        # --from-combined: Stage 1 をスキップし、Stage 2+3 を実行
        # ------------------------------------------------------------------ #
        print(f"[INFO] combined ファイルを読み込み中: {from_combined}")
        combined = Path(from_combined).read_text(encoding="utf-8")
        print(f"[INFO] combined テキスト: {len(combined)} 字")
        print(f"[INFO] ローカルLLM（{model}）で議事録を統合生成中...")
        prompt = PROMPT_TEMPLATE.format(
            claude_md_context=claude_md_context,
            transcript=combined,
        )
        minutes_text = call_local_llm(
            prompt, model, base_url, api_key, timeout,
            think=think, max_tokens=max_tokens, no_stream=no_stream,
            no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
        )
        input_text = combined

    elif multi_stage:
        # ------------------------------------------------------------------ #
        # マルチステージ: 分割→抽出→統合
        # ------------------------------------------------------------------ #
        chunk_duration_sec = chunk_minutes * 60
        chunks = chunk_transcript(segments, chunk_duration_sec)
        total = len(chunks)
        print(f"[INFO] マルチステージモード: {total} チャンクに分割（各約 {chunk_minutes} 分）")

        extractions: list[str] = []
        for i, chunk_segs in enumerate(chunks, 1):
            chunk_text = format_transcript(chunk_segs)
            h0, r0 = divmod(chunk_segs[0]["start"], 3600)
            m0, s0 = divmod(r0, 60)
            h1, r1 = divmod(chunk_segs[-1]["end"], 3600)
            m1, s1 = divmod(r1, 60)
            time_range = f"{h0:02d}:{m0:02d}:{s0:02d}〜{h1:02d}:{m1:02d}:{s1:02d}"
            print(f"[INFO] チャンク {i}/{total} を抽出中... ({time_range})")
            try:
                extraction = extract_from_chunk(
                    chunk_text, i, total, time_range,
                    claude_md_context, model, base_url, api_key, timeout,
                    think=think, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs,
                    temperature=temperature, max_tokens=max_tokens,
                )
                # 空チャンク（reasoning parser が content を返さなかった場合等）はリトライ
                if not extraction and not no_stream:
                    print(f"[WARN] チャンク {i} が空のためリトライ（no_stream=True）...")
                    extraction = extract_from_chunk(
                        chunk_text, i, total, time_range,
                        claude_md_context, model, base_url, api_key, timeout,
                        think=think, no_stream=True,
                        no_chat_template_kwargs=no_chat_template_kwargs,
                        temperature=temperature, max_tokens=max_tokens,
                    )
            except Exception as e:
                print(f"[WARN] チャンク {i} 抽出失敗（{e}）、空文字で続行")
                extraction = ""
            extractions.append(f"=== 第{i}部（{time_range}）===\n{extraction}")
            print(f"[INFO] チャンク {i}/{total} 抽出完了（{len(extraction)} 字）")

        combined = "\n\n".join(extractions)
        print(f"[INFO] 全チャンク抽出完了。統合テキスト: {len(combined)} 字")

        # combined をキャッシュファイルとして保存（Stage 3 の再実行・デバッグ用）
        combined_filename = now.strftime("%Y-%m-%d-%H%M%S") + f"-{basename}-combined.txt"
        combined_path = output_dir_path / combined_filename
        combined_path.write_text(combined, encoding="utf-8")
        print(f"[INFO] combined キャッシュを保存しました: {combined_path}")

        print(f"[INFO] ローカルLLM（{model}）で議事録を統合生成中...")
        prompt = PROMPT_TEMPLATE.format(
            claude_md_context=claude_md_context,
            transcript=combined,
        )
        minutes_text = call_local_llm(
            prompt, model, base_url, api_key, timeout,
            think=think, max_tokens=max_tokens, no_stream=no_stream,
            no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
        )
        input_text = combined
    else:
        # ------------------------------------------------------------------ #
        # 単一パス（従来の動作）
        # ------------------------------------------------------------------ #
        transcript_text = format_transcript(segments)
        prompt = PROMPT_TEMPLATE.format(
            claude_md_context=claude_md_context,
            transcript=transcript_text,
        )
        think_label = "有効" if think else "無効"
        print(f"[INFO] ローカルLLM（{model}）で議事録を生成中... （思考モード: {think_label}）")
        minutes_text = call_local_llm(
            prompt, model, base_url, api_key, timeout,
            think=think, max_tokens=max_tokens, no_stream=no_stream,
            no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
        )
        input_text = transcript_text

    # ------------------------------------------------------------------ #
    # Stage 3: 決定事項・アクションアイテムの抽出
    # ------------------------------------------------------------------ #
    # VTT が提供されている場合は combined テキストに話者情報を付加
    vtt_instructions = ""
    decisions_input = input_text
    if vtt_path:
        print(f"[INFO] VTT 話者情報を Stage 3 に付加中: {vtt_path}")
        enriched_text, speaker_map = enrich_combined_with_vtt(
            input_text, vtt_path, claude_md_context
        )
        if speaker_map:
            vtt_instructions = VTT_SPEAKER_INSTRUCTIONS.format(speaker_map=speaker_map)
            decisions_input = enriched_text
            print(f"[INFO] VTT 統合完了: {len(enriched_text)} 字（元: {len(input_text)} 字）")
        else:
            print(f"[WARN] VTT セグメントが見つかりません。話者情報なしで続行します")

    print(f"[INFO] ローカルLLM（{model}）で決定事項・アクションアイテムを生成中...")
    decisions_prompt = DECISIONS_TEMPLATE.format(
        claude_md_context=claude_md_context,
        transcript=decisions_input,
        vtt_speaker_instructions=vtt_instructions,
    )
    # thinking モデルは thinking に多くのトークンを消費するため max_tokens をそのまま使用
    decisions_max_tokens = max_tokens if (think or no_chat_template_kwargs) else 1024
    # Stage 3 は入力が大きいため timeout を 2 倍にして余裕を持たせる
    decisions_timeout = timeout * 2
    decisions_text = call_local_llm(
        decisions_prompt, model, base_url, api_key, decisions_timeout,
        think=think, max_tokens=decisions_max_tokens, no_stream=no_stream,
        no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
    )
    # 空の場合は no_stream でリトライ（reasoning parser の streaming 問題に対応）
    if not decisions_text and not no_stream:
        print("[WARN] 決定事項が空のためリトライ（no_stream=True）...")
        decisions_text = call_local_llm(
            decisions_prompt, model, base_url, api_key, decisions_timeout,
            think=think, max_tokens=decisions_max_tokens, no_stream=True,
            no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
        )
    # 決定事項のスクラッチパッド除去
    for marker in ("## 決定事項\n\n", "## 決定事項\n"):
        idx = decisions_text.find(marker)
        if idx >= 0:
            if idx > 0:
                print(f"[INFO] 決定事項スクラッチパッド除去: 先頭 {idx} 文字を削除")
                decisions_text = decisions_text[idx:]
            break

    # CoT スクラッチパッドを除去: "## 議事内容\n" 以降のみを保持
    for marker in ("## 議事内容\n\n", "## 議事内容\n"):
        idx = minutes_text.find(marker)
        if idx >= 0:
            if idx > 0:
                print(f"[INFO] スクラッチパッド除去: 先頭 {idx} 文字を削除")
                minutes_text = minutes_text[idx:]
            break

    # 決定事項・アクションアイテム + 議事内容 を結合
    # from_combined 時は minutes_text が空なので decisions_text のみ出力
    if minutes_text:
        full_text = decisions_text + "\n\n" + minutes_text
    else:
        full_text = decisions_text

    # 末尾の締めくくりコメントを除去（「以上」「以下」「上記」で始まる行以降）
    full_text = re.sub(r'\n+(?:以上|以下|上記)[^\n]*$', '', full_text.rstrip())
    # 絶対年号を除去: 「2025年3月26日」→「3月26日」（文字起こし中の相対日付が年付きに拡張された場合）
    full_text = re.sub(r'\d{4}年(\d{1,2}月\d{1,2}日)', r'\1', full_text)
    # 「（推測）」「（不明）」等の不確かさ注記を除去
    full_text = re.sub(r'（推測）|（不明）|（確認要）|（未確認）', '', full_text)
    # llama.cpp / Ollama 等でチャットテンプレートの区切りトークンが漏出する場合に除去
    full_text = re.sub(r'<\|(?:user|assistant|system|endoftext)\|>.*', '', full_text, flags=re.DOTALL).rstrip()
    # 出力フォーマットテンプレート由来の末尾コードブロック記号を除去
    full_text = re.sub(r'\n```\s*$', '', full_text.rstrip())
    # LLM がテーブルに余分な列を追加する場合、3列テーブル行の末尾 | ... | を除去
    full_text = re.sub(
        r'^(\|[^|]+\|[^|]+\|[^|]+\|)\s*[^|]+\|$',
        r'\1', full_text, flags=re.MULTILINE,
    )
    # セパレータ行も3列に正規化
    full_text = re.sub(r'^(\|---\|---\|---\|)(\|---\|)+$', r'\1', full_text, flags=re.MULTILINE)
    minutes_text = full_text

    # 出力パスを生成: {output_dir}/YYYY-MM-DD-HHMMSS-<basename>-minutes.md
    # （now / output_dir_path / basename は generate_minutes 冒頭で確定済み）
    filename = now.strftime("%Y-%m-%d-%H%M%S") + f"-{basename}-minutes.md"
    output_path = output_dir_path / filename

    output_path.write_text(minutes_text, encoding="utf-8")
    print(f"[INFO] 議事録を保存しました: {output_path}")
    return str(output_path)


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="ローカルLLMを使用して文字起こしから議事録を生成する"
    )
    parser.add_argument("transcript", help="文字起こし .md/.txt ファイルのパス")
    parser.add_argument("--model", default=None, help="使用するモデル名（省略時は vLLM /v1/models から自動取得）")
    parser.add_argument(
        "--output", "-o",
        default="minutes",
        help="議事録の出力ディレクトリ（デフォルト: minutes）",
    )
    parser.add_argument("--think", action="store_true", help="思考モードを有効化（デフォルト: 無効）")
    parser.add_argument(
        "--no-chat-template-kwargs",
        action="store_true",
        dest="no_chat_template_kwargs",
        help="chat_template_kwargs を送信しない（常時 reasoning モデル向け: Qwen3-Swallow 等）",
    )
    parser.add_argument("--url", default=None, help="ローカルLLMのURL（RIVAULT_URL 環境変数でも可）")
    parser.add_argument("--token", default=None, help="APIトークン（RIVAULT_TOKEN 環境変数でも可）")
    parser.add_argument("--timeout", type=int, default=600, help="LLM呼び出しタイムアウト秒数（デフォルト: 600）")
    parser.add_argument("--max-tokens", type=int, default=8192, help="最大出力トークン数（デフォルト: 8192）")
    parser.add_argument("--multi-stage", action="store_true", help="マルチステージ（分割→抽出→統合）モードを有効化")
    parser.add_argument("--chunk-minutes", type=int, default=30, help="マルチステージ時のチャンクサイズ（分単位、デフォルト: 30）")
    parser.add_argument("--no-stream", action="store_true", help="ストリーミングを無効化（LiteLLM プロキシ経由等で streaming が動作しない場合に使用）")
    parser.add_argument("--temperature", type=float, default=None, help="サンプリング温度（デフォルト: think=True 時 0.6、それ以外 0.8）。Kimi-K2-Thinking は 1.0 推奨")
    parser.add_argument(
        "--from-combined",
        default=None,
        dest="from_combined",
        metavar="FILE",
        help="combined キャッシュファイルから読み込んで Stage 3（決定事項抽出）のみ実行する（Stage 1+2 をスキップ）",
    )
    parser.add_argument(
        "--vtt",
        default=None,
        metavar="FILE",
        help="Zoom VTT ファイルパス（話者帰属情報を Stage 3 に付加して担当者特定を向上させる）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.transcript):
        print(f"[ERROR] ファイルが見つかりません: {args.transcript}", file=sys.stderr)
        return 1

    # 認証情報の読み込み（引数 > 環境変数 > トークンファイル）
    if args.url:
        os.environ["RIVAULT_URL"] = args.url
    if args.token:
        os.environ["RIVAULT_TOKEN"] = args.token
    base_url, api_key = load_rivault_tokens()

    if not args.model:
        args.model = detect_vllm_model(base_url)
        print(f"[INFO] モデル      : {args.model}（自動取得）")
    else:
        print(f"[INFO] モデル      : {args.model}")
    print(f"[INFO] 思考モード  : {'有効' if args.think else '無効'}")
    if args.think and args.no_chat_template_kwargs:
        print(f"[INFO] chat_template_kwargs: 送信しない（常時 reasoning モデル）")
    print(f"[INFO] max_tokens  : {args.max_tokens}")
    print(f"[INFO] マルチステージ: {'有効' if args.multi_stage else '無効'}")
    if args.multi_stage:
        print(f"[INFO] チャンク    : {args.chunk_minutes} 分")
    print(f"[INFO] ストリーミング: {'無効' if args.no_stream else '有効'}")
    if args.temperature is not None:
        print(f"[INFO] temperature : {args.temperature}")
    if args.vtt:
        print(f"[INFO] VTT ファイル  : {args.vtt}")
    print(f"[INFO] LLM URL     : {base_url}")

    try:
        output_path = generate_minutes(
            args.transcript, args.output, args.model, base_url, api_key, args.timeout,
            think=args.think, max_tokens=args.max_tokens,
            multi_stage=args.multi_stage, chunk_minutes=args.chunk_minutes,
            no_stream=args.no_stream,
            no_chat_template_kwargs=args.no_chat_template_kwargs,
            from_combined=args.from_combined,
            temperature=args.temperature,
            vtt_path=args.vtt,
        )
        print(f"[完了] {output_path}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
