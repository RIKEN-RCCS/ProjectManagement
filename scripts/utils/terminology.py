"""terminology.py — pm.db の terminology テーブルへのアクセスユーティリティ

用語辞書は Whisper initial_prompt・議事録生成プロンプト・reconcile_transcript の
三箇所から参照される。このモジュールは DB 接続不要な読み取り専用 API を提供する。
"""

from __future__ import annotations

import json
import os
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Whisper initial_prompt のトークン上限
WHISPER_PROMPT_TOKEN_LIMIT = 224

# 1 トークン ≈ 1.5 文字（日本語混在の概算）
_CHARS_PER_TOKEN = 1.5


def _pm_db_path() -> Path:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "data" / "pm.db"


_TERMINOLOGY_DDL = (
    "CREATE TABLE IF NOT EXISTS terminology ("
    "term TEXT PRIMARY KEY, category TEXT, aliases TEXT, source TEXT, "
    "last_seen TEXT, frequency INTEGER DEFAULT 1, meeting_kinds TEXT)"
)


def _open_pm(db_path: Path | None = None):
    """pm.db を開く（暗号化対応）。terminology テーブルを自動作成する。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from db_utils import open_db
    path = db_path or _pm_db_path()
    if not path.exists():
        return None
    try:
        conn = open_db(path, encrypt=True, row_factory=True, migrations=[_TERMINOLOGY_DDL])
        return conn
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 読み取り API
# --------------------------------------------------------------------------- #

def load_top_k(
    meeting_kind: str | None = None,
    categories: Sequence[str] | None = None,
    limit_tokens: int = WHISPER_PROMPT_TOKEN_LIMIT,
    db_path: Path | None = None,
) -> list[str]:
    """recency × frequency スコアで上位 K 件の正規形 term を返す。

    返す件数は limit_tokens を超えないよう自動調整する。
    """
    conn = _open_pm(db_path)
    if conn is None:
        return []

    try:
        now_ts = datetime.now(timezone.utc).isoformat()
        # recency スコア: 30日以内は weight=2, それ以降は weight=1
        sql_parts = ["SELECT term, aliases, frequency, last_seen, meeting_kinds FROM terminology WHERE 1=1"]
        params: list = []
        if categories:
            placeholders = ",".join("?" * len(categories))
            sql_parts.append(f"AND category IN ({placeholders})")
            params.extend(categories)
        sql_parts.append("ORDER BY frequency DESC, last_seen DESC LIMIT 500")
        sql = " ".join(sql_parts)

        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    # meeting_kind フィルタ（DB 側では JSON 配列なので Python でフィルタ）
    scored: list[tuple[float, str, list[str]]] = []
    for row in rows:
        term, aliases_json, freq, last_seen, mk_json = (
            row["term"], row["aliases"], row["frequency"] or 1,
            row["last_seen"], row["meeting_kinds"]
        )
        # meeting_kind 絞り込み
        if meeting_kind and mk_json:
            try:
                kinds = json.loads(mk_json)
                if meeting_kind not in kinds:
                    continue
            except Exception:
                pass

        # recency スコア（30 日以内 +1）
        recency_bonus = 0.0
        if last_seen:
            try:
                dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - dt).days
                if age_days <= 30:
                    recency_bonus = 1.0
            except Exception:
                pass

        aliases: list[str] = []
        if aliases_json:
            try:
                aliases = json.loads(aliases_json)
            except Exception:
                pass

        score = freq + recency_bonus
        scored.append((score, term, aliases))

    scored.sort(key=lambda x: x[0], reverse=True)

    # トークン上限まで term を収集
    result: list[str] = []
    used_chars = 0
    char_budget = int(limit_tokens * _CHARS_PER_TOKEN)
    for _, term, _ in scored:
        if used_chars + len(term) + 1 > char_budget:
            break
        result.append(term)
        used_chars += len(term) + 1  # 読点分

    return result


def get_aliases(term: str, db_path: Path | None = None) -> list[str]:
    """指定した term の aliases リストを返す。"""
    conn = _open_pm(db_path)
    if conn is None:
        return []
    try:
        row = conn.execute(
            "SELECT aliases FROM terminology WHERE term = ?", (term,)
        ).fetchone()
    finally:
        conn.close()
    if row and row["aliases"]:
        try:
            return json.loads(row["aliases"])
        except Exception:
            pass
    return []


def load_all_terms(db_path: Path | None = None) -> list[dict]:
    """terminology テーブルの全行を dict リストで返す（reconcile_transcript 用）。"""
    conn = _open_pm(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT term, category, aliases, source FROM terminology ORDER BY frequency DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 書き込み API（pm_terminology_update.py から呼ぶ）
# --------------------------------------------------------------------------- #

def add_term(
    term: str,
    category: str = "unknown",
    aliases: list[str] | None = None,
    source: str = "manual",
    meeting_kind: str | None = None,
    db_path: Path | None = None,
) -> None:
    """term を upsert する。既存なら frequency+1 して last_seen / aliases を更新。"""
    conn = _open_pm(db_path)
    if conn is None:
        return
    now_ts = datetime.now(timezone.utc).isoformat()
    aliases_json = json.dumps(aliases or [], ensure_ascii=False)
    try:
        existing = conn.execute(
            "SELECT aliases, meeting_kinds, frequency FROM terminology WHERE term = ?",
            (term,),
        ).fetchone()
        if existing:
            freq = (existing["frequency"] or 0) + 1
            # aliases をマージ
            old_aliases: list[str] = []
            if existing["aliases"]:
                try:
                    old_aliases = json.loads(existing["aliases"])
                except Exception:
                    pass
            merged_aliases = list(dict.fromkeys(old_aliases + (aliases or [])))
            # meeting_kinds をマージ
            old_kinds: list[str] = []
            if existing["meeting_kinds"]:
                try:
                    old_kinds = json.loads(existing["meeting_kinds"])
                except Exception:
                    pass
            if meeting_kind and meeting_kind not in old_kinds:
                old_kinds.append(meeting_kind)
            conn.execute(
                """UPDATE terminology SET frequency=?, last_seen=?, aliases=?, meeting_kinds=?
                   WHERE term=?""",
                (
                    freq, now_ts,
                    json.dumps(merged_aliases, ensure_ascii=False),
                    json.dumps(old_kinds, ensure_ascii=False),
                    term,
                ),
            )
        else:
            kinds_json = json.dumps([meeting_kind] if meeting_kind else [], ensure_ascii=False)
            conn.execute(
                """INSERT INTO terminology (term, category, aliases, source, last_seen, frequency, meeting_kinds)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (term, category, aliases_json, source, now_ts, kinds_json),
            )
        conn.commit()
    finally:
        conn.close()


