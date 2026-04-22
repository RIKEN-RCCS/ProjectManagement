#!/usr/bin/env python3
"""
ingest_goals.py

goals.yaml → pm.db の goals/milestones テーブルに完全同期するプラグイン。
元ロジックは pm_goals_import.py から移植。pm_goals_import.py は後方互換 CLI ラッパーとして残す。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, date
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML が必要です: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from ingest_plugin import IngestContext


# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOALS_FILE = REPO_ROOT / "goals.yaml"

GOALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id     TEXT PRIMARY KEY,
    name        TEXT,
    description TEXT,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
    milestone_id     TEXT PRIMARY KEY,
    goal_id          TEXT,
    name             TEXT,
    due_date         TEXT,
    area             TEXT,
    status           TEXT DEFAULT 'active',
    success_criteria TEXT,
    imported_at      TEXT
);
"""


# --------------------------------------------------------------------------- #
# スキーマ適用
# --------------------------------------------------------------------------- #
def ensure_goals_schema(pm_conn) -> None:
    for stmt in GOALS_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                pm_conn.execute(stmt)
            except Exception:
                pass
    try:
        pm_conn.execute("ALTER TABLE action_items ADD COLUMN milestone_id TEXT")
    except Exception:
        pass
    pm_conn.commit()


