#!/usr/bin/env python3
"""
enrich_items.py — ナレッジ文脈付きエンリッチメントエンジン

pm.db の既存 decisions / action_items に対し、過去ナレッジ（pm.db 構造化データ +
FTS5 全文検索）を参照して判断者・根拠・関連 ID を補完する。

2パスアーキテクチャの Pass 2 に相当する。

使い方:
  # dry-run（標準出力のみ、DB更新なし）
  python3 scripts/enrich_items.py --dry-run --since 2026-03-01

  # 特定IDのみ
  python3 scripts/enrich_items.py --id d:42 a:15 --dry-run

  # 実行（pm.db更新）
  python3 scripts/enrich_items.py --since 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import init_pm_db, open_pm_db

from enrich.knowledge_context import (
    extract_topic_keywords,
    fetch_fts_context,
    fetch_participant_patterns,
    fetch_recent_knowledge,
    format_knowledge_for_prompt,
)

# ---------------------------------------------------------------------------
# 参加者リスト（バリデーション用）
# ---------------------------------------------------------------------------
_VALID_NAMES: set[str] | None = None


def _load_valid_names() -> set[str]:
    global _VALID_NAMES
    if _VALID_NAMES is not None:
        return _VALID_NAMES

    names: set[str] = set()
    project_md = _REPO_ROOT / "docs" / "project.md"
    if not project_md.exists():
        _VALID_NAMES = names
        return names

    content = project_md.read_text(encoding="utf-8")

    # ステークホルダー・参加者セクションのみを対象にする
    participant_section = []
    capture = False
    for line in content.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者)", line):
            capture = True
            continue
        if capture and re.match(r"^###\s+", line):
            capture = False
        if capture:
            participant_section.append(line)
    section_text = "\n".join(participant_section)

    # "- 姓 名 English_Name email:" パターン（日本語名）
    for m in re.finditer(r"^- ([^\s]+(?:[\s　])[^\s]+)\s", section_text, re.MULTILINE):
        raw = m.group(1)
        full = raw.replace("　", "").replace(" ", "")
        names.add(full)
        parts = re.split(r"[\s　]+", raw)
        if parts:
            names.add(parts[0].strip())
    # 英語名
    for m in re.finditer(r"^- .+?\s([A-Z][a-z]+ [A-Z][a-z]+)\s", section_text, re.MULTILINE):
        names.add(m.group(1))
        names.add(m.group(1).split()[1])  # Last name
    # 組織名
    for org in ["NVIDIA", "富士通", "理研"]:
        names.add(org)

    _VALID_NAMES = names
    return names


def _validate_name(name: str | None) -> str | None:
    """名前が参加者リストに存在するか検証する。"""
    if not name:
        return None
    valid = _load_valid_names()
    if not valid:
        return name  # リストが空なら検証スキップ
    # 正規化して照合
    cleaned = name.replace(" ", "").replace("　", "")
    if cleaned in valid:
        return name
    # 姓のみで照合
    parts = re.split(r"[\s　]+", name)
    if parts and parts[0] in valid:
        return name
    return None


# ---------------------------------------------------------------------------
# エンリッチメントプロンプト
# ---------------------------------------------------------------------------
ENRICH_DECISION_PROMPT = """あなたは富岳NEXTプロジェクトのPMアシスタントです。
以下の決定事項について、過去のナレッジを参照して情報を補完してください。

## 補完対象（決定事項）
ID: d:{id}
内容: {content}
決定日: {decided_at}
出典: {source_context}
出典参照: {source_ref}

## 過去のナレッジ
{knowledge}

## プロジェクト文脈
{project_context}

## 補完ルール

1. **判断者 (decided_by)**: この決定を下した人物（最終的な意思決定者）。
   - 元テキストや出典に名前が明示されていれば、その名前を使い confidence: "explicit" とする
   - 明示されていなければ、過去のナレッジ（担当者パターン・関連する過去の決定）から推測し confidence: "inferred" とする。推測の根拠を evidence に具体的に記載する
   - 推測できない場合は null

2. **根拠 (rationale)**: なぜこの決定がなされたか。
   - 出典 (source_context) の内容をベースに、過去の関連決定・議論から文脈を補完して2-3文で記述
   - 「関連ドキュメント・公開情報」セクションに該当資料があれば、「〇〇設計書に準拠」のように明示的に引用する
   - 単に content を繰り返すだけの記述は不可

3. **関連ID (related_ids)**: この決定に関連する過去の決定事項・アクションアイテムのID。
   - 同じトピックの過去の決定 → "d:{{id}}" 形式
   - この決定によって発生/影響を受けるアクションアイテム → "a:{{id}}" 形式
   - 過去のナレッジに表示されたIDの中から選択すること（存在しないIDを生成しない）

