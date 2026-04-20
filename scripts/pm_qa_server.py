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
  OPENAI_MODEL      デフォルト: "gemma4"
  QA_CONFIG         デフォルト: data/qa_config.yaml（スクリプト基準）
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

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_local_llm, load_claude_md_context
from db_utils import open_pm_db, fetch_milestone_progress, fetch_overdue_items, fetch_summary_stats
from pm_argus import _run_brief, _run_draft, _run_risk, _run_transcribe, _transcribe_jobs, _transcribe_lock

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
RERANK_TIMEOUT = 30

_OPENAI_BASE = os.environ.get("OPENAI_API_BASE", "")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "dummy")
_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gemma4")

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
_default_index: str = "pm"


def load_qa_config(config_path: Path) -> None:
    """qa_config.yaml を読み込み、グローバルマップを初期化する。"""
    global _channel_index_map, _index_db_map, _default_index

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    _default_index = cfg.get("default_index", "pm")
    _channel_index_map = cfg.get("channel_map") or {}

    for name, index_cfg in (cfg.get("indices") or {}).items():
        db_path = _REPO_ROOT / index_cfg["db"]
        _index_db_map[name] = db_path

    logger.info(f"qa_config.yaml ロード: {len(_index_db_map)} インデックス, "
                f"{len(_channel_index_map)} チャンネルマッピング, "
                f"デフォルト={_default_index}")


def resolve_index_db(channel_id: str) -> tuple[str, Path]:
    """チャンネルIDからインデックス名とDBパスを返す。"""
    index_name = _channel_index_map.get(channel_id, _default_index)
    db_path = _index_db_map.get(index_name)
    if db_path is None:
        # デフォルトもなければ最初に見つかったDBを使う
        if _index_db_map:
            index_name, db_path = next(iter(_index_db_map.items()))
        else:
            return index_name, Path("data/qa_pm.db")
    return index_name, db_path


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


def _fts5_search(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT c.source_type, c.source_db, c.record_id, c.held_at,
                      c.content, c.source_ref, fts.rank
               FROM fts
               JOIN chunks c ON fts.rowid = c.id
               WHERE fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, k),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        logger.debug(f"FTS5クエリエラー: {e} (query={query!r})")
        return []


