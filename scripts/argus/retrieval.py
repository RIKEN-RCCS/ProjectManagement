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
from typing import Literal, overload

from cli_utils import env_int as _env_int

logger = logging.getLogger("pm_qa_server")

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #

TOP_K_RETRIEVE = 30   # FTS 検索で広めに取得する件数
# 2026-07-27 A/B（investigate_ab search 6問 66.7%）で gemma4 期の 5/400 から昇格
TOP_K_RERANK_DEFAULT = 10      # re-rank 後に回答生成へ渡す件数（既定値）
TOP_K_RERANK = TOP_K_RERANK_DEFAULT  # 後方互換 alias。実効値は env 経由で動的取得（_effective_top_k_rerank）
_RERANK_PREVIEW_CHARS_DEFAULT = 800  # re-rank プロンプトのチャンクプレビュー文字数（既定値）


def _effective_top_k_rerank() -> int:
    """ARGUS_TOP_K_RERANK（既定 TOP_K_RERANK_DEFAULT=10）の実効値を返す。"""
    return _env_int("ARGUS_TOP_K_RERANK", TOP_K_RERANK_DEFAULT)


def _effective_rerank_preview_chars() -> int:
    """ARGUS_RERANK_PREVIEW_CHARS（既定 _RERANK_PREVIEW_CHARS_DEFAULT=800）の実効値を返す。"""
    return _env_int("ARGUS_RERANK_PREVIEW_CHARS", _RERANK_PREVIEW_CHARS_DEFAULT)

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

# 機能動詞（辞書形）の除外リスト。検索の選択性に寄与しない一般動詞。
# 2026-07-30 の実運用障害調査を受けて追加（最小限で開始）。
_FUNCTION_VERB_STOPLIST = {"する", "いる", "ある", "なる", "できる", "行う", "おこなう"}

# 縮退時に真っ先に切り捨てたい時制・汎用語（明示的な降格リスト、最小限）。
_GENERIC_DEMOTE_TERMS = {
    "今年度", "理由", "現在", "状況", "経緯", "動向", "推移", "進捗", "検討", "議論", "背景",
}

# ASCII 複合エンティティ（例: 区切り記号を挟んだハイフン付き固有名詞、アプリ名/サブ名）。
# 区切り記号を挟んだ ASCII 連結語。
# \b は使わない: Python の Unicode 対応 \b はひらがな等も「単語文字」とみなすため、
# 「の」+ ハイフン付き ASCII 固有名詞のような日本語に囲まれた ASCII 語で境界が成立しない
# （2026-07-30 実測: r"\b<語>-<語>\b" 相当のパターンが日本語に囲まれた表記に
# マッチしない）。greedy な文字クラスの連続一致だけで最大長トークンを拾えるため
# 境界指定は不要。
# 単独 ASCII 語（例: NVIDIA, GB200）は Sudachi 側で既に一形態素として拾えるため、
# ここでは複合エンティティのみを対象にする（単独語も正規表現で二重抽出していた旧実装は
# 2026-07-30 recall_eval 実測で撤回: 追加抽出された略称語が Sudachi 由来の部分語と
# 別トークン化され、既存クエリで従来成立していた
# 4 語 AND 一致を壊し、1 語まで縮退して hybrid rank が 1→43 に劣化した）。
_ASCII_COMPOUND_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+")
_COMPOUND_SPLIT_RE = re.compile(r"[-_./]")


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


def _extract_ascii_entities(text: str) -> list[str]:
    """生テキストから ASCII 複合エンティティ（区切り記号を挟んだ ASCII 連結語）を抽出する。

    単独 ASCII 語（例: NVIDIA, GB200）は対象外。Sudachi 側で既に一形態素として
    拾えるため、ここで追加抽出すると Sudachi 由来の語と別トークン化されて
    AND 検索の縮退を余計に悪化させる（上の _ASCII_COMPOUND_RE 定義コメント参照）。
    """
    entities: list[str] = []
    seen: set[str] = set()
    for m in _ASCII_COMPOUND_RE.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            entities.append(tok)
    return entities


