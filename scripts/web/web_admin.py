"""
web_admin.py — Admin dashboard backend for PM Web UI

Provides:
  - AdminJobQueue: SQLite-backed background job queue
  - Service management (pm_daemon.sh wrapper)
  - Dashboard statistics aggregation
  - Command builders for all admin operations

Usage (indirect, via pm_api.py):
    from web_admin import AdminJobQueue, get_service_status, ...
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from db_utils import open_db

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _REPO / "logs"
_ADMIN_JOBS_DB = _REPO / "data" / "admin_jobs.db"
_DAEMON_SCRIPT = _REPO / "scripts" / "bin" / "pm_daemon.sh"
_SCRIPTS_DIR = _REPO / "scripts"

# Service log files (according to pm_daemon.sh conventions)
_SERVICE_LOG_FILES: dict[str, Path] = {
    "qa": _LOG_DIR / "pm_qa_server.log",
    "web": _LOG_DIR / "pm_web.log",
}

# --------------------------------------------------------------------------- #
# ジョブキュー
# --------------------------------------------------------------------------- #

class AdminJobQueue:
    """SQLite-backed async job queue for admin operations."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or _REPO
        self.scripts_dir = self.repo_root / "scripts"
        self.log_dir = self.repo_root / "logs"
        self.db_path = self.repo_root / "data" / "admin_jobs.db"
        self._running_jobs: dict[str, asyncio.Task] = {}
        self._python = self._detect_python()
        self._init_db()

    # ---- DB init ---- #

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_jobs (
                id              TEXT PRIMARY KEY,
                kind            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'queued',
                params_json     TEXT,
                created_at      TEXT NOT NULL,
                started_at      TEXT,
                finished_at     TEXT,
                exit_code       INTEGER,
                summary         TEXT,
                log_file        TEXT,
                progress        INTEGER DEFAULT 0,
                progress_msg    TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_status ON admin_jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_kind ON admin_jobs(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_created ON admin_jobs(created_at DESC)")
        conn.commit()
        conn.close()

    def _insert_job(self, job_id: str, kind: str, params: dict, log_file: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO admin_jobs (id, kind, status, params_json, created_at, log_file)"
            " VALUES (?,?, 'queued',?,?,?)",
            (job_id, kind, json.dumps(params, ensure_ascii=False),
             datetime.now(UTC).isoformat(), log_file),
        )
        conn.commit()
        conn.close()

    def _update_job(self, job_id: str, **kw: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        vals = list(kw.values()) + [job_id]
        conn = self._get_conn()
        conn.execute(f"UPDATE admin_jobs SET {sets} WHERE id=?", vals)
        conn.commit()
        conn.close()

    def get_job(self, job_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        d = dict(row)
        if d["params_json"]:
            d["params"] = json.loads(d["params_json"])
        else:
            d["params"] = {}
        return d

    def list_jobs(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        if kind:
            rows = conn.execute(
                "SELECT * FROM admin_jobs WHERE kind=? ORDER BY created_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM admin_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- Python detection ---- #

    def _detect_python(self) -> str:
        arch = os.uname().machine
        home = Path.home()
        if arch == "aarch64":
            guess = home / ".venv_aarch64" / "bin" / "python3"
        elif arch == "x86_64":
            guess = home / ".venv_x86_64" / "bin" / "python3"
        else:
            guess = home / ".venv_x86_64" / "bin" / "python3"
        if guess.exists():
            return str(guess)
        return "python3"

    # ---- Job enqueue / start ---- #

    def enqueue(self, kind: str, params: dict | None = None) -> str:
        job_id = str(uuid.uuid4())[:8]
        params = params or {}
        log_file = str(self.log_dir / f"admin_job_{job_id}.log")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._insert_job(job_id, kind, params, log_file)
        return job_id

    def start(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        if job["status"] != "queued":
            raise ValueError(f"Job {job_id} is {job['status']}, expected 'queued'")
        # 実行中のイベントループがあれば asyncio.Task として起動、
        # なければスレッドで起動（FastAPI 同期エンドポイントから呼ばれる場合に対応）
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._run_job(job_id))
            self._running_jobs[job_id] = task
        except RuntimeError:
            # No running event loop — run in a thread
            thread = threading.Thread(target=self._run_job_sync, args=(job_id,), daemon=True)
            thread.start()

    def _run_job_sync(self, job_id: str) -> None:
        """スレッド内で同期的にジョブを実行する。"""
        import asyncio
        asyncio.run(self._run_job(job_id))

    # ---- Command building ---- #

    def _build_command(self, kind: str, params: dict) -> list[str]:
        py = self._python

        if kind == "ingest":
            source = params.get("source", "goals")
            cmd = [py, str(self.scripts_dir / "ingest" / "pm_ingest.py"), source]
            if source == "slack" and params.get("slack_channel"):
                cmd += ["--slack-channel", params["slack_channel"]]
            if params.get("since"):
                cmd += ["--since", params["since"]]
            if params.get("dry_run"):
                cmd += ["--dry-run"]
            if params.get("no_auto_enrich"):
                cmd += ["--no-auto-enrich"]
            return cmd

        if kind == "embed":
            cmd = [py, str(self.scripts_dir / "pm_embed.py")]
            if params.get("index_name"):
                cmd += ["--index-name", params["index_name"]]
            if params.get("full_rebuild"):
                cmd += ["--full-rebuild"]
            if params.get("dry_run"):
                cmd += ["--dry-run"]
            return cmd

        if kind == "report":
            rtype = params.get("report_type", "report")
            cmd = [py, str(self.scripts_dir / f"pm_{rtype}.py")]
            if params.get("since"):
                cmd += ["--since", params["since"]]
            if params.get("skip_canvas"):
                cmd += ["--skip-canvas"]
            if params.get("dry_run"):
                cmd += ["--dry-run"]
            if params.get("output"):
                cmd += ["--output", params["output"]]
            return cmd

        if kind == "recording":
            file_path = params.get("file_path", "")
            cmd = ["bash", str(self.scripts_dir / "bin" / "pm_from_recording.sh"), file_path]
            if params.get("meeting_name"):
                cmd += ["--meeting-name", params["meeting_name"]]
            if params.get("held_at"):
                cmd += ["--held-at", params["held_at"]]
            if params.get("skip_seconds"):
                cmd += ["--skip", str(params["skip_seconds"])]
            if params.get("vtt_path"):
                cmd += ["--vtt", params["vtt_path"]]
            return cmd

        if kind == "screen":
            cmd = [py, str(self.scripts_dir / "pm_screen.py")]
            if params.get("include_decisions"):
                cmd += ["--include-decisions"]
            if params.get("export"):
                cmd += ["--export"]
            if params.get("output"):
                cmd += ["--output", params["output"]]
            return cmd

        if kind == "relink-import":
            csv_path = params.get("csv_path", "")
            cmd = [py, str(self.scripts_dir / "pm_relink.py"), "--import", csv_path]
            if params.get("dry_run"):
                cmd += ["--dry-run"]
            return cmd

        if kind == "catalog":
            cmd = [py, str(self.scripts_dir / "pm_minutes_catalog.py"), "--catalog"]
            if params.get("kind"):
                cmd += ["--meeting-name", params["kind"]]
            return cmd

        if kind == "minutes-publish":
            cmd = [py, str(self.scripts_dir / "pm_minutes_publish.py"),
                   "--meeting-id", params["meeting_id"],
                   "--kind", params["kind"],
                   "--held-at", params["held_at"]]
            if params.get("file_path"):
                cmd += ["--file-path", params["file_path"]]
            if params.get("no_encrypt"):
                cmd += ["--no-encrypt"]
            return cmd

        if kind == "xlsx-publish":
            cmd = [py, str(self.scripts_dir / "pm_minutes_publish.py"), "--xlsx-only"]
            if params.get("no_encrypt"):
                cmd += ["--no-encrypt"]
            return cmd

        raise ValueError(f"Unknown job kind: {kind}")

    # ---- Job execution ---- #

    async def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return

        self._update_job(job_id, status="running",
                         started_at=datetime.now(UTC).isoformat())

        exit_code = None
        summary = ""
        status = "error"
        try:
            cmd = self._build_command(job["kind"], job["params"])
            log_path = job["log_file"]

            with open(log_path, "w") as log_f:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=log_f,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self.repo_root),
                )
                await proc.wait()
                exit_code = proc.returncode

            if exit_code == 0:
                summary = "Completed successfully"
                status = "success"
            else:
                summary = f"Exit code: {exit_code}"
                status = "error"

        except Exception as e:
            summary = f"Error: {e}"
            status = "error"

        try:
            self._update_job(
                job_id,
                status=status,
                finished_at=datetime.now(UTC).isoformat(),
                exit_code=exit_code,
                summary=summary,
                progress=100 if status == "success" else 0,
            )
        except Exception:
            # If status update itself fails, log to stderr
            import traceback
            traceback.print_exc()

        finally:
            self._running_jobs.pop(job_id, None)


# --------------------------------------------------------------------------- #
# サービス管理
# --------------------------------------------------------------------------- #

_SERVICE_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 5.0  # seconds


async def get_service_status(name: str) -> dict:
    """pm_daemon.sh status <name> を実行しパースした結果を返す。"""
    now = time.monotonic()
    cached = _SERVICE_STATUS_CACHE.get(name)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    script = _DAEMON_SCRIPT
    if not script.exists():
        result = {"name": name, "status": "unknown", "pid": "-",
                  "log_file": str(_SERVICE_LOG_FILES.get(name, "")),
                  "running": False, "error": "pm_daemon.sh not found"}
        _SERVICE_STATUS_CACHE[name] = (now, result)
        return result

    proc = await asyncio.create_subprocess_exec(
        "bash", str(script), "status", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip()
    error = stderr.decode().strip()

    result: dict = {"name": name, "running": False, "pid": "-",
                    "log_file": str(_SERVICE_LOG_FILES.get(name, "")),
                    "raw_output": output}

    if error:
        result["error"] = error

    # Parse: "NAME   STATUS     PID     LOG"
    lines = output.split("\n")
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 3:
            result["status"] = parts[1]
            result["pid"] = parts[2]
            result["running"] = parts[1] == "running"
            if len(parts) >= 4:
                result["log_file"] = parts[3]

    _SERVICE_STATUS_CACHE[name] = (time.monotonic(), result)
    return result


async def get_all_services() -> list[dict]:
    """全サービス (qa/web) の状態を返す。"""
    results = []
    for name in ["qa", "web"]:
        results.append(await get_service_status(name))
    return results


async def service_action(name: str, action: str) -> dict:
    """pm_daemon.sh で start/stop を実行する。"""
    script = _DAEMON_SCRIPT
    if not script.exists():
        return {"success": False, "error": "pm_daemon.sh not found"}

    proc = await asyncio.create_subprocess_exec(
        "bash", str(script), action, name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    # キャッシュをクリア
    _SERVICE_STATUS_CACHE.pop(name, None)

    return {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": stdout.decode().strip(),
        "error": stderr.decode().strip(),
    }


def tail_log(log_path: str | Path, lines: int = 100) -> dict:
    """ログファイルの末尾 N 行を読み取る。"""
    p = Path(log_path) if isinstance(log_path, str) else log_path
    if not p.exists():
        return {"lines": [], "total_lines": 0, "error": "Log file not found"}

    try:
        with open(p) as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        return {
            "lines": tail,
            "total_lines": len(all_lines),
            "file": str(p),
        }
    except Exception as e:
        return {"lines": [], "total_lines": 0, "error": str(e)}


def scan_recent_errors(max_lines: int = 200) -> list[dict]:
    """直近のログファイルから ERROR/WARNING 行をスキャンする。"""
    errors = []
    log_dir = _LOG_DIR
    if not log_dir.exists():
        return errors

    # 主要なログファイルをチェック
    targets = [
        "pm_qa_server.log",
        "pm_web.log",
        "pm_from_recording_auto.log",
        "pm_box_update.log",
    ]

    for fname in targets:
        fpath = log_dir / fname
        if not fpath.exists():
            continue
        try:
            with open(fpath) as f:
                all_lines = f.readlines()
            # 末尾から max_lines 行をチェック
            for i, line in enumerate(all_lines[-max_lines:]):
                line_upper = line.upper()
                if "ERROR" in line_upper or "WARNING" in line_upper or "TRACEBACK" in line_upper or "EXCEPTION" in line_upper:
                    errors.append({
                        "file": fname,
                        "line": line.rstrip("\n"),
                        "lineno": len(all_lines) - max_lines + i + 1,
                    })
        except Exception:
            continue

    return errors[:50]  # 最大50行


# --------------------------------------------------------------------------- #
# ダッシュボード統計
# --------------------------------------------------------------------------- #

def get_dashboard_stats(conn) -> dict:
    """pm.db からダッシュボード表示用の統計を集計する。"""
    stats: dict[str, Any] = {}

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM action_items WHERE deleted=0 AND status='open'"
        ).fetchone()
        stats["open_action_items"] = row[0] if row else 0
    except Exception:
        stats["open_action_items"] = 0

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM decisions WHERE deleted=0"
            " AND (acknowledged_at IS NULL OR acknowledged_at='')"
        ).fetchone()
        stats["unacknowledged_decisions"] = row[0] if row else 0
    except Exception:
        stats["unacknowledged_decisions"] = 0

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM milestones WHERE archived=0 OR archived IS NULL"
        ).fetchone()
        stats["active_milestones"] = row[0] if row else 0
    except Exception:
        stats["active_milestones"] = 0

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM action_items WHERE deleted=0 AND status='open'"
            " AND due_date IS NOT NULL AND due_date != ''"
            " AND due_date < date('now')"
        ).fetchone()
        stats["overdue_items"] = row[0] if row else 0
    except Exception:
        stats["overdue_items"] = 0

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM action_items WHERE deleted=0"
        ).fetchone()
        stats["total_action_items"] = row[0] if row else 0
    except Exception:
        stats["total_action_items"] = 0

    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM decisions WHERE deleted=0"
        ).fetchone()
        stats["total_decisions"] = row[0] if row else 0
    except Exception:
        stats["total_decisions"] = 0

    return stats


