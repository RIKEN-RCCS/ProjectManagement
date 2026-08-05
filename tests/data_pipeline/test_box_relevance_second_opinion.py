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


@pytest.fixture(autouse=True)
def _clear_second_opinion_hold(monkeypatch):
    """第2系統の保留（config/sensitive_terms.yaml の on_hold）を外して機構をテストする。

    2026-08-04 に PM 判断で Scout が保留になった（能力不足のため）。保留は運用の判断で
    あり、**機構のテストは保留と独立に維持する**（保留を解いたときに壊れていては
    意味がない）。保留中に Box 経路が LLM を呼ばず WARN を出すことは
    TestSecondOpinionHoldSkipsBoxPath で固定する。
    """
    from ingest import slack as ing_mod
    monkeypatch.setattr(ing_mod, "second_opinion_hold", lambda: None)

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

        args = argparse.Namespace(force=False, force_human=False, index_name=None,
                                  dry_run=False, no_encrypt=True, reader="second")
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


class TestSecondOpinionHoldSkipsBoxPath:
    """第2系統が保留中のとき、Box 経路は LLM を呼ばず WARN を出す（2026-08-04）。

    **記録が無いことを「不一致なし」と読ませない**のが要点。保留は「対策が無い」状態で
    あり、黙って 0 件を返すと noise 判定が誰にも検査されていないことが見えなくなる。
    """

    def test_hold_skips_llm_and_warns(self, pm_db_path, monkeypatch):
        from ingest import slack as ing_mod
        monkeypatch.setattr(
            ing_mod, "second_opinion_hold",
            lambda: {"since": "2026-08-04", "decided_by": "PM", "reason": "Scout 能力不足"},
        )
        import utils.llm as llm_mod
        calls = []
        monkeypatch.setattr(llm_mod, "call_rivault",
                            lambda *a, **k: calls.append(1) or "core\n本質的")

        row = _make_row("200", content_md="中国製モデルの輸出規制に関するメモ")
        logs = []
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        dis = pbr.apply_second_opinion_box_relevance(
            [row], {"200": ("noise", "雑談添付")}, conn_pm=conn,
            log=lambda m: logs.append(str(m)),
        )
        rows = list(conn.execute("SELECT * FROM triage_second_opinion"))
        conn.close()

        assert calls == []          # LLM を呼ばない
        assert dis == [] and rows == []
        assert any("保留中" in m for m in logs)
        assert any("行いません" in m for m in logs)


# --------------------------------------------------------------------------- #
# --reader k3（recall チェック） / --recheck-noise / relevance_source
# --------------------------------------------------------------------------- #


def _judge_args(**over) -> argparse.Namespace:
    base = dict(force=False, force_human=False, index_name=None, dry_run=False,
                no_encrypt=True, reader="second", rejudge_relevance=None)
    base.update(over)
    return argparse.Namespace(**base)


def _recheck_args(**over) -> argparse.Namespace:
    base = dict(index_name=None, dry_run=False, no_encrypt=True, reader="k3", limit=50)
    base.update(over)
    return argparse.Namespace(**base)


def _setup_box_db(tmp_path, rows: list[tuple[str, str, str]]):
    """rows: [(box_file_id, name, content_md)] を noise 判定済みで作る。"""
    box_docs_path = tmp_path / "box_docs.db"
    conn = open_db(box_docs_path, encrypt=False, schema=_BOX_DOCS_SCHEMA)
    for fid, name, content in rows:
        conn.execute(
            "INSERT INTO box_files (box_file_id, box_folder_id, name, file_format,"
            " folder_path, index_name, source_name, registered_at, relevance)"
            " VALUES (?, 'F1', ?, 'pdf', '/tmp', 'pm', 'box', '2026-08-01', 'noise')",
            (fid, name),
        )
        conn.execute(
            "INSERT INTO doc_content (box_file_id, content_md, extracted_at)"
            " VALUES (?, ?, '2026-08-01')", (fid, content),
        )
    conn.commit()
    conn.close()
    return box_docs_path