4. **推測不能なら null**。無理に埋めない。正確さが最優先。

## 出力（JSON のみ、説明不要）

```json
{{
  "decided_by": "名前 or null",
  "decided_by_confidence": "explicit or inferred or null",
  "rationale": "根拠2-3文 or null",
  "related_ids": ["d:42", "a:15"],
  "evidence": "推測の根拠説明（inferred時のみ、それ以外はnull）"
}}
```
"""

ENRICH_ACTION_ITEM_PROMPT = """あなたは富岳NEXTプロジェクトのPMアシスタントです。
以下のアクションアイテムについて、過去のナレッジを参照して情報を補完してください。

## 補完対象（アクションアイテム）
ID: a:{id}
内容: {content}
担当者: {assignee}
期限: {due_date}
マイルストーン: {milestone_id}
出典参照: {source_ref}

## 過去のナレッジ
{knowledge}

## プロジェクト文脈
{project_context}

## 補完ルール

1. **依頼者 (requested_by)**: このタスクを依頼・指示した人物（担当者ではなく意思決定者）。
   - 元テキストや出典に名前が明示されていれば confidence: "explicit"
   - 過去のナレッジから推測すれば confidence: "inferred"、根拠を evidence に記載
   - 推測できない場合は null

2. **根拠 (rationale)**: なぜこのタスクが必要か。
   - 関連する過去の決定・議論から文脈を補完して2-3文で記述
   - 「関連ドキュメント・公開情報」セクションに該当資料があれば、「〇〇設計書に基づく」のように明示的に引用する
   - content の繰り返しは不可

3. **出典文脈 (source_context)**: このタスクが発生した背景の要約（1-2文）。
   - 関連する議論・決定の経緯を簡潔にまとめる

4. **関連ID (related_ids)**: 過去のナレッジに表示されたIDから選択。

5. **推測不能なら null**。

## 出力（JSON のみ、説明不要）