def get_recent_minutes(repo_root: Path | None = None, no_encrypt: bool = False) -> list[dict]:
    """minutes DB から最新会議一覧を取得する。"""
    root = repo_root or _REPO
    minutes_dir = root / "data" / "minutes"
    if not minutes_dir.exists():
        return []

    results = []
    for db_file in sorted(minutes_dir.glob("*.db")):
        kind = db_file.stem
        try:
            conn = open_db(str(db_file), encrypt=not no_encrypt)
            rows = conn.execute(
                "SELECT meeting_id, held_at, imported_at, kind"
                " FROM instances ORDER BY held_at DESC LIMIT 5"
            ).fetchall()
            conn.close()
            for row in rows:
                results.append({
                    "id": row[0],
                    "kind": kind,
                    "meeting_name": row[3] or kind,
                    "held_at": row[1],
                    "created_at": row[2],
                    "title": row[0],
                })
        except Exception:
            continue

    results.sort(key=lambda x: x.get("held_at", "") or "", reverse=True)
    return results[:20]


def list_minutes(repo_root: Path | None = None, no_encrypt: bool = False) -> list[dict]:
    """全 minutes DB から全会議インスタンスを一覧する。"""
    root = repo_root or _REPO
    minutes_dir = root / "data" / "minutes"
    if not minutes_dir.exists():
        return []

    results = []
    for db_file in sorted(minutes_dir.glob("*.db")):
        kind = db_file.stem
        try:
            conn = open_db(str(db_file), encrypt=not no_encrypt)
            rows = conn.execute(
                "SELECT meeting_id, held_at, imported_at, kind, file_path,"
                " slack_channel_id, slack_thread_ts, slack_file_permalink"
                " FROM instances ORDER BY held_at DESC"
            ).fetchall()
            conn.close()
            for row in rows:
                results.append({
                    "id": row[0],
                    "kind": kind,
                    "meeting_name": row[3] or kind,
                    "held_at": row[1],
                    "imported_at": row[2],
                    "file_path": row[4],
                    "slack_channel_id": row[5],
                    "slack_thread_ts": row[6],
                    "slack_file_permalink": row[7],
                })
        except Exception:
            continue

    results.sort(key=lambda x: x.get("held_at", "") or "", reverse=True)
    return results