def _compound_components(entities: list[str]) -> set[str]:
    """複合エンティティを区切り記号で分解した部分語集合（大文字小文字無視）を返す。

    Sudachi 由来の部分語（例: ハイフン複合語の後半部分）除去判定に使う。
    """
    components: set[str] = set()
    for e in entities:
        if _COMPOUND_SPLIT_RE.search(e):
            for part in _COMPOUND_SPLIT_RE.split(e):
                if part:
                    components.add(part.lower())
    return components


def _token_category(token: str, pos: str) -> int:
    """縮退時の選択性順位（小さいほど優先して残す）。

    ①ASCII複合エンティティ（区切り記号を含む語。例: ハイフン結合の固有名詞）を最優先で先頭に、
    時制・汎用語（_GENERIC_DEMOTE_TERMS）を最後方へ降格する。それ以外の語は
    Sudachi の形態素出現順（文中の語順）をそのまま選択性の代理指標として使う
    （2026-07-30 実測: カタカナ・ASCII語を一律優先する4段階の並べ替えでは、
    元の語順の方が実際に選択的な組み合わせだった既存クエリ
    （例: appname-contribution-copyright-policy）で recall_eval が悪化した
    ため、カテゴリを3段に簡略化）。
    """
    if pos == "ASCII_ENTITY" and _COMPOUND_SPLIT_RE.search(token):
        return 0
    if token in _GENERIC_DEMOTE_TERMS:
        return 2
    return 1


def sudachi_tokenize_query(question: str) -> list[str]:
    """質問文をSudachiPyで形態素解析し、検索用トークンリストを返す。

    ASCII 複合エンティティ（例: ハイフン結合の固有名詞）は形態素解析前に正規表現で抽出して
    先頭カテゴリに加え、機能動詞（する/いる/ある 等）は除外する。返すトークンは
    段階的縮退（先頭 N 語）で最も選択的な語が残るよう選択性順に並べ替える。
    """
    ascii_entities = _extract_ascii_entities(question)
    components = _compound_components(ascii_entities)

    morpheme_tokens: list[tuple[str, str]] = []
    if _sudachi_tokenizer is not None:
        try:
            morphemes = _sudachi_tokenizer.tokenize(question, _sudachi_split_mode)
            seen_morph: set[str] = set()
            for m in morphemes:
                pos = m.part_of_speech()[0]
                if pos not in _SUDACHI_TARGET_POS:
                    continue
                form = m.dictionary_form()
                if len(form) < 2 or form in seen_morph:
                    continue
                if pos == "動詞" and form in _FUNCTION_VERB_STOPLIST:
                    continue
                if form.lower() in components:
                    continue
                seen_morph.add(form)
                morpheme_tokens.append((form, pos))
        except Exception:
            morpheme_tokens = []

    combined: list[tuple[str, str]] = [(e, "ASCII_ENTITY") for e in ascii_entities] + morpheme_tokens

    # 大小文字無視で重複排除する（Sudachi の dictionary_form() は ASCII 語を
    # 小文字化するため、例えば "AppName"（ASCII複合/単独語抽出）と "appname"
    # （Sudachi辞書形）が別トークン扱いのまま残ると、段階的縮退の枠を無駄に
    # 消費してしまう。2026-07-30 実測: appname-contribution-copyright-policy
    # クエリで発生）。ASCII エンティティを先頭に積んでいるため先勝ちで元の
    # 大文字小文字表記が残る。
    tokens: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tok, pos in combined:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append((tok, pos))

    if not tokens:
        return []

    order = sorted(
        range(len(tokens)),
        key=lambda i: (_token_category(tokens[i][0], tokens[i][1]), i),
    )
    return [tokens[i][0] for i in order]


# --------------------------------------------------------------------------- #
# FTS5 検索
# --------------------------------------------------------------------------- #

# 全角の区切り記号（括弧類・句読点・中黒等）。長音符「ー」は語の一部
# （例: サーバー）のため対象外。全角括弧を除去しないと「外部GPU計算リソース
# （NVL72クラス）」のような文字列が括弧ごと1トークン化し、trigram FTS の
# AND 検索が全段不成立になる（2026-07 k3-loss-analysis で特定）。
_FULLWIDTH_PUNCT = r'（）「」『』【】〈〉《》・、。：；？！〜'


