"""send_reminder() の record_notification イベントタイプが、detect.py の
already_notified 判定キーと一致することを検証する。

過去に reminder_type ("overdue" / "deadline_warning" / "stale") のまま
record_notification していたため、detect.py 側の判定キー
("overdue_reminder" / "deadline_warning" / "stale_alert") と不一致になり、
cooldown が機能しなかった（deadline_warning のみ偶然一致）。
"""
import inspect
import re
from unittest.mock import Mock

from argus.patrol import actions, detect


def _make_ctx():
    ctx = Mock()
    ctx.dry_run = False
    ctx.slack = Mock()
    ctx.config = {"patrol": {"leader_channel": ""}}
    ctx.user_resolver = Mock()
    ctx.user_resolver.resolve.return_value = "U12345"
    ctx.state = Mock()
    return ctx


def test_overdue_records_with_detect_py_key(monkeypatch):
    ctx = _make_ctx()
    monkeypatch.setattr(
        actions, "_send_dm_or_fallback", lambda *a, **kw: ("C1", "123.456")
    )
    actions.send_reminder(ctx, "someone", [{"id": 1, "content": "x", "due_date": None}], "overdue")
    ctx.state.record_notification.assert_called_once_with(
        "overdue_reminder", "ai:1", "C1"
    )


def test_stale_records_with_detect_py_key(monkeypatch):
    ctx = _make_ctx()
    monkeypatch.setattr(
        actions, "_send_dm_or_fallback", lambda *a, **kw: ("C1", "123.456")
    )
    actions.send_reminder(ctx, "someone", [{"id": 2, "content": "x", "due_date": None}], "stale")
    ctx.state.record_notification.assert_called_once_with(
        "stale_alert", "ai:2", "C1"
    )


def test_deadline_warning_records_with_detect_py_key(monkeypatch):
    ctx = _make_ctx()
    monkeypatch.setattr(
        actions, "_send_dm_or_fallback", lambda *a, **kw: ("C1", "123.456")
    )
    actions.send_reminder(ctx, "someone", [{"id": 3, "content": "x", "due_date": None}], "deadline_warning")
    ctx.state.record_notification.assert_called_once_with(
        "deadline_warning", "ai:3", "C1"
    )


def _already_notified_key(func) -> str:
    """detect.py の関数ソースから already_notified() 第1引数の文字列リテラルを抽出する。"""
    src = inspect.getsource(func)
    m = re.search(r'already_notified\(\s*"([^"]+)"', src)
    assert m, f"already_notified呼び出しが見つからない: {func.__name__}"
    return m.group(1)


def test_reminder_event_type_mapping_matches_detect_py_keys():
    """actions._REMINDER_EVENT_TYPES の値が detect.py の判定キーと一致すること。"""
    assert actions._REMINDER_EVENT_TYPES["overdue"] == _already_notified_key(
        detect.detect_overdue_items
    )
    assert actions._REMINDER_EVENT_TYPES[
        "deadline_warning"
    ] == _already_notified_key(detect.detect_approaching_deadlines)
    assert actions._REMINDER_EVENT_TYPES["stale"] == _already_notified_key(
        detect.detect_stale_items
    )