def get_minutes_content(meeting_id: str, kind: str,
                        no_encrypt: bool = False, repo_root: Path | None = None) -> str | None:
    """minutes DB から議事内容を取得する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    if not db_path.exists():
        return None
    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        row = conn.execute(
            "SELECT content FROM minutes_content WHERE meeting_id=? ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def delete_minutes_instance(meeting_id: str, kind: str,
                            no_encrypt: bool = False,
                            repo_root: Path | None = None) -> dict:
    """minutes DB から会議インスタンスを削除する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    result = {"deleted": False, "instances": 0, "content": 0,
              "decisions": 0, "action_items": 0, "upload_log": 0,
              "error": None}

    if not db_path.exists():
        result["error"] = f"Minutes DB not found: {db_path}"
        return result

    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        cur = conn.execute("DELETE FROM minutes_content WHERE meeting_id=?", (meeting_id,))
        result["content"] = cur.rowcount
        cur = conn.execute("DELETE FROM decisions WHERE meeting_id=?", (meeting_id,))
        result["decisions"] = cur.rowcount
        cur = conn.execute("DELETE FROM action_items WHERE meeting_id=?", (meeting_id,))
        result["action_items"] = cur.rowcount
        # upload_log は catalog 済み DB にしか存在しない
        has_upload_log = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='upload_log'"
        ).fetchone() is not None
        if has_upload_log:
            cur = conn.execute("DELETE FROM upload_log WHERE meeting_id=?", (meeting_id,))
            result["upload_log"] = cur.rowcount
        cur = conn.execute("DELETE FROM instances WHERE meeting_id=?", (meeting_id,))
        result["instances"] = cur.rowcount
        conn.commit()
        conn.close()
        result["deleted"] = result["instances"] > 0
    except Exception as e:
        result["error"] = str(e)

    return result


