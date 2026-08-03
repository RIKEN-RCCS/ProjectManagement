"""議事録経路への第2系統（独立系統）差分検査（--second-opinion-minutes）のテスト。

docs/security-architecture.md R8 / Phase 4 の第2系統を議事録経路へ拡張したもの。
LLM への実アクセスは行わない（utils.llm.call_rivault を monkeypatch）。
本番DB（議事録DB・pm.db）には一切書き込まない（tmp_path のみ使用）。
"""
from __future__ import annotations

import sqlite3

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
                         "decisions": [content...], "action_items": [content...]}
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE instances (meeting_id TEXT PRIMARY KEY, held_at TEXT,"
        " kind TEXT, file_path TEXT, imported_at TEXT)"
    )
    conn.execute("CREATE TABLE decisions (meeting_id TEXT, content TEXT)")
    conn.execute("CREATE TABLE action_items (meeting_id TEXT, content TEXT)")
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
