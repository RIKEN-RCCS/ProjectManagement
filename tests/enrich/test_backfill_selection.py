"""_fetch_backfill_items の純粋な選定ロジックのテスト（LLM/暗号化DB 不使用）。

未エンリッチ（related_ids IS NULL）の行が新規挿入 ID 範囲の外に滞留したまま
二度と自動再試行されない恒久未回収バグの回収用選定ロジックを検証する。
sqlcipher3 不要な平文 sqlite3 の in-memory DB を使う。
"""
import sqlite3

import pytest
from enrich.enrich_items import _fetch_backfill_items

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    content TEXT,
    deleted INTEGER DEFAULT 0,
    related_ids TEXT
);
CREATE TABLE action_items (
    id INTEGER PRIMARY KEY,
    content TEXT,
    deleted INTEGER DEFAULT 0,
    related_ids TEXT
);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _insert_decision(conn, id_, content, *, deleted=0, related_ids=None):
    conn.execute(
        "INSERT INTO decisions (id, content, deleted, related_ids) VALUES (?, ?, ?, ?)",
        (id_, content, deleted, related_ids),
    )


def _insert_action_item(conn, id_, content, *, deleted=0, related_ids=None):
    conn.execute(
        "INSERT INTO action_items (id, content, deleted, related_ids) VALUES (?, ?, ?, ?)",
        (id_, content, deleted, related_ids),
    )


def test_only_unenriched_rows_returned(conn):
    _insert_decision(conn, 1, "決定A", related_ids=None)
    _insert_decision(conn, 2, "決定B（エンリッチ済み）", related_ids="[]")
    conn.commit()

    decisions, action_items = _fetch_backfill_items(conn, limit=10)

    assert [d["id"] for d in decisions] == [1]
    assert action_items == []


def test_deleted_rows_excluded(conn):
    _insert_decision(conn, 1, "決定A", deleted=1, related_ids=None)
    _insert_decision(conn, 2, "決定B", deleted=0, related_ids=None)
    conn.commit()

    decisions, _ = _fetch_backfill_items(conn, limit=10)

    assert [d["id"] for d in decisions] == [2]


def test_limit_applies_oldest_first(conn):
    for i in (3, 1, 2):
        _insert_decision(conn, i, f"決定{i}", related_ids=None)
    conn.commit()

    decisions, _ = _fetch_backfill_items(conn, limit=2)

    # id 昇順（古い順）で limit 件のみ
    assert [d["id"] for d in decisions] == [1, 2]


def test_action_items_selected_independently(conn):
    _insert_decision(conn, 1, "決定A", related_ids=None)
    _insert_action_item(conn, 10, "アクションX", related_ids=None)
    _insert_action_item(conn, 11, "アクションY（エンリッチ済み）", related_ids="[]")
    conn.commit()

    decisions, action_items = _fetch_backfill_items(conn, limit=10)

    assert [d["id"] for d in decisions] == [1]
    assert [a["id"] for a in action_items] == [10]


def test_no_unenriched_rows_returns_empty_lists(conn):
    _insert_decision(conn, 1, "決定A", related_ids="[]")
    _insert_action_item(conn, 10, "アクションX", related_ids="[]")
    conn.commit()

    decisions, action_items = _fetch_backfill_items(conn, limit=10)

    assert decisions == []
    assert action_items == []