class TestParseBoxVerdict:
    """先頭行だけを見ると、前置きを書くモデル（K3 等）で判定を取りこぼす。"""

    def test_first_line_verdict(self):
        assert pbr._parse_box_verdict("core\n設計資料である") == "core"

    def test_verdict_after_preamble(self):
        raw = "判定は以下の通りです。\nrelated\n参考資料である"
        assert pbr._parse_box_verdict(raw) == "related"

    def test_no_verdict_falls_back_to_unknown(self):
        # **noise に寄せない** — 索引から落とす方向の誤りだけが不可視の欠落を作る。
        assert pbr._parse_box_verdict("判定できませんでした") == "unknown"
        assert pbr._parse_box_verdict("") == "unknown"
        assert pbr._parse_box_verdict(None) == "unknown"


class TestReaderK3:
    """k3 は call_local_llm 経由で、kind は box_relevance_recall（R8 とは別集計）。"""

    def _patch_k3(self, monkeypatch, reply: str, calls: list | None = None):
        monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:9999/v1")
        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "load_llm_secrets", lambda: None)
        monkeypatch.setattr(llm_mod, "_token_for_base", lambda *a, **k: "dummy")

        def fake(*a, **k):
            if calls is not None:
                calls.append(k)
            return reply

        monkeypatch.setattr(llm_mod, "call_local_llm", fake)
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: pytest.fail("k3 経路で call_rivault を呼んではいけない"),
        )

    def test_k3_route_uses_local_llm_with_large_max_tokens(self, monkeypatch):
        calls: list = []
        self._patch_k3(monkeypatch, "core\n設計資料", calls)
        verdict, raw = pbr.second_opinion_box_verdict("doc", route="k3")
        assert verdict == "core"
        # K3 は thinking を無効化できないため、第2系統向けの 256 を流用すると
        # 本文に到達する前に切れる（全部 unknown に落ちて壊れたことに気づけない）。
        assert calls[0]["max_tokens"] >= 16384

    def test_k3_kind_is_separate_from_r8(self, pm_db_path, monkeypatch):
        self._patch_k3(monkeypatch, "core\n設計資料")
        row = _make_row("400", content_md="中国製モデルの輸出規制に関するメモ")
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pbr.apply_second_opinion_box_relevance(
            [row], {"400": ("noise", "雑談")}, conn_pm=conn,
            log=lambda *_: None, reader="k3",
        )
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM triage_second_opinion")]
        conn.close()
        assert kinds == ["box_relevance_recall"]

    def test_hold_does_not_block_k3(self, pm_db_path, monkeypatch):
        """保留は R8 の第2系統に対する判断。K3 の recall チェックは別物なので止めない。"""
        from ingest import slack as ing_mod
        monkeypatch.setattr(
            ing_mod, "second_opinion_hold",
            lambda: {"since": "2026-08-04", "decided_by": "PM", "reason": "Scout 能力不足"},
        )
        self._patch_k3(monkeypatch, "core\n設計資料")
        row = _make_row("401", content_md="中国製モデルの輸出規制に関するメモ")
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        dis = pbr.apply_second_opinion_box_relevance(
            [row], {"401": ("noise", "雑談")}, conn_pm=conn,
            log=lambda *_: None, reader="k3",
        )
        conn.close()
        assert len(dis) == 1

    def test_hold_still_blocks_second_route(self, monkeypatch):
        """route=second は保留中に**空を返さず例外**（検査済みと混同させない）。"""
        from ingest import slack as ing_mod
        monkeypatch.setattr(
            ing_mod, "second_opinion_hold",
            lambda: {"since": "2026-08-04", "decided_by": "PM", "reason": "x"},
        )
        with pytest.raises(pbr.SecondOpinionOnHold):
            pbr.second_opinion_box_verdict("doc", route="second")


