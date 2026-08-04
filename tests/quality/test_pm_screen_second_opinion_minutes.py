"""議事録経路への第2系統（独立系統）差分検査（--second-opinion-minutes）のテスト。

docs/security-architecture.md R8 / Phase 4 の第2系統を議事録経路へ拡張したもの。
LLM への実アクセスは行わない（utils.llm.call_rivault を monkeypatch）。
本番DB（議事録DB・pm.db）には一切書き込まない（tmp_path のみ使用）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from quality import pm_screen


def _triage_rows(conn) -> list:
    """triage_second_opinion は record_second_opinion 初回呼び出しまで存在しないため、
    未作成なら空リストを返す。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='triage_second_opinion'"
    ).fetchone()
    if not exists:
        return []
    return list(conn.execute("SELECT * FROM triage_second_opinion"))


def _collector() -> tuple[list[str], callable]:
    logs: list[str] = []

    def log(msg: str = "") -> None:
        logs.append(msg)

    return logs, log


def _make_minutes_db(path, meetings: list[dict]) -> None:
    """data/minutes/{kind}.db 相当の最小スキーマで会議を書き込む。

    meetings の各要素: {"meeting_id", "held_at", "file_path",
                         "decisions": [content...], "action_items": [content...],
                         "minutes_content": 議事録本文（省略可）}
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE instances (meeting_id TEXT PRIMARY KEY, held_at TEXT,"
        " kind TEXT, file_path TEXT, imported_at TEXT)"
    )
    conn.execute("CREATE TABLE decisions (meeting_id TEXT, content TEXT)")
    conn.execute("CREATE TABLE action_items (meeting_id TEXT, content TEXT)")
    conn.execute(
        "CREATE TABLE minutes_content (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " meeting_id TEXT, content TEXT)"
    )
    for m in meetings:
        conn.execute(
            "INSERT INTO instances (meeting_id, held_at, kind, file_path, imported_at)"
            " VALUES (?,?,?,?,?)",
            (m["meeting_id"], m["held_at"], "TestKind", m.get("file_path"), m["held_at"]),
        )
        for c in m.get("decisions", []):
            conn.execute(
                "INSERT INTO decisions (meeting_id, content) VALUES (?,?)", (m["meeting_id"], c)
            )
        for c in m.get("action_items", []):
            conn.execute(
                "INSERT INTO action_items (meeting_id, content) VALUES (?,?)",
                (m["meeting_id"], c),
            )
        if m.get("minutes_content"):
            conn.execute(
                "INSERT INTO minutes_content (meeting_id, content) VALUES (?,?)",
                (m["meeting_id"], m["minutes_content"]),
            )
    conn.commit()
    conn.close()


def _snapshot(path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    rows = []
    for table in ("instances", "decisions", "action_items"):
        rows.append(tuple(sorted(conn.execute(f"SELECT * FROM {table}").fetchall())))
    conn.close()
    return rows


@pytest.fixture
def minutes_dir(tmp_path):
    d = tmp_path / "minutes"
    d.mkdir()
    return d


@pytest.fixture
def processing_dir(tmp_path):
    d = tmp_path / "processing"
    d.mkdir()
    return d


def _write_combined(processing_dir, ts: str, basename: str, text: str) -> str:
    """{ts}-{basename}-combined.txt を書き込み、対応する instances.file_path
    （{ts}-{basename}-minutes.md）の文字列を返す。"""
    (processing_dir / f"{ts}-{basename}-combined.txt").write_text(text, encoding="utf-8")
    return f"{ts}-{basename}-minutes.md"


_SAMPLE_VTT = """1
00:00:00.000 --> 00:00:05.000
Speaker A: Good morning everyone.

