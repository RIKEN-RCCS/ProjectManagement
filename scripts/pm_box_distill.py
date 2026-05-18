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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
KNOWLEDGE_DB = DATA_DIR / "knowledge.db"
BOX_DOCS_DB = DATA_DIR / "box_docs.db"
MINUTES_DIR = DATA_DIR / "minutes"
PM_DB = DATA_DIR / "pm.db"

# 1 入力あたりにLLMへ渡す最大文字数（プロンプト圧迫を避ける）
_MAX_INPUT_CHARS = 12000

# 蒸留採否の重要なポリシー（プロンプト直挿し）
_DISTILL_POLICY = """\
## 抽出する対象
- アーキテクチャ選択（例: Scale-up ドメインサイズの確定）
- 外部関係者との合意事項（理研 / 富士通 / NVIDIA 三者の決定）
- 長期にわたって参照される制約・前提条件（例: FP8 ゼタスケール目標、温水冷却前提）
- 撤回・上書きが起きにくい用語定義
- 立場の表明（重要ステークホルダーの方針声明）

## 抽出してはいけない対象
- 1 回限りの会議運営事項（時刻変更、開催場所、Zoom URL）
- 当日中に消費されるアクションアイテム
- 個人の暫定見解（チーム合意に至っていない発言）
- 既に上書きされた情報
- 形式的な承認（議事録レビュー承認等）

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
# DB 書き込み
# --------------------------------------------------------------------------- #

def upsert_knowledge_records(
    kdb,
    item: dict,
    distilled: list[dict],
    log,
) -> list[str]:
    """蒸留結果を knowledge.db に upsert。produced_knowledge_ids を返す。

    マージ戦略は単純化のため「初回は INSERT、再蒸留時は同じ input_hash なら何もしない」。
    既存レコードの自動上書きは現フェーズでは行わない（人手 supersede で対応）。
    """
    produced: list[str] = []
    today = today_jst()
    now = now_iso()

    for d in distilled:
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
) -> dict:
    stats = {"scanned": 0, "skipped": 0, "distilled": 0, "produced": 0, "errors": 0}

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
        # dry-run は LLM すら呼ばずに件数報告で終わる
        log("[INFO] --dry-run: LLM 呼び出し・DB保存なし")
        log(f"  処理予定: {len(inputs)} 件")
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
                produced = upsert_knowledge_records(kdb, item, distilled, log)
                update_distill_state(kdb, item, produced, status="ok")
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
    return p.parse_args()


def main() -> None:
    args = parse_args()
    log, close_log = make_logger(args.output)
    try:
        if args.stats:
            show_stats(args.no_encrypt, log)
            return

        sources = ["box", "minutes", "decisions"] if args.source == "all" else [args.source]
        stats = run_distill(
            sources=sources,
            since=args.since,
            force=args.force,
            dry_run=args.dry_run,
            no_encrypt=args.no_encrypt,
            log=log,
        )
        log("---")
        log(f"処理結果: scanned={stats['scanned']} skipped={stats['skipped']}"
            f" distilled={stats['distilled']} produced={stats['produced']}"
            f" errors={stats['errors']}")
    finally:
        close_log()


if __name__ == "__main__":
    main()
