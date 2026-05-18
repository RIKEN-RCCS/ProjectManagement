#!/usr/bin/env python3
"""
pm_box_distill.py — 蒸留ナレッジレイヤ knowledge.db への投入スクリプト

box_docs.db (本文 Markdown), data/minutes/{kind}.db (議事録), pm.db.decisions
を入力として、ローカル LLM で「意思決定 / 制約 / 立場 / 用語」の単位に蒸留し
data/knowledge.db に書き込む。

設計原則は docs/distill_policy.md 参照:
- confidence='low' は書き込まない（採否の足切り）
- distill_state で入力ハッシュを記録し冪等な再蒸留を可能にする
- 物理削除しない（人手介入時も deleted=1 のみ）
- index_name 等のチャンネル別分割は持たない（プロジェクト全体共通）

Usage:
  # 全ソース・新規/変更分のみ蒸留
  python3 scripts/pm_box_distill.py

  # ソース指定
  python3 scripts/pm_box_distill.py --source box
  python3 scripts/pm_box_distill.py --source minutes
  python3 scripts/pm_box_distill.py --source decisions

  # 期間指定
  python3 scripts/pm_box_distill.py --since 2026-04-01

  # 確認のみ（DB更新なし）
  python3 scripts/pm_box_distill.py --dry-run

  # 既存蒸留済みも再処理
  python3 scripts/pm_box_distill.py --force

  # 統計表示
  python3 scripts/pm_box_distill.py --stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_utils import call_argus_llm, strip_think_blocks, make_logger
from db_utils import (
    init_knowledge_db,
    open_knowledge_db,
    open_db,
    open_pm_db,
    next_knowledge_id,
)
from embed_utils import (
    embed_batch,
    embed_one,
    cosine_similarity_matrix,
    vector_to_blob,
    blob_to_vector,
    healthcheck as embed_healthcheck,
)
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
KNOWLEDGE_DB = DATA_DIR / "knowledge.db"
BOX_DOCS_DB = DATA_DIR / "box_docs.db"
MINUTES_DIR = DATA_DIR / "minutes"
PM_DB = DATA_DIR / "pm.db"

# 1 入力あたりにLLMへ渡す最大文字数（プロンプト圧迫を避ける）
_MAX_INPUT_CHARS = 12000

# 蒸留採否の重要なポリシー（プロンプト直挿し）。
# docs/distill_policy.md の運用基準に加え、実観測のノイズパターン（段階1）を明示する。
_DISTILL_POLICY = """\
## 抽出する対象
- アーキテクチャ選択（例: Scale-up ドメインサイズの確定、CPU-GPU 接続方式）
- 外部関係者との合意事項（理研 / 富士通 / NVIDIA 三者の決定）
- 長期にわたって参照される制約・前提条件（FP8 ゼタスケール目標、温水冷却前提など）
- 富岳NEXT プロジェクト固有の用語定義（独自の略号・概念）
- 立場の表明（重要ステークホルダーの方針声明）

## 抽出してはいけない対象（観測されたノイズパターン）
**業界標準・記法慣例**
- 単位表記の慣例（"1EFLOPS = 1,000PFLOPS"、"1Gbps = 1,000Mbps" 等の 10 進表記）
- 業界標準の数値・単位表記方針

**形式的・定型情報**
- プロジェクトの正式名称・略称・調達案件名の定義（"富岳NEXT"・"次世代計算基盤技術検証..."）
- 議事録の冒頭定型文・参加者・配布先・議事録番号
- 落札方式・契約形態など調達文書の定型条項
- 形式的な承認（議事録レビュー承認、議事録掲載許可）

**短期・暫定情報**
- 1 回限りの会議運営事項（時刻変更、開催場所、Zoom URL）
- 当日中に消費されるアクションアイテム（pm.db.action_items の領域）
- 個人の暫定見解（チーム合意に至っていない発言）
- "〜と仮定して見積もる" のような検討中の作業仮定（同じ仮定が他資料で何度も引用されているならそれは作業仮定であって意思決定ではない）

**既知の体制説明**
- 「理研が主導」「富士通とNVIDIAが協力」のような既存の体制
- ベンダーの一般的な役割分担

**既に上書きされた情報**
- 既に新しい意思決定で上書き済みのもの

## judgement の心得
プロジェクトを知らない PM が読んで「**これは富岳NEXT 固有の意思決定だ**」と
納得できる内容のみ採用する。ただ書かれているだけ、業界の常識、
既知の前提のリピートは採用しない。**迷ったら採用しない**。

## confidence の付け方
- `high`: 議事録に明示された決定事項、外部関係者との合意、複数ソースで一致
- `medium`: 1 ソースのみだが内容が明確
- `low`: 自信がないもの → このレコードは出力に含めなくて良い

抽出不能・該当なしと判断したら "items": [] を返す。LLM の推測のみで根拠のないレコードは出さない。
"""

_DISTILL_PROMPT = """\
あなたは富岳NEXTプロジェクトのナレッジ抽出AIです。
以下の入力テキストから、プロジェクト全体に渡って共有される「意思決定 / 制約 / 立場 / 用語」を抽出します。

{policy}

## 入力ソース
- ソース種別: {source_type}
- ソース参照: {source_ref}
- 関連メタ情報: {source_meta}

## 入力本文
```
{content}
```