def sanitize_fts_query(q: str) -> str:
    """FTS5 trigram 用にクエリを変換する。
    ひらがな連続列で分割し、意味のある語句（3文字以上）を AND 条件として返す。
    """
    q = re.sub(r'["\'\*\^\(\)\[\]？?。、,，.．！!\n\r]', " ", q)
    q = re.sub(f'[{re.escape(_FULLWIDTH_PUNCT)}]', " ", q)
    parts = re.split(r'[ぁ-ん]+', q)
    tokens = [t.strip() for t in parts if len(t.strip()) >= 3]
    if not tokens:
        # q は上の re.sub 2 本で既に両方の記号クラスを除去済みのため、
        # ここで再度同じ置換を行うのは no-op（単純に整形して返すだけでよい）。
        return q.strip()
    return " ".join(tokens)


# FTS5 MATCH クエリの予約文字。特に "-" はクエリパーサで NOT 演算子として解釈されるため、
# ハイフンを含むトークン（例: プロジェクト固有のハイフン結合語, "GH200-NVL72"）を素の
# bareword として渡すと sqlite3.OperationalError（例: ハイフン以降が不正なカラム名として
# 解釈されるエラー）が発生する。この例外は
# _fts_tokens_search / retrieve_chunks の trigram ループで sqlite3.OperationalError として
# 捕捉され「ヒットなし」に丸められるため、本来ヒットしうる部分一致が silently 握り
# つぶされ、より弱い（選択性の低い）縮退段まで落ちてしまう
# （2026-07-30 実運用クエリの実測: ASCII複合エンティティ導入に伴い顕在化。
# sanitize_fts_query 自体はハイフンを除去しないため、複合エンティティ導入前から
# 潜在していた既存バグでもある）。
_FTS5_SPECIAL_CHARS_RE = re.compile(r'["\-:^*()]')


def _fts5_escape_token(token: str) -> str:
    """FTS5 予約文字を含むトークンをダブルクォートでフレーズ化して安全に渡す。"""
    if _FTS5_SPECIAL_CHARS_RE.search(token):
        return '"' + token.replace('"', '""') + '"'
    return token


def _build_date_filter(since_date: str | None, exempt_box: bool = True) -> tuple[str, list]:
    """since_date に基づく SQL WHERE 句フラグメントと params を返す。

    exempt_box=True（既定）: box_document は日付フィルタを免除する
    （`c.held_at >= ? OR c.source_type = 'box_document'`）。box 文書は
    held_at が更新日ではなく取得日/版管理日である等の事情により、従来から
    鮮度フィルタの対象外としている。
    exempt_box=False: box_document も含め `c.held_at >= ?` のみで判定する。
    Patrol の完了証拠検索など「アイテム発生後の証拠のみを候補にしたい」用途
    で使う（box 免除だと発生前の box 文書が候補に残り続けるため）。
    held_at が NULL のチャンクは exempt_box の値に関わらず除外される
    （`NULL >= ?` は偽になるため）。
    """
    if not since_date:
        return "1=1", []
    if exempt_box:
        return "(c.held_at >= ? OR c.source_type = 'box_document')", [since_date]
    return "c.held_at >= ?", [since_date]


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
                       record_filter: str = "", record_params: list | None = None
                       ) -> tuple[list[dict], int]:
    """fts_tokens（SudachiPy形態素解析）テーブルで段階的AND検索を行う。

    戻り値は (rows, tokens_used)。tokens_used はヒットした段で AND 条件に
    使ったトークン数（縮退の弱さを呼び出し元が判定するため）。
    """
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
        query = " ".join(_fts5_escape_token(t) for t in tset)
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
                return [dict(r) for r in rows], len(tset)
        except sqlite3.OperationalError as e:
            logger.debug(f"fts_tokensクエリエラー: {e} (query={query!r})")
            return [], 0
    return [], 0


