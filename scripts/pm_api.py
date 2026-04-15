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
import glob as _glob
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import open_pm_db, open_db

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

# --------------------------------------------------------------------------- #
# DB ヘルパー（pm_web.py から移植）
# --------------------------------------------------------------------------- #

def _scan_pm_dbs() -> list[str]:
    pattern = str(_REPO / "data" / "pm*.db")
    return sorted(_glob.glob(pattern))


def get_conn(db_path: Path | None = None, no_encrypt: bool | None = None):
    path = db_path or Path(_state["db_path"])
    ne = no_encrypt if no_encrypt is not None else _state["no_encrypt"]
    conn = open_pm_db(path, no_encrypt=ne)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT,
            record_id  TEXT,
            field      TEXT,
            old_value  TEXT,
            new_value  TEXT,
            changed_at TEXT,
            source     TEXT
        )
    """)
    conn.commit()
    return conn


def load_milestones(conn) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT milestone_id, name FROM milestones WHERE status='active' ORDER BY milestone_id"
        ).fetchall()
        return {r[0]: f"{r[0]}: {r[1]}" for r in rows}
    except Exception:
        return {}


def load_action_items(conn, status_f, ms_f, since, del_f="非削除") -> pd.DataFrame:
    q = ("SELECT a.id, a.content, a.assignee, a.due_date, a.milestone_id, a.status, a.note,"
         " a.extracted_at, a.source, COALESCE(a.deleted,0) AS deleted,"
         " COALESCE(a.source_ref,'') AS source_ref,"
         " COALESCE(a.meeting_id,'') AS meeting_id,"
         " COALESCE(m.kind,'') AS meeting_kind"
         " FROM action_items a"
         " LEFT JOIN meetings m ON a.meeting_id = m.meeting_id"
         " WHERE 1=1")
    p: list = []
    if del_f == "非削除":
        q += " AND COALESCE(a.deleted,0)=0"
    elif del_f == "削除のみ":
        q += " AND a.deleted=1"
    if status_f and status_f != "すべて":
        q += " AND a.status=?"; p.append(status_f)
    if ms_f and ms_f != "すべて":
        q += " AND a.milestone_id=?"; p.append(ms_f)
    if since:
        q += " AND a.extracted_at >= ?"; p.append(since)
    q += " ORDER BY a.id DESC"
    df = pd.DataFrame(conn.execute(q, p).fetchall(),
                      columns=["id", "content", "assignee", "due_date",
                               "milestone_id", "status", "note",
                               "extracted_at", "source", "deleted",
                               "source_ref", "meeting_id", "meeting_kind"])
    df = df.fillna("")
    df["extracted_at"] = df["extracted_at"].str[:10]
    df["done"] = df["status"] == "closed"
    df["deleted"] = df["deleted"].apply(lambda v: bool(int(v)) if v != "" else False)
    return df


def load_decisions(conn, ack_f, since, del_f="非削除") -> pd.DataFrame:
    q = ("SELECT id, content, decided_at, acknowledged_at, extracted_at, source,"
         " COALESCE(deleted,0) AS deleted,"
         " COALESCE(source_ref,'') AS source_ref"
         " FROM decisions WHERE 1=1")
    p: list = []
    if del_f == "非削除":
        q += " AND COALESCE(deleted,0)=0"
    elif del_f == "削除のみ":
        q += " AND deleted=1"
    if ack_f == "未確認のみ":
        q += " AND (acknowledged_at IS NULL OR acknowledged_at='')"
    elif ack_f == "確認済みのみ":
        q += " AND acknowledged_at IS NOT NULL AND acknowledged_at!=''"
    if since:
        q += " AND extracted_at >= ?"; p.append(since)
    q += " ORDER BY id DESC"
    df = pd.DataFrame(conn.execute(q, p).fetchall(),
                      columns=["id", "content", "decided_at", "acknowledged_at",
                               "extracted_at", "source", "deleted", "source_ref"])
    df = df.fillna("")
    df["extracted_at"] = df["extracted_at"].str[:10]
    df["deleted"] = df["deleted"].apply(lambda v: bool(int(v)) if v != "" else False)
    return df


def load_minutes_content(meeting_id: str, kind: str = "") -> str:
    if not meeting_id and not kind:
        return ""
    no_encrypt = _state["no_encrypt"]
    if not kind and meeting_id:
        pos = meeting_id.find("_")
        if pos >= 4:
            kind = meeting_id[pos + 1:]
    if kind:
        db_path = _REPO / "data" / "minutes" / f"{kind}.db"
        if db_path.exists():
            try:
                conn = open_db(str(db_path), encrypt=not no_encrypt)
                rows = conn.execute(
                    "SELECT content FROM minutes_content WHERE meeting_id=? ORDER BY id",
                    (meeting_id,)
                ).fetchall()
                if not rows and meeting_id and len(meeting_id) >= 10:
                    held_at = meeting_id[:10]
                    rows = conn.execute(
                        "SELECT mc.content FROM minutes_content mc"
                        " JOIN instances i ON mc.meeting_id=i.meeting_id"
                        " WHERE i.held_at=? ORDER BY mc.id",
                        (held_at,)
                    ).fetchall()
                conn.close()
                if rows:
                    return "\n\n---\n\n".join(r[0] for r in rows)
            except Exception as e:
                return f"（読み込みエラー: {e}）"
    minutes_dir = _REPO / "data" / "minutes"
    if minutes_dir.exists():
        for db_path in sorted(minutes_dir.glob("*.db")):
            try:
                conn = open_db(str(db_path), encrypt=not no_encrypt)
                rows = conn.execute(
                    "SELECT content FROM minutes_content WHERE meeting_id=? ORDER BY id",
                    (meeting_id,)
                ).fetchall()
                conn.close()
                if rows:
                    return "\n\n---\n\n".join(r[0] for r in rows)
            except Exception:
                continue
    return ""


def _nv(val):
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def _to_bool(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return False


def _audit(conn, table, record_id, field, old_val, new_val):
    conn.execute(
        "INSERT INTO audit_log (table_name,record_id,field,old_value,new_value,changed_at,source)"
        " VALUES(?,?,?,?,?,?,'web_ui')",
        (table, str(record_id), field,
         str(old_val) if old_val is not None else None,
         str(new_val) if new_val is not None else None,
         datetime.now(timezone.utc).isoformat()),
    )


def do_save_action_items(conn, original_df, edited_rows) -> tuple[int, list[dict]]:
    editable = ["content", "assignee", "due_date", "milestone_id", "note"]
    count = 0
    conflicts = []
    for row in edited_rows:
        ai_id = int(row["id"])
        orig_rows = original_df[original_df["id"] == ai_id]
        if orig_rows.empty:
            continue
        orig = orig_rows.iloc[0]
        db_row = conn.execute(
            "SELECT status, deleted, content, assignee, due_date, milestone_id, note"
            " FROM action_items WHERE id=?", (ai_id,)
        ).fetchone()
        if db_row is None:
            continue
        db = dict(db_row)
        done_val = row.get("done")
        if done_val is not None:
            new_status = "closed" if _to_bool(done_val) else "open"
            old_status = "closed" if orig["done"] else "open"
            if new_status != old_status:
                if db["status"] != old_status:
                    conflicts.append({"id": ai_id, "field": "status",
                                      "yours": new_status, "db": db["status"]})
                else:
                    _audit(conn, "action_items", ai_id, "status", old_status, new_status)
                    conn.execute("UPDATE action_items SET status=? WHERE id=?", (new_status, ai_id))
                    count += 1
        new_del = 1 if _to_bool(row.get("deleted")) else 0
        old_del = 1 if orig["deleted"] else 0
        if new_del != old_del:
            db_del = 1 if db["deleted"] else 0
            if db_del != old_del:
                conflicts.append({"id": ai_id, "field": "deleted",
                                  "yours": new_del, "db": db_del})
            else:
                _audit(conn, "action_items", ai_id, "deleted", old_del, new_del)
                conn.execute("UPDATE action_items SET deleted=? WHERE id=?", (new_del, ai_id))
                count += 1
        for col in editable:
            new_val = _nv(row.get(col))
            old_val = _nv(orig[col])
            if new_val != old_val:
                db_val = _nv(db.get(col))
                if db_val != old_val:
                    conflicts.append({"id": ai_id, "field": col,
                                      "yours": new_val, "db": db_val})
                else:
                    _audit(conn, "action_items", ai_id, col, old_val, new_val)
                    conn.execute(f"UPDATE action_items SET {col}=? WHERE id=?", (new_val, ai_id))
                    count += 1
    conn.commit()
    return count, conflicts


def do_save_decisions(conn, original_df, edited_rows) -> tuple[int, list[dict]]:
    count = 0
    conflicts = []
    for row in edited_rows:
        dec_id = int(row["id"])
        orig_rows = original_df[original_df["id"] == dec_id]
        if orig_rows.empty:
            continue
        orig = orig_rows.iloc[0]
        db_row = conn.execute(
            "SELECT deleted, content, decided_at, acknowledged_at"
            " FROM decisions WHERE id=?", (dec_id,)
        ).fetchone()
        if db_row is None:
            continue
        db = dict(db_row)
        new_del = 1 if _to_bool(row.get("deleted")) else 0
        old_del = 1 if orig["deleted"] else 0
        if new_del != old_del:
            db_del = 1 if db["deleted"] else 0
            if db_del != old_del:
                conflicts.append({"id": dec_id, "field": "deleted",
                                  "yours": new_del, "db": db_del})
            else:
                _audit(conn, "decisions", dec_id, "deleted", old_del, new_del)
                conn.execute("UPDATE decisions SET deleted=? WHERE id=?", (new_del, dec_id))
                count += 1
        for col in ("content", "decided_at", "acknowledged_at"):
            new_val = _nv(row.get(col))
            old_val = _nv(orig[col])
            if new_val != old_val:
                db_val = _nv(db.get(col))
                if db_val != old_val:
                    conflicts.append({"id": dec_id, "field": col,
                                      "yours": new_val, "db": db_val})
                else:
                    _audit(conn, "decisions", dec_id, col, old_val, new_val)
                    conn.execute(f"UPDATE decisions SET {col}=? WHERE id=?", (new_val, dec_id))
                    count += 1
    conn.commit()
    return count, conflicts


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
    dbs = _scan_pm_dbs()
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
        get_conn(p)  # validate DB can be opened
    except Exception as e:
        return JSONResponse({"error": f"DB接続エラー: {e}"}, status_code=400)
    _state["db_path"] = str(p)
    _state["ai_df"] = None
    _state["dec_df"] = None
    return {"ok": True, "name": p.name}


# --- Milestone endpoints --- #

@app.get("/api/milestones")
def get_milestones():
    return {"milestones": load_milestones(get_conn())}


# --- Action Item endpoints --- #

@app.get("/api/action-items")
def get_action_items(
    status: str = Query("open"),
    milestone: str = Query("すべて"),
    since: str = Query(""),
    deleted: str = Query("非削除"),
):
    df = load_action_items(get_conn(), status, milestone, since or None, deleted)
    _state["ai_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/action-items/save")
def save_action_items(req: SaveRowsRequest):
    if _state["ai_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_action_items(get_conn(), _state["ai_df"], req.rows)
    # スナップショットを更新
    _state["ai_df"] = None
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/action-items/new")
def create_action_item(req: NewActionItemRequest):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO action_items"
        " (content,assignee,due_date,milestone_id,status,note,source,source_ref,extracted_at)"
        " VALUES(?,?,?,?,?,?,'manual',?,?)",
        (req.content.strip(), _nv(req.assignee), _nv(req.due_date),
         _nv(req.milestone_id), req.status, _nv(req.note), _nv(req.source),
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
    df = load_decisions(get_conn(), acknowledged, since or None, deleted)
    _state["dec_df"] = df
    return {"rows": df.to_dict("records")}


@app.post("/api/decisions/save")
def save_decisions(req: SaveRowsRequest):
    if _state["dec_df"] is None:
        return JSONResponse({"error": "データ未読込。先に一覧を取得してください"}, status_code=400)
    n, conflicts = do_save_decisions(get_conn(), _state["dec_df"], req.rows)
    _state["dec_df"] = None
    return {"updated": n, "conflicts": conflicts}


@app.post("/api/decisions/new")
def create_decision(req: NewDecisionRequest):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO decisions (content,decided_at,source,source_ref,extracted_at)"
        " VALUES(?,?,'manual',?,?)",
        (req.content.strip(), _nv(req.decided_at),
         _nv(req.source), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.post("/api/decisions/ack-all")
def ack_all_decisions():
    conn = get_conn()
    if _state["dec_df"] is None:
        return JSONResponse({"error": "データ未読込"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    unacked = _state["dec_df"][_state["dec_df"]["acknowledged_at"] == ""]
    for dec_id in unacked["id"].tolist():
        _audit(conn, "decisions", int(dec_id), "acknowledged_at", None, now)
        conn.execute("UPDATE decisions SET acknowledged_at=? WHERE id=?", (now, int(dec_id)))
    conn.commit()
    return {"count": len(unacked)}


# --- Minutes endpoint --- #

@app.get("/api/minutes")
def get_minutes(id: str = Query(""), kind: str = Query("")):
    content = load_minutes_content(id, kind)
    return {"meeting_id": id, "kind": kind, "content": content}


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
    get_conn()  # validate DB on startup

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