class TestRecheckNoise:
    """既存の noise 判定を掘り返す経路（--judge は「今判定した行」しか見ない）。"""

    def _run(self, tmp_path, monkeypatch, box_path, reply="core\n設計資料である", **over):
        pm_path = tmp_path / "pm.db"
        if not pm_path.exists():
            init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        TestReaderK3()._patch_k3(monkeypatch, reply)
        pbr.cmd_recheck_noise(_recheck_args(**over), logging.getLogger("t"))
        return pm_path

    def test_records_disagreement_without_overwriting_relevance(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("500", "a.pptx", "設計資料の本文")])
        pm_path = self._run(tmp_path, monkeypatch, box_path)

        box_conn = sqlite3.connect(str(box_path))
        rel = box_conn.execute(
            "SELECT relevance FROM box_files WHERE box_file_id='500'"
        ).fetchone()[0]
        box_conn.close()
        assert rel == "noise"  # 読み手の判定で上書きしない

        pm_conn = open_db(pm_path, encrypt=True)
        rows = [dict(r) for r in pm_conn.execute("SELECT * FROM triage_second_opinion")]
        pm_conn.close()
        assert len(rows) == 1
        assert rows[0]["kind"] == "box_relevance_recall"
        assert rows[0]["second_verdict"] == "core"

    def test_agreement_is_also_recorded(self, tmp_path, monkeypatch):
        """一致も残さないと「何件中の不一致か」が分からず率として読めない。"""
        box_path = _setup_box_db(tmp_path, [("501", "b.pptx", "無関係な雑談")])
        pm_path = self._run(tmp_path, monkeypatch, box_path, reply="noise\n雑談である")
        pm_conn = open_db(pm_path, encrypt=True)
        rows = [dict(r) for r in pm_conn.execute("SELECT * FROM triage_second_opinion")]
        pm_conn.close()
        assert len(rows) == 1
        assert rows[0]["second_verdict"] == "noise"
        assert rows[0]["agreed"] == 1

    def test_no_flag_terms_still_checked(self, tmp_path, monkeypatch):
        """recall チェックはフラグ語で絞らない（絞ると大多数が検査対象外になる）。"""
        box_path = _setup_box_db(tmp_path, [("502", "c.pptx", "次回までにベンチマークをまとめる")])
        pm_path = self._run(tmp_path, monkeypatch, box_path)
        pm_conn = open_db(pm_path, encrypt=True)
        n = pm_conn.execute("SELECT COUNT(*) FROM triage_second_opinion").fetchone()[0]
        pm_conn.close()
        assert n == 1

    def test_second_run_advances_to_next_items(self, tmp_path, monkeypatch):
        """同じ先頭 N 件を舐め直すと山の奥に永久に到達しない。"""
        box_path = _setup_box_db(
            tmp_path, [("600", "x.pptx", "本文x"), ("601", "y.pptx", "本文y")],
        )
        pm_path = self._run(tmp_path, monkeypatch, box_path, limit=1)
        self._run(tmp_path, monkeypatch, box_path, limit=1)

        pm_conn = open_db(pm_path, encrypt=True)
        heads = [r[0] for r in pm_conn.execute(
            "SELECT content_head FROM triage_second_opinion ORDER BY id"
        )]
        pm_conn.close()
        assert len(heads) == 2
        assert "box_file_id=600" in heads[0]
        assert "box_file_id=601" in heads[1]   # 2回目は次の行へ進む

    def test_only_noise_rows_are_targeted(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("700", "z.pptx", "本文z")])
        conn = sqlite3.connect(str(box_path))
        conn.execute("UPDATE box_files SET relevance='core' WHERE box_file_id='700'")
        conn.commit()
        conn.close()
        pm_path = self._run(tmp_path, monkeypatch, box_path)
        pm_conn = open_db(pm_path, encrypt=True)
        n = pm_conn.execute("SELECT COUNT(*) FROM triage_second_opinion").fetchone()[0]
        pm_conn.close()
        assert n == 0

    def test_dry_run_makes_no_llm_call(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("800", "w.pptx", "本文w")])
        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "1")
        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_local_llm",
            lambda *a, **k: pytest.fail("--dry-run で LLM を呼んではいけない"),
        )
        pbr.cmd_recheck_noise(_recheck_args(dry_run=True), logging.getLogger("t"))

    def test_reader_second_on_hold_aborts_with_message(self, tmp_path, monkeypatch, capsys):
        from ingest import slack as ing_mod
        monkeypatch.setattr(
            ing_mod, "second_opinion_hold",
            lambda: {"since": "2026-08-04", "decided_by": "PM", "reason": "x"},
        )
        box_path = _setup_box_db(tmp_path, [("900", "v.pptx", "本文v")])
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        pbr.cmd_recheck_noise(_recheck_args(reader="second"), logging.getLogger("t"))
        out = capsys.readouterr().out
        assert "保留中" in out
        assert "--reader k3" in out


