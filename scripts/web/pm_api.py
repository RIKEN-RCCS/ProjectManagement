#!/usr/bin/env python3
"""
pm_api.py -- FastAPI REST backend for PM DB Editor

Usage:
    python3 scripts/pm_api.py
    python3 scripts/pm_api.py --port 8501 --db data/pm.db

フロントエンド (scripts/static/) を同一プロセスで配信する。
将来的にフロントエンドを別サーバへ移動する場合は CORS 設定を追加するだけで済む。
"""

import argparse
import io
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import open_db
from web_admin import (
    AdminJobQueue,
    delete_minutes_from_pm,
    delete_minutes_instance,
    get_all_services,
    get_dashboard_stats,
    get_minutes_action_items,
    get_minutes_content,
    get_minutes_decisions,
    get_minutes_held_at,
    get_recent_minutes,
    get_service_status,
    list_minutes,
    scan_recent_errors,
    service_action,
    tail_log,
    update_minutes_action_items,
    update_minutes_content,
    update_minutes_decisions,
)
from web_utils import (
    audit,
    do_save_achievements,
    do_save_action_items,
    do_save_decisions,
    get_conn,
    load_achievements,
    load_action_items,
    load_decisions,
    load_filter_presets,
    load_milestones,
    load_minutes_content,
    nv,
    scan_pm_dbs,
)

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB = _REPO / "data" / "pm.db"
_DEFAULT_PORT = 8501

# AdminJobQueue はモジュールロード時に初期化（pm_daemon.sh 経由の起動でも __main__ 経由でも動作）
_job_queue = AdminJobQueue(repo_root=_REPO)

# --------------------------------------------------------------------------- #
# アプリ状態（シングルプロセス前提）
# --------------------------------------------------------------------------- #
_state: dict[str, Any] = {
    "db_path": "",
    "no_encrypt": False,
    "ai_df": None,   # optimistic locking 用スナップショット
    "dec_df": None,
    "ach_df": None,
    "job_queue": _job_queue,
    "processing_dir": None,
}

def _get_conn(db_path=None):
    """_state からパラメータを取って get_conn を呼ぶ薄いラッパー。"""
    path = Path(db_path) if db_path else Path(_state["db_path"])
    return get_conn(path, no_encrypt=_state["no_encrypt"])


# --------------------------------------------------------------------------- #
# FastAPI アプリ
# --------------------------------------------------------------------------- #
app = FastAPI(title="PM DB Editor API")


# --- Request / Response models --- #

class SwitchDbRequest(BaseModel):
    path: str

class SaveRowsRequest(BaseModel):
    rows: list[dict]

class NewActionItemRequest(BaseModel):
    content: str
    assignee: str | None = None
    due_date: str | None = None
    milestone_id: str | None = None
    status: str = "open"
    note: str | None = None
    source: str | None = None

class NewDecisionRequest(BaseModel):
    content: str
    decided_at: str | None = None
    source: str | None = None

class NewAchievementRequest(BaseModel):
    app: str
    title: str
    category: str | None = None
    achieved_on: str | None = None
    evidence_ref: str | None = None
    evidence_quote: str | None = None

class NewGlossaryItemRequest(BaseModel):
    title: str
    content: str = ""
    category: str = ""


# --- DB endpoints --- #

@app.get("/api/databases")
def list_databases():
    dbs = scan_pm_dbs()
    if _state["db_path"] not in dbs:
        dbs.insert(0, _state["db_path"])
    return {
        "databases": [{"path": p, "name": Path(p).name} for p in dbs],
        "current": _state["db_path"],
    }


@app.post("/api/databases/switch")
def switch_database(req: SwitchDbRequest):
    p = Path(req.path)
    if not p.exists():
        return JSONResponse({"error": f"ファイルが見つかりません: {p}"}, status_code=400)
    try:
        _get_conn(p)  # validate DB can be opened
    except Exception as e:
        return JSONResponse({"error": f"DB接続エラー: {e}"}, status_code=400)
    _state["db_path"] = str(p)
    _state["ai_df"] = None
    _state["dec_df"] = None
    _state["ach_df"] = None
    return {"ok": True, "name": p.name}


# --- Milestone endpoints --- #

@app.get("/api/milestones")
def get_milestones():
    return {"milestones": load_milestones(_get_conn())}


# --- Filter presets endpoint --- #

@app.get("/api/filter-presets")
def get_filter_presets():
    """argus_config.yaml の filter_presets と channel_names を返す。"""
    return load_filter_presets()


# --- Action Item endpoints --- #

