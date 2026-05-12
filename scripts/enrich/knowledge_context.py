#!/usr/bin/env python3
"""
knowledge_context.py — 過去ナレッジの取得・整形モジュール

エンリッチメントエンジン (enrich_items.py) および将来のパイプライン統合で使用する。
pm.db の構造化データ + FTS5 インデックスから関連する過去のナレッジを取得し、
LLM プロンプトに埋め込める形式に整形する。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent

# ---------------------------------------------------------------------------
# SudachiPy (lazy init)
# ---------------------------------------------------------------------------
_sudachi_tokenizer = None
_sudachi_split_mode = None
_SUDACHI_TARGET_POS = {"名詞", "動詞", "形容詞", "副詞"}


def _init_sudachi() -> bool:
    global _sudachi_tokenizer, _sudachi_split_mode
    if _sudachi_tokenizer is not None:
        return True
    try:
        import sudachipy
        try:
            _sudachi_tokenizer = sudachipy.Dictionary().create()
            _sudachi_split_mode = sudachipy.SplitMode.C
        except Exception:
            from sudachipy import tokenizer as tm
            _sudachi_tokenizer = tm.Tokenizer()
            _sudachi_split_mode = tm.Tokenizer.SplitMode.C
        return True
    except ImportError:
        return False


def extract_topic_keywords(text: str) -> list[str]:
    """テキストからトピックキーワードを抽出する。
    SudachiPy があれば形態素解析、なければ簡易分割。
    """
    if _init_sudachi():
        try:
            morphemes = _sudachi_tokenizer.tokenize(text, _sudachi_split_mode)
            seen: set[str] = set()
            tokens: list[str] = []
            for m in morphemes:
                pos = m.part_of_speech()[0]
                if pos in _SUDACHI_TARGET_POS:
                    form = m.dictionary_form()
                    if len(form) >= 2 and form not in seen:
                        seen.add(form)
                        tokens.append(form)
            return tokens
        except Exception:
            pass
    # フォールバック: 英数字・カタカナの連続を抽出
    return [t for t in re.findall(r'[A-Za-z0-9]+|[゠-ヿ]{2,}|[一-鿿]{2,}', text) if len(t) >= 2]


# ---------------------------------------------------------------------------
# pm.db 構造化クエリ
# ---------------------------------------------------------------------------

def fetch_recent_knowledge(
    pm_conn: sqlite3.Connection,
    topic_keywords: list[str],
    *,
    max_decisions: int = 20,
    max_action_items: int = 20,
    since_days: int = 90,
) -> dict:
    """pm.db から関連する過去の decisions/action_items を取得する。

    Returns:
        {
            "decisions": [{"id": int, "content": str, "decided_at": str,
                           "decided_by": str|None, "source_context": str|None,
                           "milestone_id": str|None}, ...],
            "action_items": [{"id": int, "content": str, "assignee": str|None,
                              "due_date": str|None, "milestone_id": str|None,
                              "status": str, "extracted_at": str}, ...],
        }
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")

    decisions = _query_related_decisions(pm_conn, topic_keywords, cutoff, max_decisions)
    action_items = _query_related_action_items(pm_conn, topic_keywords, cutoff, max_action_items)

    return {"decisions": decisions, "action_items": action_items}


def _query_related_decisions(
    conn: sqlite3.Connection, keywords: list[str], cutoff: str, limit: int,
) -> list[dict]:
    if not keywords:
        return _fetch_recent_decisions(conn, cutoff, limit)

    like_clauses = " OR ".join(["content LIKE ?"] * len(keywords))
    params = [f"%{kw}%" for kw in keywords]
    rows = conn.execute(
        f"""SELECT id, content, decided_at, decided_by, source_context, source_ref
            FROM decisions
            WHERE COALESCE(deleted, 0) = 0
              AND decided_at >= ?
              AND ({like_clauses})
            ORDER BY decided_at DESC LIMIT ?""",
        [cutoff] + params + [limit],
    ).fetchall()

    if not rows:
        return _fetch_recent_decisions(conn, cutoff, limit)
    return [dict(r) for r in rows]