class TestRelevanceSourceProtection:
    """人手で直した relevance を LLM が黙って上書きしないこと（--force でも）。"""

    def test_import_marks_human(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("1000", "a.pptx", "本文a")])
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        csv_path = tmp_path / "screen.csv"
        csv_path.write_text(
            "box_file_id,final_relevance,name,folder_path\n1000,core,a.pptx,/tmp\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(import_csv=str(csv_path), dry_run=False, no_encrypt=True)
        pbr.cmd_import(args, logging.getLogger("t"))

        conn = sqlite3.connect(str(box_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT relevance, relevance_source FROM box_files WHERE box_file_id='1000'"
        ).fetchone()
        conn.close()
        assert row["relevance"] == "core"
        assert row["relevance_source"] == "human"

    def test_force_does_not_rejudge_human_rows(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("1001", "b.pptx", "本文b")])
        conn = sqlite3.connect(str(box_path))
        conn.execute("ALTER TABLE box_files ADD COLUMN relevance_source TEXT")
        conn.execute(
            "UPDATE box_files SET relevance='core', relevance_source='human'"
            " WHERE box_file_id='1001'"
        )
        conn.commit()
        conn.close()

        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        monkeypatch.setattr(
            pbr, "judge_batch",
            lambda rows, logger: pytest.fail("人手修正行を再判定してはいけない"),
        )
        pbr.cmd_judge(_judge_args(force=True), logging.getLogger("t"))

        conn = sqlite3.connect(str(box_path))
        rel = conn.execute(
            "SELECT relevance FROM box_files WHERE box_file_id='1001'"
        ).fetchone()[0]
        conn.close()
        assert rel == "core"

    def test_force_human_overrides_protection(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("1002", "c.pptx", "本文c")])
        conn = sqlite3.connect(str(box_path))
        conn.execute("ALTER TABLE box_files ADD COLUMN relevance_source TEXT")
        conn.execute(
            "UPDATE box_files SET relevance='core', relevance_source='human'"
            " WHERE box_file_id='1002'"
        )
        conn.commit()
        conn.close()

        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        monkeypatch.setattr(pbr, "judge_batch", lambda rows, logger: {"1002": ("noise", "雑談")})
        pbr.cmd_judge(_judge_args(force=True, force_human=True), logging.getLogger("t"))

        conn = sqlite3.connect(str(box_path))
        row = conn.execute(
            "SELECT relevance, relevance_source FROM box_files WHERE box_file_id='1002'"
        ).fetchone()
        conn.close()
        assert row[0] == "noise"
        assert row[1] == "llm"

    def test_judge_marks_llm(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [("1003", "d.pptx", "本文d")])
        conn = sqlite3.connect(str(box_path))
        conn.execute("UPDATE box_files SET relevance=NULL WHERE box_file_id='1003'")
        conn.commit()
        conn.close()

        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        monkeypatch.setattr(pbr, "judge_batch", lambda rows, logger: {"1003": ("core", "設計資料")})
        pbr.cmd_judge(_judge_args(), logging.getLogger("t"))

        conn = sqlite3.connect(str(box_path))
        row = conn.execute(
            "SELECT relevance, relevance_source FROM box_files WHERE box_file_id='1003'"
        ).fetchone()
        conn.close()
        assert row[0] == "core"
        assert row[1] == "llm"

    def test_existing_null_rows_are_not_treated_as_human(self, tmp_path, monkeypatch):
        """由来不明（NULL）を 'human' に丸めない — 保護対象を広げすぎると再判定が回らない。"""
        box_path = _setup_box_db(tmp_path, [("1004", "e.pptx", "本文e")])
        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        seen: list = []
        monkeypatch.setattr(
            pbr, "judge_batch",
            lambda rows, logger: seen.append([r["box_file_id"] for r in rows]) or {},
        )
        pbr.cmd_judge(_judge_args(force=True), logging.getLogger("t"))
        assert seen and "1004" in seen[0]


