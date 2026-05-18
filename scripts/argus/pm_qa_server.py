#!/usr/bin/env python3
"""
pm_qa_server.py - Slack Slash Command QA サーバー（Socket Mode）

/argus-ask <質問> を受け取り、実行チャンネルに対応するインデックスDB (FTS5) で
関連情報を検索し、ローカルLLMで回答を生成してSlackにephemeralで返す。

起動方法:
  source ~/.secrets/slack_tokens.sh
  export OPENAI_API_BASE="http://localhost:8000/v1" OPENAI_API_KEY="dummy"
  python3 scripts/pm_qa_server.py

環境変数:
  SLACK_BOT_TOKEN   必須: Bot Token (xoxb-)
  SLACK_APP_TOKEN   必須: App-Level Token (xapp-)
  OPENAI_API_BASE   必須: vLLM エンドポイント
  OPENAI_API_KEY    デフォルト: "dummy"
  （モデル名は vLLM /v1/models から自動取得）
  ARGUS_CONFIG      デフォルト: data/argus_config.yaml（旧 QA_CONFIG / qa_config.yaml にフォールバック）
"""

import logging
import os
import re
import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_local_llm, load_claude_md_context
from db_utils import open_pm_db, fetch_milestone_progress, fetch_overdue_items, fetch_summary_stats
from argus.pm_argus import _run_brief, _run_draft, _run_risk, _run_today_only, _run_transcribe, _transcribe_jobs, _transcribe_lock
from argus.pm_argus_agent import _run_investigate
from argus.patrol.confirm import handle_approve_close, handle_reject_close
from argus.patrol.state import PatrolState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pm_qa_server")

# --- SudachiPy 形態素解析 ---

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


# --- 設定 ---
TOP_K_RETRIEVE = 30   # FTS検索で広めに取得する件数
TOP_K_RERANK = 5      # re-rank後に回答生成へ渡す件数
MAX_TOKENS = 1024
LLM_TIMEOUT = 120
RERANK_TIMEOUT = 60  # 30 → 60秒（議事録生成の re-rank に十分な時間を確保）

_OPENAI_BASE = os.environ.get("OPENAI_API_BASE", "")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "dummy")
_OPENAI_MODEL = ""
if _OPENAI_BASE:
    try:
        from cli_utils import detect_vllm_model
        _OPENAI_MODEL = detect_vllm_model(_OPENAI_BASE)
    except Exception as _e:
        print(f"[WARN] vLLM モデル自動検出に失敗: {_e}", file=sys.stderr)

_PROJECT_CONTEXT = ""

SYSTEM_PROMPT_TEMPLATE = """\
あなたは富岳NEXTプロジェクトの情報検索アシスタントです。

【回答ルール】
- 以下の「取得した関連情報」のみを根拠として、日本語で回答してください
- 構造化データ検索結果がある場合、そこに含まれるID・担当者・期限・件数は正確に記載してください
- テキスト検索結果がある場合、出典の日付・会議名を可能な限り含めてください
- 情報が見つからない場合は「記録が見つかりません」とだけ回答してください
- 推測・創作はしないでください
- 回答全体は500字以内を目安にしてください（長い場合は要点を箇条書きに）

【プロジェクト文脈】
{project_context}
"""

# --- 設定ロード ---

_channel_index_map: dict[str, str] = {}   # channel_id → index_name
_index_db_map: dict[str, Path] = {}       # index_name → Path
_pm_db_map: dict[str, list[Path]] = {}    # index_name → [pm.db Paths]
_default_index: str = "pm"


def load_qa_config(config_path: Path) -> None:
    """argus_config.yaml（旧 qa_config.yaml）を読み込み、グローバルマップを初期化する。
    全インデックスは統合 qa_index.db を共有し、検索時に index_name でフィルタする。"""
    global _channel_index_map, _index_db_map, _pm_db_map, _default_index

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    _default_index = cfg.get("default_index", "pm")
    _channel_index_map = cfg.get("channel_map") or {}

    qa_unified = _REPO_ROOT / "data" / "qa_index.db"
    for name, index_cfg in (cfg.get("indices") or {}).items():
        # qa_*.db への個別パス指定があっても無視し、統合 DB を使う。
        # 検索クエリが index_name でフィルタする前提。
        _index_db_map[name] = qa_unified
        pm_db_list = index_cfg.get("pm_db", [])
        _pm_db_map[name] = [_REPO_ROOT / p for p in pm_db_list]

    logger.info(f"argus_config ロード: {len(_index_db_map)} インデックス, "
                f"{len(_channel_index_map)} チャンネルマッピング, "
                f"デフォルト={_default_index}")


def resolve_index_db(channel_id: str) -> tuple[str, Path, list[Path]]:
    """チャンネルIDからインデックス名・FTS DBパス（統合）・pm.dbパスリストを返す。
    DBパスは全インデックス共通で data/qa_index.db。検索時に index_name で絞り込む。"""
    index_name = _channel_index_map.get(channel_id, _default_index)
    db_path = _index_db_map.get(index_name)
    if db_path is None:
        # 設定ロード前 / インデックス未定義時のフォールバック
        db_path = _REPO_ROOT / "data" / "qa_index.db"
    pm_db_paths = _pm_db_map.get(index_name, [])
    return index_name, db_path, pm_db_paths


# --- FTS5検索 ---

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
                 date_filter: str = "1=1", date_params: list = [],
                 index_name: str | None = None) -> list[dict]:
    try:
        if index_name:
            sql = (
                "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
                "       c.content, c.source_ref, fts.rank"
                " FROM fts"
                " JOIN chunks c ON fts.rowid = c.id"
                " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
                " WHERE fts MATCH ? AND ci.index_name = ? AND " + date_filter +
                " ORDER BY rank LIMIT ?"
            )
            params = [query, index_name] + date_params + [k]
        else:
            sql = (
                "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
                "       c.content, c.source_ref, fts.rank"
                " FROM fts"
                " JOIN chunks c ON fts.rowid = c.id"
                " WHERE fts MATCH ? AND " + date_filter +
                " ORDER BY rank LIMIT ?"
            )
            params = [query] + date_params + [k]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        logger.debug(f"FTS5クエリエラー: {e} (query={query!r})")
        return []