def build_whisper_extra_prompt(
    meeting_kind: str | None = None,
    base_prompt: str = "",
    token_budget: int = WHISPER_PROMPT_TOKEN_LIMIT,
    db_path: Path | None = None,
) -> str:
    """base_prompt に用語辞書由来の固有名詞を追加した initial_prompt を返す。

    base_prompt のトークン数を引いた残り budget で用語を詰める。
    """
    base_chars = len(base_prompt)
    base_token_est = int(base_chars / _CHARS_PER_TOKEN)
    remaining_tokens = max(0, token_budget - base_token_est)
    if remaining_tokens < 10:
        return base_prompt

    terms = load_top_k(
        meeting_kind=meeting_kind,
        limit_tokens=remaining_tokens,
        db_path=db_path,
    )
    if not terms:
        return base_prompt

    extra = "追加固有名詞：" + "、".join(terms) + "。"
    return base_prompt + extra


def build_terminology_reference(
    meeting_kind: str | None = None,
    max_terms: int = 80,
    db_path: Path | None = None,
) -> str:
    """議事録生成プロンプトの Project Terminology Reference セクション用テキストを返す。

    load_claude_md_context() の出力に追記する形で使う。
    """
    terms = load_all_terms(db_path=db_path)
    if not terms:
        return ""

    if meeting_kind:
        filtered = [
            t for t in terms
            if not t.get("meeting_kinds")
            or meeting_kind in (t.get("meeting_kinds") or "")
        ]
        if filtered:
            terms = filtered

    terms = terms[:max_terms]

    lines = ["\n### 動的用語辞書 (pm.db/terminology)"]
    for t in terms:
        line = f"- {t['term']}"
        if t.get("aliases"):
            try:
                aliases = json.loads(t["aliases"])
                if aliases:
                    line += f"（別表記: {', '.join(aliases)}）"
            except Exception:
                pass
        lines.append(line)
    return "\n".join(lines) + "\n"