```json
{{
  "requested_by": "名前 or null",
  "requested_by_confidence": "explicit or inferred or null",
  "rationale": "根拠2-3文 or null",
  "source_context": "背景1-2文 or null",
  "related_ids": ["d:42", "a:15"],
  "evidence": "推測の根拠説明（inferred時のみ、それ以外はnull）"
}}
```
"""


# ---------------------------------------------------------------------------
# エンリッチメントコア
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output: {text[:300]}")


def _validate_related_ids(ids: list, pm_conn) -> list[str]:
    """related_ids が pm.db に実在するか検証し、有効なもののみ返す。"""
    valid = []
    for ref in ids:
        if not isinstance(ref, str):
            continue
        m = re.match(r"^(d|a):(\d+)$", ref)
        if not m:
            continue
        table = "decisions" if m.group(1) == "d" else "action_items"
        row = pm_conn.execute(
            f"SELECT id FROM {table} WHERE id = ?", (int(m.group(2)),)
        ).fetchone()
        if row:
            valid.append(ref)
    return valid


def _is_trivial_rationale(rationale: str | None, content: str) -> bool:
    """rationale が content の実質コピーかどうかを簡易判定する。"""
    if not rationale:
        return True
    r = rationale.replace(" ", "").replace("。", "").replace("、", "")
    c = content.replace(" ", "").replace("。", "").replace("、", "")
    if len(r) < 10:
        return True
    # 80% 以上の文字が content に含まれていたらコピーとみなす
    overlap = sum(1 for ch in r if ch in c)
    return overlap / len(r) > 0.8 if r else True


def enrich_decision(
    item: dict,
    knowledge_text: str,
    project_context: str,
    pm_conn,
) -> dict:
    prompt = ENRICH_DECISION_PROMPT.format(
        id=item["id"],
        content=item["content"],
        decided_at=item.get("decided_at") or "不明",
        source_context=item.get("source_context") or "なし",
        source_ref=item.get("source_ref") or "なし",
        knowledge=knowledge_text,
        project_context=project_context,
    )

    try:
        raw = call_argus_llm(prompt, timeout=300)
        result = _extract_json(raw)
    except Exception as e:
        return {"error": str(e)}

    # バリデーション
    decided_by = result.get("decided_by")
    confidence = result.get("decided_by_confidence")
    if confidence == "inferred":
        decided_by = _validate_name(decided_by)
        if decided_by is None:
            confidence = None
            result["evidence"] = None

    related_ids = _validate_related_ids(result.get("related_ids") or [], pm_conn)

    rationale = result.get("rationale")
    if _is_trivial_rationale(rationale, item["content"]):
        rationale = None

    return {
        "decided_by": decided_by,
        "decided_by_confidence": confidence,
        "rationale": rationale,
        "related_ids": related_ids,
        "evidence": result.get("evidence"),
    }


def enrich_action_item(
    item: dict,
    knowledge_text: str,
    project_context: str,
    pm_conn,
) -> dict:
    prompt = ENRICH_ACTION_ITEM_PROMPT.format(
        id=item["id"],
        content=item["content"],
        assignee=item.get("assignee") or "未定",
        due_date=item.get("due_date") or "なし",
        milestone_id=item.get("milestone_id") or "なし",
        source_ref=item.get("source_ref") or "なし",
        knowledge=knowledge_text,
        project_context=project_context,
    )

    try:
        raw = call_argus_llm(prompt, timeout=300)
        result = _extract_json(raw)
    except Exception as e:
        return {"error": str(e)}

    requested_by = result.get("requested_by")
    confidence = result.get("requested_by_confidence")
    if confidence == "inferred":
        requested_by = _validate_name(requested_by)
        if requested_by is None:
            confidence = None
            result["evidence"] = None

    related_ids = _validate_related_ids(result.get("related_ids") or [], pm_conn)

    rationale = result.get("rationale")
    if _is_trivial_rationale(rationale, item["content"]):
        rationale = None

    source_context = result.get("source_context")
    if source_context and _is_trivial_rationale(source_context, item["content"]):
        source_context = None

    return {
        "requested_by": requested_by,
        "requested_by_confidence": confidence,
        "rationale": rationale,
        "source_context": source_context,
        "related_ids": related_ids,
        "evidence": result.get("evidence"),
    }


# ---------------------------------------------------------------------------
# バッチ処理
# ---------------------------------------------------------------------------

def enrich_batch(
    pm_conn,
    decisions: list[dict],
    action_items: list[dict],
    *,
    project_context: str,
    config_path: Path | None = None,
    dry_run: bool = False,
    log=print,
) -> tuple[list[dict], list[dict]]:
    """decisions と action_items をバッチエンリッチする。

    Returns:
        (enriched_decisions, enriched_action_items)
        各要素は元の dict に enrichment フィールドを追加したもの。
    """
    all_content = [d["content"] for d in decisions] + [a["content"] for a in action_items]
    if not all_content:
        return [], []

    log("[INFO] ナレッジ取得中...")

    # キーワード抽出
    all_keywords: list[str] = []
    seen: set[str] = set()
    for content in all_content:
        for kw in extract_topic_keywords(content):
            if kw not in seen:
                seen.add(kw)
                all_keywords.append(kw)
    all_keywords = all_keywords[:20]

    # ナレッジ取得
    structured = fetch_recent_knowledge(pm_conn, all_keywords)
    fts_chunks = fetch_fts_context(all_keywords, config_path=config_path)
    patterns = fetch_participant_patterns(pm_conn)

    knowledge = {
        "decisions": structured["decisions"],
        "action_items": structured["action_items"],
        "fts_chunks": fts_chunks,
        "participant_patterns": patterns,
    }
    knowledge_text = format_knowledge_for_prompt(knowledge)

    log(f"[INFO] ナレッジ: decisions={len(structured['decisions'])}件, "
        f"AI={len(structured['action_items'])}件, FTS={len(fts_chunks)}チャンク")

    # エンリッチ
    enriched_decisions: list[dict] = []
    for i, d in enumerate(decisions, 1):
        log(f"\n[d:{d['id']}] ({i}/{len(decisions)}) {d['content'][:60]}...")
        result = enrich_decision(d, knowledge_text, project_context, pm_conn)

        if result.get("error"):
            log(f"  [WARN] エンリッチ失敗: {result['error']}")
            enriched_decisions.append({**d, "_enrichment": None})
            continue

        _print_enrichment(result, "decision", log)
        enriched_decisions.append({**d, "_enrichment": result})

        if not dry_run:
            _save_decision_enrichment(pm_conn, d["id"], result)

    enriched_ais: list[dict] = []
    for i, a in enumerate(action_items, 1):
        log(f"\n[a:{a['id']}] ({i}/{len(action_items)}) {a['content'][:60]}...")
        result = enrich_action_item(a, knowledge_text, project_context, pm_conn)

        if result.get("error"):
            log(f"  [WARN] エンリッチ失敗: {result['error']}")
            enriched_ais.append({**a, "_enrichment": None})
            continue

        _print_enrichment(result, "action_item", log)
        enriched_ais.append({**a, "_enrichment": result})

        if not dry_run:
            _save_action_item_enrichment(pm_conn, a["id"], result)

    if not dry_run:
        pm_conn.commit()

    return enriched_decisions, enriched_ais


def _print_enrichment(result: dict, item_type: str, log=print):
    if item_type == "decision":
        by = result.get("decided_by")
        conf = result.get("decided_by_confidence")
        if by:
            log(f"  decided_by  : {by} ({conf})")
    else:
        by = result.get("requested_by")
        conf = result.get("requested_by_confidence")
        if by:
            log(f"  requested_by: {by} ({conf})")

    if result.get("evidence"):
        log(f"  evidence    : {result['evidence']}")
    if result.get("rationale"):
        log(f"  rationale   : {result['rationale']}")
    if result.get("source_context"):
        log(f"  context     : {result['source_context']}")
    if result.get("related_ids"):
        log(f"  related_ids : {result['related_ids']}")


def _save_decision_enrichment(pm_conn, decision_id: int, result: dict):
    pm_conn.execute(
        """UPDATE decisions SET
               decided_by = ?,
               decided_by_confidence = ?,
               rationale = ?,
               related_ids = ?
           WHERE id = ?""",
        (
            result.get("decided_by"),
            result.get("decided_by_confidence"),
            result.get("rationale"),
            json.dumps(result.get("related_ids", []), ensure_ascii=False),
            decision_id,
        ),
    )


def _save_action_item_enrichment(pm_conn, ai_id: int, result: dict):
    pm_conn.execute(
        """UPDATE action_items SET
               requested_by = ?,
               requested_by_confidence = ?,
               rationale = ?,
               source_context = ?,
               related_ids = ?
           WHERE id = ?""",
        (
            result.get("requested_by"),
            result.get("requested_by_confidence"),
            result.get("rationale"),
            result.get("source_context"),
            json.dumps(result.get("related_ids", []), ensure_ascii=False),
            ai_id,
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fetch_target_items(
    pm_conn, *, since: str | None = None, item_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """対象の decisions / action_items を取得する。"""
    decisions: list[dict] = []
    action_items: list[dict] = []

    if item_ids:
        for ref in item_ids:
            m = re.match(r"^(d|a):(\d+)$", ref)
            if not m:
                print(f"[WARN] 不正なID形式: {ref}（d:N or a:N）", file=sys.stderr)
                continue
            table = "decisions" if m.group(1) == "d" else "action_items"
            row = pm_conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (int(m.group(2)),)
            ).fetchone()
            if row:
                (decisions if m.group(1) == "d" else action_items).append(dict(row))
            else:
                print(f"[WARN] 見つかりません: {ref}", file=sys.stderr)
        return decisions, action_items

    where_clause = "COALESCE(deleted, 0) = 0"
    params: list = []
    if since:
        where_clause += " AND decided_at >= ?"
        params.append(since)

    rows = pm_conn.execute(
        f"SELECT * FROM decisions WHERE {where_clause} ORDER BY decided_at DESC",
        params,
    ).fetchall()
    decisions = [dict(r) for r in rows]

    where_clause2 = "COALESCE(deleted, 0) = 0"
    params2: list = []
    if since:
        where_clause2 += " AND extracted_at >= ?"
        params2.append(since)

    rows = pm_conn.execute(
        f"SELECT * FROM action_items WHERE {where_clause2} ORDER BY extracted_at DESC",
        params2,
    ).fetchall()
    action_items = [dict(r) for r in rows]

    return decisions, action_items


def main():
    parser = argparse.ArgumentParser(
        description="pm.db の既存アイテムにナレッジ文脈を付与する（Pass 2 エンリッチメント）"
    )
    parser.add_argument("--db", default="data/pm.db", help="pm.db パス")
    parser.add_argument("--since", help="この日付以降のアイテムのみ対象 (YYYY-MM-DD)")
    parser.add_argument("--id", nargs="+", dest="item_ids",
                        help="特定IDのみ (d:42 a:15 形式)")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB更新なし、結果を標準出力のみ")
    parser.add_argument("--output", help="結果をファイルにも保存")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--config", default="data/argus_config.yaml",
                        help="FTS5インデックス設定ファイル")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = _REPO_ROOT / db_path
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _REPO_ROOT / config_path

    output_file = None
    if args.output:
        output_file = open(args.output, "w", encoding="utf-8")

    def log(msg: str):
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    if db_path.exists():
        pm_conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    else:
        pm_conn = init_pm_db(db_path, no_encrypt=args.no_encrypt)

    decisions, action_items = _fetch_target_items(
        pm_conn, since=args.since, item_ids=args.item_ids,
    )

    log(f"[INFO] 対象: decisions={len(decisions)}件, action_items={len(action_items)}件")
    if not decisions and not action_items:
        log("[INFO] エンリッチ対象なし")
        pm_conn.close()
        return

    project_context = load_claude_md_context()

    if args.dry_run:
        log("[INFO] --dry-run モード（DB更新なし）")

    enrich_batch(
        pm_conn,
        decisions,
        action_items,
        project_context=project_context,
        config_path=config_path,
        dry_run=args.dry_run,
        log=log,
    )

    pm_conn.close()

    if output_file:
        output_file.close()
        log(f"\n[INFO] 結果を {args.output} に保存しました")


if __name__ == "__main__":
    main()
