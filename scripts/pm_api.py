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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from web_utils import (
    scan_pm_dbs, get_conn, load_milestones, load_action_items, load_decisions,
    load_minutes_content, nv, to_bool, audit, do_save_action_items, do_save_decisions,
)

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "data" / "pm.db"
_DEFAULT_PORT = 8501

# --------------------------------------------------------------------------- #
# アプリ状態（シングルプロセス前提）
# --------------------------------------------------------------------------- #
_state: dict[str, Any] = {
    "db_path": "",
    "no_encrypt": False,
    "ai_df": None,   # optimistic locking 用スナップショット
    "dec_df": None,
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
    return {"ok": True, "name": p.name}


# --- Milestone endpoints --- #

@app.get("/api/milestones")
def get_milestones():
    return {"milestones": load_milestones(_get_conn())}


# --- Action Item endpoints --- #

@app.get("/api/action-items")
def get_action_items(
    status: str = Query("open"),
    milestone: str = Query("すべて"),
    since: str = Query(""),
    deleted: str = Query("非削除"),
):
    df = load_action_items(_get_conn(), status, milestone, since or None, deleted)
    _state["ai_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/action-items/save")
def save_action_items(req: SaveRowsRequest):
    if _state["ai_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_action_items(_get_conn(), _state["ai_df"], req.rows)
    # スナップショットを更新
    _state["ai_df"] = None
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
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


# --- Decision endpoints --- #

@app.get("/api/decisions")
def get_decisions(
    acknowledged: str = Query("すべて"),
    since: str = Query(""),
    deleted: str = Query("非削除"),
):
    df = load_decisions(_get_conn(), acknowledged, since or None, deleted)
    _state["dec_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/decisions/save")
def save_decisions(req: SaveRowsRequest):
    if _state["dec_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_decisions(_get_conn(), _state["dec_df"], req.rows)
    _state["dec_df"] = None
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/decisions/new")
def create_decision(req: NewDecisionRequest):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO decisions (content,decided_at,source,source_ref,extracted_at)"
        " VALUES(?,?,'manual',?,?)",
        (req.content.strip(), nv(req.decided_at),
         nv(req.source), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.post("/api/decisions/ack-all")
def ack_all_decisions():
    conn = _get_conn()
    if _state["dec_df"] is None:
        return JSONResponse({"error": "データ未読込"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    unacked = _state["dec_df"][_state["dec_df"]["acknowledged_at"] == ""]
    for dec_id in unacked["id"].tolist():
        audit(conn, "decisions", int(dec_id), "acknowledged_at", None, now)
        conn.execute("UPDATE decisions SET acknowledged_at=? WHERE id=?", (now, int(dec_id)))
    conn.commit()
    return {"count": len(unacked)}


# --- Minutes endpoint --- #

@app.get("/api/minutes")
def get_minutes(id: str = Query(""), kind: str = Query("")):
    content = load_minutes_content(id, no_encrypt=_state["no_encrypt"], kind=kind)
    return {"meeting_id": id, "kind": kind, "content": content}


# --- Files endpoint --- #

_CHANNEL_NAMES: dict[str, str] = {
    "C08M0249GRL": "20_アプリケーション開発エリア",
    "C08SXA4M7JT": "20_1_リーダ会議メンバ",
    "C08LSJP4R6K": "21_hpcアプリケーションwg",
    "C093DQFSCRH": "21_1_hpcアプリケーションwg_ブロック1",
    "C093LP1J15G": "21_2_hpcアプリケーションwg_ブロック2",
    "C08MJ0NF5UZ": "22_ベンチマークwg",
    "C096ER1A0LU": "23_benchmark_framework",
    "C0A6AC59AHM": "24_ai-hpc-application",
    "C0A9KG036CS": "personal",
    "C08PE3K9N72": "pmo",
}

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
    lines_before = [l.strip() for l in before.split("\n") if l.strip()]
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
    """全 Slack DB から Box リンク・ファイルURLを抽出して一覧を返す"""
    data_dir = _REPO / "data"
    results: list[dict] = []
    seen_urls: set[str] = set()

    db_files = sorted(data_dir.glob("C*.db"))
    for db_file in db_files:
        ch_id = db_file.stem
        if channel and ch_id != channel:
            continue
        ch_name = _CHANNEL_NAMES.get(ch_id, ch_id)
        try:
            conn = open_db(str(db_file), encrypt=not _state["no_encrypt"])
        except Exception:
            continue

        # messages と replies を両方走査
        for table, ts_col in [("messages", "thread_ts"), ("replies", "msg_ts")]:
            try:
                q = f"SELECT {ts_col}, text, COALESCE(permalink,''), timestamp FROM {table} WHERE text LIKE '%box.com%'"
                params: list = []
                if since:
                    q += " AND timestamp >= ?"
                    params.append(since)
                rows = conn.execute(q, params).fetchall()
            except Exception:
                continue
            for row in rows:
                _, text, permalink, timestamp = row
                if not text:
                    continue
                context = _msg_context(text)
                date_str = str(timestamp)[:10] if timestamp else ""
                for m in _PAT_BOX_URL.finditer(text):
                    if m.group(1):
                        # <URL|label> 形式
                        url = m.group(1).rstrip(".")
                        inline_label = m.group(2).strip()
                        # "Powered by Box" を除去
                        pb = _re.search(r"\s*\|?\s*Powered by Box", inline_label, _re.IGNORECASE)
                        if pb:
                            inline_label = inline_label[:pb.start()].strip()
                        label = inline_label
                    else:
                        # plain URL or <URL>
                        url = m.group(3).rstrip(".")
                        label = _extract_label(text, url, m.start(), m.end())
                    # URLの重複チェック（同一URLが複数チャンネルに投稿される場合は含める）
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
# 静的ファイル配信（API ルートより後に配置）
# --------------------------------------------------------------------------- #
_static_dir = Path(__file__).resolve().parent / "static"
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
