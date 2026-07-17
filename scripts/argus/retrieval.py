"""retrieval.py — Argus 検索層

FTS5 / ベクトル / ハイブリッド検索ロジック。
Slack Bolt アプリ（pm_qa_server.py）から独立して unit test できる。
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from datetime import date as _date
from pathlib import Path

logger = logging.getLogger("pm_qa_server")

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #

TOP_K_RETRIEVE = 30   # FTS 検索で広めに取得する件数
TOP_K_RERANK = 5      # re-rank 後に回答生成へ渡す件数

# 鮮度の半減期（日数）。365 日 = 約 1 年で recency_score が 0.5 になる。
# 以前は 180 日（6ヶ月）と急峻で、関連性の高い歴史的マイルストーン
# （移植完了・OSS公開・初回性能測定等）が synthesis の上位から締め出されていた。
# プロジェクト全期間の実績を検索対象にできるよう緩やかな減衰に変更。
_RECENCY_HALF_LIFE_DAYS = 365.0
# 統合スコアでの鮮度重み（0=BM25/関連性のみ、1=鮮度のみ）。
# 以前は 0.4 と大きく、鮮度が関連性を押しのけていた。関連性を主・鮮度を軽い
# タイブレークに落とすため 0.15 に緩和（PM 用途の軽い新しさ優先は維持）。
_RECENCY_WEIGHT = 0.15

_VECTOR_SEARCH_WEIGHT = 0.4  # RRF での vector スコアの重み
_VECTOR_K = 50  # vector 検索の取得件数

# --------------------------------------------------------------------------- #
# SudachiPy 形態素解析
# --------------------------------------------------------------------------- #

_sudachi_tokenizer = None
_sudachi_split_mode = None
_SUDACHI_TARGET_POS = {"名詞", "動詞", "形容詞", "副詞"}


def _init_sudachi() -> bool:
    """SudachiPy の初期化。利用可能なら True を返す。"""
    global _sudachi_tokenizer, _sudachi_split_mode
    try:
        import sudachipy
        try:
            _sudachi_tokenizer = sudachipy.Dictionary().create()
            _sudachi_split_mode = sudachipy.SplitMode.C
            return True
        except Exception:
            from sudachipy import tokenizer as tm
            _sudachi_tokenizer = tm.Tokenizer()
            _sudachi_split_mode = tm.Tokenizer.SplitMode.C
            return True
    except ImportError:
        return False


def sudachi_tokenize_query(question: str) -> list[str]:
    """質問文をSudachiPyで形態素解析し、検索用トークンリストを返す。"""
    if _sudachi_tokenizer is None:
        return []
    try:
        morphemes = _sudachi_tokenizer.tokenize(question, _sudachi_split_mode)
        tokens: list[str] = []
        seen: set[str] = set()
        for m in morphemes:
            pos = m.part_of_speech()[0]
            if pos in _SUDACHI_TARGET_POS:
                form = m.dictionary_form()
                if len(form) >= 2 and form not in seen:
                    seen.add(form)
                    tokens.append(form)
        return tokens
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# FTS5 検索
# --------------------------------------------------------------------------- #

def sanitize_fts_query(q: str) -> str:
    """FTS5 trigram 用にクエリを変換する。
    ひらがな連続列で分割し、意味のある語句（3文字以上）を AND 条件として返す。
    """
    q = re.sub(r'["\'\*\^\(\)\[\]？?。、,，.．！!\n\r]', " ", q)
    parts = re.split(r'[ぁ-ん]+', q)
    tokens = [t.strip() for t in parts if len(t.strip()) >= 3]
    if not tokens:
        return re.sub(r'["\'\*\^\(\)\[\]？?。、！!]', " ", q).strip()
    return " ".join(tokens)


def _fts5_search(conn: sqlite3.Connection, query: str, k: int,
                 date_filter: str = "1=1", date_params: list | None = None,
                 index_name: str | None = None,
                 record_filter: str = "", record_params: list | None = None) -> list[dict]:
    date_params = date_params or []
    record_params = record_params or []
    try:
        if index_name:
            sql = (
                "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
                "       c.content, c.source_ref, fts.rank"
                " FROM fts"
                " JOIN chunks c ON fts.rowid = c.id"
                " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
                " WHERE fts MATCH ? AND ci.index_name = ? AND " + date_filter + record_filter +
                " ORDER BY rank LIMIT ?"
            )
            params = [query, index_name] + date_params + record_params + [k]
        else:
            sql = (
                "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
                "       c.content, c.source_ref, fts.rank"
                " FROM fts"
                " JOIN chunks c ON fts.rowid = c.id"
                " WHERE fts MATCH ? AND " + date_filter + record_filter +
                " ORDER BY rank LIMIT ?"
            )
            params = [query] + date_params + record_params + [k]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        logger.debug(f"FTS5クエリエラー: {e} (query={query!r})")
        return []


def _fts_tokens_search(conn: sqlite3.Connection, tokens: list[str], k: int,
                       date_filter: str = "1=1", date_params: list | None = None,
                       index_name: str | None = None,
                       record_filter: str = "", record_params: list | None = None) -> list[dict]:
    """fts_tokens（SudachiPy形態素解析）テーブルで段階的AND検索を行う。"""
    date_params = date_params or []
    record_params = record_params or []
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
            if index_name:
                sql = (
                    "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
                    "       c.content, c.source_ref, fts_tokens.rank"
                    " FROM fts_tokens"
                    " JOIN chunks c ON fts_tokens.rowid = c.id"
                    " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
                    " WHERE fts_tokens MATCH ? AND ci.index_name = ? AND " + date_filter + record_filter +
                    " ORDER BY rank LIMIT ?"
                )
                params = [query, index_name] + date_params + record_params + [k]
            else:
                sql = (
                    "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
                    "       c.content, c.source_ref, fts_tokens.rank"
                    " FROM fts_tokens"
                    " JOIN chunks c ON fts_tokens.rowid = c.id"
                    " WHERE fts_tokens MATCH ? AND " + date_filter + record_filter +
                    " ORDER BY rank LIMIT ?"
                )
                params = [query] + date_params + record_params + [k]
            rows = conn.execute(sql, params).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug(f"fts_tokensクエリエラー: {e} (query={query!r})")
            return []
    return []


def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
                    since_date: str | None = None,
                    index_name: str | None = None,
                    record_ids: list[str] | None = None) -> list[dict]:
    """統合 qa_index.db から関連チャンクを取得する。

    検索戦略（順番に試行）:
    1. SudachiPy形態素解析 → fts_tokens AND検索（段階的トークン削減）
    2. trigram FTS5 AND検索（段階的トークン削減）
    3. LIKE 検索フォールバック
    4. 最新日付レコードのフォールバック
    """
    if not index_db.exists():
        logger.warning(f"インデックスDBが見つかりません: {index_db}")
        return []

    conn = sqlite3.connect(str(index_db))
    conn.row_factory = sqlite3.Row
    try:
        date_filter = "c.held_at >= ?" if since_date else "1=1"
        date_params = [since_date] if since_date else []

        if record_ids:
            placeholders = ",".join("?" * len(record_ids))
            record_filter = f" AND c.record_id IN ({placeholders})"
            record_params = list(record_ids)
        else:
            record_filter = ""
            record_params = []

        if index_name:
            ci_join = " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
            ci_where = "ci.index_name = ? AND "
            ci_params: list = [index_name]
        else:
            ci_join = ""
            ci_where = ""
            ci_params = []

        idx_label = f"{index_db.name}[{index_name}]" if index_name else index_db.name

        # --- Step 1: SudachiPy形態素解析 + fts_tokens 検索 ---
        sudachi_tokens = sudachi_tokenize_query(question)
        if sudachi_tokens:
            has_fts_tokens = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_tokens'"
            ).fetchone() is not None

            if has_fts_tokens:
                rows = _fts_tokens_search(
                    conn, sudachi_tokens, k,
                    date_filter, date_params, index_name=index_name,
                    record_filter=record_filter, record_params=record_params,
                )
                if rows:
                    logger.info(
                        f"SudachiPy FTSマッチ ({len(rows)}件): {sudachi_tokens} in {idx_label}"
                    )
                    return rows
                logger.debug(f"SudachiPy FTS: ヒットなし ({sudachi_tokens})")

        # --- Step 2: trigram FTS5 検索 ---
        sanitized = sanitize_fts_query(question)
        valid_tokens = [t for t in sanitized.split() if len(t) >= 3]

        token_sets = []
        if valid_tokens:
            token_sets.append(valid_tokens)
            if len(valid_tokens) > 3:
                token_sets.append(valid_tokens[:3])
            if len(valid_tokens) > 2:
                token_sets.append(valid_tokens[:2])
            if len(valid_tokens) > 1:
                token_sets.append(valid_tokens[:1])

        for tset in token_sets:
            q = " ".join(tset)
            rows = _fts5_search(conn, q, k, date_filter, date_params, index_name=index_name,
                               record_filter=record_filter, record_params=record_params)
            if rows:
                logger.info(f"trigram FTSマッチ ({len(rows)}件): [{q}] in {idx_label}")
                return rows

        # --- Step 3: LIKE 検索 ---
        keyword = (sudachi_tokens[0] if sudachi_tokens else
                   (valid_tokens[0] if valid_tokens else ""))
        if keyword:
            sql = (
                "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
                " c.content, c.source_ref, 0 AS rank"
                " FROM chunks c" + ci_join +
                " WHERE " + ci_where + date_filter + record_filter + " AND c.content LIKE ? LIMIT ?"
            )
            params = ci_params + date_params + record_params + [f"%{keyword}%", k]
            rows = conn.execute(sql, params).fetchall()
            if rows:
                logger.info(f"LIKE検索フォールバック ({len(rows)}件): [{keyword}]")
                return [dict(r) for r in rows]

        # --- Step 4: 最新記録フォールバック ---
        logger.info(f"マッチなし → 最新記録フォールバック (sudachi={sudachi_tokens})")
        sql = (
            "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
            " c.content, c.source_ref, 0 AS rank"
            " FROM chunks c" + ci_join +
            " WHERE " + ci_where + date_filter + record_filter +
            " AND c.held_at IS NOT NULL ORDER BY c.held_at DESC LIMIT ?"
        )
        params = ci_params + date_params + record_params + [k]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# クエリ意図抽出（メタ語除去）
# --------------------------------------------------------------------------- #

def extract_search_keywords(query: str, timeout: int = 30) -> str:
    """ユーザー質問から FTS 検索に使うべきキーワードだけを抽出する。"""
    prompt = (
        "あなたは検索クエリの整理役です。\n"
        "ユーザーの質問から、FTS全文検索で実際に当てるべき「検索対象キーワード」"
        "だけをスペース区切りで抽出してください。\n\n"
        "除外する語の例:\n"
        "- メタ要求語: 議論, 検討, 討議, 進捗, 経緯, 推移, 動向, 状況, 内容\n"
        "- 指示動詞: 整理, まとめ, 要約, 教えて, 知りたい, 説明, 整理して\n"
        "- 一般的な疑問詞: いつ, どこ, なぜ, どう, どのように\n"
        "- 時間範囲表現: 最近, 直近, 過去, 今, 現在\n\n"
        "残すべき語:\n"
        "- 固有名詞・技術用語（スケールアウトネットワーク, MONAKA-X, 帯域幅, FP8 等）\n"
        "- 人名・組織名（富士通, NVIDIA, 西澤 等）\n"
        "- 略語・型番（NVL72, M3, SubWG3 等）\n\n"
        f"質問: {query}\n\n"
        "出力（キーワードをスペース区切り、説明文・コードブロック禁止、1行のみ）:"
    )
    try:
        from cli_utils import call_argus_llm
        response = call_argus_llm(prompt, max_tokens=100, timeout=timeout)
        line = response.strip().splitlines()[0].strip() if response.strip() else ""
        line = re.sub(r"^[-*\d.）)\s]+", "", line).strip()
        if not line or len(line) < 2:
            return query
        return line
    except Exception as e:
        logger.warning(f"[KeywordExtract] 失敗: {e}")
        return query


# --------------------------------------------------------------------------- #
# 鮮度スコアリング
# --------------------------------------------------------------------------- #

def _recency_score(held_at: str | None, today=None) -> float:
    """指数減衰での鮮度スコア（0.0〜1.0、新しいほど 1 に近い）。"""
    if today is None:
        today = _date.today()
    if not held_at:
        return 0.5
    try:
        d = _date.fromisoformat(str(held_at)[:10])
    except (ValueError, TypeError):
        return 0.5
    age_days = max(0, (today - d).days)
    return math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS * math.log(2))


def _combined_score(chunk: dict, today=None) -> float:
    """BM25 ランクと鮮度スコアを加重和で統合（高いほど良い）。"""
    raw_rank = chunk.get("rank")
    if raw_rank is None:
        bm25_norm = 0.5
    else:
        try:
            r = -float(raw_rank)
            bm25_norm = 1.0 / (1.0 + max(0.0, r) * 0.1)
        except (TypeError, ValueError):
            bm25_norm = 0.5
    rec = _recency_score(chunk.get("held_at"), today)
    return (1.0 - _RECENCY_WEIGHT) * bm25_norm + _RECENCY_WEIGHT * rec


# --------------------------------------------------------------------------- #
# HyDE クエリ拡張
# --------------------------------------------------------------------------- #

def expand_query_hyde(query: str, n_extra: int = 2, timeout: int = 30) -> list[str]:
    """HyDE 風クエリ拡張: 元クエリ + LLM 生成の別表現を返す。"""
    prompt = (
        f"以下の検索クエリを、日本語と英語が混在するドキュメントの全文検索で\n"
        f"当たりやすくするため別表現に書き換えてください。\n"
        f"必ず以下を含めること:\n"
        f"  - 日本語の別表現1つ（カタカナ語・漢字熟語など本文に出てきそうな語彙）\n"
        f"  - 英語訳1つ（プロジェクト・技術用語）\n"
        f"残りは日本語または英語の補助クエリ。\n\n"
        f"元クエリ: {query}\n\n"
        f"出力フォーマット（各行1クエリ、コードブロック禁止、説明文禁止、{n_extra}行のみ）:"
    )
    try:
        from cli_utils import call_argus_llm
        response = call_argus_llm(prompt, max_tokens=200, timeout=timeout)
        extras = [ln.strip() for ln in response.splitlines() if ln.strip()]
        extras = [re.sub(r"^[-*\d.）)\s]+", "", ln).strip() for ln in extras]
        extras = [e for e in extras if e and e != query][:n_extra]
    except Exception as e:
        logger.warning(f"[HyDE] 拡張失敗: {e}")
        extras = []
    return [query] + extras


def retrieve_chunks_hyde(
    question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
    since_date: str | None = None, n_extra: int = 2, max_merged: int = 60,
    index_name: str | None = None,
    record_ids: list[str] | None = None,
) -> list[dict]:
    """HyDE クエリ拡張で複数クエリ検索→重複排除→マージ。"""
    cleaned = extract_search_keywords(question)
    if cleaned != question:
        logger.info(f"[KeywordExtract] '{question}' → '{cleaned}'")
    queries = expand_query_hyde(cleaned, n_extra=n_extra)
    logger.info(f"[HyDE] queries={queries}")
    seen: set = set()
    merged: list[dict] = []
    for q in queries:
        for c in retrieve_chunks_hybrid(q, index_db, k=k, since_date=since_date,
                                        index_name=index_name, record_ids=record_ids):
            key = (c.get("source_db"), c.get("record_id"), c.get("content", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    logger.info(f"[HyDE] マージ後 {len(merged)} チャンク")
    today = _date.today()
    merged.sort(key=lambda c: _combined_score(c, today), reverse=True)
    return merged[:max_merged]


# --------------------------------------------------------------------------- #
# ベクトル検索（embedding）
# --------------------------------------------------------------------------- #

def retrieve_chunks_vector(query: str, conn: sqlite3.Connection, k: int = _VECTOR_K,
                           index_name: str | None = None,
                           record_ids: list[str] | None = None) -> list[dict]:
    """chunk_embeddings を使って cosine similarity 検索を行う。"""
    try:
        from embed_utils import blob_to_vector, cosine_similarity_matrix, embed_one
    except ImportError:
        logger.warning("embed_utils が利用できません — vector 検索をスキップ")
        return []

    try:
        qvec = embed_one(query)
    except Exception as e:
        logger.warning(f"embedding 取得エラー: {e}")
        return []

    if record_ids:
        placeholders = ",".join("?" * len(record_ids))
        record_filter = f" AND c.record_id IN ({placeholders})"
        record_params = list(record_ids)
    else:
        record_filter = ""
        record_params = []

    if index_name:
        sql = (
            "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
            "       c.content, c.source_ref, e.vector, e.dim"
            " FROM chunks c"
            " JOIN chunk_embeddings e ON e.chunk_id = c.id"
            " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
            " WHERE ci.index_name = ?" + record_filter
        )
        rows = conn.execute(sql, [index_name] + record_params).fetchall()
    else:
        sql = (
            "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
            " c.content, c.source_ref, e.vector, e.dim"
            " FROM chunks c"
            " JOIN chunk_embeddings e ON e.chunk_id = c.id"
            " WHERE 1=1" + record_filter
        )
        rows = conn.execute(sql, record_params).fetchall()

    if not rows:
        return []

    import numpy as np
    chunks = [dict(r) for r in rows]
    vecs = []
    for c in chunks:
        dim = c.pop("dim")
        vec = blob_to_vector(c.pop("vector"), dim) if dim else None
        if vec is not None:
            vecs.append(vec)
    if not vecs:
        return []
    vectors = np.stack(vecs)
    sims = cosine_similarity_matrix(qvec, vectors)
    top_k = np.argsort(-sims)[:k]
    results = []
    for i in top_k:
        c = chunks[i]
        c["vector_score"] = float(sims[i])
        results.append(c)
    return results


def _rrf_merge(fts_chunks: list[dict], vec_chunks: list[dict], k: int,
               rrf_k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion で FTS5 と vector の結果を統合する。"""
    rank_map: dict[int, float] = {}

    for rank, c in enumerate(fts_chunks):
        cid = c["id"]
        rank_map[cid] = rank_map.get(cid, 0.0) + 1.0 / (rrf_k + rank)

    for rank, c in enumerate(vec_chunks):
        cid = c["id"]
        rank_map[cid] = rank_map.get(cid, 0.0) + _VECTOR_SEARCH_WEIGHT / (rrf_k + rank)

    sorted_ids = sorted(rank_map.keys(), key=lambda cid: -rank_map[cid])

    chunk_dict = {c["id"]: c for c in vec_chunks}
    for c in fts_chunks:
        if c["id"] not in chunk_dict:
            chunk_dict[c["id"]] = c

    merged = []
    for cid in sorted_ids[:k]:
        c = dict(chunk_dict[cid])
        c["rrf_score"] = rank_map[cid]
        merged.append(c)
    return merged