@app.get("/api/action-items")
def get_action_items(
    status: str = Query("open"),
    milestone: str = Query("すべて"),
    since: str = Query(""),
    deleted: str = Query("非削除"),
    channels: list[str] = Query(default_factory=list),
    meeting_kinds: list[str] = Query(default_factory=list),
):
    df = load_action_items(
        _get_conn(), status, milestone, since or None, deleted,
        channels=channels or None,
        meeting_kinds=meeting_kinds or None,
    )
    _state["ai_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/action-items/save")
def save_action_items(req: SaveRowsRequest):
    if _state["ai_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_action_items(_get_conn(), _state["ai_df"], req.rows)
    _state["ai_df"] = None
    # 非同期で Box XLSX を更新
    if n > 0:
        _enqueue_xlsx_publish()
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/action-items/new")
def create_action_item(req: NewActionItemRequest):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO action_items"
        " (content,assignee,due_date,milestone_id,status,note,source,source_ref,extracted_at)"
        " VALUES(?,?,?,?,?,?,'manual',?,?)",
        (req.content.strip(), nv(req.assignee), nv(req.due_date),
         nv(req.milestone_id), req.status, nv(req.note), nv(req.source),
         datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


# --- Terminology endpoints --- #

@app.get("/api/terminology")
def get_terminology():
    import json as _json
    conn = _get_conn()
    rows = conn.execute(
        "SELECT term, category, aliases, source, frequency, last_seen, meeting_kinds"
        " FROM terminology ORDER BY frequency DESC"
    ).fetchall()
    result = []
    for r in rows:
        aliases_raw = r["aliases"] or "[]"
        try:
            aliases_list = _json.loads(aliases_raw)
            aliases_str = ", ".join(aliases_list)
        except Exception:
            aliases_str = aliases_raw
        result.append({
            "term": r["term"],
            "category": r["category"],
            "aliases": aliases_str,
            "source": r["source"],
            "frequency": r["frequency"],
            "last_seen": r["last_seen"],
            "meeting_kinds": r["meeting_kinds"],
        })
    return {"rows": result}


@app.post("/api/terminology/save")
def save_terminology(req: SaveRowsRequest):
    import json as _json
    conn = _get_conn()
    now_ts = datetime.now(UTC).isoformat()
    n = 0
    for row in req.rows:
        term = row.get("term", "").strip()
        if not term:
            continue
        if row.get("deleted"):
            conn.execute("DELETE FROM terminology WHERE term = ?", (term,))
            n += 1
        else:
            aliases = row.get("aliases") or ""
            aliases_list = [a.strip() for a in aliases.split(",") if a.strip()]
            aliases_json = _json.dumps(aliases_list, ensure_ascii=False)
            category = row.get("category", "unknown") or "unknown"
            source = row.get("source", "manual") or "manual"
            existing = conn.execute(
                "SELECT frequency, meeting_kinds FROM terminology WHERE term = ?", (term,)
            ).fetchone()
            if existing:
                freq = (existing["frequency"] or 0) + 1
                conn.execute(
                    "UPDATE terminology SET category=?, aliases=?, source=?, frequency=?, last_seen=?"
                    " WHERE term=?",
                    (category, aliases_json, source, freq, now_ts, term),
                )
            else:
                conn.execute(
                    "INSERT INTO terminology (term, category, aliases, source, last_seen, frequency, meeting_kinds)"
                    " VALUES (?, ?, ?, ?, ?, 1, '[]')",
                    (term, category, aliases_json, source, now_ts),
                )
            n += 1
    conn.commit()
    return {"updated": n}


@app.post("/api/terminology/add")
def add_terminology(req: NewActionItemRequest):
    conn = _get_conn()
    now_ts = datetime.now(UTC).isoformat()
    term = req.content.strip()
    existing = conn.execute("SELECT 1 FROM terminology WHERE term = ?", (term,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO terminology (term, category, aliases, source, last_seen, frequency, meeting_kinds)"
            " VALUES (?, 'unknown', '[]', 'manual', ?, 1, '[]')",
            (term, now_ts),
        )
        conn.commit()
    return {"ok": True}


@app.post("/api/terminology/delete")
def delete_terminology(req: NewActionItemRequest):
    conn = _get_conn()
    conn.execute("DELETE FROM terminology WHERE term = ?", (req.content.strip(),))
    conn.commit()
    return {"ok": True}


# --- Glossary endpoints --- #

@app.get("/api/glossary")
def get_glossary(category: str = Query("")):
    from utils.glossary import load_all
    conn = _get_conn()
    items = load_all(category=category or None, conn=conn)
    return {"rows": items}


@app.post("/api/glossary/save")
def save_glossary(req: SaveRowsRequest):
    from utils.glossary import add, delete, update
    conn = _get_conn()
    n = 0
    for row in req.rows:
        gid = row.get("id")
        if row.get("deleted") and gid:
            delete(gid, conn=conn)
            n += 1
        elif gid:
            update(gid, row["title"], row["content"], row.get("category", ""), conn=conn)
            n += 1
        else:
            add(row["title"], row["content"], row.get("category", ""), conn=conn)
            n += 1
    return {"updated": n}


@app.post("/api/glossary/add")
def add_glossary(req: NewGlossaryItemRequest):
    from utils.glossary import add
    conn = _get_conn()
    gid = add(title=req.title.strip(), content=req.content, category=req.category, conn=conn)
    return {"ok": True, "id": gid}


@app.post("/api/glossary/delete")
def delete_glossary(req: NewGlossaryItemRequest):
    from utils.glossary import delete
    conn = _get_conn()
    try:
        delete(int(req.title.strip()), conn=conn)
    except ValueError:
        pass
    return {"ok": True}


# --- Decision endpoints --- #

@app.get("/api/decisions")
def get_decisions(
    acknowledged: str = Query("すべて"),
    since: str = Query(""),
    deleted: str = Query("非削除"),
    channels: list[str] = Query(default_factory=list),
    meeting_kinds: list[str] = Query(default_factory=list),
):
    df = load_decisions(
        _get_conn(), acknowledged, since or None, deleted,
        channels=channels or None,
        meeting_kinds=meeting_kinds or None,
    )
    _state["dec_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/decisions/save")
def save_decisions(req: SaveRowsRequest):
    if _state["dec_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_decisions(_get_conn(), _state["dec_df"], req.rows)
    _state["dec_df"] = None
    # 非同期で Box XLSX を更新
    if n > 0:
        _enqueue_xlsx_publish()
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/decisions/new")
def create_decision(req: NewDecisionRequest):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO decisions (content,decided_at,source,source_ref,extracted_at)"
        " VALUES(?,?,'manual',?,?)",
        (req.content.strip(), nv(req.decided_at),
         nv(req.source), datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.post("/api/decisions/ack-all")
def ack_all_decisions():
    conn = _get_conn()
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "UPDATE decisions SET acknowledged_at=? WHERE COALESCE(deleted,0)=0"
        " AND (acknowledged_at IS NULL OR acknowledged_at='')",
        (now,),
    )
    count = cur.rowcount
    conn.commit()
    return {"count": count}


# --- Achievement endpoints --- #

@app.get("/api/achievements")
def get_achievements(
    status: str = Query(""),
    app: str = Query(""),
    deleted: bool = Query(False),
):
    df = load_achievements(_get_conn(), status or None, app or None, deleted)
    _state["ach_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/achievements/save")
def save_achievements(req: SaveRowsRequest):
    if _state["ach_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_achievements(_get_conn(), _state["ach_df"], req.rows)
    _state["ach_df"] = None
    # 非同期で Box XLSX を更新
    if n > 0:
        _enqueue_xlsx_publish()
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/achievements/new")
def create_achievement(req: NewAchievementRequest):
    from ingest.achievements import _dedup_key

    conn = _get_conn()
    now_ts = datetime.now(UTC).isoformat()
    app_name = req.app.strip()
    title = req.title.strip()
    dedup_key = _dedup_key(app_name, title)
    cur = conn.execute(
        "INSERT INTO achievements"
        " (app,title,category,achieved_on,evidence_ref,evidence_quote,"
        "  confidence,status,source,dedup_key,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,'low','proposed','web_ui',?,?,?)",
        (app_name, title, nv(req.category), nv(req.achieved_on),
         nv(req.evidence_ref), nv(req.evidence_quote), dedup_key, now_ts, now_ts),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


# --- Minutes endpoint --- #

@app.get("/api/minutes")
def get_minutes(id: str = Query(""), kind: str = Query("")):
    content = load_minutes_content(id, no_encrypt=_state["no_encrypt"], kind=kind)
    return {"meeting_id": id, "kind": kind, "content": content}


# --- Files endpoint --- #

# channel_id → 表示名は argus_config.yaml の channel_names を参照する
# （実値はチャンネル機密のためソース内に持たない）。
_CHANNEL_NAMES: dict[str, str] = load_filter_presets().get("channel_names", {}) or {}

import re as _re

_PAT_BOX_URL = _re.compile(
    r"<(https?://(?:[\w.-]+\.)?box\.com/[^\s>|,\)\"']+)\|([^>]*)>"  # <URL|label> Slack format
    r"|<?(https?://(?:[\w.-]+\.)?box\.com/[^\s>|,\)\"']+)>?"        # plain URL or <URL>
)

def _is_url_like(s: str) -> bool:
    """文字列がURLやURLの断片のように見えるか"""
    return bool(_re.match(r"^<?https?://", s) or _re.match(r"^[\w.-]+\.[\w-]+/", s))


def _extract_label(text: str, url: str, url_start: int, url_end: int) -> str:
    """URLの前後からファイル名やタイトルを抽出する"""

    # パターン1: URLの直後（同一行内）にスペースで区切られたテキスト
    # <URL> 形式の場合は > の後が改行またはスペースのみであることが多いのでそこまで
    after_raw = text[url_end:url_end + 200]
    # 末尾の > と空白をスキップ（<URL> 形式の残り）
    after = after_raw.lstrip("> ")
    # 改行より前の同行テキストのみ対象
    line_after = after.split("\n")[0].strip().rstrip(",")
    if line_after and not _is_url_like(line_after):
        pb = _re.search(r"\s+Powered by Box", line_after, _re.IGNORECASE)
        if pb:
            line_after = line_after[:pb.start()].strip()
        # 先頭の区切り文字・ダッシュを除去
        line_after = line_after.lstrip("-– ").strip()
        if line_after and len(line_after) >= 2:
            return line_after

    # パターン2: URLを含む行全体で「- タイトル」パターン（URL - タイトル Powered by Box）
    line_start = text.rfind("\n", 0, url_start) + 1
    line_end_idx = text.find("\n", url_end)
    line_end_idx = line_end_idx if line_end_idx >= 0 else len(text)
    line_text = text[line_start:line_end_idx]
    m3 = _re.search(r"[-–]\s*([^-–\n<>]{3,80}?)\s*(?:Powered by Box|$)", line_text, _re.IGNORECASE)
    if m3:
        candidate = m3.group(1).strip().lstrip("-– ").rstrip(",")
        if candidate and not _is_url_like(candidate):
            return candidate

    # パターン3: URLの直前行に拡張子付きファイル名があれば使う
    before = text[max(0, url_start - 500):url_start]
    lines_before = [ln.strip() for ln in before.split("\n") if ln.strip()]
    for line in reversed(lines_before):
        if _is_url_like(line):
            continue
        m2 = _re.search(r"(\S+\.[a-zA-Z0-9]{2,5})(?:\s|$)", line)
        if m2 and not _is_url_like(m2.group(1)):
            return m2.group(1)
        break  # 最も近い非URL行のみチェック

    return ""


def _msg_context(text: str) -> str:
    """メッセージ本文の最初の意味のある行をコンテキストとして返す"""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # URLのみの行はスキップ
        if line.startswith("http") or line.startswith("<http"):
            continue
        # チャンネルメンション・ユーザーメンション除去
        line = _re.sub(r"<!(here|channel|everyone)>", "", line)
        line = _re.sub(r"<@[A-Z0-9]+>", "", line).strip()
        if len(line) >= 4:
            return line[:100]
    return ""


@app.get("/api/files")
def get_files(channel: str = Query(""), since: str = Query("")):
    """統合 Slack DB (data/slack.db) から Box リンク・ファイルURLを抽出して一覧を返す。"""
    data_dir = _REPO / "data"
    results: list[dict] = []
    seen_urls: set[str] = set()

    db_path = data_dir / "slack.db"
    if not db_path.exists():
        return {"files": [], "total": 0}

    try:
        conn = open_db(str(db_path), encrypt=not _state["no_encrypt"])
    except Exception:
        return {"files": [], "total": 0}

    for table, ts_col in [("messages", "thread_ts"), ("replies", "msg_ts")]:
        try:
            q = (f"SELECT channel_id, {ts_col}, text, COALESCE(permalink,''), timestamp"
                 f" FROM {table} WHERE text LIKE '%box.com%'")
            params: list = []
            if channel:
                q += " AND channel_id = ?"
                params.append(channel)
            if since:
                q += " AND timestamp >= ?"
                params.append(since)
            rows = conn.execute(q, params).fetchall()
        except Exception:
            continue
        for row in rows:
            ch_id = row[0]
            ch_name = _CHANNEL_NAMES.get(ch_id, ch_id)
            _ts, text, permalink, timestamp = row[1], row[2], row[3], row[4]
            if not text:
                continue
            context = _msg_context(text)
            date_str = str(timestamp)[:10] if timestamp else ""
            for m in _PAT_BOX_URL.finditer(text):
                if m.group(1):
                    url = m.group(1).rstrip(".")
                    inline_label = m.group(2).strip()
                    pb = _re.search(r"\s*\|?\s*Powered by Box", inline_label, _re.IGNORECASE)
                    if pb:
                        inline_label = inline_label[:pb.start()].strip()
                    label = inline_label
                else:
                    url = m.group(3).rstrip(".")
                    label = _extract_label(text, url, m.start(), m.end())
                key = f"{ch_id}:{url}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                results.append({
                    "url": url,
                    "label": label or "",
                    "context": context,
                    "channel_id": ch_id,
                    "channel_name": ch_name,
                    "date": date_str,
                    "permalink": permalink,
                })
    conn.close()

    # 日付降順でソート
    results.sort(key=lambda x: x["date"], reverse=True)
    return {"files": results, "total": len(results)}


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Admin endpoints (dashboard, jobs, services, operations)
# --------------------------------------------------------------------------- #

@app.get("/api/admin/stats")
def admin_stats():
    """ダッシュボード統計を返す。"""
    try:
        conn = _get_conn()
    except Exception:
        return {"error": "Database not available", "open_action_items": 0,
                "unacknowledged_decisions": 0, "active_milestones": 0,
                "overdue_items": 0, "total_action_items": 0, "total_decisions": 0}
    try:
        stats = get_dashboard_stats(conn)
    except Exception:
        stats = {"error": "Failed to load stats", "open_action_items": 0,
                 "unacknowledged_decisions": 0, "active_milestones": 0,
                 "overdue_items": 0, "total_action_items": 0, "total_decisions": 0}
    return stats


@app.get("/api/admin/services")
async def admin_services():
    """全サービスの状態を返す。"""
    services = await get_all_services()
    return {"services": services}


@app.get("/api/admin/services/{name}/status")
async def admin_service_status(name: str):
    """特定サービスの状態を返す。"""
    result = await get_service_status(name)
    return result


@app.post("/api/admin/services/{name}/start")
async def admin_service_start(name: str):
    """サービスを起動。"""
    result = await service_action(name, "start")
    return result


@app.post("/api/admin/services/{name}/stop")
async def admin_service_stop(name: str):
    """サービスを停止。"""
    result = await service_action(name, "stop")
    return result


@app.get("/api/admin/services/{name}/logs")
def admin_service_logs(name: str, lines: int = Query(100, ge=10, le=500)):
    """サービスのログファイル末尾を返す。"""
    log_files = {
        "qa": _REPO / "logs" / "pm_qa_server.log",
        "web": _REPO / "logs" / "pm_web.log",
        "fish": _REPO / "logs" / "pm_fish_tts.log",
    }
    log_path = log_files.get(name)
    if not log_path:
        return {"lines": [], "total_lines": 0, "error": f"Unknown service: {name}"}
    return tail_log(log_path, lines)


@app.get("/api/admin/logs/recent-errors")
def admin_recent_errors():
    """直近のログファイルから ERROR/WARNING 行をスキャンする。"""
    errors = scan_recent_errors()
    return {"errors": errors}


# --- Job queue endpoints --- #

@app.get("/api/admin/jobs")
def admin_list_jobs(kind: str = Query(""), limit: int = Query(50, le=200)):
    """ジョブ一覧を取得。"""
    queue = _state.get("job_queue")
    if not queue:
        return {"jobs": []}
    jobs = queue.list_jobs(kind=kind or None, limit=limit)
    return {"jobs": jobs}


@app.get("/api/admin/jobs/{job_id}")
def admin_get_job(job_id: str):
    """単一ジョブの状態を取得。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    job = queue.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return job


@app.get("/api/admin/jobs/{job_id}/log")
def admin_get_job_log(job_id: str, lines: int = 200):
    """ジョブのログファイル内容を取得する。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    job = queue.get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    log_path = job.get("log_file") if isinstance(job, dict) else job.get("log_file")
    if not log_path:
        return JSONResponse({"error": "No log file for this job"}, status_code=404)
    p = Path(log_path)
    if not p.exists():
        return JSONResponse({"error": "Log file not found"}, status_code=404)
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"file": str(p), "total_lines": len(all_lines), "lines": tail}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Ingest endpoints --- #

class IngestRunRequest(BaseModel):
    source: str  # "slack", "minutes", "goals"
    slack_channel: str | None = None
    since: str | None = None
    dry_run: bool = False
    no_auto_enrich: bool = False


@app.post("/api/admin/ingest/run")
def admin_ingest_run(req: IngestRunRequest):
    """Ingest ジョブを開始。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    params = req.model_dump()
    job_id = queue.enqueue("ingest", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/admin/ingest/sources")
def admin_ingest_sources():
    """利用可能な Ingest ソース一覧（チャンネル情報を含む）。"""
    try:
        presets = load_filter_presets()
        return {
            "sources": ["slack", "minutes", "goals"],
            "channels": presets.get("channels", []),
            "channel_names": presets.get("channel_names", {}),
            "meeting_kinds": presets.get("meeting_kinds", []),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Knowledge endpoints --- #

class EmbedRequest(BaseModel):
    index_name: str | None = None
    full_rebuild: bool = False
    dry_run: bool = False


@app.post("/api/admin/knowledge/embed")
def admin_knowledge_embed(req: EmbedRequest):
    """Embed (FTS5 インデックス構築) ジョブを開始。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    params = req.model_dump()
    job_id = queue.enqueue("embed", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued"}


# --- Report endpoints --- #

class ReportGenerateRequest(BaseModel):
    report_type: str  # "report", "insight", "xlsx_report"
    since: str | None = None
    skip_canvas: bool = False
    dry_run: bool = False


@app.post("/api/admin/reports/generate")
def admin_report_generate(req: ReportGenerateRequest):
    """レポート生成ジョブを開始。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    params = req.model_dump()
    job_id = queue.enqueue("report", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued"}


# --- Quality endpoints --- #

class ScreenRequest(BaseModel):
    include_decisions: bool = False
    export: bool = False


class RelinkImportRequest(BaseModel):
    csv_content: str  # Base64-encoded or raw CSV content
    dry_run: bool = False


@app.post("/api/admin/quality/screen")
def admin_quality_screen(req: ScreenRequest):
    """Screen (重複検出) ジョブを開始。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    params = req.model_dump()
    job_id = queue.enqueue("screen", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/admin/quality/screen-preview")
def admin_quality_screen_preview(
    include_decisions: bool = Query(False),
    short_threshold: int = Query(25),
    prefix_len: int = Query(20),
):
    """重複・類似・曖昧グループを同期的に検出して JSON で返す。
    UI で削除対象を選択してから /delete-items に POST する想定。"""
    try:
        sys.path.insert(0, str(_REPO / "scripts" / "quality"))
        from pm_screen import screen_for_web  # type: ignore

        db_path = _state.get("db_path") or _DEFAULT_DB
        conn = open_db(db_path, encrypt=True)
        try:
            result = screen_for_web(
                conn,
                include_decisions=include_decisions,
                short_threshold=short_threshold,
                prefix_len=prefix_len,
            )
        finally:
            conn.close()
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class DeleteItemsRequest(BaseModel):
    action_item_ids: list[int] = []
    decision_ids: list[int] = []


@app.post("/api/admin/quality/delete-items")
def admin_quality_delete_items(req: DeleteItemsRequest):
    """指定 ID のアクションアイテム・決定事項を論理削除 (deleted=1)。"""
    ai_ids = list(req.action_item_ids or [])
    dec_ids = list(req.decision_ids or [])
    if not ai_ids and not dec_ids:
        return JSONResponse({"error": "削除対象 ID が指定されていません"}, status_code=400)
    try:
        db_path = _state.get("db_path") or _DEFAULT_DB
        conn = open_db(db_path, encrypt=True)
        try:
            ai_count = 0
            for aid in ai_ids:
                cur = conn.execute(
                    "UPDATE action_items SET deleted=1 WHERE id=? AND COALESCE(deleted,0)=0",
                    (aid,),
                )
                if cur.rowcount:
                    ai_count += cur.rowcount
                    audit(conn, "action_items", aid, "deleted", 0, 1)
            dec_count = 0
            for did in dec_ids:
                cur = conn.execute(
                    "UPDATE decisions SET deleted=1 WHERE id=? AND COALESCE(deleted,0)=0",
                    (did,),
                )
                if cur.rowcount:
                    dec_count += cur.rowcount
                    audit(conn, "decisions", did, "deleted", 0, 1)
            conn.commit()
        finally:
            conn.close()
        return {"deleted_action_items": ai_count, "deleted_decisions": dec_count}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/quality/relink-export")
def admin_quality_relink_export():
    """Relink CSV をダウンロード。"""
    try:
        py = _state.get("_python", "python3")
        scripts_dir = Path(__file__).parent
        result = subprocess.run(
            [py, str(scripts_dir / "pm_relink.py"), "--export"],
            capture_output=True, text=True, cwd=str(_REPO), timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr}, status_code=500)
        # pm_relink.py outputs CSV to stdout; parse and return as download
        csv_content = result.stdout
        if not csv_content.strip():
            return JSONResponse({"error": "No data exported"}, status_code=400)
        return StreamingResponse(
            io.StringIO(csv_content),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=pm_relink_export.csv"},
        )
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Export timed out"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/quality/relink-import")
def admin_quality_relink_import(req: RelinkImportRequest):
    """Relink CSV をインポート（ジョブ経由）。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    # CSV content を一時ファイルに保存
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, dir=str(_REPO / "data"))
    tmp.write(req.csv_content)
    tmp_path = tmp.name
    tmp.close()
    params = {"csv_path": tmp_path, "dry_run": req.dry_run}
    job_id = queue.enqueue("relink-import", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued", "csv_file": tmp_path}


# --- Recording endpoints --- #

@app.post("/api/admin/recording/upload")
async def admin_recording_upload(files: list[UploadFile] = File(...),
                                  meeting_name: str = Form(""),
                                  held_at: str = Form(""),
                                  skip_seconds: int = Form(0)):
    """録音ファイル + VTT をアップロードし、パイプラインジョブを開始。"""
    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)

    if not files or len(files) == 0:
        return JSONResponse({"error": "ファイルが選択されていません"}, status_code=400)

    # Validate and save files
    proc_dir = _state.get("processing_dir")
    if not proc_dir:
        proc_dir = _REPO / "data" / "processing"
        proc_dir.mkdir(parents=True, exist_ok=True)
        _state["processing_dir"] = proc_dir

    audio_path = None
    vtt_path = None

    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in {".mp4", ".m4a", ".wav", ".mp3", ".vtt"}:
            return JSONResponse({"error": f"非対応のファイル形式: {f.filename}"}, status_code=400)

        file_path = proc_dir / f.filename
        content = await f.read()
        with open(file_path, "wb") as out:
            out.write(content)

        if ext == ".vtt":
            vtt_path = str(file_path)
        elif audio_path is None:
            audio_path = str(file_path)

    if not audio_path:
        return JSONResponse({"error": "音声ファイル (MP4/M4A/WAV/MP3) が見つかりません"}, status_code=400)

    params = {
        "file_path": audio_path,
        "meeting_name": meeting_name,
        "held_at": held_at,
        "skip_seconds": skip_seconds,
    }
    if vtt_path:
        params["vtt_path"] = vtt_path

    job_id = queue.enqueue("recording", params)
    queue.start(job_id)

    extra = " + VTT" if vtt_path else ""
    return {"job_id": job_id, "status": "queued", "file": audio_path + extra}


@app.post("/api/admin/recording/start")
async def admin_recording_start(request: Request):
    """サーバー上の既存ファイルパスを指定してパイプラインを開始。"""
    body = await request.json()
    file_path = body.get("file_path", "").strip()
    meeting_name = body.get("meeting_name", "").strip()
    held_at = body.get("held_at", "").strip()
    skip_seconds = int(body.get("skip_seconds", 0))
    vtt_path = (body.get("vtt_path") or "").strip() or None

    queue = _state.get("job_queue")
    if not queue:
        return JSONResponse({"error": "Job queue not initialized"}, status_code=500)
    if not file_path:
        return JSONResponse({"error": "file_path は必須です"}, status_code=400)
    if not Path(file_path).exists():
        return JSONResponse({"error": f"ファイルが見つかりません: {file_path}"}, status_code=400)
    if vtt_path and not Path(vtt_path).exists():
        return JSONResponse({"error": f"VTT ファイルが見つかりません: {vtt_path}"}, status_code=400)

    params: dict = {"file_path": file_path, "meeting_name": meeting_name,
                    "held_at": held_at, "skip_seconds": skip_seconds}
    if vtt_path:
        params["vtt_path"] = vtt_path

    job_id = queue.enqueue("recording", params)
    queue.start(job_id)
    return {"job_id": job_id, "status": "queued", "file": file_path}


# --- Minutes management --- #

@app.get("/api/admin/minutes/recent")
def admin_recent_minutes():
    """直近の会議一覧を返す。"""
    minutes = get_recent_minutes(_REPO, no_encrypt=_state["no_encrypt"])
    return {"minutes": minutes}


@app.get("/api/admin/minutes/list")
def admin_minutes_list(kind: str = Query("")):
    """全 minutes DB の会議インスタンス一覧。"""
    all_minutes = list_minutes(_REPO, no_encrypt=_state["no_encrypt"])
    if kind:
        all_minutes = [m for m in all_minutes if m.get("kind") == kind]
    return {"minutes": all_minutes, "total": len(all_minutes)}


@app.get("/api/admin/minutes/meetings")
def admin_minutes_meetings():
    """利用可能な会議種別（kind）一覧を返す。"""
    minutes_dir = _REPO / "data" / "minutes"
    kinds = []
    if minutes_dir.exists():
        kinds = sorted([f.stem for f in minutes_dir.glob("*.db")])
    return {"meetings": kinds}


@app.get("/api/admin/minutes/content")
def admin_minutes_content(id: str = Query(""), kind: str = Query("")):
    """議事内容を取得する。"""
    content = get_minutes_content(id, kind, no_encrypt=_state["no_encrypt"], repo_root=_REPO)
    if content is None:
        return JSONResponse({"error": "Content not found"}, status_code=404)
    return {"meeting_id": id, "kind": kind, "content": content}


class UpdateMinutesContentRequest(BaseModel):
    meeting_id: str
    kind: str
    content: str


@app.post("/api/admin/minutes/content/save")
def admin_minutes_content_save(req: UpdateMinutesContentRequest):
    """議事内容を更新し、pm.db / Box XLSX への発行ジョブをキューイングする。"""
    result = update_minutes_content(
        req.meeting_id, req.kind, req.content,
        no_encrypt=_state["no_encrypt"], repo_root=_REPO,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)

    # Enqueue async publish job (only on content save to avoid duplication)
    publish_job_id = None
    try:
        held_at = get_minutes_held_at(
            req.meeting_id, req.kind,
            no_encrypt=_state["no_encrypt"], repo_root=_REPO,
        )
        if held_at:
            params = {
                "meeting_id": req.meeting_id,
                "kind": req.kind,
                "held_at": held_at,
                "no_encrypt": _state.get("no_encrypt", False),
            }
            publish_job_id = _job_queue.enqueue("minutes-publish", params)
            _job_queue.start(publish_job_id)
    except Exception:
        pass  # publish failure should not block the save response

    result["publish_job_id"] = publish_job_id
    return result


def _enqueue_xlsx_publish() -> str | None:
    """Box XLSX 更新ジョブを非同期でエンキューする。"""
    try:
        params = {"no_encrypt": _state.get("no_encrypt", False)}
        job_id = _job_queue.enqueue("xlsx-publish", params)
        _job_queue.start(job_id)
        return job_id
    except Exception:
        return None


class UpdateMinutesDecisionsRequest(BaseModel):
    meeting_id: str
    kind: str
    items: list[dict]


@app.get("/api/admin/minutes/decisions")
def admin_minutes_decisions(id: str = Query(""), kind: str = Query("")):
    """minutes DB から決定事項一覧を取得。"""
    items = get_minutes_decisions(id, kind, no_encrypt=_state["no_encrypt"], repo_root=_REPO)
    return {"items": items}


@app.post("/api/admin/minutes/decisions/save")
def admin_minutes_decisions_save(req: UpdateMinutesDecisionsRequest):
    """minutes DB の決定事項を全置換。"""
    result = update_minutes_decisions(
        req.meeting_id, req.kind, req.items,
        no_encrypt=_state["no_encrypt"], repo_root=_REPO,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return result


@app.get("/api/admin/minutes/action-items")
def admin_minutes_action_items(id: str = Query(""), kind: str = Query("")):
    """minutes DB からアクションアイテム一覧を取得。"""
    items = get_minutes_action_items(id, kind, no_encrypt=_state["no_encrypt"], repo_root=_REPO)
    return {"items": items}


@app.post("/api/admin/minutes/action-items/save")
def admin_minutes_action_items_save(req: UpdateMinutesDecisionsRequest):
    """minutes DB のアクションアイテムを全置換。"""
    result = update_minutes_action_items(
        req.meeting_id, req.kind, req.items,
        no_encrypt=_state["no_encrypt"], repo_root=_REPO,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return result


class DeleteMinutesRequest(BaseModel):
    meeting_id: str
    kind: str
    cascade_pm: bool = True          # pm.db からも削除
    cascade_canvas: bool = False     # Canvas 目録を再生成
    cascade_box: bool = False        # Box ファイルは削除しない（バージョン管理のため）


@app.post("/api/admin/minutes/delete")
def admin_minutes_delete(req: DeleteMinutesRequest):
    """会議インスタンスを削除（カスケード削除対応）。"""
    # 1. minutes DB から削除
    result = delete_minutes_instance(
        req.meeting_id, req.kind, no_encrypt=_state["no_encrypt"], repo_root=_REPO,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)

    # 2. pm.db から削除（カスケード）
    cascade_result = {}
    if req.cascade_pm:
        try:
            conn = _get_conn()
            pm_result = delete_minutes_from_pm(
                req.meeting_id, repo_root=_REPO, pm_conn=conn,
                no_encrypt=_state["no_encrypt"],
            )
            cascade_result["pm_db"] = pm_result
        except Exception as e:
            cascade_result["pm_db"] = {"error": str(e)}

    # 3. Canvas 目録再生成（カスケード）
    if req.cascade_canvas:
        cascade_result["canvas"] = {"scheduled": True}
        # Canvas 再生成はジョブとして非同期実行
        queue = _state.get("job_queue")
        if queue:
            job_id = queue.enqueue("catalog", {"kind": req.kind})
            queue.start(job_id)
            cascade_result["canvas"]["job_id"] = job_id

    return {
        "minutes_db": result,
        "cascade": cascade_result,
    }


# 静的ファイル配信（API ルートより後に配置）
# --------------------------------------------------------------------------- #
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="PM DB Editor API server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PM_WEB_PORT", _DEFAULT_PORT)))
    parser.add_argument("--db", default=os.environ.get("PM_WEB_DB", str(_DEFAULT_DB)))
    parser.add_argument("--no-encrypt", action="store_true",
                        default=os.environ.get("PM_WEB_NO_ENCRYPT", "").lower() in ("1", "true", "yes"))
    args = parser.parse_args()

    _state["db_path"] = str(Path(args.db).resolve())
    _state["no_encrypt"] = args.no_encrypt
    _get_conn()  # validate DB on startup

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
