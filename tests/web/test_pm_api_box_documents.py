"""/api/box-documents（Box relevance 編集画面）のテスト。

`box_files.relevance` は「検索インデックスに残すか」を決める。noise にした文書は
`pm_embed.py` が索引から外すため二度と検索に出てこない — **落とした側の誤りは
観測できない**（出なかったことには気づけない）。CSV 往復しか修正経路が無かったので
画面を足した、その API 部分の固定。

本番 box_docs.db / pm.db には一切書き込まない（tmp_path のスクラッチ DB のみ）。
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
from db_utils import record_second_opinion  # noqa: E402

_BOX_DOCS_SCHEMA = """
CREATE TABLE IF NOT EXISTS box_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    box_file_id    TEXT NOT NULL UNIQUE,
    box_folder_id  TEXT NOT NULL,
    name           TEXT NOT NULL,
    file_format    TEXT,
    size_bytes     INTEGER,
    modified_at    TEXT,
    folder_path    TEXT,
    index_name     TEXT,
    source_name    TEXT,
    registered_at  TEXT NOT NULL,
    relevance          TEXT,
    relevance_reason   TEXT,
    relevance_judged_at TEXT
);

CREATE TABLE IF NOT EXISTS doc_content (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    box_file_id         TEXT NOT NULL UNIQUE,
    content_md          TEXT NOT NULL,
    content_hash        TEXT,
    page_count          INTEGER,
    char_count          INTEGER,
    convert_method      TEXT,
    extracted_at        TEXT NOT NULL,
    source_modified_at  TEXT,
    last_figures_attempt_at TEXT
);
"""


@pytest.fixture
def box_db(tmp_path, monkeypatch):
    """relevance_source 列**なし**で作る（本番 box_docs.db の後付け前と同じ形）。"""
    path = tmp_path / "box_docs.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_BOX_DOCS_SCHEMA)
    rows = [
        ("1", "a.pptx", "noise", "WG資料である", '["pm"]', "本文A"),
        ("2", "b.pdf", "core", "設計資料である", '["pm"]', "本文B"),
        ("3", "c.docx", "noise", "雑談メモ", '["pm-hpc"]', "本文C"),
        ("4", "d.xlsx", None, None, '["pm"]', "本文D"),
    ]
    for fid, name, rel, reason, idx, content in rows:
        conn.execute(
            "INSERT INTO box_files (box_file_id, box_folder_id, name, file_format,"
            " folder_path, index_name, source_name, registered_at, relevance,"
            " relevance_reason)"
            " VALUES (?, 'F1', ?, 'pdf', '/tmp', ?, 'box', '2026-08-01', ?, ?)",
            (fid, name, idx, rel, reason),
        )
        conn.execute(
            "INSERT INTO doc_content (box_file_id, content_md, extracted_at)"
            " VALUES (?, ?, '2026-08-01')", (fid, content),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pm_api, "_BOX_DOCS_DB", path)
    return path


@pytest.fixture
def client(pm_db_path, box_db):
    pm_api._state["db_path"] = str(pm_db_path)
    pm_api._state["no_encrypt"] = True
    return TestClient(pm_api.app)


def _relevance(box_db, fid: str) -> tuple:
    conn = sqlite3.connect(str(box_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT relevance, relevance_source FROM box_files WHERE box_file_id=?", (fid,)
    ).fetchone()
    conn.close()
    return (row["relevance"], row["relevance_source"])


class TestListBoxDocuments:
    def test_lists_all(self, client):
        r = client.get("/api/box-documents")
        assert r.status_code == 200
        assert r.json()["total"] == 4

    def test_filter_by_relevance(self, client):
        rows = client.get("/api/box-documents?relevance=noise").json()["rows"]
        assert {r["box_file_id"] for r in rows} == {"1", "3"}

    def test_filter_unjudged(self, client):
        rows = client.get("/api/box-documents?relevance=(未判定)").json()["rows"]
        assert [r["box_file_id"] for r in rows] == ["4"]

    def test_filter_by_index_name(self, client):
        rows = client.get("/api/box-documents?index_name=pm-hpc").json()["rows"]
        assert [r["box_file_id"] for r in rows] == ["3"]

    def test_filter_by_query(self, client):
        rows = client.get("/api/box-documents?q=b.pdf").json()["rows"]
        assert [r["box_file_id"] for r in rows] == ["2"]

    def test_works_without_relevance_source_column(self, client):
        """列が無い box_docs.db でも 500 にせず NULL を返す（後付け前の形）。"""
        rows = client.get("/api/box-documents").json()["rows"]
        assert all(r["relevance_source"] is None for r in rows)

    def test_content_chars_exposed(self, client):
        rows = client.get("/api/box-documents?q=a.pptx").json()["rows"]
        assert rows[0]["content_chars"] == len("本文A")


class TestRecallVerdictJoin:
    def _record(self, pm_db_path, fid, verdict):
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        record_second_opinion(
            conn, kind="box_relevance_recall", content=f"a.pptx (box_file_id={fid})",
            primary_verdict="noise", second_verdict=verdict,
            flagged_terms=[], model="kimi-k3", raw="raw",
        )
        conn.commit()
        conn.close()

    def test_disagreement_is_flagged(self, pm_db_path, client):
        self._record(pm_db_path, "1", "core")
        rows = client.get("/api/box-documents?q=a.pptx").json()["rows"]
        assert rows[0]["recall_verdict"] == "core"
        assert rows[0]["recall_disagrees"] is True

    def test_agreement_is_not_flagged(self, pm_db_path, client):
        self._record(pm_db_path, "1", "noise")
        rows = client.get("/api/box-documents?q=a.pptx").json()["rows"]
        assert rows[0]["recall_verdict"] == "noise"
        assert rows[0]["recall_disagrees"] is False

    def test_recall_only_filter(self, pm_db_path, client):
        self._record(pm_db_path, "1", "core")
        rows = client.get("/api/box-documents?recall_only=true").json()["rows"]
        assert [r["box_file_id"] for r in rows] == ["1"]

    def test_no_ledger_is_not_an_error(self, client):
        """--recheck-noise 未実行（台帳が空）でも一覧は出る。"""
        rows = client.get("/api/box-documents").json()["rows"]
        assert all(r["recall_verdict"] == "" for r in rows)


class TestSaveBoxDocuments:
    def test_change_marks_human(self, box_db, client):
        r = client.post("/api/box-documents/save",
                        json={"rows": [{"box_file_id": "1", "relevance": "core"}]})
        assert r.json()["updated"] == 1
        assert _relevance(box_db, "1") == ("core", "human")

    def test_unchanged_rows_are_not_marked_human(self, box_db, client):
        """画面を開いて何も触らず保存しただけで全件 human になると、
        LLM の再判定対象から永久に外れてしまう。値が変わった行だけ書く。"""
        r = client.post("/api/box-documents/save", json={"rows": [
            {"box_file_id": "1", "relevance": "noise"},   # 変更なし
            {"box_file_id": "2", "relevance": "core"},    # 変更なし
        ]})
        assert r.json()["updated"] == 0
        assert _relevance(box_db, "1") == ("noise", None)
        assert _relevance(box_db, "2") == ("core", None)

    def test_invalid_value_is_rejected_not_written(self, box_db, client):
        r = client.post("/api/box-documents/save",
                        json={"rows": [{"box_file_id": "1", "relevance": "CORE以外"}]})
        body = r.json()
        assert body["updated"] == 0
        assert len(body["invalid"]) == 1
        assert _relevance(box_db, "1") == ("noise", None)

    def test_unknown_id_is_ignored(self, client):
        r = client.post("/api/box-documents/save",
                        json={"rows": [{"box_file_id": "9999", "relevance": "core"}]})
        assert r.json()["updated"] == 0

    def test_adds_relevance_source_column_when_missing(self, box_db, client):
        client.post("/api/box-documents/save",
                    json={"rows": [{"box_file_id": "1", "relevance": "core"}]})
        conn = sqlite3.connect(str(box_db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(box_files)")}
        conn.close()
        assert "relevance_source" in cols