2
00:00:05.000 --> 00:00:10.000
Speaker A: Let's start today's meeting agenda.
"""


def _write_vtt(processing_dir, basename: str, text: str = _SAMPLE_VTT) -> None:
    (processing_dir / f"{basename}.vtt").write_text(text, encoding="utf-8")


class TestVttTakesPriority:
    def test_vtt_is_preferred_over_resolver(self, processing_dir):
        """VTT・combined.txt の両方が存在する場合、_resolve_transcript_for_meeting は
        VTT（"vtt"）を選ぶ（combined.txt を先に探さない）。"""
        ts, basename = "2026-07-01-120000", "2026-06-30_VttPriority"
        file_path = _write_combined(processing_dir, ts, basename, "combined stage1 text")
        _write_vtt(processing_dir, basename)

        path, source_kind = pm_screen._resolve_transcript_for_meeting(file_path, processing_dir)
        assert source_kind == "vtt"
        assert path == processing_dir / f"{basename}.vtt"

    def test_vtt_used_end_to_end_when_both_present(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        ts, basename = "2026-07-01-120000", "2026-06-30_VttE2E"
        file_path = _write_combined(processing_dir, ts, basename, "combined stage1 text")
        _write_vtt(processing_dir, basename)
        _make_minutes_db(db_path, [{
            "meeting_id": f"{ts}-{basename}-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [], "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=True,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        conn.close()
        tier_lines = [m for m in logs if "入力階層の内訳" in m]
        assert len(tier_lines) == 1
        assert f"{pm_screen._TIER_LABELS['vtt']} 1 件" in tier_lines[0]
        assert f"{pm_screen._TIER_LABELS['combined_degraded']} 0 件" in tier_lines[0]


class TestCombinedFallbackWarns:
    def test_combined_only_meeting_warns_and_tags_content(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """VTT・生Whisperが無く combined.txt にしかフォールバックできない会議は、
        処理時にWARNが出て、record_second_opinion の content に階層タグ
        （combined_degraded）が残ること。"""
        db_path = minutes_dir / "TestKind.db"
        ts, basename = "2026-07-01-120000", "2026-06-30_CombinedOnly"
        file_path = _write_combined(
            processing_dir, ts, basename,
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": f"{ts}-{basename}-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["既存の決定事項"],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "第2系統だけが見つけた決定事項"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert any("Stage1" in m and "検出できません" in m for m in logs)
        assert len(rows) == 1
        assert "combined_degraded" in rows[0]["content_head"]


class TestEnvFlagDisables:
    def test_disabled_skips_entirely(self, pm_db_path, minutes_dir, processing_dir, monkeypatch):
        monkeypatch.setenv("ARGUS_SECOND_OPINION", "0")

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        conn.close()
        assert any("ARGUS_SECOND_OPINION" in m for m in logs)


class TestTranscriptNotFound:
    def test_missing_transcript_is_skipped_and_counted(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_NoTranscript-minutes",
            "held_at": "2026-06-30",
            "file_path": "2026-07-01-120000-2026-06-30_NoTranscript-minutes.md",
            "decisions": ["既存の決定事項"],
        }])
        # processing_dir は空のまま（対応する combined.txt / raw を用意しない）

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        conn.close()
        assert any("見つからなかった会議: 1 件" in m for m in logs)


# --------------------------------------------------------------------------- #
# --reader（second/k3/both）: kimi-k3 を「読み手」として追加する経路のテスト。
#
# k3 は R8（提供元レベルの集中リスク）の第2系統ではない — K3（Moonshot）は主系統
# （glm/DeepSeek/Qwen）と同じく本番経路が一系統に寄っている構図の外に出られない。
# ここで検証するのは「呼び出し経路が call_rivault と call_local_llm で正しく
# 切り替わること」と「記録の kind が systemごとに分かれること」のみ。
# --------------------------------------------------------------------------- #


def _setup_single_meeting(minutes_dir, processing_dir, basename: str) -> None:
    db_path = minutes_dir / "TestKind.db"
    ts = "2026-07-01-120000"
    file_path = _write_combined(processing_dir, ts, basename, "combined stage1 text")
    _make_minutes_db(db_path, [{
        "meeting_id": f"{ts}-{basename}-minutes",
        "held_at": "2026-06-30",
        "file_path": file_path,
        "decisions": [],
    }])


class TestReaderSecond:
    def test_reader_second_uses_call_rivault_and_records_minutes_extraction(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--reader second（既定）は従来どおり call_rivault のみを使い、
        kind="minutes_extraction" で記録される。"""
        _setup_single_meeting(minutes_dir, processing_dir, "2026-06-30_ReaderSecond")

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "second系統が見つけた項目"}],'
                            ' "action_items": []}',
        )
        monkeypatch.setattr(
            llm_mod, "call_local_llm", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, reader="second",
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert rows[0]["kind"] == "minutes_extraction"
        assert not any("production: false" in m for m in logs)


class TestReaderK3:
    def test_reader_k3_uses_call_local_llm_and_records_recall_kind(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--reader k3 は call_local_llm のみを使い（call_rivault は呼ばれない）、
        kind="minutes_extraction_recall" で記録され、content に [reader=k3] タグが付く。"""
        _setup_single_meeting(minutes_dir, processing_dir, "2026-06-30_ReaderK3")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:9999/v1")

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )
        monkeypatch.setattr(
            llm_mod, "call_local_llm",
            lambda *a, **k: '{"decisions": [{"content": "k3が見つけた項目"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, reader="k3",
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert rows[0]["kind"] == "minutes_extraction_recall"
        assert "reader=k3" in rows[0]["content_head"]
        # 2026-08-03 に PM 判断で production: true（読み手専用）になった。
        # 検証するのは「pin の注意が黙って消えていないこと」であって、
        # production の値そのものではない（値は model_pin.yaml 側のテストで検証する）。
        assert any("recall" in m and "Llama-4-Scout" in m for m in logs)


class TestReaderBoth:
    def test_reader_both_calls_both_routes_and_records_two_kinds(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--reader both は second/k3 の両方を呼び、2種類の kind が記録される。"""
        _setup_single_meeting(minutes_dir, processing_dir, "2026-06-30_ReaderBoth")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:9999/v1")

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "second系統が見つけた項目"}],'
                            ' "action_items": []}',
        )
        monkeypatch.setattr(
            llm_mod, "call_local_llm",
            lambda *a, **k: '{"decisions": [{"content": "k3が見つけた項目"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, reader="both",
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        kinds = {r["kind"] for r in rows}
        assert kinds == {"minutes_extraction", "minutes_extraction_recall"}


class TestReaderEnvFlagDisablesAll:
    @pytest.mark.parametrize("reader", ["second", "k3", "both"])
    def test_disabled_skips_regardless_of_reader(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch, reader
    ):
        """ARGUS_SECOND_OPINION=0 のときは --reader の値によらずどの読み手も呼ばれない。"""
        monkeypatch.setenv("ARGUS_SECOND_OPINION", "0")

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )
        monkeypatch.setattr(
            llm_mod, "call_local_llm", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, reader=reader,
        )
        conn.close()
        assert any("ARGUS_SECOND_OPINION" in m for m in logs)


