"""Box relevance 判定への第2系統（独立系統）差分検査のテスト。

docs/security-architecture.md R8 / Phase 4。
noise 判定のみが対象（索引から落とす判定だけが欠落を作るため）。
box_files.relevance は上書きしない — 記録のみ行う。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3

import pm_box_relevance as pbr
import pytest
from db_utils import init_pm_db, open_db

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


def _make_row(box_file_id: str, name: str = "doc.pdf", folder_path: str = "/tmp",
              file_format: str = "pdf", content_md: str = "") -> dict:
    return {
        "box_file_id": box_file_id, "name": name, "folder_path": folder_path,
        "file_format": file_format, "content_md": content_md,
    }


class TestApplySecondOpinionBoxRelevance:
    def test_noise_vs_core_is_recorded(self, pm_db_path, monkeypatch):
        row = _make_row("100", content_md="中国製モデルの輸出規制に関する個人的な雑談メモ")
        batch = [row]
        verdicts = {"100": ("noise", "雑談添付")}

        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: "core\n本質的な設計資料である")

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        dis = pbr.apply_second_opinion_box_relevance(
            batch, verdicts, conn_pm=conn, log=lambda *_: None,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(dis) == 1
        assert len(rows) == 1
        assert rows[0]["kind"] == "box_relevance"
        assert rows[0]["primary_verdict"] == "noise"
        assert rows[0]["second_verdict"] == "core"

    def test_noise_vs_noise_agreement_is_not_recorded(self, pm_db_path, monkeypatch):
        row = _make_row("101", content_md="中国製モデルに関する個人的な雑談メモ")
        batch = [row]
        verdicts = {"101": ("noise", "雑談添付")}

        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: "noise\n雑談である")

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        dis = pbr.apply_second_opinion_box_relevance(
            batch, verdicts, conn_pm=conn, log=lambda *_: None,
        )
        rows = list(conn.execute("SELECT * FROM triage_second_opinion"))
        conn.close()
        assert dis == []
        assert rows == []

    def test_non_noise_primary_is_not_sent(self, monkeypatch):
        """primary が core/related のときは索引から落ちないため対象外。"""
        row = _make_row("102", content_md="中国製モデルに関する設計資料")
        batch = [row]
        verdicts = {"102": ("core", "本質的資料")}

        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "core\n"

        monkeypatch.setattr(llm_mod, "call_rivault", fake)
        dis = pbr.apply_second_opinion_box_relevance(batch, verdicts, log=lambda *_: None)
        assert dis == []
        assert calls["n"] == 0

    def test_no_flagged_terms_skips(self, monkeypatch):
        row = _make_row("103", content_md="次回までにベンチマーク結果をまとめる")
        batch = [row]
        verdicts = {"103": ("noise", "雑談添付")}

        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "core\n"

        monkeypatch.setattr(llm_mod, "call_rivault", fake)
        dis = pbr.apply_second_opinion_box_relevance(batch, verdicts, log=lambda *_: None)
        assert dis == []
        assert calls["n"] == 0

    def test_env_flag_disables(self, monkeypatch):
        monkeypatch.setenv("ARGUS_SECOND_OPINION", "0")
        row = _make_row("104", content_md="中国製モデルに関する個人的な雑談メモ")
        batch = [row]
        verdicts = {"104": ("noise", "雑談添付")}

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない"),
        )
        dis = pbr.apply_second_opinion_box_relevance(batch, verdicts, log=lambda *_: None)
        assert dis == []

    def test_cap_stops_further_calls_and_warns(self, monkeypatch):
        # pm_box_relevance.py は `from ingest.slack import _load_second_opinion_config,
        # flag_sensitive_terms` で名前を取り込んでいるが、これは同じ関数オブジェクトへの
        # 別名にすぎない。`flag_sensitive_terms` は `ingest.slack` モジュール内で直接
        # `_load_second_opinion_config()` を呼ぶため、その参照先は `ingest.slack` 自身の
        # モジュールグローバルであり、`pbr._load_second_opinion_config` だけを差し替えても
        # 効かない（`pbr` 側の呼び出し箇所にしか効かない）。共有キャッシュ
        # `ingest.slack._second_opinion_cache` を直接差し替えることで両方に効かせる
        # （こうしないと本テストは実 config/sensitive_terms.yaml の内容に依存してしまう）。
        import ingest.slack as ingest_slack

        monkeypatch.setattr(
            ingest_slack, "_second_opinion_cache",
            {
                "second_opinion": {"model": "test-model", "max_flagged_per_run": 1},
                "terms": {"geopolitical": ["中国"]},
            },
        )
        rows = [
            _make_row("200", content_md="中国製モデルに関する個人的な雑談メモ"),
            _make_row("201", content_md="中国製モデルに関する別の雑談メモ"),
        ]
        verdicts = {"200": ("noise", "雑談"), "201": ("noise", "雑談")}

        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "core\n"

        monkeypatch.setattr(llm_mod, "call_rivault", fake)
        logs: list[str] = []
        state: dict = {}
        pbr.apply_second_opinion_box_relevance(
            rows, verdicts, log=logs.append, state=state,
        )
        assert calls["n"] == 1
        assert any("[WARN]" in m and "上限" in m for m in logs)


class TestCmdJudgeDoesNotOverwriteRelevance:
    def test_noise_stays_noise_after_second_opinion_disagreement(self, tmp_path, monkeypatch):
        box_docs_path = tmp_path / "box_docs.db"
        pm_db_path = tmp_path / "pm.db"

        conn = open_db(box_docs_path, encrypt=False, schema=_BOX_DOCS_SCHEMA)
        conn.execute(
            "INSERT INTO box_files (box_file_id, box_folder_id, name, file_format,"
            " folder_path, index_name, source_name, registered_at)"
            " VALUES ('300', 'F1', 'doc.pdf', 'pdf', '/tmp', 'pm', 'box', '2026-08-01')"
        )
        conn.execute(
            "INSERT INTO doc_content (box_file_id, content_md, extracted_at)"
            " VALUES ('300', '中国製モデルの輸出規制に関する個人的な雑談メモ', '2026-08-01')"
        )
        conn.commit()
        conn.close()

        # pm.db は本番同様に暗号化して作る。`_open_pm_db_for_second_opinion()` は
        # box_docs.db 用の --no-encrypt を pm.db に流用しない（常に暗号化前提で開く）
        # ため、平文で作ると開けずに第2系統の記録がすべて落ちる。
        init_pm_db(pm_db_path)

        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_docs_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_db_path)
        monkeypatch.setattr(
            pbr, "judge_batch", lambda rows, logger: {"300": ("noise", "雑談添付")},
        )

        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: "core\n本質的な設計資料である")

        args = argparse.Namespace(force=False, index_name=None, dry_run=False, no_encrypt=True)
        logger = logging.getLogger("test-pm-box-relevance")
        pbr.cmd_judge(args, logger)

        box_conn = sqlite3.connect(str(box_docs_path))
        box_conn.row_factory = sqlite3.Row
        row = box_conn.execute(
            "SELECT relevance FROM box_files WHERE box_file_id='300'"
        ).fetchone()
        box_conn.close()
        assert row["relevance"] == "noise"  # 第2系統は上書きしない

        pm_conn = open_db(pm_db_path, encrypt=True)
        so_rows = [
            dict(r) for r in pm_conn.execute("SELECT * FROM triage_second_opinion")
        ]
        pm_conn.close()
        assert len(so_rows) == 1
        assert so_rows[0]["kind"] == "box_relevance"
        assert so_rows[0]["primary_verdict"] == "noise"
        assert so_rows[0]["second_verdict"] == "core"