## 出力形式（厳密にこの JSON のみを返す。前置き・後置き・コードフェンスなし）
{{
  "items": [
    {{
      "kind": "decision" | "constraint" | "position" | "glossary",
      "topic": "1行サマリ（30字以内）",
      "current_state": "現在の状態・採用案（80字以内）",
      "rationale": "根拠・採用理由（200字以内、不明なら空文字）",
      "alternatives_rejected": ["却下案1", "却下案2"],
      "constraints_invariants": ["制約1", "制約2"],
      "tags": ["architecture", "scale-up"],
      "owners": ["近藤", "佐野"],
      "decided_at": "YYYY-MM-DD（不明なら空文字）",
      "confidence": "high" | "medium",
      "excerpt": "該当箇所の抜粋（200字以内、トレース用）"
    }}
  ]
}}

## 注意
- "items": [] でも構わない（抽出対象なしの判断）。
- "confidence": "low" に該当する候補は items に含めない。
- excerpt は必ず入力本文中の文字列をコピーする（要約しない）。
- 1 入力から複数件抽出してもよい。
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_jst() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 入力ソースの収集
# --------------------------------------------------------------------------- #

def collect_box_inputs(*, since: str | None, no_encrypt: bool) -> list[dict]:
    """box_docs.db から蒸留候補を収集。relevance ∈ {core, related} のみ。"""
    if not BOX_DOCS_DB.exists():
        return []
    conn = open_db(BOX_DOCS_DB, encrypt=not no_encrypt)
    try:
        q = (
            "SELECT bf.box_file_id, bf.name, bf.folder_path, bf.modified_at,"
            "  bf.relevance, dc.content_md, dc.content_hash"
            " FROM doc_content dc JOIN box_files bf ON dc.box_file_id = bf.box_file_id"
            " WHERE COALESCE(bf.relevance, '') IN ('core', 'related')"
        )
        params: list = []
        if since:
            q += " AND COALESCE(bf.modified_at, '') >= ?"
            params.append(since)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        content = (r["content_md"] or "").strip()
        if not content:
            continue
        items.append({
            "source_type": "box_file",
            "source_ref": str(r["box_file_id"]),
            "input_hash": r["content_hash"] or hash_text(content),
            "content": content[:_MAX_INPUT_CHARS],
            "meta": {
                "name": r["name"],
                "folder_path": r["folder_path"],
                "modified_at": r["modified_at"],
                "relevance": r["relevance"],
            },
        })
    return items


def collect_minutes_inputs(*, since: str | None, no_encrypt: bool) -> list[dict]:
    """data/minutes/*.db の minutes_content から蒸留候補を収集。"""
    if not MINUTES_DIR.exists():
        return []
    items = []
    for db_file in sorted(MINUTES_DIR.glob("*.db")):
        try:
            conn = open_db(db_file, encrypt=not no_encrypt)
        except Exception:
            continue
        try:
            q = (
                "SELECT i.meeting_id, i.held_at, mc.content"
                " FROM instances i JOIN minutes_content mc ON mc.meeting_id = i.meeting_id"
            )
            params: list = []
            if since:
                q += " WHERE i.held_at >= ?"
                params.append(since)
            for r in conn.execute(q, params).fetchall():
                content = (r["content"] or "").strip()
                if not content:
                    continue
                items.append({
                    "source_type": "minutes",
                    "source_ref": r["meeting_id"],
                    "input_hash": hash_text(content),
                    "content": content[:_MAX_INPUT_CHARS],
                    "meta": {
                        "kind": db_file.stem,
                        "held_at": r["held_at"],
                    },
                })
        except Exception:
            pass
        finally:
            conn.close()
    return items


def collect_decisions_inputs(*, since: str | None, no_encrypt: bool) -> list[dict]:
    """pm.db.decisions（slack 由来も含む）から蒸留候補を収集。"""
    if not PM_DB.exists():
        return []
    conn = open_pm_db(PM_DB, no_encrypt=no_encrypt)
    try:
        q = (
            "SELECT id, content, decided_at, source, source_ref, source_context, channel_id"
            " FROM decisions"
            " WHERE COALESCE(deleted, 0) = 0"
        )
        params: list = []
        if since:
            q += " AND COALESCE(decided_at, extracted_at) >= ?"
            params.append(since)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        body = (r["content"] or "").strip()
        if not body:
            continue
        ctx = r["source_context"] or ""
        full = f"{body}\n\n## 議論の文脈\n{ctx}" if ctx else body
        items.append({
            "source_type": "decision",
            "source_ref": str(r["id"]),
            "input_hash": hash_text(full),
            "content": full[:_MAX_INPUT_CHARS],
            "meta": {
                "decided_at": r["decided_at"],
                "source": r["source"],
                "source_ref": r["source_ref"],
                "channel_id": r["channel_id"],
            },
        })
    return items


# --------------------------------------------------------------------------- #
# LLM 蒸留
# --------------------------------------------------------------------------- #