def delete_minutes_from_pm(meeting_id: str, repo_root: Path | None = None,
                           pm_conn=None, no_encrypt: bool = False) -> dict:
    """pm.db から meeting_id に関連するレコードを削除する。"""
    result = {"meetings": 0, "decisions": 0, "action_items": 0}
    close_conn = False
    try:
        if pm_conn is None:
            root = repo_root or _REPO
            pm_conn = open_db(str(root / "data" / "pm.db"), encrypt=not no_encrypt)
            close_conn = True
        cur = pm_conn.execute("DELETE FROM action_items WHERE meeting_id=?", (meeting_id,))
        result["action_items"] = cur.rowcount
        cur = pm_conn.execute("DELETE FROM decisions WHERE meeting_id=?", (meeting_id,))
        result["decisions"] = cur.rowcount
        cur = pm_conn.execute("DELETE FROM meetings WHERE meeting_id=?", (meeting_id,))
        result["meetings"] = cur.rowcount
        pm_conn.commit()
    except Exception as e:
        result["error"] = str(e)
    finally:
        if close_conn and pm_conn:
            pm_conn.close()
    return result


def get_minutes_held_at(meeting_id: str, kind: str,
                         no_encrypt: bool = False,
                         repo_root: Path | None = None) -> str | None:
    """minutes.db から meeting_id に対応する held_at を取得する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    if not db_path.exists():
        return None
    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        row = conn.execute(
            "SELECT held_at FROM instances WHERE meeting_id=?", (meeting_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def update_minutes_content(meeting_id: str, kind: str, content: str,
                           no_encrypt: bool = False,
                           repo_root: Path | None = None) -> dict:
    """minutes DB の議事内容を更新する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    result = {"updated": False, "error": None}

    if not db_path.exists():
        result["error"] = f"Minutes DB not found: {db_path}"
        return result

    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        existing = conn.execute(
            "SELECT id FROM minutes_content WHERE meeting_id=? ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE minutes_content SET content=? WHERE id=?",
                (content, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO minutes_content (meeting_id, content) VALUES (?,?)",
                (meeting_id, content),
            )
        conn.commit()
        conn.close()
        result["updated"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


# --- Minutes decisions & action items helpers --- #

def get_minutes_decisions(meeting_id: str, kind: str,
                          no_encrypt: bool = False,
                          repo_root: Path | None = None) -> list[dict]:
    """minutes DB から決定事項一覧を取得する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    if not db_path.exists():
        return []
    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        rows = conn.execute(
            "SELECT id, content, source_context FROM decisions"
            " WHERE meeting_id=? ORDER BY id", (meeting_id,)
        ).fetchall()
        conn.close()
        return [{"id": r[0], "content": r[1], "source_context": r[2]} for r in rows]
    except Exception:
        return []


def get_minutes_action_items(meeting_id: str, kind: str,
                             no_encrypt: bool = False,
                             repo_root: Path | None = None) -> list[dict]:
    """minutes DB からアクションアイテム一覧を取得する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    if not db_path.exists():
        return []
    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        rows = conn.execute(
            "SELECT id, content, assignee, due_date FROM action_items"
            " WHERE meeting_id=? ORDER BY id", (meeting_id,)
        ).fetchall()
        conn.close()
        return [{"id": r[0], "content": r[1], "assignee": r[2], "due_date": r[3]} for r in rows]
    except Exception:
        return []


def update_minutes_decisions(meeting_id: str, kind: str,
                             decisions: list[dict],
                             no_encrypt: bool = False,
                             repo_root: Path | None = None) -> dict:
    """minutes DB の決定事項を全置換する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    result = {"updated": 0, "error": None}

    if not db_path.exists():
        result["error"] = f"Minutes DB not found: {db_path}"
        return result

    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        conn.execute("DELETE FROM decisions WHERE meeting_id=?", (meeting_id,))
        for d in decisions:
            content = (d.get("content") or "").strip()
            if not content:
                continue
            conn.execute(
                "INSERT INTO decisions (meeting_id, content, source_context) VALUES (?,?,?)",
                (meeting_id, content, d.get("source_context")),
            )
            result["updated"] += 1
        conn.commit()
        conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def update_minutes_action_items(meeting_id: str, kind: str,
                                action_items: list[dict],
                                no_encrypt: bool = False,
                                repo_root: Path | None = None) -> dict:
    """minutes DB のアクションアイテムを全置換する。"""
    root = repo_root or _REPO
    db_path = root / "data" / "minutes" / f"{kind}.db"
    result = {"updated": 0, "error": None}

    if not db_path.exists():
        result["error"] = f"Minutes DB not found: {db_path}"
        return result

    try:
        conn = open_db(str(db_path), encrypt=not no_encrypt)
        conn.execute("DELETE FROM action_items WHERE meeting_id=?", (meeting_id,))
        for a in action_items:
            content = (a.get("content") or "").strip()
            if not content:
                continue
            conn.execute(
                "INSERT INTO action_items (meeting_id, content, assignee, due_date) VALUES (?,?,?,?)",
                (meeting_id, content, a.get("assignee"), a.get("due_date")),
            )
            result["updated"] += 1
        conn.commit()
        conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result