# retrieve_chunks が「どの段で結果を得たか」を表す stage 名（return_stage=True 時）。
# STAGE_DATE_FALLBACK は関連度シグナルを持たない「最新日付順」の最終手段であり、
# retrieve_chunks_hybrid の RRF マージで通常ヒットと同格に扱ってはならない
# （2026-07 k3-loss-analysis で mh-nvl72 の vector 候補が押し出される劣化を確認）。
# STAGE_FTS_TOKENS_WEAK / STAGE_TRIGRAM_WEAK は、複数語クエリが段階的縮退で
# 1 語まで落ちた状態でのヒット（選択性を失った弱いマッチ）を表す。日付・LIKE
# フォールバックと同じ枠組みで RRF マージから除外対象になる
# （2026-07-30 の実運用障害実測: 190件ヒットの低関連結果が
# vector候補を押し出した）。1語まで縮退したら語形によらず一律弱段扱いとする
# （2026-07-30 recall_eval 実測: 語形ベースの「エンティティ級は弱段除外」の
# 較正を一度試みたが、あるプロジェクト固有語がコーパス内では低選択性語だったため
# hybrid rank が 43→圏外にさらに悪化し撤回。真因は _extract_ascii_entities の
# 単独語抽出が別トークン（略称語）を注入し既存の4語AND一致を壊していたこと
# だったため、そちらを是正して対処する）。
STAGE_NO_INDEX = "no_index"
STAGE_FTS_TOKENS = "fts_tokens"
STAGE_FTS_TOKENS_WEAK = "fts_tokens_weak"
STAGE_TRIGRAM = "trigram"
STAGE_TRIGRAM_WEAK = "trigram_weak"
STAGE_LIKE = "like"
STAGE_DATE_FALLBACK = "date_fallback"


@overload
def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
                    since_date: str | None = None,
                    index_name: str | None = None,
                    record_ids: list[str] | None = None,
                    exempt_box: bool = True,
                    return_stage: Literal[False] = False) -> list[dict]: ...


@overload
def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
                    since_date: str | None = None,
                    index_name: str | None = None,
                    record_ids: list[str] | None = None,
                    exempt_box: bool = True,
                    return_stage: Literal[True] = True) -> tuple[list[dict], str]: ...