def distill_one(item: dict, log) -> list[dict]:
    """1 入力に対して LLM を呼び、items リストを返す。失敗時は []。"""
    prompt = _DISTILL_PROMPT.format(
        policy=_DISTILL_POLICY,
        source_type=item["source_type"],
        source_ref=item["source_ref"],
        source_meta=json.dumps(item.get("meta", {}), ensure_ascii=False),
        content=item["content"],
    )
    try:
        raw = call_argus_llm(
            prompt,
            timeout=300,
            max_tokens=4096,
            system="あなたは富岳NEXTプロジェクトのナレッジ蒸留AIです。",
        )
    except Exception as e:
        log(f"  [WARN] LLM 呼び出し失敗 ({item['source_type']}/{item['source_ref']}): {e}")
        return []

    raw = strip_think_blocks(raw or "").strip()
    # コードフェンス除去
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    # 最初の { から最後の } までを抽出
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        log(f"  [WARN] LLM 応答が JSON でない ({item['source_type']}/{item['source_ref']})")
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except Exception as e:
        log(f"  [WARN] JSON パース失敗 ({item['source_type']}/{item['source_ref']}): {e}")
        return []

    items = data.get("items") or []
    out = []
    for it in items:
        kind = (it.get("kind") or "").strip().lower()
        confidence = (it.get("confidence") or "").strip().lower()
        if kind not in {"decision", "constraint", "position", "glossary"}:
            continue
        # 採否ポリシー: low は書き込まない
        if confidence not in {"high", "medium"}:
            continue
        topic = (it.get("topic") or "").strip()
        current = (it.get("current_state") or "").strip()
        if not topic or not current:
            continue
        out.append({
            "kind": kind,
            "topic": topic,
            "current_state": current,
            "rationale": (it.get("rationale") or "").strip(),
            "alternatives_rejected": json.dumps(it.get("alternatives_rejected") or [], ensure_ascii=False),
            "constraints_invariants": json.dumps(it.get("constraints_invariants") or [], ensure_ascii=False),
            "tags": json.dumps(it.get("tags") or [], ensure_ascii=False),
            "owners": json.dumps(it.get("owners") or [], ensure_ascii=False),
            "decided_at": (it.get("decided_at") or "").strip() or None,
            "confidence": confidence,
            "excerpt": (it.get("excerpt") or "").strip(),
        })
    return out


# --------------------------------------------------------------------------- #
# 段階3: Stage 2 — 採否ゲート（embedding 類似度 + LLM 判定）
# --------------------------------------------------------------------------- #

# bge-m3 のしきい値（コサイン類似度）
DEFAULT_MERGE_THRESHOLD = 0.92    # >= はそのまま既存に merge（追加しない）
DEFAULT_REVIEW_THRESHOLD = 0.85   # >= は LLM に「同じ意味か？」を問う

EMBED_MODEL_NAME = "bge-m3:567m"  # 記録用ラベル


_QUALITY_PROMPT = """\
あなたは富岳NEXTプロジェクトのナレッジ品質審査AIです。
新規ナレッジ候補が以下の基準を満たすか厳しく判定してください。

## 採用基準（すべてを満たす場合のみ keep）
1. プロジェクトを知らない PM が読んで「これは富岳NEXT 固有の意思決定だ」と納得できる
2. 個別議事録に閉じた情報ではない
3. 既存ナレッジと内容が異なる
4. 業界標準・形式的情報・短期作業仮定ではない

## 不採用パターン（いずれかに該当したら drop）
- 業界標準の単位表記・記法慣例
- プロジェクト名称・調達案件名の定義のような形式情報
- 議事録の冒頭定型文・落札方式・契約形態
- "〜と仮定して見積もる" のような短期作業仮定
- 既知の体制説明（"理研が主導" など）
- 既に上書きされた情報

## 既存ナレッジ判定
類似度の高い既存ナレッジが提示されている場合:
- 内容が**同じ意味**なら verdict="merge_with"、merge_target に既存 KN-XXXX を指定
- 内容が**異なる**なら verdict="keep" でよい（補強情報として）

## 入力
{candidates_block}

## 出力（厳密にこの JSON のみを返す。前置き・後置き・コードフェンスなし）
{{
  "judgements": [
    {{
      "candidate_index": 0,
      "verdict": "keep" | "drop" | "merge_with",
      "merge_target": "KN-XXXX (verdict=merge_with の時のみ。それ以外は空文字)",
      "reason": "1行の根拠"
    }}
  ]
}}

迷ったら drop。candidate_index は入力の通し番号と一致させてください。
"""


def fetch_existing_embeddings(
    kdb,
) -> tuple[list[dict], np.ndarray]:
    """knowledge.db の現役レコード（superseded_by IS NULL かつ deleted=0）の
    埋め込み済みデータを (records, matrix) で返す。"""
    rows = kdb.execute(
        "SELECT k.id, k.kind, k.topic, k.current_state, k.confidence,"
        "       e.dim, e.vector"
        " FROM knowledge k"
        " JOIN knowledge_embeddings e ON e.knowledge_id = k.id"
        " WHERE COALESCE(k.deleted, 0) = 0 AND k.superseded_by IS NULL"
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    records = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "topic": r["topic"] or "",
            "current_state": r["current_state"] or "",
            "confidence": r["confidence"] or "",
        }
        for r in rows
    ]
    dim = rows[0]["dim"]
    mat = np.stack([blob_to_vector(r["vector"], dim=dim) for r in rows])
    return records, mat


def upsert_embedding(kdb, knowledge_id: str, vec: np.ndarray) -> None:
    """knowledge_embeddings に埋め込みを upsert する。"""
    kdb.execute(
        "INSERT OR REPLACE INTO knowledge_embeddings"
        " (knowledge_id, model, dim, vector, embedded_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (knowledge_id, EMBED_MODEL_NAME, vec.shape[0],
         vector_to_blob(vec), now_iso()),
    )


def find_similar_existing(
    candidate_vec: np.ndarray,
    existing_records: list[dict],
    existing_matrix: np.ndarray,
    *,
    top_k: int = 5,
) -> list[tuple[dict, float]]:
    """1 候補に対して既存レコードから上位 top_k 件を類似度付きで返す。"""
    if existing_matrix.size == 0:
        return []
    sims = cosine_similarity_matrix(candidate_vec, existing_matrix)
    order = np.argsort(-sims)[:top_k]
    return [(existing_records[i], float(sims[i])) for i in order]


