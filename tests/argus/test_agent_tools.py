"""Tests for agent_tools tool functions (mcp_tools mocked).

All MCP-delegated functions (_call_mcp targets) are tested by mocking
argus.mcp_tools module-level functions via monkeypatch.
"""
import pytest
from unittest.mock import MagicMock


# --------------------------------------------------------------------------- #
# Helper: inject a mock function into argus.mcp_tools
# --------------------------------------------------------------------------- #

def _mock_mcp(monkeypatch, fn_name: str, return_value: str) -> MagicMock:
    """Inject a MagicMock into argus.mcp_tools so _call_mcp resolves it."""
    import argus.mcp_tools
    mock = MagicMock(return_value=return_value)
    monkeypatch.setattr(argus.mcp_tools, fn_name, mock)
    return mock


# --------------------------------------------------------------------------- #
# get_milestone_progress
# --------------------------------------------------------------------------- #

class TestToolMilestoneProgress:
    def test_no_milestones_returns_message(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_milestone_progress",
                         "マイルストーンは登録されていません")
        fn = _call_mcp("get_milestone_progress")
        result = fn({}, agent_context)
        assert "登録されていません" in result
        mock.assert_called_once_with(since=agent_context.since)

    def test_with_milestone_returns_table(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_milestone_progress",
                         "MS-01  システム設計完了  2026-09-30")
        fn = _call_mcp("get_milestone_progress")
        result = fn({}, agent_context)
        assert "MS-01" in result
        mock.assert_called_once()


# --------------------------------------------------------------------------- #
# get_overdue_items
# --------------------------------------------------------------------------- #

class TestToolOverdueItems:
    def test_no_overdue_returns_message(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_overdue_items",
                         "該当する期限超過アイテムはありません")
        fn = _call_mcp("get_overdue_items")
        result = fn({}, agent_context)
        assert "ありません" in result
        mock.assert_called_once_with(since=agent_context.since)

    def test_overdue_item_appears(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_overdue_items",
                         "鈴木  期限超過タスク  2026-01-01")
        fn = _call_mcp("get_overdue_items")
        result = fn({"assignee": "鈴木", "limit": 5}, agent_context)
        assert "鈴木" in result
        mock.assert_called_once_with(assignee="鈴木", limit=5, since=agent_context.since)


# --------------------------------------------------------------------------- #
# get_assignee_workload
# --------------------------------------------------------------------------- #

class TestToolAssigneeWorkload:
    def test_no_data_returns_message(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_assignee_workload",
                         "データがありません")
        fn = _call_mcp("get_assignee_workload")
        result = fn({}, agent_context)
        assert "ありません" in result
        mock.assert_called_once_with(since=agent_context.since)

    def test_workload_with_items(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "get_assignee_workload",
                         "田中  3 件")
        fn = _call_mcp("get_assignee_workload")
        result = fn({}, agent_context)
        assert "田中" in result



# --------------------------------------------------------------------------- #
# search_action_items
# --------------------------------------------------------------------------- #

class TestToolSearchActionItems:
    def test_no_items_returns_message(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "search_action_items",
                         "該当するアクションアイテムはありません")
        fn = _call_mcp("search_action_items")
        result = fn({"keyword": "存在しないキーワード"}, agent_context)
        assert "ありません" in result
        mock.assert_called_once_with(keyword="存在しないキーワード",
                                     since=agent_context.since)

    def test_keyword_search(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "search_action_items",
                         "ネットワーク構成を見直す")
        fn = _call_mcp("search_action_items")
        result = fn({"keyword": "ネットワーク"}, agent_context)
        assert "ネットワーク" in result
        mock.assert_called_once_with(keyword="ネットワーク",
                                     since=agent_context.since)

    def test_assignee_filter(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "search_action_items",
                         "山田  タスクA\n鈴木  タスクB")
        fn = _call_mcp("search_action_items")
        result = fn({"assignee": "山田"}, agent_context)
        assert "山田" in result
        mock.assert_called_once_with(assignee="山田", since=agent_context.since)


# --------------------------------------------------------------------------- #
# search_decisions
# --------------------------------------------------------------------------- #

class TestToolSearchDecisions:
    def test_no_decisions_returns_message(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "search_decisions",
                         "該当する決定事項はありません")
        fn = _call_mcp("search_decisions")
        result = fn({"keyword": "存在しないキーワード"}, agent_context)
        assert "ありません" in result
        mock.assert_called_once_with(keyword="存在しないキーワード",
                                     since=agent_context.since)

    def test_keyword_search(self, agent_context, monkeypatch):
        from argus.agent_tools import _call_mcp
        mock = _mock_mcp(monkeypatch, "search_decisions",
                         "クラウドへの移行を承認")
        fn = _call_mcp("search_decisions")
        result = fn({"keyword": "クラウド"}, agent_context)
        assert "クラウド" in result
        mock.assert_called_once_with(keyword="クラウド",
                                     since=agent_context.since)


# --------------------------------------------------------------------------- #
# mcp_tools._excerpt — ARGUS_SEARCH_EXCERPT_CHARS 環境変数オーバーライド
# --------------------------------------------------------------------------- #

class TestExcerptEnvOverride:
    def test_default_1200_chars(self, monkeypatch):
        import argus.mcp_tools as mt
        monkeypatch.delenv("ARGUS_SEARCH_EXCERPT_CHARS", raising=False)
        content = "あ" * 1500
        assert len(mt._excerpt(content)) == 1200

    def test_overridden_length(self, monkeypatch):
        import argus.mcp_tools as mt
        monkeypatch.setenv("ARGUS_SEARCH_EXCERPT_CHARS", "1200")
        content = "あ" * 1500
        assert len(mt._excerpt(content)) == 1200

    def test_invalid_value_falls_back_to_1200(self, monkeypatch):
        import argus.mcp_tools as mt
        monkeypatch.setenv("ARGUS_SEARCH_EXCERPT_CHARS", "invalid")
        content = "あ" * 1500
        assert len(mt._excerpt(content)) == 1200

    def test_zero_or_negative_falls_back_to_1200(self, monkeypatch):
        import argus.mcp_tools as mt
        monkeypatch.setenv("ARGUS_SEARCH_EXCERPT_CHARS", "-1")
        content = "あ" * 1500
        assert len(mt._excerpt(content)) == 1200

    def test_zu_marker_content_returns_full_text_regardless_of_env(self, monkeypatch):
        """[図: を含むチャンクは env の値に関わらず全文を返す（既存の分岐は現行維持）。"""
        import argus.mcp_tools as mt
        monkeypatch.setenv("ARGUS_SEARCH_EXCERPT_CHARS", "10")
        content = "[図: 説明]" + "あ" * 500
        assert mt._excerpt(content) == content