class TestMissingItemRecorded:
    def test_second_system_only_item_is_recorded_as_missing(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        file_path = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_Meeting",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_Meeting-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["既存の決定事項A"],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "第2系統だけが見つけた決定事項B"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=lambda *_a, **_k: None,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert rows[0]["kind"] == "minutes_extraction"
        assert rows[0]["primary_verdict"] == "MISSING"
        assert rows[0]["second_verdict"] == "PRESENT"


class TestBareStringResponseDoesNotAbort:
    """第2系統が `{"decisions": ["文字列"]}`（content dict でない）を返しても
    検査が落ちず、所見として記録されること。

    2026-08-04 の実障害の再現（logs/admin_job_25725851.log）: Llama-4-Scout が
    素の文字列配列を返し、`item.get("content")` が AttributeError となって
    **会議1件の第2系統検査が丸ごと落ち、後続の読み手（k3）も走らなかった**。
    """

    def test_bare_string_items_are_recorded_not_crashed(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        file_path = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_BareStr",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_BareStr-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["既存の決定事項A"],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": ["第2系統だけが見つけた決定事項B"],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=lambda *_a, **_k: None,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert "第2系統だけが見つけた決定事項B" in rows[0]["content_head"]


class TestDedupAgainstExistingFindings:
    """同じ会議を2回検査したときに所見が二重に並ばないこと。

    2026-08-04 実測: 同一会議・同一VTT・同一モデル（kimi-k3）で検出数が 18 → 26 に
    変わり、共通していたのは 3 件だけだった。**読み手の抽出は再現しない**ため、
    重要な会議を複数回読ませて検出を積み増す使い方が有効で、その前提として
    既存所見との重複排除が要る。
    """

    def _setup(self, minutes_dir, processing_dir):
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_Dedup",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_Dedup-minutes",
            "held_at": "2026-06-30", "file_path": fp, "decisions": [],
        }])

    def _run(self, pm_db_path, minutes_dir, processing_dir, **kw):
        logs, log = _collector()
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, **kw,
        )
        rows = [dict(r) for r in conn.execute(
            "SELECT id, content_head FROM triage_second_opinion ORDER BY id")]
        conn.close()
        return rows, logs

    def test_second_run_records_only_new_items(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        self._setup(minutes_dir, processing_dir)
        import utils.llm as llm_mod

        # 1回目: 2件
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: json.dumps({
            "decisions": [{"content": "計算資源の追加割当を9月末までに実施する"},
                          {"content": "測定項目を8月14日までに登録する"}],
            "action_items": [],
        }, ensure_ascii=False))
        rows1, _ = self._run(pm_db_path, minutes_dir, processing_dir)
        assert len(rows1) == 2

        # 2回目: 1件は表現を変えた同一項目、もう1件は完全に新しい項目
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: json.dumps({
            "decisions": [{"content": "計算資源の追加割当を9月末までに実施すること"},
                          {"content": "ネットワーク感度分析のフォーマットを担当者間で調整する"}],
            "action_items": [],
        }, ensure_ascii=False))
        rows2, logs = self._run(pm_db_path, minutes_dir, processing_dir)

        assert len(rows2) == 3  # 2 + 新規1件のみ
        assert any("ネットワーク感度分析" in r["content_head"] for r in rows2)
        assert any("[重複]" in m for m in logs)
        assert any("既存所見と重複（除外） 1 件" in m for m in logs)

    def test_no_dedup_flag_records_duplicates(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--no-dedup-existing 相当（再現性の測定用）では重複も記録する。"""
        self._setup(minutes_dir, processing_dir)
        import utils.llm as llm_mod
        payload = json.dumps({
            "decisions": [{"content": "計算資源の追加割当を9月末までに実施する"}],
            "action_items": [],
        }, ensure_ascii=False)
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: payload)

        self._run(pm_db_path, minutes_dir, processing_dir, dedup_existing=False)
        rows, logs = self._run(pm_db_path, minutes_dir, processing_dir,
                               dedup_existing=False)
        assert len(rows) == 2  # 同じ内容が2行
        assert any("重複排除は無効" in m for m in logs)

    def test_other_reader_kind_is_not_deduped_against(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """読み手が違えば（kind が違えば）同じ内容でも別に記録する。

        Scout（R8 対策）と K3（recall）は別系統・別集計であり、片方の所見を
        もう片方の重複として消してはいけない。
        """
        self._setup(minutes_dir, processing_dir)
        import utils.llm as llm_mod
        payload = json.dumps({
            "decisions": [{"content": "計算資源の追加割当を9月末までに実施する"}],
            "action_items": [],
        }, ensure_ascii=False)
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: payload)
        monkeypatch.setattr(llm_mod, "call_local_llm", lambda *a, **k: payload)
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:9/v1")

        self._run(pm_db_path, minutes_dir, processing_dir, reader="second")
        rows, _ = self._run(pm_db_path, minutes_dir, processing_dir, reader="k3")
        assert len(rows) == 2
        assert sum("[reader=k3]" in r["content_head"] for r in rows) == 1


class TestChunkErrorIsIsolatedAndReported:
    """突合段で予期しない例外が出ても会議全体を落とさず、スキップ件数を必ず出す。"""

    def test_compare_failure_is_logged_and_counted(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        file_path = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_ChunkErr",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_ChunkErr-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["既存の決定事項A"],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [], "action_items": []}',
        )
        # pm_screen は関数内で `from ingest.slack import compare_extractions` して
        # いるため、差し替えは import 元（ingest.slack）側に当てる。
        from ingest import slack as ing_mod
        monkeypatch.setattr(
            ing_mod, "compare_extractions",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        logs: list[str] = []
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=lambda m="", *_a, **_k: logs.append(str(m)),
        )
        rows = _triage_rows(conn)
        conn.close()

        assert rows == []                                  # 記録は無い
        assert any("突合に失敗" in m for m in logs)          # 黙って飛ばさない
        assert any("網羅的ではありません" in m for m in logs)  # 「全部見た」と言わない


class TestSynonymousItemNotRecorded:
    def test_matching_item_is_not_recorded(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        file_path = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_Meeting2",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "2026-07-01-120000-2026-06-30_Meeting2-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["ベンチ環境を更新する"],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "ベンチ環境の更新"}], "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=lambda *_a, **_k: None,
        )
        rows = _triage_rows(conn)
        conn.close()
        assert rows == []


class TestNoDbMutation:
    def test_minutes_db_and_pm_db_source_tables_untouched(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        file_path = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_Meeting3",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の内容についての要約テキスト。",
        )
        meetings = [{
            "meeting_id": "2026-07-01-120000-2026-06-30_Meeting3-minutes",
            "held_at": "2026-06-30",
            "file_path": file_path,
            "decisions": ["既存の決定事項C"],
            "action_items": ["既存のアクションD"],
        }]
        _make_minutes_db(db_path, meetings)
        before_minutes = _snapshot(db_path)

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        before_pm_actions = list(conn.execute("SELECT * FROM action_items"))
        before_pm_decisions = list(conn.execute("SELECT * FROM decisions"))

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "第2系統のみの決定事項"}],'
                            ' "action_items": []}',
        )

        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=lambda *_a, **_k: None,
        )
        after_pm_actions = list(conn.execute("SELECT * FROM action_items"))
        after_pm_decisions = list(conn.execute("SELECT * FROM decisions"))
        conn.close()

        after_minutes = _snapshot(db_path)
        assert before_minutes == after_minutes
        assert before_pm_actions == after_pm_actions
        assert before_pm_decisions == after_pm_decisions


class TestLimitEnforced:
    def test_limit_warns_and_stops_further_processing(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        meetings = []
        for i in range(3):
            fp = _write_combined(
                processing_dir, f"2026-07-0{i+1}-120000", f"2026-06-3{i}_MeetingLimit",
                f"=== 第1部（00:00:00〜00:30:00）===\n会議{i}の内容についての要約テキスト。",
            )
            meetings.append({
                "meeting_id": f"m{i}",
                "held_at": f"2026-06-3{i}",
                "file_path": fp,
                "decisions": [],
            })
        _make_minutes_db(db_path, meetings)

        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return '{"decisions": [], "action_items": []}'

        monkeypatch.setattr(llm_mod, "call_rivault", fake)

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=1, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        conn.close()

        assert any("[WARN]" in m and "limit" in m.lower() or "上限" in m for m in logs)
        # 1会議・1チャンクのみ処理されるため呼び出しは1回のみ
        assert calls["n"] == 1


class TestDryRun:
    def test_dry_run_reports_counts_without_llm_call(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingDry",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_dry", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=True,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = _triage_rows(conn)
        conn.close()

        assert rows == []
        assert any("対象会議数" in m for m in logs)
        assert any("推定LLM呼び出し回数" in m for m in logs)


class TestMaxFindingsPerMeeting:
    """1会議・1読み手あたりの記録件数の上限（--max-findings-per-meeting）。

    読み手の粒度が主系統より細かい場合（K3が日程調整・事務連絡まで拾う等）に
    大量の「欠落」を記録してしまう問題への対策。上位N件のみ記録し、
    切り捨てた件数をWARNに明示する（黙って打ち切らない）。
    """

    def _second_opinion_payload(self, n: int) -> str:
        decisions = [{"content": f"第2系統のみが見つけた決定事項{i}"} for i in range(n)]
        return json.dumps({"decisions": decisions, "action_items": []}, ensure_ascii=False)

    def test_default_cap_truncates_and_warns(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingCap",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_cap", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        # 既定値そのものをテストに書かない（2026-08-04 に 10 → 25 へ変更した際、
        # 15 件固定のペイロードでは打ち切りが起きなくなった）。定数から導出する。
        cap = pm_screen._DEFAULT_MAX_FINDINGS_PER_MEETING
        n_found = cap + 5

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: self._second_opinion_payload(n_found),
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == cap
        assert any(
            "[WARN]" in m and str(cap) in m and str(n_found) in m
            # 上限は「妥当な件数」ではなく事故防止の last resort である旨を出すこと
            # （2026-08-04 に 25 → 100 へ上げた際に文面を変更した）
            and "last resort" in m
            for m in logs
        )
        # 切り捨てた件数（n_found - cap = 5）も明示されていること
        assert any("5" in m and "記録しません" in m for m in logs)

    def test_custom_cap_param_is_respected(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingCapCustom",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_cap_custom", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: self._second_opinion_payload(5),
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, max_findings_per_meeting=2,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 2
        assert any("[WARN]" in m and "2" in m and "5" in m for m in logs)

    def test_no_warn_when_under_cap(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingNoCap",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_no_cap", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: self._second_opinion_payload(3),
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 3
        assert not any("読み手の粒度が主系統より細かい" in m for m in logs)


# --------------------------------------------------------------------------- #
# 突合の偽陽性対策（ratio単独では検出できない②③のパターン、docs参照）:
# 議事録本文（extra_haystack）を突合対象に加え、分類の内訳をログに出す。
# --------------------------------------------------------------------------- #


class TestExtraHaystackFromMinutesContent:
    def test_body_only_item_is_excluded_and_breakdown_is_logged(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """議事録本文（minutes_content）にはあるが抽出表に無い項目は欠落として
        記録しない（②の再現）。抽出表にはあるが長さ違いで ratio だけでは
        一致しない項目も包含判定で除外される（③の再現）。真の欠落だけが
        記録され、分類の内訳がログに出ること。"""
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingHaystack",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_haystack", "held_at": "2026-06-30", "file_path": fp,
            "decisions": ["計算資源を9月末までに追加割当する"],
            "minutes_content": "## 議事内容\n今回の会議では備品を来週までに"
                               "発注することを合意した。",
        }])

        import utils.llm as llm_mod
        payload = json.dumps({
            "decisions": [
                # ③ 抽出表にもある（ratio<0.6・包含で救われる、除外）
                {"content": "関係者間で複数回にわたり議論した結果、計算資源を9月末までに"
                            "追加割当することが正式に決定した"},
                # ② 本文にはあるが抽出表に無い（extra_haystack で救われる、除外）
                {"content": "備品を来週までに発注する"},
                # ① 本文にも抽出表にも無い（真の欠落候補）
                {"content": "第2系統だけが見つけた本当の欠落決定事項"},
            ],
            "action_items": [],
        }, ensure_ascii=False)
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: payload)

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert "第2系統だけが見つけた本当の欠落決定事項" in rows[0]["content_head"]

        breakdown = [m for m in logs if m.startswith("所見:")]
        assert len(breakdown) == 1
        assert "真の欠落候補 1 件" in breakdown[0]
        assert "本文にあり（除外） 1 件" in breakdown[0]
        assert "抽出表にあり（除外） 1 件" in breakdown[0]

    def test_without_minutes_content_body_item_is_reported_missing(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """minutes_content が無い（空）会議では、本文相当の照合ができないため、
        本文にありそうな項目でも抽出表に無ければ欠落として記録される
        （extra_haystack が渡らない場合の挙動確認）。"""
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_MeetingNoContent",
            "=== 第1部（00:00:00〜00:30:00）===\n要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_no_content", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "備品を来週までに発注する"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        breakdown = [m for m in logs if m.startswith("所見:")]
        assert "真の欠落候補 1 件" in breakdown[0]
        assert "本文にあり（除外） 0 件" in breakdown[0]


# --------------------------------------------------------------------------- #
# --meeting-stem: 録音経路（pm_from_recording.sh）が処理直後の会議だけを
# 即時検査するための絞り込み。instances.file_path の拡張子抜きファイル名
# （Path(file_path).stem）で一致させる。
# --------------------------------------------------------------------------- #


class TestMeetingStemFilter:
    def test_only_matching_meeting_is_processed(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--meeting-stem を指定すると、複数会議が対象期間内にあってもその1件
        だけが処理される（他方の会議はLLMに渡されない）。"""
        db_path = minutes_dir / "TestKind.db"
        fp_a = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_StemA",
            "=== 第1部（00:00:00〜00:30:00）===\n会議Aの要約テキスト。",
        )
        fp_b = _write_combined(
            processing_dir, "2026-07-02-120000", "2026-07-01_StemB",
            "=== 第1部（00:00:00〜00:30:00）===\n会議Bの要約テキスト。",
        )
        _make_minutes_db(db_path, [
            {"meeting_id": "m_stem_a", "held_at": "2026-06-30", "file_path": fp_a,
             "decisions": []},
            {"meeting_id": "m_stem_b", "held_at": "2026-07-01", "file_path": fp_b,
             "decisions": []},
        ])

        import utils.llm as llm_mod
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return '{"decisions": [], "action_items": []}'

        monkeypatch.setattr(llm_mod, "call_rivault", fake)

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        target_stem = Path(fp_b).stem
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, meeting_stem=target_stem,
        )
        conn.close()

        assert any("対象会議数: 1 件" in m for m in logs)
        assert len(prompts) == 1
        assert "会議Bの要約テキスト" in prompts[0]
        assert "会議Aの要約テキスト" not in prompts[0]