def llm_quality_judge(
    candidates: list[dict],
    similar_lists: list[list[tuple[dict, float]]],
    log,
) -> list[dict]:
    """候補と類似既存リストを LLM に渡して keep/drop/merge_with を判定する。
    返り値: 各候補に対する judgement dict（candidate_index 順）。
    LLM 失敗時は全 drop（保守的）。
    """
    if not candidates:
        return []

    blocks = []
    for i, (cand, sims) in enumerate(zip(candidates, similar_lists)):
        b = [f"### 候補 {i}"]
        b.append(f"- kind: {cand['kind']}")
        b.append(f"- topic: {cand['topic']}")
        b.append(f"- current_state: {cand['current_state']}")
        if cand.get("rationale"):
            b.append(f"- rationale: {cand['rationale']}")
        if sims:
            b.append("- 既存類似ナレッジ:")
            for rec, score in sims:
                b.append(
                    f"  • {rec['id']} (sim={score:.3f}, {rec['kind']}/{rec['confidence']}) "
                    f"{rec['topic']}: {rec['current_state'][:80]}"
                )
        else:
            b.append("- 既存類似ナレッジ: なし")
        blocks.append("\n".join(b))
    prompt = _QUALITY_PROMPT.format(candidates_block="\n\n".join(blocks))

    try:
        raw = call_argus_llm(
            prompt,
            timeout=300,
            max_tokens=4096,
            system="あなたは富岳NEXT プロジェクトのナレッジ品質審査AIです。",
        )
    except Exception as e:
        log(f"  [WARN] LLM 品質判定エラー: {e}（保守的に全 drop します）")
        return [
            {"candidate_index": i, "verdict": "drop",
             "merge_target": "", "reason": f"LLM error: {e}"}
            for i in range(len(candidates))
        ]

    raw = strip_think_blocks(raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        log(f"  [WARN] LLM 品質判定: JSON でない応答（保守的に全 drop）")
        return [
            {"candidate_index": i, "verdict": "drop",
             "merge_target": "", "reason": "LLM 応答が JSON でない"}
            for i in range(len(candidates))
        ]
    try:
        data = json.loads(raw[start: end + 1])
    except Exception as e:
        log(f"  [WARN] LLM 品質判定 JSON parse 失敗: {e}（保守的に全 drop）")
        return [
            {"candidate_index": i, "verdict": "drop",
             "merge_target": "", "reason": "JSON parse failure"}
            for i in range(len(candidates))
        ]

    raw_judgements = data.get("judgements") or []
    by_index = {int(j.get("candidate_index", -1)): j for j in raw_judgements
                if isinstance(j, dict)}
    out = []
    for i in range(len(candidates)):
        j = by_index.get(i, {})
        verdict = (j.get("verdict") or "drop").strip().lower()
        if verdict not in {"keep", "drop", "merge_with"}:
            verdict = "drop"
        out.append({
            "candidate_index": i,
            "verdict": verdict,
            "merge_target": (j.get("merge_target") or "").strip(),
            "reason": (j.get("reason") or "").strip(),
        })
    return out


def stage2_filter(
    distilled: list[dict],
    item: dict,
    kdb,
    *,
    merge_threshold: float,
    review_threshold: float,
    log,
) -> tuple[list[tuple[dict, np.ndarray]], list[tuple[str, str, dict]]]:
    """Stage 2 ゲート。

    Returns:
        keep_records: (distilled_record, embedding_vec) のリスト。
                      呼び出し側で upsert 時に埋め込みもまとめて保存する。
        merges: 既存に統合する組 (existing_kn_id, reason, distilled_record)
                ※ 新規 KN は発番せず、既存レコードの knowledge_sources に
                  当該ソースを追加するだけ。
    """
    if not distilled:
        return [], []

    existing_recs, existing_mat = fetch_existing_embeddings(kdb)

    # 候補テキストを 1 つにまとめて埋め込み
    cand_texts = [
        f"{d['topic']} || {d['current_state']}"
        for d in distilled
    ]
    cand_mat = embed_batch(cand_texts)

    keep: list[tuple[dict, np.ndarray]] = []
    merges: list[tuple[str, str, dict]] = []
    pending_review: list[int] = []
    pending_sims: list[list[tuple[dict, float]]] = []

    for i, d in enumerate(distilled):
        sims = find_similar_existing(cand_mat[i], existing_recs, existing_mat, top_k=5)
        # 高類似度: 自動 merge
        if sims and sims[0][1] >= merge_threshold:
            keeper = sims[0][0]
            log(f"    auto-merge -> {keeper['id']} (sim={sims[0][1]:.3f})")
            merges.append((keeper["id"], f"auto-merge sim={sims[0][1]:.3f}", d))
            continue
        # 中類似度: LLM に審査
        if sims and sims[0][1] >= review_threshold:
            pending_review.append(i)
            pending_sims.append(sims)
            continue
        # 低類似度: 既存類似なしとして LLM 審査
        pending_review.append(i)
        pending_sims.append(sims[:3])

    # LLM Stage 2 判定（候補ありの場合のみ）
    if pending_review:
        review_cands = [distilled[i] for i in pending_review]
        judgements = llm_quality_judge(review_cands, pending_sims, log)
        for j, src_idx in zip(judgements, pending_review):
            d = distilled[src_idx]
            if j["verdict"] == "keep":
                keep.append((d, cand_mat[src_idx]))
                log(f"    keep      {d['topic'][:50]} ({j['reason'][:60]})")
            elif j["verdict"] == "merge_with":
                target = j["merge_target"]
                if target.startswith("KN-"):
                    merges.append((target, f"LLM merge: {j['reason']}", d))
                    log(f"    LLM-merge -> {target} ({j['reason'][:60]})")
                else:
                    # 不正な merge_target は keep に倒す
                    keep.append((d, cand_mat[src_idx]))
                    log(f"    keep (invalid merge_target) {d['topic'][:50]}")
            else:
                log(f"    drop      {d['topic'][:50]} ({j['reason'][:60]})")
    return keep, merges


def apply_merges(
    kdb,
    item: dict,
    merges: list[tuple[str, str, dict]],
) -> int:
    """既存 KN-XXXX への merge を knowledge_sources に追加する形で適用。
    返り値: 実際に追加した行数。"""
    n = 0
    now = now_iso()
    for keeper_id, reason, d in merges:
        # 既存 keeper が現存かつ非削除であることを確認
        row = kdb.execute(
            "SELECT id FROM knowledge"
            " WHERE id = ? AND COALESCE(deleted, 0) = 0",
            (keeper_id,),
        ).fetchone()
        if not row:
            continue
        kdb.execute(
            "INSERT OR IGNORE INTO knowledge_sources"
            " (knowledge_id, source_type, source_ref, weight, excerpt, added_at)"
            " VALUES (?, ?, ?, 'supporting', ?, ?)",
            (keeper_id, item["source_type"], item["source_ref"],
             d.get("excerpt") or "", now),
        )
        kdb.execute(
            "INSERT INTO knowledge_audit"
            " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
            " VALUES (?, '__merge_source__', NULL, ?, ?, 'distill_llm', NULL)",
            (keeper_id,
             json.dumps({
                 "from": f"{item['source_type']}/{item['source_ref']}",
                 "reason": reason,
             }, ensure_ascii=False),
             now),
        )
        n += 1
    return n


# --------------------------------------------------------------------------- #
# DB 書き込み
# --------------------------------------------------------------------------- #

def upsert_knowledge_records(
    kdb,
    item: dict,
    distilled_with_vec: list[tuple[dict, np.ndarray | None]],
    log,
) -> list[str]:
    """蒸留結果を knowledge.db に upsert。produced_knowledge_ids を返す。
    distilled_with_vec の各要素は (record_dict, embedding_vec_or_None)。
    embedding が渡された場合は knowledge_embeddings にも保存する。
    """
    produced: list[str] = []
    today = today_jst()
    now = now_iso()

    for d, vec in distilled_with_vec:
        new_id = next_knowledge_id(kdb)
        kdb.execute(
            "INSERT INTO knowledge"
            " (id, kind, topic, current_state, rationale, alternatives_rejected,"
            "  constraints_invariants, tags, owners, decided_at, last_validated_at,"
            "  confidence, superseded_by, deleted, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)",
            (
                new_id, d["kind"], d["topic"], d["current_state"],
                d["rationale"], d["alternatives_rejected"],
                d["constraints_invariants"], d["tags"], d["owners"],
                d["decided_at"], today, d["confidence"], now, now,
            ),
        )
        kdb.execute(
            "INSERT INTO knowledge_sources"
            " (knowledge_id, source_type, source_ref, weight, excerpt, added_at)"
            " VALUES (?, ?, ?, 'primary', ?, ?)",
            (new_id, item["source_type"], item["source_ref"], d["excerpt"], now),
        )
        kdb.execute(
            "INSERT INTO knowledge_audit"
            " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
            " VALUES (?, '__create__', NULL, ?, ?, 'distill_llm', NULL)",
            (new_id, json.dumps({"topic": d["topic"], "kind": d["kind"]}, ensure_ascii=False), now),
        )
        if vec is not None and vec.size > 0:
            upsert_embedding(kdb, new_id, vec)
        produced.append(new_id)
        log(f"    + {new_id} [{d['kind']}/{d['confidence']}] {d['topic']}")
    return produced


def update_distill_state(
    kdb,
    item: dict,
    produced: list[str],
    status: str,
    note: str | None = None,
) -> None:
    kdb.execute(
        "INSERT OR REPLACE INTO distill_state"
        " (source_type, source_ref, last_input_hash, last_distilled_at,"
        "  produced_knowledge_ids, status, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item["source_type"], item["source_ref"], item["input_hash"],
            now_iso(), json.dumps(produced, ensure_ascii=False), status, note,
        ),
    )