def retrieve_chunks_hybrid(
    question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
    since_date: str | None = None, index_name: str | None = None,
    record_ids: list[str] | None = None,
) -> list[dict]:
    """FTS5 + vector のハイブリッド検索。RRF で統合する。"""
    fts_results = retrieve_chunks(question, index_db, k=k+20,
                                  since_date=since_date, index_name=index_name,
                                  record_ids=record_ids)
    conn = sqlite3.connect(str(index_db))
    conn.row_factory = sqlite3.Row
    try:
        vec_results = retrieve_chunks_vector(question, conn, k=_VECTOR_K,
                                             index_name=index_name,
                                             record_ids=record_ids)
    finally:
        conn.close()

    if not vec_results:
        return fts_results[:k]

    return _rrf_merge(fts_results, vec_results, k)


# --------------------------------------------------------------------------- #
# Re-ranking
# --------------------------------------------------------------------------- #

def rerank_chunks(question: str, chunks: list[dict],
                  openai_base: str = "", top_k: int = TOP_K_RERANK,
                  format_source_label=None) -> list[dict]:
    """LLMを使って質問に最も関連するチャンクを top_k 件に絞り込む。

    format_source_label: chunk → str のラベル生成関数（省略時は source_ref/source_type を使用）。
    pm_qa_server.py から呼ぶ場合は _format_source_label を渡す。
    """
    from cli_utils import call_argus_llm

    if not chunks or len(chunks) <= top_k:
        return chunks

    if not openai_base:
        return chunks[:top_k]

    def _default_label(c: dict) -> str:
        return c.get("source_ref") or c.get("source_type", "?")

    _label = format_source_label or _default_label

    lines = []
    for i, chunk in enumerate(chunks):
        label = _label(chunk)
        preview = chunk["content"][:400].strip().replace("\n", " ")
        lines.append(f"[{i}] {label}\n{preview}")
    context_str = "\n\n".join(lines)

    prompt = (
        f"以下のチャンク一覧から、質問に最も関連するものを{top_k}件選んでください。\n"
        f"**関連性を最優先し、同程度に関連する場合のみ新しいものを優先してください。**\n"
        f"番号のみをスペース区切りで出力してください（例: 0 3 7 12 15）。\n\n"
        f"質問: {question}\n\n"
        f"チャンク一覧:\n{context_str}"
    )

    try:
        result = call_argus_llm(prompt=prompt, max_tokens=30, timeout=60, temperature=0.0)
        indices: list[int] = []
        for token in result.strip().split():
            try:
                idx = int(token)
                if 0 <= idx < len(chunks) and idx not in indices:
                    indices.append(idx)
            except ValueError:
                continue

        if indices:
            logger.info(f"  re-rank選択: {indices} → {len(indices)} 件")
            return [chunks[i] for i in indices[:top_k]]

        logger.warning("re-rank: 有効な番号が得られず先頭件数で代替")
    except Exception as e:
        logger.warning(f"re-rankエラー: {e}. 日付降順フォールバックを使用")
        return sorted(chunks, key=lambda x: x.get("held_at", ""), reverse=True)[:top_k]

    return chunks[:top_k]