class TestRejudgeRelevance:
    """--rejudge-relevance: 実行単位の事故から、健全な判定を巻き込まずに復旧する。"""

    def _setup(self, tmp_path, monkeypatch):
        box_path = _setup_box_db(tmp_path, [
            ("2000", "bad1.pptx", "本文1"),
            ("2001", "bad2.pptx", "本文2"),
            ("2002", "ok.pptx", "本文3"),
        ])
        conn = sqlite3.connect(str(box_path))
        conn.execute("UPDATE box_files SET relevance='core' WHERE box_file_id='2002'")
        conn.commit()
        conn.close()
        pm_path = tmp_path / "pm.db"
        init_pm_db(pm_path)
        monkeypatch.setattr(pbr, "BOX_DOCS_DB", box_path)
        monkeypatch.setattr(pbr, "PM_DB", pm_path)
        return box_path

    def test_targets_only_the_given_relevance(self, tmp_path, monkeypatch):
        box_path = self._setup(tmp_path, monkeypatch)
        seen: list = []

        def fake_judge(rows, logger):
            seen.extend(r["box_file_id"] for r in rows)
            return {r["box_file_id"]: ("core", "設計資料") for r in rows}

        monkeypatch.setattr(pbr, "judge_batch", fake_judge)
        pbr.cmd_judge(_judge_args(rejudge_relevance="noise"), logging.getLogger("t"))

        assert sorted(seen) == ["2000", "2001"]   # core の行は触らない

        conn = sqlite3.connect(str(box_path))
        conn.row_factory = sqlite3.Row
        rows = {r["box_file_id"]: (r["relevance"], r["relevance_source"])
                for r in conn.execute(
                    "SELECT box_file_id, relevance, relevance_source FROM box_files")}
        conn.close()
        assert rows["2000"] == ("core", "llm")
        assert rows["2001"] == ("core", "llm")
        assert rows["2002"] == ("core", None)     # 元から core、再判定されていない

    def test_human_rows_still_protected(self, tmp_path, monkeypatch):
        """事故からの復旧でも、人手で直した行は巻き込まない。"""
        box_path = self._setup(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(box_path))
        conn.execute("ALTER TABLE box_files ADD COLUMN relevance_source TEXT")
        conn.execute("UPDATE box_files SET relevance_source='human' WHERE box_file_id='2000'")
        conn.commit()
        conn.close()

        seen: list = []
        monkeypatch.setattr(
            pbr, "judge_batch",
            lambda rows, logger: seen.extend(r["box_file_id"] for r in rows) or {},
        )
        pbr.cmd_judge(_judge_args(rejudge_relevance="noise"), logging.getLogger("t"))
        assert seen == ["2001"]

    def test_absent_option_keeps_unjudged_only_behaviour(self, tmp_path, monkeypatch):
        """既定（オプション無し）は従来どおり未判定のみ。"""
        box_path = self._setup(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(box_path))
        conn.execute("UPDATE box_files SET relevance=NULL WHERE box_file_id='2001'")
        conn.commit()
        conn.close()

        seen: list = []
        monkeypatch.setattr(
            pbr, "judge_batch",
            lambda rows, logger: seen.extend(r["box_file_id"] for r in rows) or {},
        )
        pbr.cmd_judge(_judge_args(), logging.getLogger("t"))
        assert seen == ["2001"]