class TestMeetingStemNotFound:
    def test_unknown_stem_warns_and_returns_without_error(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """存在しない会議を --meeting-stem に指定しても例外にせず、警告を出して
        何も処理せずに正常終了する（録音ジョブ全体を落とさないため）。"""
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_StemUnknown",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_stem_unknown", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない")
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        # 例外を送出しないことそのものがアサーション（送出されればテストが落ちる）
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=10, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, meeting_stem="not-a-real-meeting-stem",
        )
        rows = _triage_rows(conn)
        conn.close()

        assert rows == []
        assert any(
            "[WARN]" in m and "not-a-real-meeting-stem" in m for m in logs
        )


class TestMeetingStemIgnoresLimit:
    def test_limit_is_ignored_when_meeting_stem_given(
        self, pm_db_path, minutes_dir, processing_dir, monkeypatch
    ):
        """--meeting-stem 指定時は --limit を無視する（対象は常に高々1件）。
        limit=0（通常なら1件でも超過扱いで打ち切られる値）を渡しても
        --meeting-stem 指定時は打ち切られず処理されること。"""
        db_path = minutes_dir / "TestKind.db"
        fp = _write_combined(
            processing_dir, "2026-07-01-120000", "2026-06-30_StemLimit",
            "=== 第1部（00:00:00〜00:30:00）===\n会議の要約テキスト。",
        )
        _make_minutes_db(db_path, [{
            "meeting_id": "m_stem_limit", "held_at": "2026-06-30", "file_path": fp,
            "decisions": [],
        }])

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "限定検査で見つかった項目"}],'
                            ' "action_items": []}',
        )

        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        logs, log = _collector()
        target_stem = Path(fp).stem
        pm_screen.run_second_opinion_minutes(
            conn, since="2026-01-01", limit=0, dry_run=False,
            minutes_dir=minutes_dir, processing_dir=processing_dir,
            no_encrypt=True, log=log, meeting_stem=target_stem,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()

        assert len(rows) == 1
        assert not any("--limit" in m and "超えています" in m for m in logs)
