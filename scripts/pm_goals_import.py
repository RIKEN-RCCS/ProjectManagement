#!/usr/bin/env python3
"""
pm_goals_import.py

goals.yaml を読み込み、pm.db の goals / milestones テーブルに保存する。
再実行時は差分のみ更新（upsert）。

【運用ルール】
  goals.yaml を編集・承認した後、本スクリプトを実行して pm.db に反映する。
  LLM による自動編集は行わない。

Usage:
    python3 scripts/pm_goals_import.py
    python3 scripts/pm_goals_import.py --goals-file goals.yaml
    python3 scripts/pm_goals_import.py --dry-run
    python3 scripts/pm_goals_import.py --list

Options:
    --goals-file PATH   goals.yaml のパス（デフォルト: goals.yaml）
    --db PATH           pm.db のパス（デフォルト: data/pm.db）
    --dry-run           DB保存なし・内容を表示のみ
    --list              pm.db に登録済みのゴール・マイルストーン一覧を表示
    --no-encrypt        DBを暗号化しない（平文モード）
"""

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOALS_FILE = REPO_ROOT / "goals.yaml"
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"

SCHEMA = """
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


def open_db_with_schema(db_path: Path, no_encrypt: bool):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(
        db_path,
        encrypt=not no_encrypt,
        schema=SCHEMA,
        migrations=[
            "ALTER TABLE action_items ADD COLUMN milestone_id TEXT",
        ],
    )


def load_goals_yaml(goals_file: Path) -> dict:
    with open(goals_file, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_registered(db_path: Path, no_encrypt: bool) -> None:
    """pm.db に登録済みのゴール・マイルストーン一覧を表示する"""
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
        print("登録済みゴールはありません。pm_goals_import.py を実行してください。")
        return

    for g in goals:
        print(f"\n[{g['goal_id']}] {g['name']}")
        if g["description"]:
            print(f"     {g['description'][:80].strip()}")

    print(f"\n{'ID':<4} {'マイルストーン':<30} {'期限':<12} {'状況':<8} {'完了/計':<8}  エリア")
    print("-" * 90)
    for m in milestones:
        mid        = m["milestone_id"]
        name       = (m["name"] or "")[:28]
        due        = m["due_date"] or "未定"
        open_c     = m["open_count"]
        closed_c   = m["closed_count"]
        total      = open_c + closed_c
        ratio      = f"{closed_c}/{total}" if total else "-/-"
        area       = (m["area"] or "")[:20]

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

        print(f"{mid:<4} {name:<30} {due:<12} {mark:<8} {ratio:<8}  {area}")


def main() -> None:
    parser = argparse.ArgumentParser(description="goals.yaml を pm.db に読み込む")
    parser.add_argument("--goals-file", default=None, help="goals.yaml のパス")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--dry-run", action="store_true", help="DB保存なし・内容表示のみ")
    parser.add_argument("--list", action="store_true", help="登録済み一覧を表示して終了")
    parser.add_argument("--no-encrypt", action="store_true", help="DBを暗号化しない（平文モード）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB

    if args.list:
        list_registered(db_path, args.no_encrypt)
        return

    goals_file = Path(args.goals_file) if args.goals_file else DEFAULT_GOALS_FILE
    if not goals_file.exists():
        print(f"ERROR: goals.yaml が見つかりません: {goals_file}", file=sys.stderr)
        sys.exit(1)

    data = load_goals_yaml(goals_file)
    goals = data.get("goals", [])
    milestones = data.get("milestones", [])

    print(f"[INFO] goals.yaml   : {goals_file}")
    print(f"[INFO] pm.db        : {db_path}")
    print(f"[INFO] ゴール       : {len(goals)} 件")
    print(f"[INFO] マイルストーン: {len(milestones)} 件")

    if args.dry_run:
        print("\n-- ゴール --")
        for g in goals:
            print(f"  [{g['id']}] {g['name']}")
        print("\n-- マイルストーン --")
        for m in milestones:
            print(f"  [{m['id']}] {m['name']}  期限: {m.get('due_date', '未定')}  エリア: {m.get('area', '')}")
        print("\n[INFO] --dry-run のためDB保存をスキップしました")
        return

    conn = open_db_with_schema(db_path, args.no_encrypt)
    now = datetime.now().isoformat()

    for g in goals:
        conn.execute(
            """
            INSERT OR REPLACE INTO goals (goal_id, name, description, imported_at)
            VALUES (?, ?, ?, ?)
            """,
            (g["id"], g["name"], g.get("description", ""), now),
        )
        print(f"  [ゴール] {g['id']}: {g['name']}")

    for m in milestones:
        criteria_json = json.dumps(m.get("success_criteria", []), ensure_ascii=False)
        conn.execute(
            """
            INSERT OR REPLACE INTO milestones
                (milestone_id, goal_id, name, due_date, area, status, success_criteria, imported_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                m["id"],
                m.get("goal_id", ""),
                m["name"],
                m.get("due_date"),
                m.get("area", ""),
                criteria_json,
                now,
            ),
        )
        print(f"  [MS] {m['id']}: {m['name']}  期限: {m.get('due_date', '未定')}")

    conn.commit()
    conn.close()
    print(f"\n✓ pm.db に保存完了: {db_path}")
    print("  action_items.milestone_id カラムも追加済み（既存データへの影響なし）")


if __name__ == "__main__":
    main()
