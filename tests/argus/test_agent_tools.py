"""Tests for pm_argus_agent tool functions (fixture pm.db)."""
import sqlite3
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Helper: insert test data into pm.db
# --------------------------------------------------------------------------- #

def _insert_milestone(conn, ms_id: str, name: str, due_date: str, status: str = "active"):
    conn.execute(
        "INSERT OR IGNORE INTO meetings (meeting_id, held_at, kind) VALUES (?,?,?)",
        ("meeting-1", "2026-06-01", "test"),
    )
    conn.execute(
        "INSERT INTO milestones (milestone_id, name, due_date, status)"
        " VALUES (?,?,?,?)",
        (ms_id, name, due_date, status),
    )
    conn.commit()


def _insert_action_item(conn, content: str, assignee: str, due_date: str,
                        status: str = "open", milestone_id: str | None = None):
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, status, milestone_id, extracted_at)"
        " VALUES (?,?,?,?,?,?)",
        (content, assignee, due_date, status, milestone_id, "2026-06-01"),
    )
    conn.commit()


def _insert_decision(conn, content: str, decided_at: str, acknowledged_at: str | None = None):
    conn.execute(
        "INSERT INTO decisions (content, decided_at, source, extracted_at, acknowledged_at)"
        " VALUES (?,?,?,?,?)",
        (content, decided_at, "meeting", "2026-06-01", acknowledged_at),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# _tool_get_milestone_progress
# --------------------------------------------------------------------------- #

class TestToolGetMilestoneProgress:
    def test_no_milestones_returns_message(self, agent_context):
        from argus.agent_tools import _tool_get_milestone_progress
        result = _tool_get_milestone_progress({}, agent_context)
        assert "登録されていません" in result

    def test_with_milestone_returns_table(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_milestone(conn, "MS-01", "システム設計完了", "2026-09-30")
        _insert_action_item(conn, "設計書作成", "山田", "2026-08-01", milestone_id="MS-01")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_get_milestone_progress
        result = _tool_get_milestone_progress({}, agent_context)
        assert "MS-01" in result or "システム設計" in result


# --------------------------------------------------------------------------- #
# _tool_get_overdue_items
# --------------------------------------------------------------------------- #

class TestToolGetOverdueItems:
    def test_no_overdue_returns_message(self, agent_context):
        from argus.agent_tools import _tool_get_overdue_items
        result = _tool_get_overdue_items({}, agent_context)
        assert "なし" in result or "該当" in result

    def test_overdue_item_appears(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_action_item(conn, "期限超過タスク", "鈴木", "2026-01-01", status="open")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_get_overdue_items
        result = _tool_get_overdue_items({}, agent_context)
        # today=2026-06-19 に対して due_date=2026-01-01 は超過
        assert "期限超過タスク" in result or "鈴木" in result


# --------------------------------------------------------------------------- #
# _tool_get_assignee_workload
# --------------------------------------------------------------------------- #

class TestToolGetAssigneeWorkload:
    def test_no_data_returns_message(self, agent_context):
        from argus.agent_tools import _tool_get_assignee_workload
        result = _tool_get_assignee_workload({}, agent_context)
        assert "なし" in result or "担当者" in result

    def test_workload_with_items(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        for i in range(3):
            _insert_action_item(conn, f"タスク{i}", "田中", "2026-12-01", status="open")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_get_assignee_workload
        result = _tool_get_assignee_workload({}, agent_context)
        assert "田中" in result


# --------------------------------------------------------------------------- #
# _tool_get_unacknowledged_decisions
# --------------------------------------------------------------------------- #

class TestToolGetUnacknowledgedDecisions:
    def test_no_decisions_returns_message(self, agent_context):
        from argus.agent_tools import _tool_get_unacknowledged_decisions
        result = _tool_get_unacknowledged_decisions({}, agent_context)
        assert "なし" in result

    def test_unacknowledged_decision_appears(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_decision(conn, "予算を増額することを決定した", "2026-06-01", acknowledged_at=None)
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_get_unacknowledged_decisions
        result = _tool_get_unacknowledged_decisions({}, agent_context)
        assert "予算" in result or "増額" in result

    def test_acknowledged_decision_excluded(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_decision(conn, "確認済み決定", "2026-06-01", acknowledged_at="2026-06-02")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_get_unacknowledged_decisions
        result = _tool_get_unacknowledged_decisions({}, agent_context)
        assert "なし" in result


# --------------------------------------------------------------------------- #
# _tool_search_action_items
# --------------------------------------------------------------------------- #

class TestToolSearchActionItems:
    def test_no_items_returns_message(self, agent_context):
        from argus.agent_tools import _tool_search_action_items
        result = _tool_search_action_items({"keyword": "存在しないキーワード"}, agent_context)
        assert "なし" in result

    def test_keyword_search(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_action_item(conn, "ネットワーク構成を見直す", "佐藤", "2026-12-01")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_search_action_items
        result = _tool_search_action_items({"keyword": "ネットワーク"}, agent_context)
        assert "ネットワーク" in result

    def test_assignee_filter(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_action_item(conn, "タスクA", "山田", "2026-12-01")
        _insert_action_item(conn, "タスクB", "鈴木", "2026-12-01")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_search_action_items
        result = _tool_search_action_items({"assignee": "山田"}, agent_context)
        assert "山田" in result
        assert "鈴木" not in result


# --------------------------------------------------------------------------- #
# _tool_search_decisions
# --------------------------------------------------------------------------- #

class TestToolSearchDecisions:
    def test_no_decisions_returns_message(self, agent_context):
        from argus.agent_tools import _tool_search_decisions
        result = _tool_search_decisions({"keyword": "存在しないキーワード"}, agent_context)
        assert "なし" in result

    def test_keyword_search(self, agent_context, pm_db_path):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_decision(conn, "クラウドへの移行を承認した", "2026-06-01")
        conn.close()

        agent_context.conns[0].close()
        new_conn = sqlite3.connect(str(pm_db_path))
        new_conn.row_factory = sqlite3.Row
        agent_context.conns[0] = new_conn

        from argus.agent_tools import _tool_search_decisions
        result = _tool_search_decisions({"keyword": "クラウド"}, agent_context)
        assert "クラウド" in result