def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
                    since_date: str | None = None,
                    index_name: str | None = None,
                    record_ids: list[str] | None = None,
                    exempt_box: bool = True,
                    return_stage: bool = False) -> list[dict] | tuple[list[dict], str]:
    """統合 qa_index.db から関連チャンクを取得する。

    検索戦略（順番に試行）:
    1. SudachiPy形態素解析 → fts_tokens AND検索（段階的トークン削減）
    2. trigram FTS5 AND検索（段階的トークン削減）
    3. LIKE 検索フォールバック
    4. 最新日付レコードのフォールバック

    exempt_box: `_build_date_filter()` 参照。既定 True で従来挙動を維持。
    return_stage: True の場合 (chunks, stage) を返す。stage は STAGE_* 定数
        （どの段で結果を得たか）。既定 False は従来どおり chunks のみを返す
        （後方互換）。STAGE_FTS_TOKENS_WEAK / STAGE_TRIGRAM_WEAK は、複数語
        クエリの段階的縮退が 1 語まで落ちた状態でのヒットを表す（選択性を
        失った弱いマッチ）。
    """
    if not index_db.exists():
        logger.warning(f"インデックスDBが見つかりません: {index_db}")
        return ([], STAGE_NO_INDEX) if return_stage else []

    from db_utils import open_maybe_encrypted
    conn = open_maybe_encrypted(index_db)
    # row_factory は open_db が設定済み（sqlite3.Row を上書きすると sqlcipher3 の
    # カーソルと型が合わず TypeError になる）
    try:
        date_filter, date_params = _build_date_filter(since_date, exempt_box=exempt_box)

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
                rows, tokens_used = _fts_tokens_search(
                    conn, sudachi_tokens, k,
                    date_filter, date_params, index_name=index_name,
                    record_filter=record_filter, record_params=record_params,
                )
                if rows:
                    weak = tokens_used <= 1 < len(sudachi_tokens)
                    logger.info(
                        f"SudachiPy FTSマッチ ({len(rows)}件): {sudachi_tokens} in {idx_label} "
                        f"tokens_used={tokens_used}/{len(sudachi_tokens)}"
                        + (" [WEAK]" if weak else "")
                    )
                    stage = STAGE_FTS_TOKENS_WEAK if weak else STAGE_FTS_TOKENS
                    return (rows, stage) if return_stage else rows
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
            match_q = " ".join(_fts5_escape_token(t) for t in tset)
            rows = _fts5_search(conn, match_q, k, date_filter, date_params, index_name=index_name,
                               record_filter=record_filter, record_params=record_params)
            if rows:
                weak = len(tset) <= 1 < len(valid_tokens)
                logger.info(
                    f"trigram FTSマッチ ({len(rows)}件): [{q}] in {idx_label} "
                    f"tokens_used={len(tset)}/{len(valid_tokens)}" + (" [WEAK]" if weak else "")
                )
                stage = STAGE_TRIGRAM_WEAK if weak else STAGE_TRIGRAM
                return (rows, stage) if return_stage else rows

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
                result = [dict(r) for r in rows]
                return (result, STAGE_LIKE) if return_stage else result

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
        result = [dict(r) for r in rows]
        return (result, STAGE_DATE_FALLBACK) if return_stage else result

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
        # reasoning系が思考で消費するため。上限であり通常の消費は不変
        response = call_argus_llm(prompt, max_tokens=4096, timeout=timeout)
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
        # reasoning系が思考で消費するため。上限であり通常の消費は不変
        response = call_argus_llm(prompt, max_tokens=4096, timeout=timeout)
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
    skip_keyword_extract: bool = False,
) -> list[dict]:
    """HyDE クエリ拡張で複数クエリ検索→重複排除→マージ。

    skip_keyword_extract: True の場合 extract_search_keywords（LLM呼び出し）をスキップし、
    question をそのまま HyDE 拡張の入力に使う（呼び出し元で既にキーワード抽出済みの場合の
    二重 rewrite 回避用）。既定 False は従来どおりの挙動。
    """
    cleaned = question if skip_keyword_extract else extract_search_keywords(question)
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
                           record_ids: list[str] | None = None,
                           since_date: str | None = None,
                           exempt_box: bool = True) -> list[dict]:
    """chunk_embeddings を使って cosine similarity 検索を行う。

    since_date / exempt_box: retrieve_chunks（FTS 経路）と同じ意味論の
    フィルタ。`_build_date_filter()` 参照。
    """
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

    date_filter, date_params = _build_date_filter(since_date, exempt_box=exempt_box)

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
            " WHERE ci.index_name = ? AND " + date_filter + record_filter
        )
        rows = conn.execute(sql, [index_name] + date_params + record_params).fetchall()
    else:
        sql = (
            "SELECT c.id, c.source_type, c.source_db, c.record_id, c.held_at,"
            " c.content, c.source_ref, e.vector, e.dim"
            " FROM chunks c"
            " JOIN chunk_embeddings e ON e.chunk_id = c.id"
            " WHERE " + date_filter + record_filter
        )
        rows = conn.execute(sql, date_params + record_params).fetchall()

    if not rows:
        return []

    import numpy as np
    # dim が falsy な行はベクトル化できずスキップするため、chunks と vecs を
    # ペアで詰め直す（別リストに独立して append すると、スキップされた行の分だけ
    # 添字がずれて誤ったチャンクにスコアが割り当たる）。
    paired: list[tuple[dict, object]] = []
    for r in rows:
        c = dict(r)
        dim = c.pop("dim")
        vec = blob_to_vector(c.pop("vector"), dim) if dim else None
        if vec is not None:
            paired.append((c, vec))
    if not paired:
        return []
    chunks = [p[0] for p in paired]
    vecs = [p[1] for p in paired]
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
    exempt_box: bool = True,
    vector_k: int | None = None,
) -> list[dict]:
    """FTS5 + vector のハイブリッド検索。RRF で統合する。

    exempt_box: `_build_date_filter()` 参照。既定 True で従来挙動を維持。
    vector_k: one-shot broad-recall 用。既定 None = 従来の _VECTOR_K=50。
        指定するとベクトル検索脚の取得件数を上書きできる。
    """
    fts_results, fts_stage = retrieve_chunks(question, index_db, k=k+20,
                                             since_date=since_date, index_name=index_name,
                                             record_ids=record_ids, exempt_box=exempt_box,
                                             return_stage=True)
    from db_utils import open_maybe_encrypted
    conn = open_maybe_encrypted(index_db)
    # row_factory は open_db が設定済み（sqlite3.Row を上書きすると sqlcipher3 の
    # カーソルと型が合わず TypeError になる）
    try:
        vec_results = retrieve_chunks_vector(question, conn,
                                             k=(vector_k if vector_k is not None else _VECTOR_K),
                                             index_name=index_name,
                                             record_ids=record_ids,
                                             since_date=since_date,
                                             exempt_box=exempt_box)
    finally:
        conn.close()

    if vec_results:
        logger.info(f"[hybrid] vector_leg n={len(vec_results)}")
    else:
        logger.warning("[hybrid] vector_leg n=0")

    # STAGE_DATE_FALLBACK（最新日付順の最終手段）と STAGE_LIKE（rank 一律 0・
    # ORDER BY なしの LIKE 検索）はどちらも関連度シグナルを持たない。
    # STAGE_FTS_TOKENS_WEAK / STAGE_TRIGRAM_WEAK は複数語クエリが 1 語まで
    # 縮退した弱いマッチで、選択性を失い低関連の大量ヒットになりやすい。
    # これらを FTS 脚として RRF に混ぜると、件数（k+20）が vector 側の重み
    # （_VECTOR_SEARCH_WEIGHT=0.4）を数で上回り、無関係なチャンクが意味的に
    # 正しい vector 候補を押し出してしまう（RRF 数式上 rank r<90 の FTS 候補が
    # vector 1位に勝つため、LIMIT k+20 は容易にこれを満たす）。
    # 2026-07 k3-loss-analysis: mh-nvl72 で vector 上位50件が全滅した実測
    # （date_fallback 発生時）。2026-07-30: 別の実運用クエリで
    # fts_tokens が「今年度」1語まで縮退し同様の押し出しを実測（weak 段追加）。
    # vector 脚が空の場合のみ、従来どおり FTS 結果を最終手段として使う。
    if fts_stage in (STAGE_DATE_FALLBACK, STAGE_LIKE, STAGE_FTS_TOKENS_WEAK, STAGE_TRIGRAM_WEAK) and vec_results:
        logger.info(f"[hybrid] FTS {fts_stage} excluded from RRF (vector-only)")
        return _rrf_merge([], vec_results, k)

    if not vec_results:
        return fts_results[:k]

    return _rrf_merge(fts_results, vec_results, k)