def _fts_tokens_search(conn: sqlite3.Connection, tokens: list[str], k: int,
                       date_filter: str = "1=1", date_params: list = [],
                       index_name: str | None = None) -> list[dict]:
    """fts_tokens（SudachiPy形態素解析）テーブルで段階的AND検索を行う。"""
    # 全トークン → 先頭3 → 先頭2 → 先頭1 の順で試行
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
                    "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
                    "       c.content, c.source_ref, fts_tokens.rank"
                    " FROM fts_tokens"
                    " JOIN chunks c ON fts_tokens.rowid = c.id"
                    " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
                    " WHERE fts_tokens MATCH ? AND ci.index_name = ? AND " + date_filter +
                    " ORDER BY rank LIMIT ?"
                )
                params = [query, index_name] + date_params + [k]
            else:
                sql = (
                    "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
                    "       c.content, c.source_ref, fts_tokens.rank"
                    " FROM fts_tokens"
                    " JOIN chunks c ON fts_tokens.rowid = c.id"
                    " WHERE fts_tokens MATCH ? AND " + date_filter +
                    " ORDER BY rank LIMIT ?"
                )
                params = [query] + date_params + [k]
            rows = conn.execute(sql, params).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug(f"fts_tokensクエリエラー: {e} (query={query!r})")
            return []
    return []


def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
                    since_date: str | None = None,
                    index_name: str | None = None) -> list[dict]:
    """統合 qa_index.db から関連チャンクを取得する。

    検索戦略（順番に試行）:
    1. SudachiPy形態素解析 → fts_tokens AND検索（段階的トークン削減）
    2. trigram FTS5 AND検索（段階的トークン削減）
    3. LIKE 検索フォールバック
    4. 最新日付レコードのフォールバック

    Args:
        question: 検索クエリ
        index_db: 統合インデックスDBのパス（通常 data/qa_index.db）
        k: 取得件数上限
        since_date: YYYY-MM-DD形式。指定時はこの日付以降のレコードのみ検索
        index_name: 指定すると chunk_indexes 経由で当該 index に紐づくチャンクのみ
    """
    if not index_db.exists():
        logger.warning(f"インデックスDBが見つかりません: {index_db}")
        return []

    conn = sqlite3.connect(str(index_db))
    conn.row_factory = sqlite3.Row
    try:
        # 日付フィルタ条件句の構築
        date_filter = "c.held_at >= ?" if since_date else "1=1"
        date_params = [since_date] if since_date else []

        # LIKE/最新フォールバック用に、index_name フィルタの SQL 片を組み立てる
        if index_name:
            ci_join = " JOIN chunk_indexes ci ON ci.chunk_id = c.id"
            ci_where = "ci.index_name = ? AND "
            ci_params = [index_name]
        else:
            ci_join = ""
            ci_where = ""
            ci_params: list = []

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
                    date_filter.replace("c.held_at", "c.held_at"),
                    date_params, index_name=index_name,
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
            rows = _fts5_search(
                conn, q, k, date_filter, date_params, index_name=index_name,
            )
            if rows:
                logger.info(f"trigram FTSマッチ ({len(rows)}件): [{q}] in {idx_label}")
                return rows

        # --- Step 3: LIKE 検索 ---
        keyword = (sudachi_tokens[0] if sudachi_tokens else
                   (valid_tokens[0] if valid_tokens else ""))
        if keyword:
            sql = (
                "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
                " c.content, c.source_ref, 0 AS rank"
                " FROM chunks c" + ci_join +
                " WHERE " + ci_where + date_filter + " AND c.content LIKE ? LIMIT ?"
            )
            params = ci_params + date_params + [f"%{keyword}%", k]
            rows = conn.execute(sql, params).fetchall()
            if rows:
                logger.info(f"LIKE検索フォールバック ({len(rows)}件): [{keyword}]")
                return [dict(r) for r in rows]

        # --- Step 4: 最新記録フォールバック ---
        logger.info(f"マッチなし → 最新記録フォールバック (sudachi={sudachi_tokens})")
        sql = (
            "SELECT c.source_type, c.source_db, c.record_id, c.held_at,"
            " c.content, c.source_ref, 0 AS rank"
            " FROM chunks c" + ci_join +
            " WHERE " + ci_where + date_filter +
            " AND c.held_at IS NOT NULL ORDER BY c.held_at DESC LIMIT ?"
        )
        params = ci_params + date_params + [k]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    finally:
        conn.close()


# --- クエリ意図抽出（メタ語除去）---

def extract_search_keywords(query: str, timeout: int = 30) -> str:
    """ユーザー質問から FTS 検索に使うべきキーワードだけを抽出する。

    「議論」「推移」「経緯」「整理して」のようなメタ要求語・指示動詞は
    検索キーワードとして AND 条件に入ると本来欲しい文書を弾いてしまう。
    LLM で純粋な検索対象キーワードだけを残す。

    エラー時は元クエリをそのまま返す（フォールバック）。
    """
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
        # 1 行目だけ取り、余計な記号を除去
        line = response.strip().splitlines()[0].strip() if response.strip() else ""
        import re as _re
        line = _re.sub(r"^[-*\d.）)\s]+", "", line).strip()
        # 出力が空・元クエリと同一・極端に短い（記号のみ等）場合はフォールバック
        if not line or len(line) < 2:
            return query
        return line
    except Exception as e:
        logger.warning(f"[KeywordExtract] 失敗: {e}")
        return query


# --- 鮮度スコアリング ---

# 鮮度の半減期（日数）。180 日 = 約 6 ヶ月で recency_score が 0.5 になる
_RECENCY_HALF_LIFE_DAYS = 180.0
# 統合スコアでの鮮度重み（0=BM25 のみ、1=鮮度のみ）
_RECENCY_WEIGHT = 0.4


def _recency_score(held_at: str | None, today=None) -> float:
    """指数減衰での鮮度スコア（0.0〜1.0、新しいほど 1 に近い）。

    held_at が不明な場合は中央値 0.5 を返す。
    half_life_days 経過で 0.5、その2倍で 0.25、と指数減衰する。
    """
    import math
    from datetime import date as _date
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
    """BM25 ランクと鮮度スコアを加重和で統合（高いほど良い）。

    FTS5 の rank は負の値で「小さいほど（より負ほど）良い」ため正規化する。
    """
    raw_rank = chunk.get("rank")
    if raw_rank is None:
        bm25_norm = 0.5
    else:
        # rank は通常 -10〜0 の範囲。-rank を取って 1/(1+x) で 0..1 に正規化。
        try:
            r = -float(raw_rank)
            bm25_norm = 1.0 / (1.0 + max(0.0, r) * 0.1)
        except (TypeError, ValueError):
            bm25_norm = 0.5
    rec = _recency_score(chunk.get("held_at"), today)
    return (1.0 - _RECENCY_WEIGHT) * bm25_norm + _RECENCY_WEIGHT * rec


