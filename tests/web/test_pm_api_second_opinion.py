"""/api/second-opinion（所見レビュー画面）のテスト。

triage_second_opinion に溜まった第2系統の所見を Web UI からレビュー済みにする経路
（CLI の pm_screen.py --list-findings / --mark-reviewed しか無かったため追加）。
本番 pm.db には一切書き込まない。pm_db_path フィクスチャのスクラッチ DB のみ使用する。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pm_api  # noqa: E402
from db_utils import mark_second_opinion_reviewed, record_second_opinion  # noqa: E402


def _insert(conn, *, kind="minutes_extraction", content="所見の本文",
            primary_verdict="MISSING", second_verdict="PRESENT"):
    record_second_opinion(
        conn, kind=kind, content=content,
        primary_verdict=primary_verdict, second_verdict=second_verdict,
        flagged_terms=[], model="test-model", raw="raw応答の本文",
    )


@pytest.fixture
def client(pm_db_path):
    pm_api._state["db_path"] = str(pm_db_path)
    pm_api._state["no_encrypt"] = True
    return TestClient(pm_api.app)


class TestListSecondOpinion:
    def test_excludes_pretune_and_t8192_by_default(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, kind="minutes_extraction", content="通常")
        _insert(conn, kind="minutes_extraction_pretune", content="調整前")
        _insert(conn, kind="minutes_extraction_t8192", content="調整前2")
        conn.close()

        res = client.get("/api/second-opinion", params={"unreviewed_only": False})
        assert res.status_code == 200
        kinds = [r["kind"] for r in res.json()["rows"]]
        assert "minutes_extraction" in kinds
        assert "minutes_extraction_pretune" not in kinds
        assert "minutes_extraction_t8192" not in kinds

    def test_kind_query_can_explicitly_show_pretune(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, kind="minutes_extraction_pretune", content="調整前")
        conn.close()

        res = client.get(
            "/api/second-opinion",
            params={"kind": "_pretune", "unreviewed_only": False},
        )
        kinds = [r["kind"] for r in res.json()["rows"]]
        assert "minutes_extraction_pretune" in kinds

    def test_unreviewed_only_filters(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, content="a")
        _insert(conn, content="b")
        rows = conn.execute("SELECT id FROM triage_second_opinion ORDER BY id").fetchall()
        mark_second_opinion_reviewed(conn, [rows[0]["id"]])
        conn.close()

        res = client.get("/api/second-opinion", params={"unreviewed_only": True})
        contents = [r["content_head"] for r in res.json()["rows"]]
        assert "b" in contents
        assert "a" not in contents

        res_all = client.get("/api/second-opinion", params={"unreviewed_only": False})
        contents_all = [r["content_head"] for r in res_all.json()["rows"]]
        assert "a" in contents_all
        assert "b" in contents_all

    def test_raw_not_returned(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, content="所見本文")
        conn.close()

        res = client.get("/api/second-opinion", params={"unreviewed_only": False})
        row = res.json()["rows"][0]
        assert "raw" not in row


class TestMarkSecondOpinionReviewed:
    def test_marks_reviewed_at(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, content="レビュー対象")
        row_id = conn.execute("SELECT id FROM triage_second_opinion").fetchone()["id"]
        conn.close()

        res = client.post("/api/second-opinion/mark-reviewed", json={"ids": [row_id]})
        assert res.status_code == 200
        assert res.json()["updated"] == 1

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT reviewed_at FROM triage_second_opinion WHERE id=?", (row_id,),
        ).fetchone()
        conn.close()
        assert row["reviewed_at"] is not None

    def test_idempotent(self, pm_db_path, client):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert(conn, content="レビュー対象")
        row_id = conn.execute("SELECT id FROM triage_second_opinion").fetchone()["id"]
        conn.close()

        res1 = client.post("/api/second-opinion/mark-reviewed", json={"ids": [row_id]})
        res2 = client.post("/api/second-opinion/mark-reviewed", json={"ids": [row_id]})
        assert res1.status_code == 200
        assert res2.status_code == 200
        assert res2.json()["updated"] == 1

    def test_nonexistent_id_no_error(self, pm_db_path, client):
        res = client.post("/api/second-opinion/mark-reviewed", json={"ids": [999999]})
        assert res.status_code == 200
        assert res.json()["updated"] == 0
