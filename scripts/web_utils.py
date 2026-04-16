"""
web_utils.py — pm_api.py / pm_web.py 共通の DB ヘルパー・保存ロジック

pm_api.py（FastAPI）と pm_web.py（NiceGUI、非推奨）で重複していた
DB操作・フィルタリング・楽観的排他制御ロジックを統合。
"""

import glob as _glob
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from db_utils import open_pm_db, open_db

_REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# ユーティリティ
# --------------------------------------------------------------------------- #

def scan_pm_dbs() -> list[str]:
    """data/pm*.db のパス一覧を返す。"""
    pattern = str(_REPO / "data" / "pm*.db")
    return sorted(_glob.glob(pattern))


def get_conn(db_path: Path, no_encrypt: bool = False):
    """pm.db を開き audit_log テーブルを保証して返す。"""
    conn = open_pm_db(db_path, no_encrypt=no_encrypt)
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


def nv(val):
    """値を正規化: 空文字・NaN → None"""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def to_bool(val) -> bool:
    """各種型を bool に変換"""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(int(val))
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return False


def audit(conn, table: str, record_id, field: str, old_val, new_val):
    """変更を audit_log に記録"""
    conn.execute(
        "INSERT INTO audit_log (table_name,record_id,field,old_value,new_value,changed_at,source)"
        " VALUES(?,?,?,?,?,?,'web_ui')",
        (table, str(record_id), field,
         str(old_val) if old_val is not None else None,
         str(new_val) if new_val is not None else None,
         datetime.now(timezone.utc).isoformat()),
    )


# --------------------------------------------------------------------------- #
# データ読み込み
# --------------------------------------------------------------------------- #

def load_milestones(conn) -> dict[str, str]:
    """アクティブなマイルストーン一覧を {id: "id: name"} で返す。"""
    try:
        rows = conn.execute(
            "SELECT milestone_id, name FROM milestones WHERE status='active' ORDER BY milestone_id"
        ).fetchall()
        return {r[0]: f"{r[0]}: {r[1]}" for r in rows}
    except Exception:
        return {}


def load_action_items(conn, status_f, ms_f, since, del_f="非削除") -> pd.DataFrame:
    """フィルタ条件に合うアクションアイテムを DataFrame で返す。"""
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
    """フィルタ条件に合う決定事項を DataFrame で返す。"""
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


def load_minutes_content(meeting_id: str, no_encrypt: bool = False, kind: str = "") -> str:
    """議事録本文を取得して結合テキストで返す。"""
    if not meeting_id and not kind:
        return ""
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


# --------------------------------------------------------------------------- #
# データ保存（楽観的排他制御付き）
# --------------------------------------------------------------------------- #

def do_save_action_items(conn, original_df, edited_rows) -> tuple[int, list[dict]]:
    """アクションアイテムの変更を保存。(変更件数, コンフリクト一覧) を返す。"""
    editable = ["content", "assignee", "due_date", "milestone_id", "note"]
    count = 0
    conflicts: list[dict] = []
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
            new_status = "closed" if to_bool(done_val) else "open"
            old_status = "closed" if orig["done"] else "open"
            if new_status != old_status:
                if db["status"] != old_status:
                    conflicts.append({"id": ai_id, "field": "status",
                                      "yours": new_status, "db": db["status"]})
                else:
                    audit(conn, "action_items", ai_id, "status", old_status, new_status)
                    conn.execute("UPDATE action_items SET status=? WHERE id=?", (new_status, ai_id))
                    count += 1
        new_del = 1 if to_bool(row.get("deleted")) else 0
        old_del = 1 if orig["deleted"] else 0
        if new_del != old_del:
            db_del = 1 if db["deleted"] else 0
            if db_del != old_del:
                conflicts.append({"id": ai_id, "field": "deleted",
                                  "yours": new_del, "db": db_del})
            else:
                audit(conn, "action_items", ai_id, "deleted", old_del, new_del)
                conn.execute("UPDATE action_items SET deleted=? WHERE id=?", (new_del, ai_id))
                count += 1
        for col in editable:
            new_val = nv(row.get(col))
            old_val = nv(orig[col])
            if new_val != old_val:
                db_val = nv(db.get(col))
                if db_val != old_val:
                    conflicts.append({"id": ai_id, "field": col,
                                      "yours": new_val, "db": db_val})
                else:
                    audit(conn, "action_items", ai_id, col, old_val, new_val)
                    conn.execute(f"UPDATE action_items SET {col}=? WHERE id=?", (new_val, ai_id))
                    count += 1
    conn.commit()
    return count, conflicts


def do_save_decisions(conn, original_df, edited_rows) -> tuple[int, list[dict]]:
    """決定事項の変更を保存。(変更件数, コンフリクト一覧) を返す。"""
    count = 0
    conflicts: list[dict] = []
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
        new_del = 1 if to_bool(row.get("deleted")) else 0
        old_del = 1 if orig["deleted"] else 0
        if new_del != old_del:
            db_del = 1 if db["deleted"] else 0
            if db_del != old_del:
                conflicts.append({"id": dec_id, "field": "deleted",
                                  "yours": new_del, "db": db_del})
            else:
                audit(conn, "decisions", dec_id, "deleted", old_del, new_del)
                conn.execute("UPDATE decisions SET deleted=? WHERE id=?", (new_del, dec_id))
                count += 1
        for col in ("content", "decided_at", "acknowledged_at"):
            new_val = nv(row.get(col))
            old_val = nv(orig[col])
            if new_val != old_val:
                db_val = nv(db.get(col))
                if db_val != old_val:
                    conflicts.append({"id": dec_id, "field": col,
                                      "yours": new_val, "db": db_val})
                else:
                    audit(conn, "decisions", dec_id, col, old_val, new_val)
                    conn.execute(f"UPDATE decisions SET {col}=? WHERE id=?", (new_val, dec_id))
                    count += 1
    conn.commit()
    return count, conflicts