def _fetch_recent_decisions(conn: sqlite3.Connection, cutoff: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, content, decided_at, decided_by, source_context, source_ref
           FROM decisions
           WHERE COALESCE(deleted, 0) = 0 AND decided_at >= ?
           ORDER BY decided_at DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_related_action_items(
    conn: sqlite3.Connection, keywords: list[str], cutoff: str, limit: int,
) -> list[dict]:
    if not keywords:
        return _fetch_recent_action_items(conn, cutoff, limit)

    like_clauses = " OR ".join(["content LIKE ?"] * len(keywords))
    params = [f"%{kw}%" for kw in keywords]
    rows = conn.execute(
        f"""SELECT id, content, assignee, due_date, milestone_id, status, extracted_at, source_ref
            FROM action_items
            WHERE COALESCE(deleted, 0) = 0
              AND extracted_at >= ?
              AND ({like_clauses})
            ORDER BY extracted_at DESC LIMIT ?""",
        [cutoff] + params + [limit],
    ).fetchall()

    if not rows:
        return _fetch_recent_action_items(conn, cutoff, limit)
    return [dict(r) for r in rows]


def _fetch_recent_action_items(conn: sqlite3.Connection, cutoff: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, content, assignee, due_date, milestone_id, status, extracted_at, source_ref
           FROM action_items
           WHERE COALESCE(deleted, 0) = 0 AND extracted_at >= ?
           ORDER BY extracted_at DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 担当者パターン
# ---------------------------------------------------------------------------

def fetch_participant_patterns(
    pm_conn: sqlite3.Connection,
    *,
    since_days: int = 180,
) -> dict:
    """マイルストーン別・会議種別別の担当者頻度パターンを集計する。

    Returns:
        {
            "by_milestone": {"M1": [("青木", 5), ("西澤", 3)], ...},
            "by_meeting_kind": {"Leader_Meeting": [("青木", 8), ...], ...},
        }
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")

    by_milestone: dict[str, list[tuple[str, int]]] = {}
    rows = pm_conn.execute(
        """SELECT milestone_id, assignee, COUNT(*) as freq
           FROM action_items
           WHERE assignee IS NOT NULL AND COALESCE(deleted, 0) = 0
             AND milestone_id IS NOT NULL AND extracted_at >= ?
           GROUP BY milestone_id, assignee
           ORDER BY milestone_id, freq DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        ms = r["milestone_id"]
        by_milestone.setdefault(ms, []).append((r["assignee"], r["freq"]))

    by_kind: dict[str, list[tuple[str, int]]] = {}
    rows = pm_conn.execute(
        """SELECT m.kind, a.assignee, COUNT(*) as freq
           FROM action_items a
           JOIN meetings m ON a.meeting_id = m.meeting_id
           WHERE a.assignee IS NOT NULL AND COALESCE(a.deleted, 0) = 0
             AND a.extracted_at >= ?
           GROUP BY m.kind, a.assignee
           ORDER BY m.kind, freq DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        kind = r["kind"]
        by_kind.setdefault(kind, []).append((r["assignee"], r["freq"]))

    # 決定事項の判断者パターンも追加
    by_decider: dict[str, list[tuple[str, int]]] = {}
    rows = pm_conn.execute(
        """SELECT m.kind, d.decided_by, COUNT(*) as freq
           FROM decisions d
           JOIN meetings m ON d.meeting_id = m.meeting_id
           WHERE d.decided_by IS NOT NULL AND COALESCE(d.deleted, 0) = 0
             AND d.decided_at >= ?
           GROUP BY m.kind, d.decided_by
           ORDER BY m.kind, freq DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        kind = r["kind"]
        by_decider.setdefault(kind, []).append((r["decided_by"], r["freq"]))

    return {
        "by_milestone": by_milestone,
        "by_meeting_kind": by_kind,
        "by_decider": by_decider,
    }


# ---------------------------------------------------------------------------
# FTS5 全文検索
# ---------------------------------------------------------------------------

def _load_index_db_paths(config_path: Path | None = None) -> list[Path]:
    """argus_config.yaml から全インデックスDBパスを解決する。"""
    if config_path is None:
        config_path = _REPO_ROOT / "data" / "argus_config.yaml"
    if not config_path.exists():
        return []
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    paths = []
    for idx_cfg in (cfg.get("indices") or {}).values():
        db_rel = idx_cfg.get("db", "")
        if db_rel:
            p = _REPO_ROOT / db_rel
            if p.exists():
                paths.append(p)
    return paths


def _sanitize_fts_query(q: str) -> str:
    q = re.sub(r'["\'\*\^\(\)\[\]？?。、,，.．！!\n\r]', " ", q)
    parts = re.split(r'[ぁ-ん]+', q)
    tokens = [t.strip() for t in parts if len(t.strip()) >= 3]
    if not tokens:
        return re.sub(r'["\'\*\^\(\)\[\]？?。、！!]', " ", q).strip()
    return " ".join(tokens)


def _fts5_search(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT c.source_type, c.source_db, c.record_id, c.held_at,
                      c.content, c.source_ref, fts.rank
               FROM fts JOIN chunks c ON fts.rowid = c.id
               WHERE fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, k),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _fts_tokens_search(conn: sqlite3.Connection, tokens: list[str], k: int) -> list[dict]:
    token_sets = [tokens]
    if len(tokens) > 3:
        token_sets.append(tokens[:3])
    if len(tokens) > 2:
        token_sets.append(tokens[:2])
    if len(tokens) > 1:
        token_sets.append(tokens[:1])

    for tset in token_sets:
        query = " ".join(tset)
        try:
            rows = conn.execute(
                """SELECT c.source_type, c.source_db, c.record_id, c.held_at,
                          c.content, c.source_ref, fts_tokens.rank
                   FROM fts_tokens JOIN chunks c ON fts_tokens.rowid = c.id
                   WHERE fts_tokens MATCH ? ORDER BY rank LIMIT ?""",
                (query, k),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass
    return []


def fetch_fts_context(
    topic_keywords: list[str],
    index_db_paths: list[Path] | None = None,
    *,
    max_chunks: int = 10,
    config_path: Path | None = None,
) -> list[dict]:
    """FTS5 インデックスから関連する議事録本文・Slack生メッセージを検索する。

    Returns:
        [{"content": str, "held_at": str, "source_type": str, "source_ref": str}, ...]
    """
    if index_db_paths is None:
        index_db_paths = _load_index_db_paths(config_path)
    if not index_db_paths or not topic_keywords:
        return []

    query_text = " ".join(topic_keywords)
    sudachi_tokens = []
    if _init_sudachi():
        sudachi_tokens = extract_topic_keywords(query_text)

    all_chunks: list[dict] = []
    per_db_limit = max(max_chunks // len(index_db_paths), 5)

    for db_path in index_db_paths:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # SudachiPy → trigram → LIKE の順に試行
            rows = []
            if sudachi_tokens:
                has_fts_tokens = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_tokens'"
                ).fetchone() is not None
                if has_fts_tokens:
                    rows = _fts_tokens_search(conn, sudachi_tokens, per_db_limit)

            if not rows:
                sanitized = _sanitize_fts_query(query_text)
                valid_tokens = [t for t in sanitized.split() if len(t) >= 3]
                if valid_tokens:
                    for tset in [valid_tokens, valid_tokens[:3], valid_tokens[:2], valid_tokens[:1]]:
                        if not tset:
                            continue
                        rows = _fts5_search(conn, " ".join(tset), per_db_limit)
                        if rows:
                            break

            if not rows and topic_keywords:
                kw = topic_keywords[0]
                rows = conn.execute(
                    """SELECT source_type, source_db, record_id, held_at, content, source_ref, 0 AS rank
                       FROM chunks WHERE content LIKE ? LIMIT ?""",
                    (f"%{kw}%", per_db_limit),
                ).fetchall()
                rows = [dict(r) for r in rows]

            all_chunks.extend(rows)
        finally:
            conn.close()

    # 重複除去（content先頭200文字でハッシュ）
    seen: set[str] = set()
    unique: list[dict] = []
    for c in all_chunks:
        key = c.get("content", "")[:200]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # 日付降順でソートし上位を返す
    unique.sort(key=lambda x: x.get("held_at") or "", reverse=True)
    return unique[:max_chunks]


# ---------------------------------------------------------------------------
# プロンプト用テキスト整形
# ---------------------------------------------------------------------------

def format_knowledge_for_prompt(
    knowledge: dict,
    *,
    max_chars: int = 30000,
) -> str:
    """取得したナレッジを LLM プロンプト用テキストに整形する。

    knowledge: fetch_recent_knowledge() + fetch_fts_context() +
               fetch_participant_patterns() の結果を統合した dict
    """
    sections: list[str] = []
    total = 0

    # 1. 関連する過去の決定事項
    decisions = knowledge.get("decisions", [])
    if decisions:
        lines = ["### 関連する過去の決定事項"]
        for d in decisions:
            by = f" [判断者: {d['decided_by']}]" if d.get("decided_by") else ""
            ctx = f" — {d['source_context']}" if d.get("source_context") else ""
            line = f"- [d:{d['id']}] ({d.get('decided_at', '?')}) {d['content']}{by}{ctx}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        sections.append("\n".join(lines))

    # 2. 関連するアクションアイテム
    action_items = knowledge.get("action_items", [])
    if action_items:
        lines = ["### 関連するアクションアイテム"]
        for a in action_items:
            assignee = a.get("assignee") or "未定"
            ms = f" [MS:{a['milestone_id']}]" if a.get("milestone_id") else ""
            status = a.get("status", "open")
            line = f"- [a:{a['id']}] ({a.get('extracted_at', '?')}) [{assignee}] {a['content']} ({status}){ms}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        sections.append("\n".join(lines))

    # 3. 議事録・Slackからの関連記述 (FTS5)
    # document / web は別セクションに分離し、source_ref を明示することで
    # LLM が rationale や source_context で「〇〇設計書に基づき」と引用できるようにする
    fts_chunks = knowledge.get("fts_chunks", [])
    if fts_chunks:
        docs_and_web = [c for c in fts_chunks if c.get("source_type") in ("document", "web")]
        others       = [c for c in fts_chunks if c.get("source_type") not in ("document", "web")]

        if docs_and_web:
            lines = ["### 関連ドキュメント・公開情報（根拠として引用可）"]
            for c in docs_and_web:
                src = c.get("source_type", "?")
                date = c.get("held_at") or "?"
                ref = c.get("source_ref") or ""
                text = c.get("content", "")[:500].replace("\n", " ")
                line = f"- ({date}, {src}) {text}" + (f"  [出典: {ref}]" if ref else "")
                if total + len(line) > max_chars:
                    break
                lines.append(line)
                total += len(line)
            sections.append("\n".join(lines))

        if others:
            lines = ["### 議事録・Slackからの関連記述"]
            for c in others:
                src = c.get("source_type", "?")
                date = c.get("held_at") or "?"
                text = c.get("content", "")[:500].replace("\n", " ")
                line = f"- ({date}, {src}) {text}"
                if total + len(line) > max_chars:
                    break
                lines.append(line)
                total += len(line)
            sections.append("\n".join(lines))

    # 4. 担当者パターン
    patterns = knowledge.get("participant_patterns", {})
    if patterns.get("by_milestone") or patterns.get("by_meeting_kind") or patterns.get("by_decider"):
        lines = ["### 担当者パターン"]

        by_ms = patterns.get("by_milestone", {})
        if by_ms:
            lines.append("マイルストーン別の主な担当者:")
            for ms, entries in list(by_ms.items())[:10]:
                top = ", ".join(f"{name}({cnt}件)" for name, cnt in entries[:3])
                lines.append(f"  {ms}: {top}")

        by_kind = patterns.get("by_meeting_kind", {})
        if by_kind:
            lines.append("会議種別別の主な担当者:")
            for kind, entries in list(by_kind.items())[:10]:
                top = ", ".join(f"{name}({cnt}件)" for name, cnt in entries[:3])
                lines.append(f"  {kind}: {top}")

        by_decider = patterns.get("by_decider", {})
        if by_decider:
            lines.append("会議種別別の主な判断者:")
            for kind, entries in list(by_decider.items())[:10]:
                top = ", ".join(f"{name}({cnt}件)" for name, cnt in entries[:3])
                lines.append(f"  {kind}: {top}")

        section = "\n".join(lines)
        if total + len(section) <= max_chars:
            sections.append(section)

    return "\n\n".join(sections) if sections else "（関連する過去ナレッジなし）"


# ---------------------------------------------------------------------------
# 統合: 1バッチ分のナレッジを一括取得
# ---------------------------------------------------------------------------

def gather_knowledge(
    pm_conn: sqlite3.Connection,
    items_content: list[str],
    *,
    since_days: int = 90,
    max_chars: int = 30000,
    config_path: Path | None = None,
) -> dict:
    """複数アイテムの content からキーワードを抽出し、ナレッジを一括取得する。

    Returns:
        format_knowledge_for_prompt() に渡せる dict
    """
    all_keywords: list[str] = []
    seen: set[str] = set()
    for content in items_content:
        for kw in extract_topic_keywords(content):
            if kw not in seen:
                seen.add(kw)
                all_keywords.append(kw)

    # 上位20キーワードに絞る
    all_keywords = all_keywords[:20]

    structured = fetch_recent_knowledge(pm_conn, all_keywords, since_days=since_days)
    fts_chunks = fetch_fts_context(all_keywords, config_path=config_path)
    patterns = fetch_participant_patterns(pm_conn, since_days=since_days * 2)

    return {
        "decisions": structured["decisions"],
        "action_items": structured["action_items"],
        "fts_chunks": fts_chunks,
        "participant_patterns": patterns,
    }