def _fts_tokens_search(conn: sqlite3.Connection, tokens: list[str], k: int) -> list[dict]:
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
            rows = conn.execute(
                """SELECT c.source_type, c.source_db, c.record_id, c.held_at,
                          c.content, c.source_ref, fts_tokens.rank
                   FROM fts_tokens
                   JOIN chunks c ON fts_tokens.rowid = c.id
                   WHERE fts_tokens MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, k),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug(f"fts_tokensクエリエラー: {e} (query={query!r})")
            return []
    return []


def retrieve_chunks(question: str, index_db: Path, k: int = TOP_K_RETRIEVE) -> list[dict]:
    """指定インデックスDBから関連チャンクを取得する。

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
        # --- Step 1: SudachiPy形態素解析 + fts_tokens 検索 ---
        sudachi_tokens = sudachi_tokenize_query(question)
        if sudachi_tokens:
            has_fts_tokens = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_tokens'"
            ).fetchone() is not None

            if has_fts_tokens:
                rows = _fts_tokens_search(conn, sudachi_tokens, k)
                if rows:
                    logger.info(
                        f"SudachiPy FTSマッチ ({len(rows)}件): {sudachi_tokens} in {index_db.name}"
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
            rows = _fts5_search(conn, q, k)
            if rows:
                logger.info(f"trigram FTSマッチ ({len(rows)}件): [{q}] in {index_db.name}")
                return rows

        # --- Step 3: LIKE 検索 ---
        keyword = (sudachi_tokens[0] if sudachi_tokens else
                   (valid_tokens[0] if valid_tokens else ""))
        if keyword:
            rows = conn.execute(
                """SELECT source_type, source_db, record_id, held_at, content, source_ref, 0 AS rank
                   FROM chunks WHERE content LIKE ? LIMIT ?""",
                (f"%{keyword}%", k),
            ).fetchall()
            if rows:
                logger.info(f"LIKE検索フォールバック ({len(rows)}件): [{keyword}]")
                return [dict(r) for r in rows]

        # --- Step 4: 最新記録フォールバック ---
        logger.info(f"マッチなし → 最新記録フォールバック (sudachi={sudachi_tokens})")
        rows = conn.execute(
            """SELECT source_type, source_db, record_id, held_at, content, source_ref, 0 AS rank
               FROM chunks WHERE held_at IS NOT NULL ORDER BY held_at DESC LIMIT ?""",
            (k,),
        ).fetchall()
        return [dict(r) for r in rows]

    finally:
        conn.close()


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
            timeout=RERANK_TIMEOUT,
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
        logger.warning(f"re-rankエラー: {e} → 先頭{TOP_K_RERANK}件で代替")

    return chunks[:TOP_K_RERANK]


# --- プロンプト構築 ---

_SOURCE_TYPE_LABEL = {
    "minutes_content": "議事録本文",
    "slack_raw": "Slackメッセージ",
}

_CHANNEL_NAMES: dict[str, str] = {
    "C08M0249GRL": "20_アプリケーション開発エリア",
    "C08SXA4M7JT": "20_1_リーダ会議メンバ",
    "C08LSJP4R6K": "21_hpcアプリケーションwg",
    "C093DQFSCRH": "21_1_hpcアプリケーションwg_ブロック1",
    "C093LP1J15G": "21_2_hpcアプリケーションwg_ブロック2",
    "C08MJ0NF5UZ": "22_ベンチマークwg",
    "C096ER1A0LU": "23_benchmark_framework",
    "C0A6AC59AHM": "24_ai-hpc-application",
    "C08PE3K9N72": "pmo",
    "C0A9KG036CS": "personal",
}


def _format_source_label(chunk: dict) -> str:
    label = _SOURCE_TYPE_LABEL.get(chunk["source_type"], chunk["source_type"])
    db_name = chunk["source_db"].replace("minutes/", "").replace(".db", "")
    # Slack チャンネルIDを人名称に変換
    if chunk["source_type"] == "slack_raw":
        db_name = _CHANNEL_NAMES.get(db_name, db_name)
    held_at = chunk["held_at"] or "日付不明"
    return f"{db_name} / {label} ({held_at})"


def format_context(chunks: list[dict]) -> str:
    lines = []
    for i, chunk in enumerate(chunks, 1):
        label = _format_source_label(chunk)
        ref = chunk.get("source_ref") or ""
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

_PM_DB_PATH = _REPO_ROOT / "data" / "pm.db"


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
        f"""SELECT id, content, assignee, due_date, status, milestone_id, source_ref
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
        f"""SELECT id, content, decided_at, source, source_ref
            FROM decisions WHERE {where}
            ORDER BY decided_at DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def run_structured_query(entities: dict) -> str:
    """entities に基づき pm.db を構造化クエリし、整形済みテキストを返す。"""
    query_type = entities.get("query_type") or "tasks"
    assignee = entities.get("assignee")
    milestone = entities.get("milestone")
    status = entities.get("status")
    keyword = entities.get("keyword")

    try:
        conn = open_pm_db(_PM_DB_PATH, no_encrypt=False)
    except Exception as e:
        logger.warning(f"pm.db接続エラー: {e}")
        return ""

    try:
        from datetime import date
        today = date.today().isoformat()

        if query_type == "tasks":
            rows = _query_action_items(conn, assignee=assignee, status=status, milestone=milestone, keyword=keyword)
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
            rows = _query_decisions(conn, keyword=keyword)
            if not rows:
                return ""
            lines = [f"【決定事項検索結果: {len(rows)}件】"]
            for r in rows:
                lines.append(f"- [D:{r['id']}][{r.get('decided_at') or '日付不明'}] {r['content'][:120]}")
            return "\n".join(lines)

        elif query_type == "milestones":
            milestones = fetch_milestone_progress(conn)
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
            items = fetch_overdue_items(conn, today, since=None)
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
            stats = fetch_summary_stats(conn, since=None, today=today)
            lines = ["【統計情報】"]
            lines.append(f"- オープンAI: {stats.get('total_open', 0)}件")
            lines.append(f"- 完了AI: {stats.get('total_closed', 0)}件")
            lines.append(f"- 期限超過: {stats.get('overdue_count', 0)}件")
            return "\n".join(lines)

    except Exception as e:
        logger.warning(f"構造化クエリエラー: {e}")
        return ""
    finally:
        conn.close()

    return ""


def generate_answer(question: str, chunks: list[dict], *, structured_context: str = "") -> str:
    if not _OPENAI_BASE:
        return ":warning: OPENAI_API_BASE が設定されていません。`pm_qa_start.sh` 経由で起動してください。"

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

def _run_qa(question: str, respond, index_name: str, index_db: Path) -> None:
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
            structured_context = run_structured_query(entities)
            if structured_context:
                logger.info(f"  構造化クエリ: {len(structured_context)} 文字")
            else:
                logger.info("  構造化クエリ: 結果なし")

        if intent in ("text", "hybrid") or (intent == "structured" and not structured_context):
            chunks = retrieve_chunks(question, index_db)
            logger.info(f"  {len(chunks)} チャンク取得")
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
        ack()
        question = (command.get("text") or "").strip()
        channel_id = command.get("channel_id", "")

        if not question:
            respond(
                text="質問を入力してください。例: `/argus-ask 設計方針について`",
                response_type="ephemeral",
            )
            return

        index_name, index_db = resolve_index_db(channel_id)
        logger.info(f"チャンネル {channel_id} → インデックス [{index_name}] ({index_db.name})")

        respond(
            text=f":hourglass_flowing_sand: 検索中... `{question[:50]}`",
            response_type="ephemeral",
        )
        executor.submit(_run_qa, question, respond, index_name, index_db)

    # --- Argus コマンドハンドラ ---

    @app.command("/argus-brief")
    def handle_argus_brief(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus 分析中...", response_type="ephemeral")
        executor.submit(_run_brief, respond, command)

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

    @app.command("/argus-transcribe")
    def handle_argus_transcribe(ack, respond, command):
        ack()
        filename = (command.get("text") or "").strip()
        if not filename:
            respond(
                text=(
                    "ファイル名を指定してください。\n"
                    "例: `/argus-transcribe GMT20260302-032528_Recording.mp4`"
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

    config_path = _REPO_ROOT / os.environ.get("QA_CONFIG", "data/qa_config.yaml")
    if config_path.exists():
        load_qa_config(config_path)
    else:
        logger.warning(f"qa_config.yaml が見つかりません: {config_path}")

    for name, db_path in _index_db_map.items():
        if db_path.exists():
            count = sqlite3.connect(str(db_path)).execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            logger.info(f"  [{name}] {db_path.name}: {count} チャンク")
        else:
            logger.warning(f"  [{name}] {db_path.name}: 未構築（pm_embed.py を実行してください）")

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

    index_name, index_db = resolve_index_db("")
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
        chunks = retrieve_chunks(question, index_db)
        print(f"\n[FTS検索] {len(chunks)} チャンク取得")
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
