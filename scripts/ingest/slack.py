#!/usr/bin/env python3
"""
ingest_slack.py

Slack {channel_id}.db → pm.db へ決定事項・アクションアイテムを抽出するプラグイン。
pm_ingest.py slack 経由で呼び出される。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_utils import open_db, normalize_assignee
from cli_utils import (
    load_claude_md,
    call_claude,
    call_local_llm,
    detect_vllm_model,
    retrieve_knowledge_for_extraction,
)
from ingest.ingest_plugin import IngestContext


# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
# 既定チャンネルは環境変数 PM_DEFAULT_SLACK_CHANNEL から取得する。
# （実値はチャンネル機密のためソース内に持たない）。
DEFAULT_CHANNEL = os.environ.get("PM_DEFAULT_SLACK_CHANNEL", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_extractions (
    thread_ts    TEXT,
    channel_id   TEXT,
    extracted_at TEXT,
    PRIMARY KEY (thread_ts, channel_id)
);
"""


# --------------------------------------------------------------------------- #
# Slack DB 接続
# --------------------------------------------------------------------------- #
def open_slack_db(db_path: Path, no_encrypt: bool = False):
    if not db_path.exists():
        print(f"ERROR: Slack DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


# --------------------------------------------------------------------------- #
# pm.db 初期化（slack_extractions テーブル追加）
# --------------------------------------------------------------------------- #
def ensure_slack_extractions(pm_conn) -> None:
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                pm_conn.execute(stmt)
            except Exception:
                pass
    pm_conn.commit()


# --------------------------------------------------------------------------- #
# コンテキスト読み込み
# --------------------------------------------------------------------------- #
def load_context_from_claude_md() -> str:
    text = load_claude_md(CLAUDE_MD)
    sections = []
    capture = False
    for line in text.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)", line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else text[:3000]


