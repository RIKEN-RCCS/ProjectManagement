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
  --url URL           ローカルLLMのURL（OPENAI_API_BASE 環境変数でも可）
  --token TOKEN       APIトークン（OPENAI_API_KEY 環境変数でも可）
  --timeout SEC       LLM呼び出しタイムアウト秒数（デフォルト: 600）

認証情報の読み込み順序:
  1. --url / --token 引数
  2. OPENAI_API_BASE / OPENAI_API_KEY 環境変数
  3. デフォルト: http://localhost:8000/v1 / "dummy"

ローカル LLM (vLLM gemma4) 用と RiVault Embedding (bge-m3) 用は環境変数を分離する:
  - OPENAI_API_BASE / OPENAI_API_KEY    — vLLM gemma4（議事録生成）
  - RIVAULT_URL / RIVAULT_TOKEN          — RiVault（embed_utils 経由で embedding 取得）
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
SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPT_DIR.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
PROJECT_MD = REPO_ROOT / "docs" / "project.md"

sys.path.insert(0, str(SCRIPT_DIR))
from cli_utils import (
    strip_think_blocks, call_local_llm, load_claude_md_context, detect_vllm_model,
    enrich_combined_with_vtt, retrieve_knowledge_for_extraction,
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
9. Actively correct speech recognition errors: cross-reference ALL technical terms, application names, and project-specific words against the Project Terminology Reference AND the Slides Shown in This Meeting (if provided). Replace phonetic approximations or garbled terms with the correct form (e.g. 「ファントホローブルー」→ FrontFlow/Blue, 「サーモン」→ SALMON, 「スケールTKF」→ SCALE-LETKF). Slide text is ground truth for proper nouns and numeric figures.
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
{slide_context_block}
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
5. Actively correct speech recognition errors: replace phonetic approximations with correct terms from the Participant List, Project Terminology Reference, AND the Slides Shown in This Meeting (if provided) (e.g. 「ファントホローブルー」→ FrontFlow/Blue, 「サーモン」→ SALMON). Slide text is ground truth for proper nouns and numeric figures.
CRITICAL: "SPEAKER_00", "SPEAKER_01", "SPEAKER_02", etc. must NEVER appear in output.
{vtt_speaker_instructions}
## 決定事項 rules:
- **Decisions = judgments by decision-makers ONLY (3-7 items max)**. A decision is a choice among alternatives that changes the project's direction, resources, or commitments.
- Include ONLY: policy/strategy decisions, resource allocation decisions, schedule/scope changes, agreements with external parties.
- Ask yourself: "Would this item appear in a formal board-level summary?" If not, omit it.
- Definitely omit: routine procedural confirmations ("資料を配布する", "Slackで共有する", "アジェンダを反映する", "議事録を作成する"), minor operational steps, restatements of deadlines that are already obvious from context, information sharing ("〜が判明した"), and meeting scheduling ("次回は〇月〇日に開催").
- Do NOT restate action items as decisions. If someone is assigned a task, that is an action item, not a decision.
- Markers for explicit agreement: 〜で進める / 〜に決定 / 〜することが合意 / 〜することになった / 〜が決定した
- Vary the ending naturally in Japanese (e.g. 〜することになった / 〜と合意した / 〜する方針となった / 〜をキャンセルした). Do NOT end every item with 〜が決定された.
- If no significant decisions found: write a single line "（なし）"

## アクションアイテム rules:
- An action item is a task that is **essential to project progress** and produces a **concrete deliverable** (report, design doc, code, estimate, proposal, etc.).
- List only specific tasks explicitly assigned or delegated to an identifiable person.
- Do NOT infer tasks that were not explicitly assigned in the summaries.
- Definitely omit: routine check/confirm tasks ("確認する", "チェックする"), recurring tasks ("スケジュールの更新", "TWIの更新"), meeting scheduling ("ミーティングを設定する"), Slack communication ("Slackで共有する", "連絡する"), and one-off administrative tasks ("出席登録", "チャンネル追加", "アカウント削除").
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
{slide_context_block}
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
def load_local_llm_endpoint() -> tuple[str, str]:
    """vLLM gemma4 のエンドポイント (URL, token) を返す。

    ローカル vLLM は OPENAI_API_BASE / OPENAI_API_KEY を使う。
    RiVault (embedding 用) との混同を避けるため RIVAULT_URL は参照しない。
    """
    url = os.environ.get("OPENAI_API_BASE", "http://localhost:8000/v1")
    token = os.environ.get("OPENAI_API_KEY", "dummy")
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
6. If "Slides Shown in This Meeting" is provided below, treat the slide text as ground truth for proper nouns, acronyms, and numeric figures — prefer slide spellings over ASR when they conflict.
CRITICAL: Never write "SPEAKER_00", "SPEAKER_01", "SPEAKER_02" or any "SPEAKER_" token in the output.

## Project Terminology
{claude_md_context}
{slide_context_block}

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
    slide_context_block: str = "",
) -> str:
    """1チャンクから事実を抽出する（Stage 1）"""
    prompt = CHUNK_EXTRACTION_TEMPLATE.format(
        chunk_idx=chunk_idx,
        total_chunks=total_chunks,
        time_range=time_range,
        claude_md_context=claude_md_context,
        chunk_text=chunk_text,
        slide_context_block=slide_context_block,
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
# Self-consistency: N サンプリング + embedding クラスタリング + LLM 集約
#   --consensus N が指定された場合のみ Stage 2 / Stage 3 で適用される
# --------------------------------------------------------------------------- #
CONSENSUS_SECTION_TEMPLATE = """\
You are merging {n_drafts} independent draft summaries of the same Japanese meeting topic
into a single canonical section.

The drafts below were produced by sampling the same LLM at slightly different temperatures.
Treat each draft as an independent witness. Your job:
- Keep ONLY facts that appear in {min_vote} or more drafts (cross-verified majority).
- Drop facts unique to a single draft (likely hallucination or sampling artifact).
- When phrasing differs but the underlying fact is the same, choose the most precise wording.
- Keep numeric figures, dates, and proper nouns EXACTLY as they appear (do not round, do not infer).
- Output Japanese prose only. No bullet lists, no speaker attribution, no SPEAKER_XX tokens.
- Preserve the topic title `{title}` (do not rename it).

Output format (begin immediately, no preamble):
### {title}

(2〜4 段落、各段落 2〜3 文)

## Drafts
{drafts_block}
"""


CONSENSUS_DECISIONS_TEMPLATE = """\
You are merging {n_drafts} independent decision lists from the same meeting into a canonical list.

Rules:
- Keep ONLY decisions supported by {min_vote} or more independent drafts.
- A decision is the same across drafts if it refers to the same subject and outcome,
  even if the wording differs. Merge phrasing variants into one entry.
- Drop entries unique to a single draft.
- Preserve numeric figures, dates, and proper nouns exactly as they appear.
- Vary the Japanese sentence ending naturally (〜することになった / 〜と合意した /
  〜する方針となった / 〜をキャンセルした). Do not end every line with 〜が決定された.
- If no decision survives the vote: write a single line `（なし）`.
- Output Japanese only. Begin immediately with `## 決定事項` — no preamble, no thinking.

Output format:
## 決定事項

- （決定事項1）
- （決定事項2）

## Drafts
{drafts_block}
"""


CONSENSUS_ACTIONS_TEMPLATE = """\
You are merging {n_drafts} independent action item tables from the same meeting into a canonical table.

Rules:
- Keep ONLY action items supported by {min_vote} or more independent drafts.
- Two rows refer to the same item if the assignee matches AND the task content is
  semantically equivalent (even if the wording differs). Merge phrasing variants.
- Drop rows unique to a single draft.
- Preserve numeric figures, dates, and proper nouns exactly as they appear.
- タスク内容: 2-3 sentences (40-80 chars each), background + deliverable.
  Do NOT include deadline expressions in タスク内容 — those belong in 期限 only.
- 担当者: normalize using the Participant List below; if unclear write `（未定）`.
- If no action item survives the vote: write a single line `（なし）`.
- Output Japanese only. Begin immediately with `## アクションアイテム` — no preamble.

Output format (EXACTLY 3 columns):
## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| ... | ... | ... |

## Participant List
{claude_md_context}

## Drafts
{drafts_block}
"""


def _sample_n_times(
    prompt: str,
    n: int,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    think: bool,
    max_tokens: int,
    no_stream: bool,
    no_chat_template_kwargs: bool,
    base_temperature: Optional[float],
    label: str,
) -> list[str]:
    """同一プロンプトを n 回サンプリングし、空でない結果のリストを返す。

    各サンプルで temperature を僅かにずらしてサンプリング多様性を確保する
    （base ± 0.1 程度の範囲）。空応答（reasoning parser の streaming 問題等）は
    no_stream=True でリトライする。
    """
    base_t = base_temperature if base_temperature is not None else (0.6 if think else 0.8)
    if n <= 1:
        deltas = [0.0]
    elif n == 2:
        deltas = [-0.05, 0.05]
    else:
        # n=3 → -0.1, 0, +0.1 / n=5 → -0.1, -0.05, 0, +0.05, +0.1
        step = 0.2 / (n - 1)
        deltas = [-0.1 + step * i for i in range(n)]

    drafts: list[str] = []
    for i, d in enumerate(deltas, 1):
        t = max(0.05, min(1.5, base_t + d))
        print(f"[INFO] {label} サンプル {i}/{n} (temperature={t:.2f}) 生成中...", file=sys.stderr)
        try:
            text = call_local_llm(
                prompt, model, base_url, api_key, timeout,
                think=think, max_tokens=max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs, temperature=t,
            )
            if not text and not no_stream:
                print(f"[WARN] {label} サンプル {i} が空のためリトライ（no_stream=True）", file=sys.stderr)
                text = call_local_llm(
                    prompt, model, base_url, api_key, timeout,
                    think=think, max_tokens=max_tokens, no_stream=True,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=t,
                )
        except Exception as e:
            print(f"[WARN] {label} サンプル {i} 失敗: {e}", file=sys.stderr)
            text = ""
        if text and text.strip():
            drafts.append(text)
        else:
            print(f"[WARN] {label} サンプル {i} は空。残りで集約します", file=sys.stderr)
    print(f"[INFO] {label}: {len(drafts)}/{n} 件のドラフトを取得", file=sys.stderr)
    return drafts


def _split_sections(text: str) -> list[tuple[str, str]]:
    """`## 議事内容` 配下を `### タイトル` で分解して [(title, body), ...] を返す。"""
    if not text:
        return []
    body = text
    # `## 議事内容` 以降を抽出（先頭の前置き除去）
    m = re.search(r"^##\s*議事内容\s*$", body, flags=re.MULTILINE)
    if m:
        body = body[m.end():]
    # `## XXX` で始まる別セクションが現れたらそこで切る
    m = re.search(r"^##\s+(?!議事内容)\S", body, flags=re.MULTILINE)
    if m:
        body = body[: m.start()]
    sections = []
    pattern = re.compile(r"^###\s+(.+?)\s*$", flags=re.MULTILINE)
    matches = list(pattern.finditer(body))
    for i, mm in enumerate(matches):
        title = mm.group(1).strip()
        start = mm.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        if section_body:
            sections.append((title, section_body))
    return sections


def _split_decisions_list(text: str) -> list[str]:
    """`## 決定事項` 配下の箇条書き行を抽出する。"""
    if not text:
        return []
    m = re.search(r"^##\s*決定事項\s*$", text, flags=re.MULTILINE)
    if not m:
        return []
    body = text[m.end():]
    # 次の `## ` で切る
    m2 = re.search(r"^##\s+\S", body, flags=re.MULTILINE)
    if m2:
        body = body[: m2.start()]
    items = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ", "・")):
            content = re.sub(r"^[-*・]\s*", "", line).strip()
            if content and content != "（なし）":
                items.append(content)
    return items


def _split_action_rows(text: str) -> list[tuple[str, str, str]]:
    """`## アクションアイテム` のテーブル行を [(担当者, タスク内容, 期限), ...] で返す。"""
    if not text:
        return []
    m = re.search(r"^##\s*アクションアイテム\s*$", text, flags=re.MULTILINE)
    if not m:
        return []
    body = text[m.end():]
    m2 = re.search(r"^##\s+\S", body, flags=re.MULTILINE)
    if m2:
        body = body[: m2.start()]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # ヘッダ行・セパレータ行をスキップ
        if re.match(r"^\|\s*[-:]+\s*\|", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        # ヘッダ「担当者 | タスク内容 | 期限」をスキップ
        if cells[0] in ("担当者", "Assignee") and "タスク" in cells[1]:
            continue
        assignee, task, deadline = cells[0], cells[1], cells[2]
        if not task or task in ("（なし）", "なし"):
            continue
        rows.append((assignee, task, deadline))
    return rows


def _greedy_cluster(
    items: list[str],
    threshold: float,
    *,
    label: str,
) -> list[list[int]]:
    """embedding コサイン類似度に基づくグリーディクラスタリング。

    items[i] に最も近い既存クラスタの中心との類似度が threshold 以上ならそのクラスタに
    所属させ、そうでなければ新規クラスタを作る。中心はクラスタ内の embedding 平均。
    """
    if not items:
        return []
    from embed_utils import embed_batch, cosine_similarity_matrix
    vecs = embed_batch(items)

    import numpy as np
    clusters: list[list[int]] = []
    centers: list[np.ndarray] = []
    for i, v in enumerate(vecs):
        if not clusters:
            clusters.append([i])
            centers.append(v.copy())
            continue
        sims = cosine_similarity_matrix(v, np.stack(centers))
        best = int(np.argmax(sims))
        if float(sims[best]) >= threshold:
            clusters[best].append(i)
            # 中心を更新（インクリメンタル平均）
            n_old = len(clusters[best]) - 1
            centers[best] = (centers[best] * n_old + v) / (n_old + 1)
        else:
            clusters.append([i])
            centers.append(v.copy())
    return clusters


def _consensus_stage2(
    drafts: list[str],
    *,
    min_vote: int,
    threshold: float,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    think: bool,
    max_tokens: int,
    no_stream: bool,
    no_chat_template_kwargs: bool,
    temperature: Optional[float],
) -> str:
    """Stage 2 のドラフト群を集約して `## 議事内容` 全体の Markdown を返す。"""
    # 各ドラフトをセクションに分解
    all_sections: list[tuple[int, str, str]] = []  # (draft_idx, title, body)
    for di, d in enumerate(drafts):
        for title, body in _split_sections(d):
            all_sections.append((di, title, body))
    if not all_sections:
        # 集約に失敗したら最長のドラフトを返す
        print("[WARN] Stage 2 ドラフトからセクション抽出失敗、最長ドラフトを採用", file=sys.stderr)
        return max(drafts, key=len) if drafts else ""

    # タイトル＋本文先頭でクラスタリング（タイトルだけだと曖昧なため本文も含める）
    keys = [f"{t}\n{b[:300]}" for _, t, b in all_sections]
    try:
        clusters = _greedy_cluster(keys, threshold, label="Stage 2 セクション")
    except Exception as e:
        print(f"[ERROR] Stage 2 embedding 失敗、最長ドラフトを採用: {e}", file=sys.stderr)
        return max(drafts, key=len) if drafts else ""

    # 各クラスタ: 含むユニーク draft 数で投票
    accepted: list[list[int]] = []
    for cl in clusters:
        unique_drafts = {all_sections[i][0] for i in cl}
        if len(unique_drafts) >= min_vote:
            accepted.append(cl)
    if not accepted:
        # 投票で全部切られたら閾値を緩めて再クラスタリング
        print(f"[WARN] Stage 2: 投票閾値 {min_vote} で全クラスタ却下。閾値 {threshold:.2f} → {threshold-0.05:.2f} で再試行", file=sys.stderr)
        clusters = _greedy_cluster(keys, max(0.5, threshold - 0.05), label="Stage 2 セクション (緩和)")
        for cl in clusters:
            unique_drafts = {all_sections[i][0] for i in cl}
            if len(unique_drafts) >= min_vote:
                accepted.append(cl)
    if not accepted:
        print("[WARN] Stage 2 集約: 投票通過クラスタなし、最長ドラフトを採用", file=sys.stderr)
        return max(drafts, key=len) if drafts else ""

    # 各クラスタを LLM に集約させる
    print(f"[INFO] Stage 2 集約: {len(accepted)} クラスタを LLM に投入", file=sys.stderr)
    merged_sections: list[str] = []
    for ci, cl in enumerate(accepted, 1):
        cluster_drafts = [all_sections[i] for i in cl]
        # タイトルは出現頻度最多のものを採用
        title_counts: dict[str, int] = {}
        for _, t, _ in cluster_drafts:
            title_counts[t] = title_counts.get(t, 0) + 1
        title = max(title_counts.items(), key=lambda x: x[1])[0]
        drafts_block = "\n\n".join(
            f"### Draft {idx + 1}\n{cluster_drafts[idx][2]}"
            for idx in range(len(cluster_drafts))
        )
        prompt = CONSENSUS_SECTION_TEMPLATE.format(
            n_drafts=len(cluster_drafts),
            min_vote=min_vote,
            title=title,
            drafts_block=drafts_block,
        )
        print(f"[INFO]   クラスタ {ci}/{len(accepted)}: '{title}' ({len(cluster_drafts)} ドラフト)", file=sys.stderr)
        try:
            merged = call_local_llm(
                prompt, model, base_url, api_key, timeout,
                think=think, max_tokens=max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
            )
        except Exception as e:
            print(f"[WARN] Stage 2 集約失敗（'{title}'）: {e}、最長ドラフトを採用", file=sys.stderr)
            longest = max(cluster_drafts, key=lambda x: len(x[2]))
            merged = f"### {longest[1]}\n\n{longest[2]}"
        merged = merged.strip()
        if not merged.startswith("### "):
            merged = f"### {title}\n\n{merged}"
        merged_sections.append(merged)
    return "## 議事内容\n\n" + "\n\n".join(merged_sections)


def _consensus_stage3(
    drafts: list[str],
    *,
    min_vote: int,
    threshold: float,
    claude_md_context: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    think: bool,
    max_tokens: int,
    no_stream: bool,
    no_chat_template_kwargs: bool,
    temperature: Optional[float],
) -> str:
    """Stage 3 のドラフト群を集約して `## 決定事項` + `## アクションアイテム` を返す。"""
    # 決定事項とアクションアイテムをドラフトごとに分離
    all_decisions: list[tuple[int, str]] = []
    all_actions: list[tuple[int, str, str, str]] = []
    for di, d in enumerate(drafts):
        for item in _split_decisions_list(d):
            all_decisions.append((di, item))
        for assignee, task, deadline in _split_action_rows(d):
            all_actions.append((di, assignee, task, deadline))

    # --- 決定事項の集約 --- #
    if all_decisions:
        keys = [item for _, item in all_decisions]
        try:
            clusters = _greedy_cluster(keys, threshold, label="Stage 3 決定事項")
        except Exception as e:
            print(f"[ERROR] Stage 3 決定事項 embedding 失敗、最長ドラフトを採用: {e}", file=sys.stderr)
            return max(drafts, key=len) if drafts else ""
        accepted = [cl for cl in clusters
                    if len({all_decisions[i][0] for i in cl}) >= min_vote]
        if accepted:
            drafts_block_lines = []
            for ci, cl in enumerate(accepted, 1):
                bullets = "\n".join(f"- {all_decisions[i][1]}" for i in cl)
                drafts_block_lines.append(f"### Cluster {ci}\n{bullets}")
            drafts_block = "\n\n".join(drafts_block_lines)
            prompt = CONSENSUS_DECISIONS_TEMPLATE.format(
                n_drafts=len(drafts), min_vote=min_vote, drafts_block=drafts_block,
            )
            print(f"[INFO] Stage 3 決定事項集約: {len(accepted)} クラスタを LLM に投入", file=sys.stderr)
            try:
                decisions_md = call_local_llm(
                    prompt, model, base_url, api_key, timeout,
                    think=think, max_tokens=max_tokens, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
                )
            except Exception as e:
                print(f"[WARN] Stage 3 決定事項集約失敗: {e}、最長ドラフトを採用", file=sys.stderr)
                decisions_md = ""
        else:
            print(f"[WARN] Stage 3 決定事項: 投票閾値 {min_vote} 通過なし → 「（なし）」", file=sys.stderr)
            decisions_md = "## 決定事項\n\n（なし）"
    else:
        decisions_md = "## 決定事項\n\n（なし）"

    # --- アクションアイテムの集約 --- #
    if all_actions:
        # 担当者を embedding キーに混ぜて、同一担当者の似た内容だけクラスタ化
        keys = [f"[{a}] {t}" for _, a, t, _ in all_actions]
        try:
            clusters = _greedy_cluster(keys, threshold, label="Stage 3 AI")
        except Exception as e:
            print(f"[ERROR] Stage 3 AI embedding 失敗、最長ドラフトを採用: {e}", file=sys.stderr)
            return max(drafts, key=len) if drafts else ""
        accepted = [cl for cl in clusters
                    if len({all_actions[i][0] for i in cl}) >= min_vote]
        if accepted:
            drafts_block_lines = []
            for ci, cl in enumerate(accepted, 1):
                lines = ["| 担当者 | タスク内容 | 期限 |", "|---|---|---|"]
                for i in cl:
                    _, a, t, dl = all_actions[i]
                    lines.append(f"| {a} | {t} | {dl} |")
                drafts_block_lines.append(f"### Cluster {ci}\n" + "\n".join(lines))
            drafts_block = "\n\n".join(drafts_block_lines)
            prompt = CONSENSUS_ACTIONS_TEMPLATE.format(
                n_drafts=len(drafts), min_vote=min_vote,
                claude_md_context=claude_md_context, drafts_block=drafts_block,
            )
            print(f"[INFO] Stage 3 AI集約: {len(accepted)} クラスタを LLM に投入", file=sys.stderr)
            try:
                actions_md = call_local_llm(
                    prompt, model, base_url, api_key, timeout,
                    think=think, max_tokens=max_tokens, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
                )
            except Exception as e:
                print(f"[WARN] Stage 3 AI集約失敗: {e}", file=sys.stderr)
                actions_md = "## アクションアイテム\n\n（なし）"
        else:
            print(f"[WARN] Stage 3 AI: 投票閾値 {min_vote} 通過なし → 「（なし）」", file=sys.stderr)
            actions_md = "## アクションアイテム\n\n（なし）"
    else:
        actions_md = "## アクションアイテム\n\n（なし）"

    # 決定事項と AI を結合（既存の単発出力と同じ並び）
    decisions_md = decisions_md.strip() if decisions_md else "## 決定事項\n\n（なし）"
    actions_md = actions_md.strip() if actions_md else "## アクションアイテム\n\n（なし）"
    # decisions_md に既に AI セクションが混入している場合は削る
    m = re.search(r"^##\s*アクションアイテム", decisions_md, flags=re.MULTILINE)
    if m:
        decisions_md = decisions_md[: m.start()].strip()
    return decisions_md + "\n\n" + actions_md


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
    slide_context: Optional[str] = None,
    consensus_n: int = 3,
    consensus_threshold: float = 0.78,
    consensus_min_vote: Optional[int] = None,
) -> str:
    """文字起こしファイルから議事録を生成してファイルに保存する。

    from_combined が指定された場合は Stage 1+2（チャンク抽出・統合）をスキップし、
    指定ファイルから combined テキストを読み込んで Stage 3（決定事項抽出）のみ実行する。

    vtt_path が指定された場合は Stage 3 で Zoom VTT の話者帰属情報を付加して
    担当者特定の精度を向上させる。

    consensus_n >= 2 の場合は Stage 2 / Stage 3 をそれぞれ N 回サンプリングし
    embedding クラスタリング + LLM 集約で表現ブレを吸収する（self-consistency）。
    consensus_n == 1（デフォルト）は完全に従来動作。
    """
    consensus_enabled = consensus_n is not None and consensus_n >= 2
    if consensus_enabled and consensus_min_vote is None:
        consensus_min_vote = (consensus_n + 1) // 2  # ceil(N/2)
    if consensus_enabled:
        print(
            f"[INFO] Self-consistency 有効: N={consensus_n}, threshold={consensus_threshold}, "
            f"min_vote={consensus_min_vote}"
        )
    print(f"[INFO] 文字起こしファイルを読み込み中: {transcript_path}")
    segments = parse_transcript(transcript_path)
    if not segments and from_combined is None:
        raise ValueError(f"文字起こしセグメントが見つかりません: {transcript_path}")
    print(f"[INFO] {len(segments)} セグメントを検出")

    claude_md_context = load_claude_md_context()

    # スライドOCRから得た文脈をプロンプトに同梱するブロック
    if slide_context:
        slide_context_block = (
            "\n## Slides Shown in This Meeting\n"
            "These are the ACTUAL presentation slides shown during this meeting, extracted via OCR. "
            "Treat these as ground truth for proper nouns, acronyms, technical terms, and numeric figures. "
            "When Whisper ASR output conflicts with slide text, trust the slide text.\n\n"
            f"{slide_context}\n"
        )
        print(f"[INFO] スライド文脈を同梱: {len(slide_context)} 字")
    else:
        slide_context_block = ""

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
            slide_context_block=slide_context_block,
        )
        if consensus_enabled:
            drafts = _sample_n_times(
                prompt, consensus_n, model=model, base_url=base_url, api_key=api_key,
                timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs,
                base_temperature=temperature, label="Stage 2 (from-combined)",
            )
            if len(drafts) >= 2:
                minutes_text = _consensus_stage2(
                    drafts, min_vote=consensus_min_vote, threshold=consensus_threshold,
                    model=model, base_url=base_url, api_key=api_key, timeout=timeout,
                    think=think, max_tokens=max_tokens, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
                )
            else:
                print("[WARN] Stage 2 ドラフトが不足、単発フォールバック", file=sys.stderr)
                minutes_text = drafts[0] if drafts else ""
        else:
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
                    slide_context_block=slide_context_block,
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
            slide_context_block=slide_context_block,
        )
        if consensus_enabled:
            drafts = _sample_n_times(
                prompt, consensus_n, model=model, base_url=base_url, api_key=api_key,
                timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs,
                base_temperature=temperature, label="Stage 2 (multi-stage)",
            )
            if len(drafts) >= 2:
                minutes_text = _consensus_stage2(
                    drafts, min_vote=consensus_min_vote, threshold=consensus_threshold,
                    model=model, base_url=base_url, api_key=api_key, timeout=timeout,
                    think=think, max_tokens=max_tokens, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
                )
            else:
                print("[WARN] Stage 2 ドラフトが不足、単発フォールバック", file=sys.stderr)
                minutes_text = drafts[0] if drafts else ""
        else:
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
        if consensus_enabled:
            drafts = _sample_n_times(
                prompt, consensus_n, model=model, base_url=base_url, api_key=api_key,
                timeout=timeout, think=think, max_tokens=max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs,
                base_temperature=temperature, label="Stage 2 (single-pass)",
            )
            if len(drafts) >= 2:
                minutes_text = _consensus_stage2(
                    drafts, min_vote=consensus_min_vote, threshold=consensus_threshold,
                    model=model, base_url=base_url, api_key=api_key, timeout=timeout,
                    think=think, max_tokens=max_tokens, no_stream=no_stream,
                    no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
                )
            else:
                print("[WARN] Stage 2 ドラフトが不足、単発フォールバック", file=sys.stderr)
                minutes_text = drafts[0] if drafts else ""
        else:
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

    # ナレッジ検索（Phase 3追加）— いったん停止
    # knowledge_context = retrieve_knowledge_for_extraction(
    #     decisions_input,
    #     qa_db_path=REPO_ROOT / "data" / "qa_index.db",
    #     top_k=5,
    #     since_days=90,
    #     index_name="pm-all",
    # )
    decisions_prompt = DECISIONS_TEMPLATE.format(
        claude_md_context=claude_md_context,
        transcript=decisions_input,
        vtt_speaker_instructions=vtt_instructions,
        slide_context_block=slide_context_block,
    )
    # thinking モデルは thinking に多くのトークンを消費するため max_tokens をそのまま使用
    decisions_max_tokens = max_tokens if (think or no_chat_template_kwargs) else 1024
    # Stage 3 は入力が大きいため timeout を 2 倍にして余裕を持たせる
    decisions_timeout = timeout * 2
    if consensus_enabled:
        drafts = _sample_n_times(
            decisions_prompt, consensus_n, model=model, base_url=base_url, api_key=api_key,
            timeout=decisions_timeout, think=think, max_tokens=decisions_max_tokens,
            no_stream=no_stream, no_chat_template_kwargs=no_chat_template_kwargs,
            base_temperature=temperature, label="Stage 3",
        )
        if len(drafts) >= 2:
            decisions_text = _consensus_stage3(
                drafts, min_vote=consensus_min_vote, threshold=consensus_threshold,
                claude_md_context=claude_md_context,
                model=model, base_url=base_url, api_key=api_key, timeout=decisions_timeout,
                think=think, max_tokens=decisions_max_tokens, no_stream=no_stream,
                no_chat_template_kwargs=no_chat_template_kwargs, temperature=temperature,
            )
        else:
            print("[WARN] Stage 3 ドラフトが不足、単発フォールバック", file=sys.stderr)
            decisions_text = drafts[0] if drafts else ""
    else:
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
    parser.add_argument("--url", default=None, help="ローカルLLMのURL（OPENAI_API_BASE 環境変数でも可）")
    parser.add_argument("--token", default=None, help="APIトークン（OPENAI_API_KEY 環境変数でも可）")
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
    parser.add_argument(
        "--slide-context",
        default=None,
        metavar="FILE",
        help="スライドOCR から得た文脈テキスト（slide_ocr.py の --context-out 出力）。"
             "Stage 1/2/3 プロンプトに同梱して固有名詞の誤変換を補正する",
    )
    parser.add_argument(
        "--consensus",
        type=int,
        default=3,
        metavar="N",
        help="Self-consistency サンプリング数。N>=2 で Stage 2 / Stage 3 を N 回サンプリング → "
             "embedding クラスタリング + LLM 集約で表現ブレを吸収する。デフォルト 3。"
             "実測では baseline 比 +15〜25% 程度の追加コストで採用率が改善する。"
             "従来の単発生成に戻したい場合は --consensus 1 を指定",
    )
    parser.add_argument(
        "--consensus-threshold",
        type=float,
        default=0.78,
        metavar="FLOAT",
        help="Self-consistency クラスタリングのコサイン類似度閾値（デフォルト: 0.78）",
    )
    parser.add_argument(
        "--consensus-min-vote",
        type=int,
        default=None,
        metavar="N",
        help="Self-consistency クラスタ採用に必要な最小独立サンプル数（デフォルト: ceil(N/2)）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.transcript):
        print(f"[ERROR] ファイルが見つかりません: {args.transcript}", file=sys.stderr)
        return 1

    # 認証情報: vLLM gemma4 (議事録生成) は OPENAI_API_BASE / OPENAI_API_KEY を使う。
    # RiVault (embed_utils 経由の embedding) は RIVAULT_URL / RIVAULT_TOKEN をそのまま使う。
    if args.url:
        os.environ["OPENAI_API_BASE"] = args.url
    if args.token:
        os.environ["OPENAI_API_KEY"] = args.token
    base_url, api_key = load_local_llm_endpoint()

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
    if args.slide_context:
        print(f"[INFO] スライド文脈  : {args.slide_context}")
    if args.consensus and args.consensus >= 2:
        print(f"[INFO] consensus    : N={args.consensus}, threshold={args.consensus_threshold}")
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
            slide_context=(Path(args.slide_context).read_text(encoding="utf-8")
                           if args.slide_context and Path(args.slide_context).exists()
                           else None),
            consensus_n=args.consensus,
            consensus_threshold=args.consensus_threshold,
            consensus_min_vote=args.consensus_min_vote,
        )
        print(f"[完了] {output_path}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