# --- HyDE クエリ拡張 ---

def expand_query_hyde(query: str, n_extra: int = 2, timeout: int = 30) -> list[str]:
    """HyDE 風クエリ拡張: 元クエリ + LLM 生成の別表現を返す。

    富岳NEXT プロジェクトの議事録・Slack は日本語と英語の用語が混在する
    （例: "スケールアウトネットワーク" vs "scale-out network"、"演算性能" vs "FP8 performance"）。
    片方の言語だけで FTS 検索すると半分の文書しか当たらないため、
    必ず日本語版・英語版の両方を生成する。
    エラー時は元クエリのみ返す（フォールバック）。
    """
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
        import re as _re
        extras = [_re.sub(r"^[-*\d.）)\s]+", "", ln).strip() for ln in extras]
        extras = [e for e in extras if e and e != query][:n_extra]
    except Exception as e:
        logger.warning(f"[HyDE] 拡張失敗: {e}")
        extras = []
    return [query] + extras


def retrieve_chunks_hyde(
    question: str, index_db: Path, k: int = TOP_K_RETRIEVE,
    since_date: str | None = None, n_extra: int = 2, max_merged: int = 60,
    index_name: str | None = None,
) -> list[dict]:
    """HyDE クエリ拡張で複数クエリ検索→重複排除→マージ。retrieve_chunks のラッパ。

    前段で意図抽出（メタ要求語・指示動詞の除去）を行ってから HyDE で言い換えを生成する。
    """
    cleaned = extract_search_keywords(question)
    if cleaned != question:
        logger.info(f"[KeywordExtract] '{question}' → '{cleaned}'")
    queries = expand_query_hyde(cleaned, n_extra=n_extra)
    logger.info(f"[HyDE] queries={queries}")
    seen: set = set()
    merged: list[dict] = []
    for q in queries:
        for c in retrieve_chunks(q, index_db, k=k, since_date=since_date,
                                 index_name=index_name):
            key = (c.get("source_db"), c.get("record_id"), c.get("content", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    logger.info(f"[HyDE] マージ後 {len(merged)} チャンク")
    # 鮮度を加味した統合スコアで再ソート（新しい情報を優先）
    from datetime import date as _date
    today = _date.today()
    merged.sort(key=lambda c: _combined_score(c, today), reverse=True)
    return merged[:max_merged]


# --- Re-ranking ---

def rerank_chunks(question: str, chunks: list[dict]) -> list[dict]:
    """LLMを使って質問に最も関連するチャンクをTOP_K_RERANK件に絞り込む。
    LLMが利用不可またはエラー時は先頭TOP_K_RERANK件をそのまま返す。
    """
    if not chunks or len(chunks) <= TOP_K_RERANK:
        return chunks

    if not _OPENAI_BASE:
        return chunks[:TOP_K_RERANK]

    lines = []
    for i, chunk in enumerate(chunks):
        label = _format_source_label(chunk)
        preview = chunk["content"][:400].strip().replace("\n", " ")
        lines.append(f"[{i}] {label}\n{preview}")
    context_str = "\n\n".join(lines)

    prompt = (
        f"以下のチャンク一覧から、質問に最も関連するものを{TOP_K_RERANK}件選んでください。\n"
        f"**直近の議論を優先してください。**\n"
        f"番号のみをスペース区切りで出力してください（例: 0 3 7 12 15）。\n\n"
        f"質問: {question}\n\n"
        f"チャンク一覧:\n{context_str}"
    )

    try:
        result = call_local_llm(
            prompt=prompt,
            model=_OPENAI_MODEL,
            base_url=_OPENAI_BASE,
            api_key=_OPENAI_KEY,
            max_tokens=30,
            no_stream=True,
            timeout=60,  # 30 → 60秒
        )
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
            return [chunks[i] for i in indices[:TOP_K_RERANK]]

        logger.warning("re-rank: 有効な番号が得られず先頭件数で代替")
    except Exception as e:
        logger.warning(f"re-rankエラー: {e}. 日付降順フォールバックを使用")
        # フォールバック: 日付降順でソート
        return sorted(chunks, key=lambda x: x.get("held_at", ""), reverse=True)[:TOP_K_RERANK]

    return chunks[:TOP_K_RERANK]


# --- プロンプト構築 ---

_SOURCE_TYPE_LABEL = {
    "minutes_content": "議事録本文",
    "slack_raw": "Slackメッセージ",
    "document": "資料",
    "box_document": "Box資料",
    "web": "Web記事",
}

_CHANNEL_NAMES: dict[str, str] = {
    "<CHANNEL_ID>": "20_アプリケーション開発エリア",
    "<CHANNEL_ID>": "20_1_リーダ会議メンバ",
    "<CHANNEL_ID>": "21_hpcアプリケーションwg",
    "<CHANNEL_ID>": "21_1_hpcアプリケーションwg_ブロック1",
    "<CHANNEL_ID>": "21_2_hpcアプリケーションwg_ブロック2",
    "<CHANNEL_ID>": "22_ベンチマークwg",
    "<CHANNEL_ID>": "23_benchmark_framework",
    "<CHANNEL_ID>": "24_ai-hpc-application",
    "<CHANNEL_ID>": "pmo",
    "<CHANNEL_ID>": "personal",
}


def _format_source_label(chunk: dict) -> str:
    label = _SOURCE_TYPE_LABEL.get(chunk["source_type"], chunk["source_type"])
    source_type = chunk["source_type"]
    if source_type == "web":
        from urllib.parse import urlparse
        ref = chunk.get("source_ref") or ""
        domain = urlparse(ref).netloc.replace("www.", "") if ref else "web"
        held_at = chunk["held_at"] or "日付不明"
        return f"{domain} / {label} ({held_at})"
    if source_type == "box_document":
        # content 先頭の【folder/filename】からタイトルを抽出
        content = chunk.get("content") or ""
        title = ""
        if content.startswith("【"):
            end = content.find("】")
            if end > 0:
                title = content[1:end]
        held_at = chunk.get("held_at") or "日付不明"
        return f"{title or 'Box資料'} ({held_at})"
    db_name = chunk["source_db"].replace("minutes/", "").replace(".db", "")
    # Slack チャンネルIDを人名称に変換
    if source_type == "slack_raw":
        db_name = _CHANNEL_NAMES.get(db_name, db_name)
    held_at = chunk["held_at"] or "日付不明"
    return f"{db_name} / {label} ({held_at})"


def format_context(chunks: list[dict]) -> str:
    lines = []
    for i, chunk in enumerate(chunks, 1):
        label = _format_source_label(chunk)
        ref = chunk.get("source_ref") or ""
        source_type = chunk.get("source_type", "")

        if source_type == "slack_raw" and ref:
            ref_str = f" | <{ref}|スレッドを開く>"
        elif source_type == "minutes_content" and ref:
            held_at = chunk.get("held_at") or ""
            ref_str = f" | {held_at} {ref}" if held_at else f" | {ref}"
        elif source_type == "web" and ref:
            ref_str = f" | <{ref}|リンク>"
        elif source_type == "box_document" and ref:
            ref_str = f" | <{ref}|Boxで開く>"
        else:
            ref_str = f" | {ref}" if ref else ""

        lines.append(f"[{i}] 出典: {label}{ref_str}")
        lines.append(f"    {chunk['content'].strip()}")
        lines.append("")
    return "\n".join(lines)


# --- Hybrid検索: Intent分類 + 構造化クエリ ---

_CLASSIFY_PROMPT = """\
あなたはクエリ分類器です。質問がどの種類のデータを必要としているか判定してください。

カテゴリ:
- "structured": 担当者・期限・ステータス・マイルストーン・件数など構造化データで回答可能
- "text": 議事録内容・議論の詳細・経緯などテキスト検索が必要
- "hybrid": 両方が必要

構造化データの例:
- 「西澤さんの担当タスクは？」→ structured, query_type=tasks, assignee=西澤
- 「期限超過しているアクションアイテムは？」→ structured, query_type=overdue
- 「M1マイルストーンの進捗は？」→ structured, query_type=milestones, milestone=M1
- 「決定事項の一覧」→ structured, query_type=decisions
- 「オープンなタスクは何件？」→ structured, query_type=stats

テキスト検索の例:
- 「GPU性能の評価方針について」→ text
- 「前回の会議で何が議論された？」→ text

両方の例:
- 「GPU性能に関する決定事項は？」→ hybrid, query_type=decisions, keyword=GPU
- 「西澤さんが議論していた内容は？」→ hybrid, query_type=tasks, assignee=西澤

JSONのみ出力:
{"intent": "structured"|"text"|"hybrid", "entities": {"assignee": null, "milestone": null, "status": null, "keyword": null, "query_type": null}}

質問: """

def classify_intent(question: str) -> dict:
    """質問の意図を分類し、エンティティを抽出する。失敗時はFTSフォールバック。"""
    if not _OPENAI_BASE:
        return {"intent": "text", "entities": {}}

    prompt = _CLASSIFY_PROMPT + question
    try:
        result = call_local_llm(
            prompt=prompt,
            model=_OPENAI_MODEL,
            base_url=_OPENAI_BASE,
            api_key=_OPENAI_KEY,
            max_tokens=80,
            no_stream=True,
            timeout=15,
            temperature=0.1,
        )
        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r"^```\w*\n?", "", result)
            result = re.sub(r"\n?```$", "", result)
        import json
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "intent" in parsed:
            parsed.setdefault("entities", {})
            return parsed
    except Exception as e:
        logger.warning(f"Intent分類失敗: {e} → FTSフォールバック")

    return {"intent": "text", "entities": {}}


def _query_action_items(conn, *, assignee=None, status=None, milestone=None, keyword=None, limit=20) -> list[dict]:
    clauses = ["COALESCE(deleted,0)=0"]
    params: list = []
    if assignee:
        clauses.append("assignee LIKE ?")
        params.append(f"%{assignee}%")
    if status:
        clauses.append("status = ?")
        params.append(status)
    if milestone:
        clauses.append("milestone_id = ?")
        params.append(milestone)
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT id, content, assignee, due_date, status, milestone_id, source_ref,
                   requested_by, rationale, source_context, related_ids
            FROM action_items WHERE {where}
            ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, due_date ASC NULLS LAST
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _query_decisions(conn, *, keyword=None, limit=20) -> list[dict]:
    clauses = ["COALESCE(deleted,0)=0"]
    params: list = []
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT id, content, decided_at, source, source_ref,
                   decided_by, rationale, source_context, related_ids
            FROM decisions WHERE {where}
            ORDER BY decided_at DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def run_structured_query(entities: dict, pm_db_paths: list[Path] | None = None) -> str:
    """entities に基づき pm.db を構造化クエリし、整形済みテキストを返す。"""
    query_type = entities.get("query_type") or "tasks"
    assignee = entities.get("assignee")
    milestone = entities.get("milestone")
    status = entities.get("status")
    keyword = entities.get("keyword")

    if not pm_db_paths:
        pm_db_paths = _pm_db_map.get(_default_index, [_REPO_ROOT / "data" / "pm.db"])

    conns: list = []
    for p in pm_db_paths:
        try:
            conns.append(open_pm_db(p, no_encrypt=False))
        except Exception as e:
            logger.warning(f"pm.db接続エラー ({p}): {e}")
    if not conns:
        return ""

    try:
        from datetime import date
        today = date.today().isoformat()

        if query_type == "tasks":
            rows: list[dict] = []
            for c in conns:
                rows.extend(_query_action_items(c, assignee=assignee, status=status, milestone=milestone, keyword=keyword))
            if not rows:
                return ""
            lines = [f"【アクションアイテム検索結果: {len(rows)}件】"]
            for r in rows:
                a = r.get("assignee") or "未定"
                d = r.get("due_date") or "期限なし"
                ms = r.get("milestone_id") or "-"
                lines.append(f"- [ID:{r['id']}] {r['content'][:100]} (担当:{a}, 期限:{d}, 状態:{r['status']}, MS:{ms})")
            return "\n".join(lines)

        elif query_type == "decisions":
            rows = []
            for c in conns:
                rows.extend(_query_decisions(c, keyword=keyword))
            if not rows:
                return ""
            lines = [f"【決定事項検索結果: {len(rows)}件】"]
            for r in rows:
                lines.append(f"- [D:{r['id']}][{r.get('decided_at') or '日付不明'}] {r['content'][:120]}")
            return "\n".join(lines)

        elif query_type == "milestones":
            milestones: list = []
            for c in conns:
                milestones.extend(fetch_milestone_progress(c))
            if milestone:
                milestones = [m for m in milestones if m.get("milestone_id") == milestone]
            if not milestones:
                return ""
            lines = ["【マイルストーン進捗】"]
            for m in milestones:
                total = m.get("total", 0)
                closed = m.get("closed", 0)
                pct = f"{closed}/{total}" if total > 0 else "0/0"
                lines.append(f"- {m['milestone_id']}: {m['name']} (期限:{m.get('due_date') or '-'}, 完了:{pct})")
            return "\n".join(lines)

        elif query_type == "overdue":
            items: list = []
            for c in conns:
                items.extend(fetch_overdue_items(c, today, since=None))
            if assignee:
                items = [i for i in items if assignee in (i.get("assignee") or "")]
            if not items:
                return ""
            lines = [f"【期限超過アイテム: {len(items)}件】"]
            for r in items[:20]:
                a = r.get("assignee") or "未定"
                lines.append(f"- [ID:{r['id']}] {r['content'][:80]} (担当:{a}, 期限:{r['due_date']})")
            return "\n".join(lines)

        elif query_type == "stats":
            from pm_argus import merge_pm_stats, fetch_pm_stats
            stats_list = [fetch_pm_stats(c, today) for c in conns]
            merged = merge_pm_stats(stats_list)
            s = merged["stats"]
            lines = ["【統計情報】"]
            lines.append(f"- オープンAI: {s.get('total_open', 0)}件")
            lines.append(f"- 完了AI: {s.get('total_closed', 0)}件")
            lines.append(f"- 期限超過: {s.get('overdue_count', 0)}件")
            return "\n".join(lines)

    except Exception as e:
        logger.warning(f"構造化クエリエラー: {e}")
        return ""
    finally:
        for c in conns:
            c.close()

    return ""


def generate_answer(question: str, chunks: list[dict], *, structured_context: str = "") -> str:
    if not _OPENAI_BASE:
        return ":warning: OPENAI_API_BASE が設定されていません。`bash scripts/pm_daemon.sh start qa` 経由で起動してください。"

    parts = []
    if structured_context:
        parts.append(f"## 構造化データ検索結果\n\n{structured_context}")
    if chunks:
        parts.append(f"## テキスト検索結果\n\n{format_context(chunks)}")
    if not parts:
        return "記録が見つかりません。"
    context_str = "\n\n---\n\n".join(parts)
    system = SYSTEM_PROMPT_TEMPLATE.format(project_context=_PROJECT_CONTEXT)
    user_prompt = f"## 取得した関連情報\n\n{context_str}\n---\n\n## 質問\n\n{question}"

    try:
        answer = call_local_llm(
            prompt=user_prompt,
            model=_OPENAI_MODEL,
            base_url=_OPENAI_BASE,
            api_key=_OPENAI_KEY,
            max_tokens=MAX_TOKENS,
            no_stream=True,
            system=system,
            timeout=LLM_TIMEOUT,
        )
        return answer.strip()
    except Exception as e:
        logger.exception("LLM呼び出しエラー")
        return f":warning: LLMエラー: {e}"


def format_slack_response(question: str, chunks: list[dict], answer: str,
                          index_name: str, *, search_mode: str = "テキスト検索") -> str:
    header = f"*Q: {question}*\n\n"
    body = answer
    body += f"\n\n_（検索対象: {index_name} / {search_mode}）_"
    return header + body


# --- Slack Bolt ハンドラ ---

def _run_qa(question: str, respond, index_name: str, index_db: Path,
            pm_db_paths: list[Path] | None = None) -> None:
    try:
        logger.info(f"QA開始: [{index_name}] {question[:60]}")

        # Step 1: Intent分類
        intent_result = classify_intent(question)
        intent = intent_result.get("intent", "text")
        entities = intent_result.get("entities", {})
        logger.info(f"  Intent: {intent}, entities: {entities}")

        structured_context = ""
        chunks: list[dict] = []
        search_mode = "テキスト検索"

        # Step 2: Intent に基づく検索
        if intent in ("structured", "hybrid"):
            structured_context = run_structured_query(entities, pm_db_paths=pm_db_paths)
            if structured_context:
                logger.info(f"  構造化クエリ: {len(structured_context)} 文字")
            else:
                logger.info("  構造化クエリ: 結果なし")

        if intent in ("text", "hybrid") or (intent == "structured" and not structured_context):
            chunks = retrieve_chunks_hyde(question, index_db, index_name=index_name)
            logger.info(f"  {len(chunks)} チャンク取得（HyDE拡張後）")
            chunks = rerank_chunks(question, chunks)
            logger.info(f"  re-rank後: {len(chunks)} チャンク")

        # 検索モード判定
        if structured_context and chunks:
            search_mode = "ハイブリッド検索"
        elif structured_context:
            search_mode = "構造化検索"

        # Step 3: 回答生成
        answer = generate_answer(question, chunks, structured_context=structured_context)
        response_text = format_slack_response(
            question, chunks, answer, index_name, search_mode=search_mode,
        )
        respond(text=response_text, response_type="ephemeral", replace_original=True)
        logger.info(f"QA完了 ({search_mode})")
    except Exception as e:
        logger.exception("QA処理エラー")
        respond(
            text=f":warning: エラーが発生しました: {e}",
            response_type="ephemeral",
            replace_original=True,
        )


def build_app():
    from slack_bolt import App

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.error("SLACK_BOT_TOKEN が設定されていません")
        sys.exit(1)

    app = App(token=bot_token)
    executor = ThreadPoolExecutor(max_workers=4)

    @app.command("/argus-ask")
    def handle_ask(ack, respond, command):
        """互換性のため残存。内部は argus-investigate に転送する。"""
        ack()
        question = (command.get("text") or "").strip()
        if not question:
            respond(
                text="質問を入力してください。例: `/argus-ask 設計方針について`",
                response_type="ephemeral",
            )
            return
        respond(text=f":mag: `{question[:80]}`", response_type="ephemeral")
        executor.submit(_run_investigate, respond, command)

    # --- Argus コマンドハンドラ ---

    @app.command("/argus-brief")
    def handle_argus_brief(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus 分析中...", response_type="ephemeral")
        executor.submit(_run_brief, respond, command)

    @app.command("/argus-today")
    def handle_argus_today(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus 今日の活動を分析中...", response_type="ephemeral")
        executor.submit(_run_today_only, respond, command)

    @app.command("/argus-draft")
    def handle_argus_draft(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()
        if not text:
            respond(
                text=(
                    "用途と件名を指定してください。\n"
                    "例: `/argus-draft agenda 次回リーダー会議`\n"
                    "用途: `agenda`(会議アジェンダ), `report`(進捗報告), `request`(確認依頼)"
                ),
                response_type="ephemeral",
            )
            return
        respond(text=":hourglass_flowing_sand: Argus 草案生成中...", response_type="ephemeral")
        executor.submit(_run_draft, respond, command)

    @app.command("/argus-risk")
    def handle_argus_risk(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus リスク分析中...", response_type="ephemeral")
        executor.submit(_run_risk, respond, command)

    @app.command("/argus-investigate")
    def handle_argus_investigate(ack, respond, command):
        ack()
        question = (command.get("text") or "").strip()
        if not question:
            respond(
                text=(
                    "調査内容を入力してください。\n"
                    "例: `/argus-investigate M3の遅延原因を調査して`\n"
                    "例: `/argus-investigate 先週の決定事項が実行されているか確認`"
                ),
                response_type="ephemeral",
            )
            return
        respond(text=f":mag: `{question[:80]}`", response_type="ephemeral")
        executor.submit(_run_investigate, respond, command)

    # --- Patrol Agent Block Kit ボタンハンドラ ---

    _patrol_state = PatrolState(_REPO_ROOT / "data" / "patrol_state.db")

    @app.action("patrol_approve_close")
    def on_approve_close(ack, body, client):
        ack()
        pm_paths = _pm_db_map.get(_default_index, [_REPO_ROOT / "data" / "pm.db"])
        action = (body.get("actions") or [{}])[0]
        pending_id = int(action.get("value", 0))
        pending = _patrol_state.get_pending(pending_id)
        ai_id = pending["target_id"] if pending else None

        conns = [open_pm_db(p) for p in pm_paths]
        target_conn = conns[0]
        if ai_id is not None:
            for c in conns:
                if c.execute("SELECT id FROM action_items WHERE id=?", (ai_id,)).fetchone():
                    target_conn = c
                    break
        try:
            handle_approve_close(body, client, _patrol_state, target_conn)
            target_conn.commit()
        finally:
            for c in conns:
                c.close()

    @app.action("patrol_reject_close")
    def on_reject_close(ack, body, client):
        ack()
        handle_reject_close(body, client, _patrol_state)

    def _handle_transcribe_command(ack, respond, command, example_cmd):
        """共通: 文字起こしコマンドの受付・排他制御・バックグラウンド実行。"""
        ack()
        filename = (command.get("text") or "").strip()
        if not filename:
            respond(
                text=(
                    "ファイル名を指定してください。\n"
                    f"例: `{example_cmd} GMT20260302-032528_Recording.mp4`"
                ),
                response_type="ephemeral",
            )
            return
        with _transcribe_lock:
            if _transcribe_jobs:
                running = ", ".join(
                    f"`{fname}` (ch={chid})"
                    for _, (fname, chid) in _transcribe_jobs.items()
                )
                respond(
                    text=f":warning: 現在処理中のジョブがあります。完了後に再実行してください。\n処理中: {running}",
                    response_type="ephemeral",
                )
                return
        respond(
            text=f":hourglass_flowing_sand: `{filename}` の文字起こし・議事録生成を開始します...",
            response_type="ephemeral",
        )
        executor.submit(_run_transcribe, respond, command)

    @app.command("/argus-transcribe")
    def handle_argus_transcribe(ack, respond, command):
        _handle_transcribe_command(ack, respond, command, "/argus-transcribe")

    @app.command("/transcribe")
    def handle_transcribe(ack, respond, command):
        _handle_transcribe_command(ack, respond, command, "/transcribe")

    @app.command("/argus-delete")
    def handle_argus_delete(ack, client, command):
        handle_delete(ack, client, command)

    @app.command("/delete")
    def handle_delete(ack, client, command):
        filename = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")

        if not filename:
            ack("使い方: `/delete <ファイル名>`")
            return

        # Bold書式のアスタリスクを除去（例: *foo.md* → foo.md）
        filename = filename.strip("*")
        # 拡張子がなければ .md を付加
        if "." not in filename:
            filename += ".md"

        ack(f"`{filename}` を検索して削除します...")

        files = []
        cursor = None
        for _ in range(20):
            kwargs = {"channel": channel_id, "types": "all", "count": 200}
            if cursor:
                kwargs["cursor"] = cursor
            response = client.files_list(**kwargs)
            files.extend(response.get("files", []))
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break

        target = filename.lower()
        matched = [f for f in files if (f.get("name") or "").lower() == target]
        if not matched:
            matched = [f for f in files if (f.get("title") or "").lower() == target]
        if not matched:
            matched = [f for f in files if target in (f.get("name") or "").lower()]

        if not matched:
            client.chat_postMessage(
                channel=channel_id,
                text=f"`{filename}` がこのチャンネルに見つかりませんでした。",
            )
            return

        file_id = matched[0]["id"]
        try:
            client.files_delete(file=file_id)
            client.chat_postMessage(
                channel=channel_id,
                text=f"`{filename}` を削除しました。",
            )
        except Exception as e:
            logger.error("ファイル削除に失敗: %s", e)
            client.chat_postMessage(
                channel=channel_id,
                text=f"`{filename}` の削除に失敗しました: {e}",
            )

    # --- app_mention ハンドラ（常駐AIとしての @mention 応答） ---
    # 当面は <CHANNEL_ID>（personal/debug）のみで有効。動作確認後に拡大する。
    _MENTION_ALLOWED_CHANNELS = {"<CHANNEL_ID>"}

    @app.event("app_mention")
    def handle_app_mention(event, client):
        channel_id = event.get("channel", "")
        if channel_id not in _MENTION_ALLOWED_CHANNELS:
            logger.info(f"[mention] 許可外チャンネル {channel_id} からのメンションを無視")
            return

        raw_text = event.get("text", "") or ""
        # <@Uxxxx> 形式のメンションを全て除去
        question = re.sub(r"<@[UW][A-Z0-9]+>", "", raw_text).strip()
        if not question:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        user_id = event.get("user", "")
        ts_for_reply = thread_ts

        # 受付通知（スレッドに公開）
        try:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=ts_for_reply,
                text=f":mag: 調査中... `{question[:80]}`",
            )
        except Exception as e:
            logger.warning(f"[mention] 受付通知失敗: {e}")

        executor.submit(
            _run_mention_investigate,
            client, channel_id, ts_for_reply, question, user_id, event,
        )

    def _run_mention_investigate(client, channel_id, thread_ts, question, user_id, event):
        """app_mention からの調査実行。スレッドに公開返信する。

        2026-05-14: ローカル gemma4 reasoning モードに統一したため
        prefer_rivault() のラップは撤去（旧 Kimi 切替時の名残）。
        """
        _run_mention_investigate_impl(client, channel_id, thread_ts, question, user_id, event)

    def _run_mention_investigate_impl(client, channel_id, thread_ts, question, user_id, event):
        try:
            from datetime import date, timedelta
            from argus.pm_argus_agent import (
                _resolve_index_and_channels, _expand_id_references,
                AgentContext, build_seed_data, run_agent,
                _DEFAULT_SINCE_DAYS, _DATA_DIR, _MINUTES_DIR,
            )
            from pm_argus import _to_slack_mrkdwn

            today = date.today().isoformat()
            since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()
            index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels(channel_id)
            conns = [open_pm_db(p) for p in pm_db_paths]

            # スレッド文脈の取り込み（メンションがスレッド内にある場合）
            thread_context = ""
            is_threaded_followup = False
            parent_ts = event.get("thread_ts")
            current_ts = event.get("ts")
            if parent_ts and parent_ts != current_ts:
                is_threaded_followup = True
                try:
                    resp = client.conversations_replies(
                        channel=channel_id, ts=parent_ts, limit=50,
                    )
                    msgs = resp.get("messages", []) or []
                    # 現在の質問メッセージ自体は除外（文脈は「過去のやり取り」）
                    past_msgs = [m for m in msgs if m.get("ts") != current_ts]

                    # ユーザーID → display_name キャッシュ
                    name_cache: dict[str, str] = {}

                    def _resolve_name(uid: str) -> str:
                        if not uid:
                            return "?"
                        if uid in name_cache:
                            return name_cache[uid]
                        try:
                            u = client.users_info(user=uid)
                            prof = (u.get("user") or {}).get("profile") or {}
                            nm = prof.get("real_name") or prof.get("display_name") or uid
                        except Exception:
                            nm = uid
                        name_cache[uid] = nm
                        return nm

                    lines = []
                    for m in past_msgs:
                        uid = m.get("user") or m.get("bot_id") or ""
                        is_bot = bool(m.get("bot_id")) or (m.get("subtype") == "bot_message")
                        speaker = "Argus" if is_bot else _resolve_name(uid)
                        # Bot が Block Kit で投稿した場合 text が空で blocks 側に本文があることがある
                        text_body = m.get("text") or ""
                        if not text_body and m.get("blocks"):
                            texts = []
                            for b in m["blocks"]:
                                t = (b.get("text") or {}).get("text") or ""
                                if t:
                                    texts.append(t)
                            text_body = "\n".join(texts)
                        text_body = text_body.replace("\n", " ")[:800]
                        lines.append(f"- **{speaker}**: {text_body}")
                    if lines:
                        thread_context = (
                            "\n\n## スレッド内の過去のやり取り（時系列、直近のメンションの手前まで）\n"
                            + "\n".join(lines)
                            + "\n\n上記はこのスレッドでの過去の会話。"
                            "直近の質問は上記を前提とした**深掘り・追質問**である可能性が高い。"
                            "まず上記のやり取りから直接答えられないかを検討し、答えられる場合はツールを呼ばずに回答する。"
                            "必要に応じて補足情報をツールで取得する。"
                        )
                except Exception as e:
                    logger.warning(f"[mention] スレッド取得失敗: {e}")

            ctx = AgentContext(
                conns=conns, today=today, since=since_date,
                no_encrypt=False, data_dir=_DATA_DIR, minutes_dir=_MINUTES_DIR,
                index_db=index_db, index_name=index_name, channels=channels,
            )

            # 実行者情報をシードに注入（search_mentions ツールに使わせる）
            user_info = ""
            if user_id:
                display_name = ""
                try:
                    u = client.users_info(user=user_id)
                    prof = (u.get("user") or {}).get("profile") or {}
                    display_name = prof.get("real_name") or prof.get("display_name") or ""
                except Exception as e:
                    logger.warning(f"[mention] users_info 失敗: {e}")
                # 姓のみ（スペース前の部分）も抽出。日本語名指し検索の補助
                name_first_token = display_name.split()[0] if display_name else ""
                user_info = (
                    f"\n\n## 実行者情報\n"
                    f"- user_id: {user_id}\n"
                    f"- 名前（display_name）: {display_name}\n"
                    f"- 姓/first token: {name_first_token}\n"
                    f"「自分」「私」「あなた宛」など一人称の参照はこのユーザーを指す。\n"
                    f"\n"
                    f"## ツール選択のガイド（重要）\n"
                    f"- **質問が一人称/名指し参照を含まない場合は search_mentions を使わないこと**。\n"
                    f"  例: 「Xのリストは？」「Yの進捗は？」→ まず search_text で本文検索する。\n"
                    f"- search_mentions は「私宛のメンションは？」「〇〇さん宛の依頼は？」等、\n"
                    f"  メンション対象が明確な質問のみで使う。\n"
                    f"- search_mentions を使う場合は user_id={user_id} と name=「{display_name}」を同時指定する\n"
                    f"  （どちらか一方では取りこぼす）。"
                )
            seed_data = build_seed_data(ctx) + user_info + thread_context

            result = run_agent(
                question=question, seed_data=seed_data, respond=None, ctx=ctx,
            )
            result = _expand_id_references(result, conns)
            for c in conns:
                c.close()

            header = f"<@{user_id}> *Argus 調査結果* ({today})\n\n" if user_id else f"*Argus 調査結果* ({today})\n\n"
            body = _to_slack_mrkdwn(header + result)
            # Slack section block は 3000 文字、chat_postMessage text は 40000 文字上限。
            # 長い出力は段落単位でチャンク分割し、複数メッセージに分けて投稿する。
            _CHUNK = 2800

            def _split(text: str, size: int) -> list[str]:
                chunks: list[str] = []
                remaining = text
                while len(remaining) > size:
                    cut = remaining.rfind("\n---", 0, size)
                    if cut < size // 2:
                        cut = remaining.rfind("\n\n", 0, size)
                    if cut < size // 2:
                        cut = remaining.rfind("\n", 0, size)
                    if cut <= 0:
                        cut = size
                    chunks.append(remaining[:cut])
                    remaining = remaining[cut:].lstrip("\n")
                if remaining:
                    chunks.append(remaining)
                return chunks

            parts = _split(body, _CHUNK)
            for i, part in enumerate(parts):
                suffix = f"\n\n（{i+1}/{len(parts)}）" if len(parts) > 1 else ""
                text_part = part + suffix
                client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts, text=text_part,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text_part}}],
                )
            logger.info(f"[mention] 完了 channel={channel_id} thread={thread_ts}")

        except Exception as e:
            logger.exception(f"[mention] エラー: {e}")
            try:
                client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts,
                    text=f":warning: 調査中にエラーが発生しました: {e}",
                )
            except Exception:
                pass

    def _shutdown(signum, frame):
        logger.info("シャットダウン中...")
        executor.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    return app, executor