# --------------------------------------------------------------------------- #
# Re-ranking
# --------------------------------------------------------------------------- #

def rerank_chunks(question: str, chunks: list[dict],
                  openai_base: str = "", top_k: int | None = None,
                  format_source_label=None, use_llm: bool = False) -> list[dict]:
    """LLMを使って質問に最も関連するチャンクを top_k 件に絞り込む。

    top_k: 省略時は _effective_top_k_rerank()（ARGUS_TOP_K_RERANK、既定10）を使用。
    format_source_label: chunk → str のラベル生成関数（省略時は source_ref/source_type を使用）。
    pm_qa_server.py から呼ぶ場合は _format_source_label を渡す。
    openai_base: 歴史的経緯の有効化フラグ（truthy なら re-rank を実行）。
        call_argus_llm が内部でルーティングするため URL としては未使用。
    use_llm: openai_base と同様の有効化フラグ（新規呼び出し元向け）。
        有効判定は `use_llm or bool(openai_base)`。
    """
    from cli_utils import call_argus_llm

    if top_k is None:
        top_k = _effective_top_k_rerank()

    if not chunks or len(chunks) <= top_k:
        return chunks

    if not (use_llm or openai_base):
        return chunks[:top_k]

    def _default_label(c: dict) -> str:
        return c.get("source_ref") or c.get("source_type", "?")

    _label = format_source_label or _default_label

    preview_chars = _effective_rerank_preview_chars()
    lines = []
    for i, chunk in enumerate(chunks):
        label = _label(chunk)
        preview = chunk["content"][:preview_chars].strip().replace("\n", " ")
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
        # max_tokens=4096: 上限であり非thinkingモデルの消費は増えない。rivaultフォールバック先が
        # Kimi-K2-Thinkingの場合thinkingだけで2-3k消費するため4096未満だと本文が出ず無言で先頭切りに退化する。
        # timeout=30s: search_textは高頻度ツールのためinvestigateの480s予算を守る。
        result = call_argus_llm(prompt=prompt, max_tokens=4096, timeout=30, temperature=0.0)
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