# --------------------------------------------------------------------------- #
# YAML 読み込み
# --------------------------------------------------------------------------- #
def load_goals_yaml(goals_file: Path) -> dict:
    with open(goals_file, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# 一覧表示
# --------------------------------------------------------------------------- #
def list_registered(db_path: Path, no_encrypt: bool, log=print) -> None:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(db_path, encrypt=not no_encrypt)
    today = date.today().isoformat()

    goals = conn.execute("SELECT * FROM goals ORDER BY goal_id").fetchall()
    milestones = conn.execute(
        """
        SELECT m.*,
               COUNT(DISTINCT CASE WHEN a.status='open'   THEN a.id END) AS open_count,
               COUNT(DISTINCT CASE WHEN a.status='closed' THEN a.id END) AS closed_count
        FROM milestones m
        LEFT JOIN action_items a ON a.milestone_id = m.milestone_id
        GROUP BY m.milestone_id
        ORDER BY m.due_date ASC NULLS LAST
        """
    ).fetchall()
    conn.close()

    if not goals:
        log("登録済みゴールはありません。pm_goals_import.py を実行してください。")
        return

    for g in goals:
        log(f"\n[{g['goal_id']}] {g['name']}")
        if g["description"]:
            log(f"     {g['description'][:80].strip()}")

    log(f"\n{'ID':<4} {'マイルストーン':<30} {'期限':<12} {'状況':<8} {'完了/計':<8}  エリア")
    log("-" * 90)
    for m in milestones:
        mid      = m["milestone_id"]
        name     = (m["name"] or "")[:28]
        due      = m["due_date"] or "未定"
        open_c   = m["open_count"]
        closed_c = m["closed_count"]
        total    = open_c + closed_c
        ratio    = f"{closed_c}/{total}" if total else "-/-"
        area     = (m["area"] or "")[:20]

        if m["status"] == "achieved":
            mark = "達成済"
        elif not m["due_date"]:
            mark = "未着手"
        elif m["due_date"] < today:
            mark = "遅延"
        elif total == 0:
            mark = "未着手"
        else:
            mark = "進行中"

        log(f"{mid:<4} {name:<30} {due:<12} {mark:<8} {ratio:<8}  {area}")


# --------------------------------------------------------------------------- #
# 同期コア
# --------------------------------------------------------------------------- #
def sync_goals(
    pm_conn,
    goals_file: Path,
    dry_run: bool,
    log=print,
) -> None:
    data = load_goals_yaml(goals_file)
    goals = data.get("goals", [])
    milestones = data.get("milestones", [])

    log(f"[INFO] goals.yaml   : {goals_file}")
    log(f"[INFO] ゴール       : {len(goals)} 件")
    log(f"[INFO] マイルストーン: {len(milestones)} 件")

    yaml_goal_ids = {g["id"] for g in goals}
    yaml_ms_ids   = {m["id"] for m in milestones}

    db_goal_ids = {r[0] for r in pm_conn.execute("SELECT goal_id FROM goals").fetchall()}
    db_ms_ids   = {r[0] for r in pm_conn.execute("SELECT milestone_id FROM milestones").fetchall()}
    obsolete_goals = db_goal_ids - yaml_goal_ids
    obsolete_ms    = db_ms_ids   - yaml_ms_ids

    if dry_run:
        log("\n-- ゴール（追加/更新）--")
        for g in goals:
            log(f"  [{g['id']}] {g['name']}")
        log("\n-- マイルストーン（追加/更新）--")
        for m in milestones:
            log(f"  [{m['id']}] {m['name']}  期限: {m.get('due_date', '未定')}  エリア: {m.get('area', '')}")
        if obsolete_goals:
            log("\n-- ゴール（削除予定）--")
            for gid in obsolete_goals:
                log(f"  [{gid}] DBから削除されます")
        if obsolete_ms:
            log("\n-- マイルストーン（削除予定）--")
            for mid in obsolete_ms:
                log(f"  [{mid}] DBから削除されます（紐づいた action_items の milestone_id は NULL になります）")
        log("\n[INFO] --dry-run のためDB保存をスキップしました")
        return

    now = datetime.now().isoformat()

    for g in goals:
        pm_conn.execute(
            "INSERT OR REPLACE INTO goals (goal_id, name, description, imported_at) VALUES (?, ?, ?, ?)",
            (g["id"], g["name"], g.get("description", ""), now),
        )
        log(f"  [ゴール] {g['id']}: {g['name']}")

    for m in milestones:
        criteria_json = json.dumps(m.get("success_criteria", []), ensure_ascii=False)
        pm_conn.execute(
            "INSERT OR REPLACE INTO milestones"
            " (milestone_id, goal_id, name, due_date, area, status, success_criteria, imported_at)"
            " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (m["id"], m.get("goal_id", ""), m["name"], m.get("due_date"),
             m.get("area", ""), criteria_json, now),
        )
        log(f"  [MS] {m['id']}: {m['name']}  期限: {m.get('due_date', '未定')}")

    for gid in obsolete_goals:
        pm_conn.execute("DELETE FROM goals WHERE goal_id = ?", (gid,))
        log(f"  [削除] ゴール {gid}")
    for mid in obsolete_ms:
        pm_conn.execute("UPDATE action_items SET milestone_id = NULL WHERE milestone_id = ?", (mid,))
        pm_conn.execute("DELETE FROM milestones WHERE milestone_id = ?", (mid,))
        log(f"  [削除] マイルストーン {mid}（紐づき action_items の milestone_id を NULL に更新）")

    pm_conn.commit()
    log(f"\n✓ pm.db に同期完了")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class GoalsIngestPlugin:
    source_name = "goals"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--goals-file", default=None,
            metavar="PATH",
            help="goals.yaml のパス（goals ソース用、デフォルト: goals.yaml）",
        )
        parser.add_argument(
            "--goals-list", action="store_true",
            help="登録済みゴール・マイルストーン一覧を表示して終了（goals ソース用）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        if getattr(args, "goals_list", False):
            list_registered(ctx.pm_db_path, ctx.no_encrypt, log=ctx.log)
            return

        goals_file = (
            Path(args.goals_file) if getattr(args, "goals_file", None)
            else DEFAULT_GOALS_FILE
        )
        if not goals_file.exists():
            print(f"ERROR: goals.yaml が見つかりません: {goals_file}", file=sys.stderr)
            sys.exit(1)

        ensure_goals_schema(ctx.pm_conn)
        sync_goals(ctx.pm_conn, goals_file, ctx.dry_run, log=ctx.log)