# --------------------------------------------------------------------------- #
# マイルストーン取得
# --------------------------------------------------------------------------- #
def fetch_milestones(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT milestone_id, name, due_date, area FROM milestones WHERE status='active' ORDER BY due_date"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def format_milestones_for_prompt(milestones: list[dict]) -> str:
    if not milestones:
        return "（マイルストーン未登録）"
    lines = ["| ID | マイルストーン名 | 期限 | エリア |",
             "|----|----------------|------|--------|"]
    for m in milestones:
        lines.append(f"| {m['milestone_id']} | {m['name']} | {m.get('due_date') or '未定'} | {m.get('area') or ''} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# スレッド取得
# --------------------------------------------------------------------------- #
def fetch_threads(slack_conn, channel_id: str, since: str | None) -> list[dict]:
    query = """
        SELECT m.thread_ts, m.timestamp, m.permalink, m.user_name, m.text
        FROM messages m
        WHERE m.channel_id = ?
    """
    params: list = [channel_id]
    if since:
        query += " AND m.timestamp >= ?"
        params.append(since)
    query += " ORDER BY m.timestamp ASC"
    parents = slack_conn.execute(query, params).fetchall()

    results = []
    for p in parents:
        thread_ts = p["thread_ts"]
        lines = [f"[{(p['timestamp'] or '')[:16]}] {p['user_name'] or '不明'}: {p['text'] or ''}"]
        replies = slack_conn.execute(
            "SELECT timestamp, user_name, text FROM replies"
            " WHERE thread_ts=? AND channel_id=? ORDER BY msg_ts ASC",
            (thread_ts, channel_id),
        ).fetchall()
        for r in replies:
            lines.append(f"  [{(r['timestamp'] or '')[:16]}] {r['user_name'] or '不明'}: {r['text'] or ''}")
        results.append({
            "thread_ts": thread_ts,
            "thread_text": "\n".join(lines),
            "timestamp": p["timestamp"],
            "permalink": p["permalink"],
            "user_name": p["user_name"],
        })
    return results


# --------------------------------------------------------------------------- #
# 重複管理
# --------------------------------------------------------------------------- #
def is_already_extracted(pm_conn, thread_ts: str, channel_id: str) -> bool:
    row = pm_conn.execute(
        "SELECT 1 FROM slack_extractions WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row is not None


def mark_extracted(pm_conn, thread_ts: str, channel_id: str) -> None:
    pm_conn.execute(
        "INSERT OR REPLACE INTO slack_extractions (thread_ts, channel_id, extracted_at) VALUES (?,?,?)",
        (thread_ts, channel_id, datetime.now().isoformat()),
    )


# --------------------------------------------------------------------------- #
# LLM 抽出
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のSlackスレッドのメッセージを読み、決定事項とアクションアイテムを抽出してください。

## アクションアイテムの定義（厳守）

アクションアイテムとは **プロジェクトを推進するうえで欠かせない作業で、明確なアウトプットがあるもの** に限る。
以下の基準を **すべて** 満たすものだけを抽出すること:

1. **第三者からの依頼・合意された作業である**: 投稿者が他者に依頼している、または会議・スレッドで担当を決めて合意したもの。
   投稿者自身による自発的な意志表明（「〜します」「〜する予定」「〜を目指す」「〜したい」）や、
   既に完了した作業の事後報告（「〜しました」「〜をリリースしました」「〜を実施した」）は **抽出しない**。
2. **未来に向けた未完了の作業である**: 過去形・完了形（「〜した」「〜済」「〜完了」「〜しました」）で書かれた進捗報告は抽出しない。
3. **具体的な成果物・アウトプットがある**: 報告書、資料、設計書、コード、見積もり、提案書など、形のある成果物が生まれる作業であること。
4. **プロジェクト推進に不可欠**: その作業が完了しないと後続の意思決定やマイルストーン達成に支障が出ること。
5. **担当者が特定可能**: 誰がやるかがスレッド中に明示されていること。担当が「?」「未定」「正メンバー」のような不明確な記載しか得られない場合は抽出しない。

以下は **抽出しない**（誤抽出が頻発するため特に注意）:
- **進捗報告・宣言**: 「Gromacsベータ版をリリースしました」「ベンチマークの完成を目指します」「対応中です」など
- **会議運営事項**: アジェンダ作成・Zoom URL投稿・カレンダー招待・ミーティング日程調整・会議への招集
  - アジェンダの中身が技術的な議題（性能ギャップ調査・統合状況等）であっても、本体が「ミーティングを開催する／設定する／セットアップする」であれば会議運営事項として扱い抽出しない
  - 議題そのものを実行する具体的な作業（例:「性能ギャップ調査結果をレポートにまとめて期限内に提出する」）が明示されている場合のみ、その作業を抽出する
- **日常的な確認・周知作業**: 「確認する」「チェックする」「共有する」「展開する」「周知する」「連絡する」だけのもの
- **定期的な繰り返し作業**: 「スケジュールの更新」「議事録の確認」「TWIの更新」など毎週/毎月発生するルーチン
- **単なる会議開催・日程調整**: 「〜について議論する」「ミーティングを設定する」「打合せを実施する」
- **Slack上の連絡・伝達行為**: 「〜をSlackで共有する」「〜に連絡する」「〜に声掛けする」
- **一過性の事務手続き**: 「出席登録」「欠席連絡」「チャンネルへの追加」「アカウント削除」「カレンダー招待送付」
- **資料アップロード・投稿の指示**: 「Boxフォルダへアップロード」「スライドを投稿」など格納先を指示するだけのもの

## Few-shot 判定例

**抽出する例 (✓)**:
- 「富岳太郎さん、Gromacsの性能評価に関する契約状況（MoU等）を確認してください。フレームワーク本体への導入が止まっているため、別契約の有無を明確化する必要があります。」
  → 担当者明確 / 第三者依頼 / 後続作業のブロッカー解消
- 「富岳次郎、デベロッパーサーベイを作成して各SubWGへ送付してください。今後の開発支援方針を決定するための基礎資料です。期限: 5/31」
  → 担当者・期限・成果物・背景がすべて明示
- 「富岳太郎さんからのコメントで、Gromacsに関しては別の契約（MoU?）が必要との指摘があったため、契約状況を整理して関係者に共有する必要がある。」
  → 文中で名前が示された担当者（富岳太郎さん）に対する確認依頼。間接的な依頼表現でも、担当・必要性・成果物（整理結果の共有）が読み取れれば抽出する

**抽出しない例 (✗)**:
- 「OpenFOAMベータ版をリリースしました。GitHub上で公開されました。」
  → 完了済の進捗報告
- 「現行のmainブランチを用いて、各環境でのベンチマーク完成を目指します。」
  → 投稿者自身の意志表明・抽象的な目標
- 「ISCでのミーティングのセットアップを行う。」
  → 会議運営事項
- 「ISCでHeCBenchに関するミーティングを開催したい。アジェンダ: NVIDIAによる性能ギャップ調査、Kokkos版の進捗、F2Kokkosの活用促進。」
  → 議題（アジェンダ）が技術的でも、本体は「ミーティングの開催・設定」なので会議運営事項として除外。
    アジェンダ内の各議題を実行する具体的作業（成果物・期限・担当が明示されたもの）が別途あればそちらは抽出する
- 「アーキテクチャ会議用のスライド資料を指定のBoxフォルダへアップロードする。」
  → 資料格納先の指示・会議運営の付随作業
- 「次回のミーティング（5/18）に向けてアプリ進捗を更新してください。」
  → 定期更新・会議運営の付随作業
- 「2026年度のスケジュールを更新してください。」
  → ルーチン更新作業（担当も「各エリアリーダー」と曖昧）

## 決定事項の定義（厳守）

決定事項とは **意思決定者による判断・方針決定** に限る。
以下の基準を **すべて** 満たすものだけを抽出すること:

1. **意思決定者による合意・判断である**: プロジェクト・組織として方針を決めた・選んだ・承認したという内容。
2. **未来の行動・状態を規定する**: 今後どう進めるかを示すもの。過去形の進捗報告・状況報告は決定事項ではない。
3. 種別はいずれか:
   - **方針・戦略の決定**: プロジェクトの進め方、技術選定、開発方針に関する決定
   - **リソース配分の決定**: 予算、人員、計算資源の割り当てに関する決定
   - **スケジュール・スコープの変更**: マイルストーン期限の変更、機能の追加・削除
   - **対外的な合意・承認**: 他組織との取り決め、承認事項

以下は **抽出しない**（誤抽出が頻発するため特に注意）:
- **会議運営に関する取り決め**: 「ミーティングを開催する」「次回は〇月〇日に開催」「アジェンダに追加する」「Zoom URLを発行する」
  - アジェンダの中身が技術的議題（性能評価・統合状況等）であっても、本体が「会議の開催・設定」であれば抽出しない
- **進捗報告・状況報告**: 「〜しました」「〜が完了した」「〜をリリースした」「ブランチを変更しました」「〜を更新しました」
  - 過去形・完了形で書かれた事実の報告は、たとえ運用変更を含んでいても決定事項ではない
- 情報の共有・報告（「〜が判明した」「〜の状況を報告した」）
- 既知事実の確認（「〜であることを確認した」）
- アクションアイテムの言い換え（担当者への作業依頼を決定事項として重複記載しない）

## 決定事項の Few-shot 判定例

**抽出する例 (✓)**:
- 「Co-Designレビューでの議論の結果、Scale-upネットワークはNVL4方式を採用する方針に決定した。」
  → 意思決定者による合意・技術選定
- 「2026年度予算のうち、ベンチマークWGに XX 万円を割り当てることが承認された。」
  → リソース配分の決定

**抽出しない例 (✗)**:
- 「Gromacsベータ版をリリースしました。GitHub上で公開されました。」
  → 完了済の進捗報告（決定事項ではない）
- 「ベンチマークリポジトリのdevelopブランチをFN_appsブランチへ名称変更しました。」
  → 過去形の運用変更通知（事実の報告であり、意思決定者による方針決定ではない）
- 「ISCでHeCBenchおよびF2Kokkosに関するミーティングを開催する。」
  → 会議運営事項（アジェンダが技術的でも会議開催そのものは決定事項ではない）
- 「次回ミーティングを5/18に開催する。」
  → 会議運営事項

## その他の指示

1. **明示されたものだけ抽出**: メッセージに明示されていない内容を推測・補完しないこと
2. **出力形式**: 必ず以下のJSON形式のみ出力すること（前後の説明テキスト不要）
3. 決定事項・アクションアイテムがない場合は空配列 `[]` を返すこと。**大半のスレッドは空配列が正しい。**
4. **マイルストーン紐づけ**: 各アクションアイテムについて、下記「マイルストーン一覧」の
   いずれかに明らかに関連する場合は milestone_id を記入すること。判断できない場合は null。
5. **content は2〜3文で記述**: (1) 何をするか (2) なぜ必要か・背景 (3) 期待される成果物。
   1文だけの曖昧な記述（例:「予算の確認」「資料の作成」）は不可。

## マイルストーン一覧

{milestones}

## 過去の関連議論・決定事項（参考情報）

{knowledge_context}

## プロジェクト文脈

{context}

## Slackスレッド

投稿日時: {timestamp}
投稿者: {user_name}
{thread_text}

## 出力JSON形式

```json
{{
  "decisions": [
    {{
      "content": "決定事項の内容（意思決定の結論とその理由・影響を1〜2文で）",
      "decided_at": "YYYY-MM-DD または null"
    }}
  ],
  "action_items": [
    {{
      "content": "何をするか・なぜ必要か・期待される成果物を2〜3文で記述",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null",
      "milestone_id": "マイルストーンID（M1等）または null"
    }}
  ]
}}
```
"""


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


def _sample_extractions(prompt: str, n: int) -> list[dict]:
    """同一プロンプトを N 回サンプリングし、JSON 抽出に成功したドラフトのリストを返す。

    OPENAI_API_BASE が設定されていれば call_local_llm で temperature を僅かに振って
    多様性を確保する。RiVault フォールバック時は temperature 制御不可のため
    1 回だけ call_claude を呼ぶ（その場合 N=1 相当）。
    """
    if n <= 1:
        raw = call_claude(prompt)
        try:
            return [extract_json(raw)]
        except Exception:
            return []

    base_url = os.environ.get("OPENAI_API_BASE")
    if not base_url:
        # ローカル vLLM が無い場合は temperature 多様化できないため単発に縮退
        try:
            return [extract_json(call_claude(prompt))]
        except Exception:
            return []

    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    model = os.environ.get("OPENAI_MODEL") or detect_vllm_model(base_url)
    max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))

    # n=3 → -0.1, 0, +0.1
    if n == 2:
        deltas = [-0.05, 0.05]
    else:
        step = 0.2 / (n - 1)
        deltas = [-0.1 + step * i for i in range(n)]
    base_t = 0.6  # gemma4 reasoning モードの推奨温度

    drafts: list[dict] = []
    for i, d in enumerate(deltas, 1):
        t = max(0.05, min(1.5, base_t + d))
        try:
            text = call_local_llm(
                prompt, model=model, base_url=base_url, api_key=api_key,
                timeout=600, think=True, max_tokens=max_tokens,
                temperature=t,
            )
            if not text:
                # 空応答はストリーミング崩れの可能性 → 非ストリーミングでリトライ
                text = call_local_llm(
                    prompt, model=model, base_url=base_url, api_key=api_key,
                    timeout=600, think=True, max_tokens=max_tokens,
                    no_stream=True, temperature=t,
                )
        except Exception as e:
            print(f"[WARN] Slack 抽出サンプル {i}/{n} 失敗: {e}", file=sys.stderr)
            continue
        if not text or not text.strip():
            continue
        try:
            drafts.append(extract_json(text))
        except Exception as e:
            print(f"[WARN] Slack 抽出サンプル {i}/{n} JSON パース失敗: {e}", file=sys.stderr)
    return drafts


def _consensus_decisions(drafts: list[dict], min_vote: int, threshold: float) -> list[dict]:
    """各ドラフトの decisions をクラスタ化し、min_vote 以上の独立票を得たクラスタから代表を採用する。"""
    flat: list[tuple[int, dict]] = []  # (draft_idx, decision)
    for di, d in enumerate(drafts):
        for item in d.get("decisions", []) or []:
            content = (item or {}).get("content")
            if content and content.strip():
                flat.append((di, item))
    if not flat:
        return []
    keys = [item["content"] for _, item in flat]
    try:
        from embed_utils import embed_batch, cosine_similarity_matrix
        import numpy as np
        vecs = embed_batch(keys)
        clusters: list[list[int]] = []
        centers = []
        for i, v in enumerate(vecs):
            if not clusters:
                clusters.append([i]); centers.append(v.copy()); continue
            sims = cosine_similarity_matrix(v, np.stack(centers))
            best = int(np.argmax(sims))
            if float(sims[best]) >= threshold:
                clusters[best].append(i)
                n_old = len(clusters[best]) - 1
                centers[best] = (centers[best] * n_old + v) / (n_old + 1)
            else:
                clusters.append([i]); centers.append(v.copy())
    except Exception as e:
        print(f"[ERROR] Slack 決定事項 embedding 失敗、最初のドラフトを採用: {e}", file=sys.stderr)
        return list(drafts[0].get("decisions") or []) if drafts else []

    accepted: list[dict] = []
    for cl in clusters:
        if len({flat[i][0] for i in cl}) < min_vote:
            continue
        # 代表選定: content が最長で decided_at が埋まっているものを優先
        cl_items = [flat[i][1] for i in cl]
        rep = max(cl_items, key=lambda d: (bool(d.get("decided_at")), len(d.get("content") or "")))
        accepted.append(rep)
    return accepted


def _consensus_action_items(drafts: list[dict], min_vote: int, threshold: float) -> list[dict]:
    """各ドラフトの action_items をクラスタ化し、min_vote 以上の独立票を得たクラスタから代表を採用する。

    クラスタリングキー: `[担当者] content` — 担当者違いは別クラスタ扱い。
    """
    flat: list[tuple[int, dict]] = []
    for di, d in enumerate(drafts):
        for item in d.get("action_items", []) or []:
            content = (item or {}).get("content")
            if content and content.strip():
                flat.append((di, item))
    if not flat:
        return []
    keys = [
        f"[{(item.get('assignee') or '未定')}] {item.get('content') or ''}"
        for _, item in flat
    ]
    try:
        from embed_utils import embed_batch, cosine_similarity_matrix
        import numpy as np
        vecs = embed_batch(keys)
        clusters: list[list[int]] = []
        centers = []
        for i, v in enumerate(vecs):
            if not clusters:
                clusters.append([i]); centers.append(v.copy()); continue
            sims = cosine_similarity_matrix(v, np.stack(centers))
            best = int(np.argmax(sims))
            if float(sims[best]) >= threshold:
                clusters[best].append(i)
                n_old = len(clusters[best]) - 1
                centers[best] = (centers[best] * n_old + v) / (n_old + 1)
            else:
                clusters.append([i]); centers.append(v.copy())
    except Exception as e:
        print(f"[ERROR] Slack AI embedding 失敗、最初のドラフトを採用: {e}", file=sys.stderr)
        return list(drafts[0].get("action_items") or []) if drafts else []

    accepted: list[dict] = []
    for cl in clusters:
        if len({flat[i][0] for i in cl}) < min_vote:
            continue
        cl_items = [flat[i][1] for i in cl]
        # 代表選定: due_date / milestone_id が埋まっており content が最長のものを優先
        rep = max(
            cl_items,
            key=lambda a: (
                bool(a.get("due_date")),
                bool(a.get("milestone_id")),
                bool(a.get("assignee")),
                len(a.get("content") or ""),
            ),
        )
        accepted.append(rep)
    return accepted


def extract_from_thread(
    row: dict,
    context: str,
    milestones: list[dict],
    repo_root: Path = None,
    *,
    consensus_n: int = 1,
    consensus_threshold: float = 0.78,
    consensus_min_vote: int | None = None,
) -> dict:
    # ナレッジ検索（Phase 2追加）— 統合 qa_index.db の pm-all で全件横断
    knowledge_context = retrieve_knowledge_for_extraction(
        row["thread_text"],
        qa_db_path=(repo_root or REPO_ROOT) / "data" / "qa_index.db",
        top_k=3,
        index_name="pm-all",
    )

    prompt = EXTRACT_PROMPT.format(
        context=context,
        knowledge_context=knowledge_context,
        timestamp=row.get("timestamp", "不明"),
        user_name=row.get("user_name", "不明"),
        thread_text=row["thread_text"],
        milestones=format_milestones_for_prompt(milestones),
    )

    if consensus_n <= 1:
        raw = call_claude(prompt)
        return extract_json(raw)

    drafts = _sample_extractions(prompt, consensus_n)
    if not drafts:
        return {"decisions": [], "action_items": []}
    if len(drafts) == 1:
        # サンプルが 1 件しか得られなかった場合は集約しない（投票不可）
        print(f"[WARN] Slack consensus: ドラフトが {len(drafts)}/{consensus_n} 件のみ。集約せず採用", file=sys.stderr)
        return drafts[0]

    min_vote = consensus_min_vote if consensus_min_vote is not None else math.ceil(len(drafts) / 2)
    decisions = _consensus_decisions(drafts, min_vote, consensus_threshold)
    action_items = _consensus_action_items(drafts, min_vote, consensus_threshold)
    print(
        f"[INFO] Slack consensus: {len(drafts)}/{consensus_n} ドラフト, min_vote={min_vote} → "
        f"decisions={len(decisions)}, action_items={len(action_items)}",
        file=sys.stderr,
    )
    return {"decisions": decisions, "action_items": action_items}


# --------------------------------------------------------------------------- #
# pm.db 書き込み
# --------------------------------------------------------------------------- #
def save_slack_items(
    pm_conn,
    thread_ts: str,
    channel_id: str,
    permalink: str | None,
    timestamp: str,
    extracted: dict,
) -> tuple[int, int]:
    post_date = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
    source_ref = permalink or f"slack://{channel_id}/{thread_ts}"
    fallback_ref = f"slack://{channel_id}/{thread_ts}"

    # 再抽出時の重複防止: 同一スレッド由来の既存 slack レコードを削除してから INSERT する。
    # permalink が後から付与・変更されるケースを考慮し、fallback 形式の source_ref も対象に含める。
    pm_conn.execute(
        "DELETE FROM action_items WHERE source='slack' AND source_ref IN (?, ?)",
        (source_ref, fallback_ref),
    )
    pm_conn.execute(
        "DELETE FROM decisions WHERE source='slack' AND source_ref IN (?, ?)",
        (source_ref, fallback_ref),
    )

    d_count = 0
    for d in extracted.get("decisions", []):
        if not d.get("content"):
            continue
        decided_at = d.get("decided_at") or post_date
        pm_conn.execute(
            "INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref,"
            " extracted_at, channel_id)"
            " VALUES (?, ?, ?, 'slack', ?, ?, ?)",
            (None, d["content"], decided_at, source_ref, post_date, channel_id),
        )
        d_count += 1

    a_count = 0
    for a in extracted.get("action_items", []):
        if not a.get("content"):
            continue
        pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref,"
            " extracted_at, milestone_id, channel_id)"
            " VALUES (?, ?, ?, ?, 'open', 'slack', ?, ?, ?, ?)",
            (None, a["content"], normalize_assignee(a.get("assignee")), a.get("due_date"),
             source_ref, post_date, a.get("milestone_id"), channel_id),
        )
        a_count += 1

    return d_count, a_count


# --------------------------------------------------------------------------- #
# 抽出済み一覧表示
# --------------------------------------------------------------------------- #
def cmd_list_extractions(slack_conn, pm_conn, channel_id: str, since: str | None, log=print) -> None:
    se_query = "SELECT thread_ts, extracted_at FROM slack_extractions WHERE channel_id = ?"
    se_params: list = [channel_id]
    if since:
        se_query += " AND extracted_at >= ?"
        se_params.append(since)

    se_rows = pm_conn.execute(se_query, se_params).fetchall()

    ts_map: dict[str, str] = {}
    if se_rows:
        placeholders = ",".join("?" * len(se_rows))
        ts_rows = slack_conn.execute(
            f"SELECT thread_ts, timestamp FROM messages WHERE channel_id = ? AND thread_ts IN ({placeholders})",
            [channel_id] + [r["thread_ts"] for r in se_rows],
        ).fetchall()
        ts_map = {r["thread_ts"]: r["timestamp"] for r in ts_rows}

    sorted_rows = sorted(se_rows, key=lambda r: ts_map.get(r["thread_ts"], r["extracted_at"]))

    log(f"抽出済みスレッド一覧（チャンネル: {channel_id}）")
    log("─" * 50)
    for i, row in enumerate(sorted_rows, 1):
        ts = (ts_map.get(row["thread_ts"]) or "")[:19]
        extracted = (row["extracted_at"] or "")[:19]
        log(f"[{i:3d}] {ts}  抽出: {extracted}")
    log(f"合計: {len(sorted_rows)} 件")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class SlackIngestPlugin:
    source_name = "slack"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--slack-channel", default=DEFAULT_CHANNEL,
            metavar="CHANNEL_ID",
            help="対象チャンネルID（slack ソース用、未指定時は環境変数 PM_DEFAULT_SLACK_CHANNEL）",
        )
        parser.add_argument(
            "--slack-db", default=None,
            metavar="PATH",
            help="Slack DB のパス（slack ソース用、省略時は data/slack.db）",
        )
        parser.add_argument(
            "--slack-force-reextract", action="store_true",
            help="抽出済みスレッドも再処理（slack ソース用）",
        )
        parser.add_argument(
            "--slack-list", action="store_true",
            help="抽出済みスレッドの一覧を表示して終了（slack ソース用）",
        )
        parser.add_argument(
            "--slack-consensus", type=int, default=3, metavar="N",
            help="Self-Consistency サンプリング数（デフォルト: 3。1 で従来動作の単発抽出）",
        )
        parser.add_argument(
            "--slack-consensus-threshold", type=float, default=0.78, metavar="FLOAT",
            help="Self-Consistency クラスタリングの cosine 閾値（デフォルト: 0.78）",
        )
        parser.add_argument(
            "--slack-consensus-min-vote", type=int, default=None, metavar="INT",
            help="Self-Consistency クラスタ採用に必要な独立票数（デフォルト: ⌈N/2⌉）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        channel_id = args.slack_channel
        slack_db_path = (
            Path(args.slack_db) if args.slack_db
            else ctx.repo_root / "data" / "slack.db"
        )

        slack_conn = open_slack_db(slack_db_path, no_encrypt=ctx.no_encrypt)
        ensure_slack_extractions(ctx.pm_conn)

        if getattr(args, "slack_list", False):
            cmd_list_extractions(slack_conn, ctx.pm_conn, channel_id, ctx.since, log=ctx.log)
            slack_conn.close()
            return

        ctx.log(f"[INFO] チャンネル  : {channel_id}")
        ctx.log(f"[INFO] Slack DB    : {slack_db_path}")
        if ctx.since:
            ctx.log(f"[INFO] since       : {ctx.since}")

        context = load_context_from_claude_md()
        milestones = fetch_milestones(ctx.pm_conn)
        ctx.log(f"[INFO] マイルストーン: {len(milestones)} 件")

        threads = fetch_threads(slack_conn, channel_id, ctx.since)
        ctx.log(f"[INFO] 対象スレッド: {len(threads)} 件")

        total_d = total_a = skipped = 0
        force_reextract = getattr(args, "slack_force_reextract", False)
        consensus_n = getattr(args, "slack_consensus", 3)
        consensus_threshold = getattr(args, "slack_consensus_threshold", 0.78)
        consensus_min_vote = getattr(args, "slack_consensus_min_vote", None)
        if consensus_n >= 2:
            ctx.log(
                f"[INFO] Self-Consistency 有効: N={consensus_n}, "
                f"threshold={consensus_threshold}, min_vote={consensus_min_vote or '⌈N/2⌉'}"
            )

        for i, row in enumerate(threads, 1):
            ts = row["thread_ts"]
            if not force_reextract and is_already_extracted(ctx.pm_conn, ts, channel_id):
                skipped += 1
                continue

            ctx.log(f"\n[{i}/{len(threads)}] {row.get('user_name')} ({row.get('timestamp', '')[:16]})")

            if ctx.dry_run:
                ctx.log("  [INFO] --dry-run のため LLM呼び出し・DB保存をスキップしました")
                skipped += 1
                continue

            try:
                extracted = extract_from_thread(
                    row, context, milestones, ctx.repo_root,
                    consensus_n=consensus_n,
                    consensus_threshold=consensus_threshold,
                    consensus_min_vote=consensus_min_vote,
                )
            except Exception as e:
                ctx.log(f"  [WARN] 抽出失敗: {e}")
                continue

            d_count = len(extracted.get("decisions", []))
            a_count = len(extracted.get("action_items", []))

            if d_count == 0 and a_count == 0:
                ctx.log("  → 決定事項・アクションアイテムなし")
            else:
                for d in extracted.get("decisions", []):
                    ctx.log(f"  [決定] {d['content']}")
                for a in extracted.get("action_items", []):
                    assignee = a.get("assignee") or "未定"
                    due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
                    ctx.log(f"  [AI  ] [{assignee}] {a['content']}{due}")

            nd, na = save_slack_items(
                ctx.pm_conn, ts, channel_id,
                row.get("permalink"), row.get("timestamp", ""), extracted,
            )
            mark_extracted(ctx.pm_conn, ts, channel_id)
            ctx.pm_conn.commit()
            total_d += nd
            total_a += na

        slack_conn.close()

        ctx.log("\n" + "=" * 60)
        ctx.log(f"完了: decisions={total_d}件, action_items={total_a}件, スキップ={skipped}件")