def _init_common() -> None:
    """main / test-hybrid 共通の初期化処理。"""
    global _PROJECT_CONTEXT

    if _init_sudachi():
        logger.info("SudachiPy: 初期化完了（形態素解析検索を使用します）")
    else:
        logger.warning("SudachiPy: 利用不可（trigram検索のみで動作します）")

    env_config = os.environ.get("ARGUS_CONFIG") or os.environ.get("QA_CONFIG")
    if env_config:
        config_path = _REPO_ROOT / env_config
    else:
        config_path = _REPO_ROOT / "data/argus_config.yaml"
        if not config_path.exists():
            config_path = _REPO_ROOT / "data/qa_config.yaml"
    if config_path.exists():
        load_qa_config(config_path)
    else:
        logger.warning(f"argus_config.yaml が見つかりません: {config_path}")

    # 統合 qa_index.db のチャンク数を index_name 別で表示
    qa_index = _REPO_ROOT / "data" / "qa_index.db"
    if qa_index.exists():
        try:
            ic = sqlite3.connect(str(qa_index))
            counts = dict(ic.execute(
                "SELECT index_name, COUNT(*) FROM chunk_indexes GROUP BY index_name"
            ).fetchall())
            ic.close()
        except Exception as e:
            logger.warning(f"  qa_index.db クエリ失敗: {e}")
            counts = {}
        for name in _index_db_map.keys():
            n = counts.get(name, 0)
            logger.info(f"  [{name}] qa_index.db: {n} チャンク")
    else:
        logger.warning(f"  data/qa_index.db: 未構築（pm_embed.py を実行してください）")

    try:
        _PROJECT_CONTEXT = load_claude_md_context()
        logger.info(f"CLAUDE.md 文脈ロード: {len(_PROJECT_CONTEXT)} 文字")
    except Exception as e:
        logger.warning(f"CLAUDE.md ロード失敗: {e}")
        _PROJECT_CONTEXT = ""