def needs_distill(kdb, item: dict, *, force: bool) -> bool:
    """distill_state の last_input_hash と現在のハッシュを比較。"""
    if force:
        return True
    row = kdb.execute(
        "SELECT last_input_hash, status FROM distill_state"
        " WHERE source_type = ? AND source_ref = ?",
        (item["source_type"], item["source_ref"]),
    ).fetchone()
    if not row:
        return True
    if row["last_input_hash"] != item["input_hash"]:
        return True
    return False


# --------------------------------------------------------------------------- #
# メイン処理
# --------------------------------------------------------------------------- #

def run_distill(
    *,
    sources: list[str],
    since: str | None,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
    log,
    two_stage: bool = True,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> dict:
    stats = {
        "scanned": 0, "skipped": 0, "distilled": 0,
        "produced": 0, "merged": 0, "dropped": 0, "errors": 0,
    }

    # 入力収集
    inputs: list[dict] = []
    if "box" in sources:
        log("[INFO] box_docs.db から候補を収集中...")
        bx = collect_box_inputs(since=since, no_encrypt=no_encrypt)
        log(f"  box: {len(bx)} 件")
        inputs.extend(bx)
    if "minutes" in sources:
        log("[INFO] minutes/*.db から候補を収集中...")
        mn = collect_minutes_inputs(since=since, no_encrypt=no_encrypt)
        log(f"  minutes: {len(mn)} 件")
        inputs.extend(mn)
    if "decisions" in sources:
        log("[INFO] pm.db.decisions から候補を収集中...")
        ds = collect_decisions_inputs(since=since, no_encrypt=no_encrypt)
        log(f"  decisions: {len(ds)} 件")
        inputs.extend(ds)

    stats["scanned"] = len(inputs)
    if not inputs:
        log("[INFO] 候補なし")
        return stats

    if dry_run:
        log("[INFO] --dry-run: LLM 呼び出し・DB保存なし")
        log(f"  処理予定: {len(inputs)} 件")
        return stats

    if two_stage and not embed_healthcheck(timeout=10):
        log("[ERROR] embedding API に接続できません。"
            "RIVAULT_URL / EMBED_API_BASE を確認してください。"
            " --no-two-stage で段階1のみ実行することもできます。")
        return stats

    kdb = init_knowledge_db(KNOWLEDGE_DB, no_encrypt=no_encrypt)
    try:
        for item in inputs:
            if not needs_distill(kdb, item, force=force):
                stats["skipped"] += 1
                continue
            label = f"{item['source_type']}/{item['source_ref']}"
            log(f"[INFO] 蒸留: {label}")
            distilled = distill_one(item, log)
            if not distilled:
                update_distill_state(kdb, item, [], status="skipped",
                                     note="LLM が抽出対象なしと判定 / または応答エラー")
                kdb.commit()
                stats["skipped"] += 1
                continue

            try:
                if two_stage:
                    keep_with_vec, merges = stage2_filter(
                        distilled, item, kdb,
                        merge_threshold=merge_threshold,
                        review_threshold=review_threshold,
                        log=log,
                    )
                    n_merge = apply_merges(kdb, item, merges)
                    stats["merged"] += n_merge
                    stats["dropped"] += len(distilled) - len(keep_with_vec) - len(merges)
                    produced = upsert_knowledge_records(kdb, item, keep_with_vec, log)
                else:
                    # 段階1のみ。embedding は計算せず後追い --embed-backfill で対応
                    produced = upsert_knowledge_records(
                        kdb, item, [(d, None) for d in distilled], log,
                    )

                if produced or not two_stage:
                    update_distill_state(kdb, item, produced, status="ok")
                else:
                    # 全件 drop / merge → quality_dropped
                    update_distill_state(
                        kdb, item, [], status="quality_dropped",
                        note=f"all candidates merged or dropped",
                    )
                kdb.commit()
                stats["distilled"] += 1
                stats["produced"] += len(produced)
            except Exception as e:
                log(f"  [ERROR] DB 書き込み失敗: {e}")
                kdb.rollback()
                update_distill_state(kdb, item, [], status="error", note=str(e)[:200])
                kdb.commit()
                stats["errors"] += 1
    finally:
        kdb.close()

    return stats


def show_stats(no_encrypt: bool, log) -> None:
    if not KNOWLEDGE_DB.exists():
        log("[INFO] knowledge.db が未作成です")
        return
    kdb = open_knowledge_db(KNOWLEDGE_DB, no_encrypt=no_encrypt)
    try:
        total = kdb.execute(
            "SELECT COUNT(*) FROM knowledge WHERE COALESCE(deleted,0) = 0"
        ).fetchone()[0]
        active = kdb.execute(
            "SELECT COUNT(*) FROM knowledge"
            " WHERE COALESCE(deleted,0) = 0 AND superseded_by IS NULL"
        ).fetchone()[0]
        by_kind = kdb.execute(
            "SELECT kind, COUNT(*) FROM knowledge"
            " WHERE COALESCE(deleted,0) = 0 GROUP BY kind ORDER BY 2 DESC"
        ).fetchall()
        by_conf = kdb.execute(
            "SELECT confidence, COUNT(*) FROM knowledge"
            " WHERE COALESCE(deleted,0) = 0 GROUP BY confidence"
        ).fetchall()
        by_status = kdb.execute(
            "SELECT status, COUNT(*) FROM distill_state GROUP BY status"
        ).fetchall()
        log(f"総レコード数（非削除）: {total}")
        log(f"  うち現役（superseded_by IS NULL）: {active}")
        log("kind 別:")
        for r in by_kind:
            log(f"  {r[0]:<12} {r[1]}")
        log("confidence 別:")
        for r in by_conf:
            log(f"  {r[0]:<12} {r[1]}")
        log("distill_state.status 別:")
        for r in by_status:
            log(f"  {r[0]:<12} {r[1]}")
        if total > 500:
            log("[WARN] 総レコード数 500 超過。抽出粒度の見直しを検討してください。"
                " (docs/distill_policy.md 参照)")
    finally:
        kdb.close()


def run_embed_backfill(*, no_encrypt: bool, log) -> dict:
    """既存レコードのうち knowledge_embeddings がないものに埋め込みを後追い計算する。"""
    stats = {"target": 0, "embedded": 0, "errors": 0}
    if not KNOWLEDGE_DB.exists():
        log("[INFO] knowledge.db が未作成です")
        return stats
    if not embed_healthcheck(timeout=10):
        log("[ERROR] embedding API に接続できません")
        return stats
    kdb = open_knowledge_db(KNOWLEDGE_DB, no_encrypt=no_encrypt)
    try:
        rows = kdb.execute(
            "SELECT k.id, k.topic, k.current_state"
            " FROM knowledge k"
            " LEFT JOIN knowledge_embeddings e ON e.knowledge_id = k.id"
            " WHERE COALESCE(k.deleted, 0) = 0 AND e.knowledge_id IS NULL"
        ).fetchall()
        stats["target"] = len(rows)
        log(f"[INFO] 埋め込み未計算: {len(rows)} 件")
        if not rows:
            return stats
        BATCH = 32
        for chunk_start in range(0, len(rows), BATCH):
            chunk = rows[chunk_start: chunk_start + BATCH]
            texts = [f"{r['topic']} || {r['current_state']}" for r in chunk]
            try:
                mat = embed_batch(texts)
            except Exception as e:
                log(f"  [ERROR] バッチ {chunk_start} 失敗: {e}")
                stats["errors"] += len(chunk)
                continue
            for r, vec in zip(chunk, mat):
                upsert_embedding(kdb, r["id"], vec)
                stats["embedded"] += 1
            kdb.commit()
            log(f"  ...{chunk_start + len(chunk)}/{len(rows)} 完了")
    finally:
        kdb.close()
    return stats


def run_quality_only(
    *,
    no_encrypt: bool,
    merge_threshold: float,
    review_threshold: float,
    apply: bool,
    output_csv: Path | None,
    log,
    limit: int | None = None,
) -> dict:
    """既存レコードに対する後追い品質審査。各レコードを LLM に judgement させ、
    drop と判定されたら deleted=1、merge と判定されたら supersede_by を立てる。

    apply=False では CSV にプランを書き出すのみ（DB 変更なし）。
    """
    stats = {"target": 0, "kept": 0, "dropped": 0, "merged": 0, "errors": 0}
    if not KNOWLEDGE_DB.exists():
        log("[INFO] knowledge.db が未作成です")
        return stats
    if not embed_healthcheck(timeout=10):
        log("[ERROR] embedding API に接続できません")
        return stats

    kdb = open_knowledge_db(KNOWLEDGE_DB, no_encrypt=no_encrypt)
    try:
        # 全現役レコード
        rows = kdb.execute(
            "SELECT k.id, k.kind, k.topic, k.current_state, k.confidence,"
            " e.dim, e.vector"
            " FROM knowledge k"
            " LEFT JOIN knowledge_embeddings e ON e.knowledge_id = k.id"
            " WHERE COALESCE(k.deleted, 0) = 0 AND k.superseded_by IS NULL"
        ).fetchall()
        if limit is not None and limit > 0:
            rows = rows[:limit]
            log(f"[INFO] --limit {limit} で先頭のみ対象")
        stats["target"] = len(rows)
        log(f"[INFO] 審査対象: {len(rows)} 件")

        # 埋め込みがないレコードがあれば事前に backfill
        missing = [r for r in rows if r["vector"] is None]
        if missing:
            log(f"[INFO] {len(missing)} 件に埋め込みがないため事前計算します")
            run_embed_backfill(no_encrypt=no_encrypt, log=log)
            # 再取得
            rows = kdb.execute(
                "SELECT k.id, k.kind, k.topic, k.current_state, k.confidence,"
                " e.dim, e.vector"
                " FROM knowledge k"
                " LEFT JOIN knowledge_embeddings e ON e.knowledge_id = k.id"
                " WHERE COALESCE(k.deleted, 0) = 0 AND k.superseded_by IS NULL"
            ).fetchall()

        # CSV 出力ヘッダ
        csv_writer = None
        csv_file = None
        if output_csv:
            import csv as _csv
            csv_file = output_csv.open("w", newline="", encoding="utf-8-sig")
            csv_writer = _csv.DictWriter(
                csv_file,
                fieldnames=["id", "kind", "confidence", "verdict",
                             "merge_target", "reason", "topic", "current_state"],
            )
            csv_writer.writeheader()

        # 1 件ずつ LLM 審査
        for i, r in enumerate(rows, 1):
            if r["vector"] is None:
                stats["errors"] += 1
                continue
            cand_vec = blob_to_vector(r["vector"], dim=r["dim"])
            # 自分以外の現役レコードと類似度比較
            others = [r2 for r2 in rows if r2["id"] != r["id"] and r2["vector"]]
            other_recs = [
                {"id": r2["id"], "kind": r2["kind"], "topic": r2["topic"] or "",
                 "current_state": r2["current_state"] or "",
                 "confidence": r2["confidence"] or ""}
                for r2 in others
            ]
            other_mat = (
                np.stack([blob_to_vector(r2["vector"], dim=r2["dim"]) for r2 in others])
                if others else np.zeros((0, 0), dtype=np.float32)
            )
            sims = find_similar_existing(cand_vec, other_recs, other_mat, top_k=5)

            cand = {
                "kind": r["kind"], "topic": r["topic"] or "",
                "current_state": r["current_state"] or "",
                "rationale": "",
            }
            judgements = llm_quality_judge([cand], [sims], log)
            j = judgements[0] if judgements else {"verdict": "drop", "reason": "no judgement"}

            if csv_writer:
                csv_writer.writerow({
                    "id": r["id"], "kind": r["kind"],
                    "confidence": r["confidence"],
                    "verdict": j["verdict"],
                    "merge_target": j["merge_target"],
                    "reason": j["reason"],
                    "topic": (r["topic"] or "")[:100],
                    "current_state": (r["current_state"] or "")[:200],
                })

            # 集計（apply 有無に関わらず）
            if j["verdict"] == "drop":
                stats["dropped"] += 1
            elif j["verdict"] == "merge_with" and j["merge_target"].startswith("KN-"):
                stats["merged"] += 1
            else:
                stats["kept"] += 1

            if apply:
                if j["verdict"] == "drop":
                    kdb.execute(
                        "UPDATE knowledge SET deleted = 1, updated_at = ? WHERE id = ?",
                        (now_iso(), r["id"]),
                    )
                    kdb.execute(
                        "INSERT INTO knowledge_audit"
                        " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
                        " VALUES (?, 'deleted', '0', '1', ?, 'distill_llm',"
                        " 'quality_only')",
                        (r["id"], now_iso()),
                    )
                elif j["verdict"] == "merge_with" and j["merge_target"].startswith("KN-"):
                    target = j["merge_target"]
                    if target != r["id"]:
                        kdb.execute(
                            "UPDATE knowledge SET superseded_by = ?, updated_at = ?"
                            " WHERE id = ?",
                            (target, now_iso(), r["id"]),
                        )
                        kdb.execute(
                            "INSERT OR IGNORE INTO knowledge_relations"
                            " (from_id, to_id, relation, note, created_at)"
                            " VALUES (?, ?, 'supersedes', 'quality_only', ?)",
                            (target, r["id"], now_iso()),
                        )
                        kdb.execute(
                            "INSERT INTO knowledge_audit"
                            " (knowledge_id, field, old_value, new_value, changed_at, source, actor)"
                            " VALUES (?, 'superseded_by', NULL, ?, ?, 'merge',"
                            " 'quality_only')",
                            (r["id"], target, now_iso()),
                        )
                if i % 20 == 0:
                    kdb.commit()

            if i % 50 == 0:
                log(f"  ...{i}/{len(rows)}: kept={stats['kept']} "
                    f"dropped={stats['dropped']} merged={stats['merged']}")
        if apply:
            kdb.commit()
        if csv_file:
            csv_file.close()
            log(f"[INFO] プラン CSV: {output_csv}")
    finally:
        kdb.close()
    return stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ナレッジ蒸留: 入力ソース → knowledge.db")
    p.add_argument("--source", choices=["box", "minutes", "decisions", "all"],
                   default="all", help="蒸留対象ソース")
    p.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="この日付以降のみ対象")
    p.add_argument("--force", action="store_true",
                   help="distill_state を無視して再蒸留")
    p.add_argument("--dry-run", action="store_true",
                   help="DB保存・LLM 呼び出しなし、件数のみ表示")
    p.add_argument("--no-encrypt", action="store_true",
                   help="平文モード")
    p.add_argument("--stats", action="store_true",
                   help="knowledge.db の統計を表示して終了")
    p.add_argument("--output", default=None, metavar="PATH",
                   help="ログをファイルにも保存")
    # 段階3 オプション
    p.add_argument("--no-two-stage", action="store_true",
                   help="段階2 (LLM 品質審査 + embedding 重複判定) を無効化")
    p.add_argument("--merge-threshold", type=float, default=DEFAULT_MERGE_THRESHOLD,
                   help=f"自動 merge のコサイン類似度しきい値 (default: {DEFAULT_MERGE_THRESHOLD})")
    p.add_argument("--review-threshold", type=float, default=DEFAULT_REVIEW_THRESHOLD,
                   help=f"LLM 審査対象のコサイン類似度しきい値 (default: {DEFAULT_REVIEW_THRESHOLD})")
    p.add_argument("--embed-backfill", action="store_true",
                   help="既存レコードに埋め込みを後追い計算する（蒸留はしない）")
    p.add_argument("--quality-only", action="store_true",
                   help="既存レコードに対して LLM 品質審査のみ実行（後追いパージ）")
    p.add_argument("--quality-plan", default=None, metavar="PATH",
                   help="--quality-only 時のプラン CSV 出力先")
    p.add_argument("--apply", action="store_true",
                   help="--quality-only 時に DB に変更を適用する。"
                        "未指定時は CSV 出力のみ")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="--quality-only 時に先頭 N 件のみ処理（スモークテスト用）")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    log, close_log = make_logger(args.output)
    try:
        if args.stats:
            show_stats(args.no_encrypt, log)
            return

        if args.embed_backfill:
            stats = run_embed_backfill(no_encrypt=args.no_encrypt, log=log)
            log("---")
            log(f"埋め込み後追い: target={stats['target']} embedded={stats['embedded']}"
                f" errors={stats['errors']}")
            return

        if args.quality_only:
            stats = run_quality_only(
                no_encrypt=args.no_encrypt,
                merge_threshold=args.merge_threshold,
                review_threshold=args.review_threshold,
                apply=args.apply,
                output_csv=Path(args.quality_plan) if args.quality_plan else None,
                log=log,
                limit=args.limit,
            )
            log("---")
            label = "適用" if args.apply else "プランのみ"
            log(f"品質審査 ({label}): target={stats['target']} kept={stats['kept']}"
                f" dropped={stats['dropped']} merged={stats['merged']}"
                f" errors={stats['errors']}")
            return

        sources = ["box", "minutes", "decisions"] if args.source == "all" else [args.source]
        stats = run_distill(
            sources=sources,
            since=args.since,
            force=args.force,
            dry_run=args.dry_run,
            no_encrypt=args.no_encrypt,
            log=log,
            two_stage=not args.no_two_stage,
            merge_threshold=args.merge_threshold,
            review_threshold=args.review_threshold,
        )
        log("---")
        log(f"処理結果: scanned={stats['scanned']} skipped={stats['skipped']}"
            f" distilled={stats['distilled']} produced={stats['produced']}"
            f" merged={stats['merged']} dropped={stats['dropped']}"
            f" errors={stats['errors']}")
    finally:
        close_log()


if __name__ == "__main__":
    main()
