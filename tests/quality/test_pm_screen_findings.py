"""pm_screen.py --list-findings / --mark-reviewed のテスト。

triage_second_opinion に溜まった第2系統・K3recall の所見を人が読む経路
（誰も見ない検査は意味がない）。pending_egress の --list-pending / --approve
（output_broker.py）と同じ形。
"""
from __future__ import annotations

import sqlite3
import sys

from quality import pm_screen


def _insert_finding(conn, *, kind="minutes_extraction", content_head="所見の本文"):
    from db_utils import record_second_opinion

    record_second_opinion(
        conn, kind=kind, content=content_head,
        primary_verdict="MISSING", second_verdict="PRESENT", flagged_terms=[],
    )


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["pm_screen.py", *argv])
    pm_screen.main()


class TestListFindings:
    def test_list_findings_prints_rows(self, pm_db_path, monkeypatch, capsys):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_finding(conn, content_head="第2系統が見つけた項目")
        conn.close()

        _run_main(monkeypatch, ["--db", str(pm_db_path), "--no-encrypt", "--list-findings"])
        out = capsys.readouterr().out
        assert "kind=minutes_extraction" in out
        assert "第2系統が見つけた項目" in out

    def test_list_findings_kind_filter(self, pm_db_path, monkeypatch, capsys):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_finding(conn, kind="minutes_extraction", content_head="second系統")
        _insert_finding(conn, kind="minutes_extraction_recall", content_head="k3系統")
        conn.close()

        _run_main(
            monkeypatch,
            ["--db", str(pm_db_path), "--no-encrypt", "--list-findings",
             "--kind", "minutes_extraction_recall"],
        )
        out = capsys.readouterr().out
        assert "k3系統" in out
        assert "second系統" not in out

    def test_list_findings_unreviewed_only(self, pm_db_path, monkeypatch, capsys):
        from db_utils import list_second_opinion_findings, mark_second_opinion_reviewed

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_finding(conn, content_head="レビュー済み")
        _insert_finding(conn, content_head="未レビュー")
        rows = list_second_opinion_findings(conn)
        reviewed_id = next(r["id"] for r in rows if r["content_head"] == "レビュー済み")
        mark_second_opinion_reviewed(conn, [reviewed_id])
        conn.close()

        _run_main(
            monkeypatch,
            ["--db", str(pm_db_path), "--no-encrypt", "--list-findings", "--unreviewed-only"],
        )
        out = capsys.readouterr().out
        assert "未レビュー" in out
        assert "レビュー済み" not in out

    def test_list_findings_empty_reports_none(self, pm_db_path, monkeypatch, capsys):
        _run_main(monkeypatch, ["--db", str(pm_db_path), "--no-encrypt", "--list-findings"])
        out = capsys.readouterr().out
        assert "所見はありません" in out


class TestMarkReviewed:
    def test_mark_reviewed_single_id(self, pm_db_path, monkeypatch, capsys):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_finding(conn)
        row_id = conn.execute("SELECT id FROM triage_second_opinion").fetchone()["id"]
        conn.close()

        _run_main(
            monkeypatch,
            ["--db", str(pm_db_path), "--no-encrypt", "--mark-reviewed", str(row_id)],
        )
        out = capsys.readouterr().out
        assert "1 件" in out

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT reviewed_at FROM triage_second_opinion WHERE id=?", (row_id,)
        ).fetchone()
        conn.close()
        assert row["reviewed_at"] is not None

    def test_mark_reviewed_multiple_ids(self, pm_db_path, monkeypatch, capsys):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        _insert_finding(conn, content_head="1件目")
        _insert_finding(conn, content_head="2件目")
        ids = [str(r["id"]) for r in conn.execute("SELECT id FROM triage_second_opinion")]
        conn.close()

        _run_main(
            monkeypatch,
            ["--db", str(pm_db_path), "--no-encrypt", "--mark-reviewed", ",".join(ids)],
        )
        out = capsys.readouterr().out
        assert f"{len(ids)} 件" in out

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT reviewed_at FROM triage_second_opinion").fetchall()
        conn.close()
        assert all(r["reviewed_at"] is not None for r in rows)