def test_hybrid(question: str) -> None:
    """CLIテストモード: Slackデーモン不要でハイブリッド検索をテストする。"""
    _init_common()

    if not _OPENAI_BASE:
        logger.warning("OPENAI_API_BASE が未設定です")

    index_name, index_db, _pm_dbs = resolve_index_db("")
    print(f"\n質問: {question}")
    print(f"インデックス: [{index_name}] {index_db}")
    print("-" * 60)

    # Intent分類
    intent_result = classify_intent(question)
    intent = intent_result.get("intent", "text")
    entities = intent_result.get("entities", {})
    print(f"Intent: {intent}")
    print(f"Entities: {entities}")
    print("-" * 60)

    structured_context = ""
    chunks: list[dict] = []

    if intent in ("structured", "hybrid"):
        structured_context = run_structured_query(entities)
        if structured_context:
            print(f"\n[構造化クエリ結果]\n{structured_context}")
        else:
            print("\n[構造化クエリ結果] なし")

    if intent in ("text", "hybrid") or (intent == "structured" and not structured_context):
        chunks = retrieve_chunks_hyde(question, index_db, index_name=index_name)
        print(f"\n[FTS検索] {len(chunks)} チャンク取得（HyDE拡張後）")
        chunks = rerank_chunks(question, chunks)
        print(f"[re-rank後] {len(chunks)} チャンク")
        for i, c in enumerate(chunks, 1):
            print(f"  [{i}] {_format_source_label(c)}: {c['content'][:80]}...")

    if structured_context and chunks:
        search_mode = "ハイブリッド検索"
    elif structured_context:
        search_mode = "構造化検索"
    else:
        search_mode = "テキスト検索"

    print(f"\n検索モード: {search_mode}")
    print("-" * 60)

    answer = generate_answer(question, chunks, structured_context=structured_context)
    print(f"\n[回答]\n{answer}")
    print(f"\n_（検索対象: {index_name} / {search_mode}）_")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Slack QA Server (Socket Mode)")
    parser.add_argument("--test-hybrid", metavar="QUESTION",
                        help="CLIテストモード: ハイブリッド検索をテスト（Slack不要）")
    args = parser.parse_args()

    if args.test_hybrid:
        test_hybrid(args.test_hybrid)
        return

    logger.info("pm_qa_server 起動中...")
    _init_common()

    if not _OPENAI_BASE:
        logger.warning("OPENAI_API_BASE が未設定です（QA実行時にエラーになります）")
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("SLACK_BOT_TOKEN が未設定です")
    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("SLACK_APP_TOKEN が未設定です")

    app, executor = build_app()

    from slack_bolt.adapter.socket_mode import SocketModeHandler
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        logger.error("SLACK_APP_TOKEN が未設定です")
        sys.exit(1)

    logger.info("Socket Mode で接続中... /argus-ask コマンドを待機します")
    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
