"""pm_screen.py --triage（既存データの一括トリアージ）モードのテスト。

LLM 実接続なし（ingest.slack.triage_items を monkeypatch）。人名はダミー
（富岳太郎等）のみを使う。実データ（data/pm.db 等）には書き込まない
（pm_db_path フィクスチャの一時 DB のみを使用する）。
"""
from __future__ import annotations

import sqlite3
import sys

from ingest import slack as slack_mod
from quality import pm_relink, pm_screen


def _collector() -> tuple[list[str], callable]:
    logs: list[str] = []

    def log(msg: str = "") -> None:
        logs.append(msg)

    return logs, log


def _conn(pm_db_path):
    c = sqlite3.connect(str(pm_db_path))
    c.row_factory = sqlite3.Row
    return c


def _seed_milestone(pm_db_path) -> None:
    """fetch_milestones() が空を返すとトリアージ自体がスキップされる（F2）ため、
    トリアージが実際に走ることを前提とするテストでは事前に1件登録する。"""
    conn = _conn(pm_db_path)
    conn.execute(
        "INSERT INTO milestones (milestone_id, name, due_date, area, status)"
        " VALUES ('M1', 'テストマイルストーン', '2026-12-31', 'テスト', 'active')"
    )
    conn.commit()
    conn.close()


def _drop_if_contains(*substrings: str):
    def fake_triage_items(extracted, milestones, *, context_note="", return_verdicts=False,
                          missing_verdict="DROP"):
        d_items = extracted.get("decisions") or []
        a_items = extracted.get("action_items") or []

        def verdict_for(content: str | None) -> str:
            return "DROP" if any(s in (content or "") for s in substrings) else "KEEP"

        result = {
            "decisions": [d for d in d_items if verdict_for(d.get("content")) == "KEEP"],
            "action_items": [a for a in a_items if verdict_for(a.get("content")) == "KEEP"],
        }
        if return_verdicts:
            result["verdicts"] = {
                "decisions": [
                    {"content": d.get("content"), "verdict": verdict_for(d.get("content")), "reason": "テスト理由"}
                    for d in d_items
                ],
                "action_items": [
                    {"content": a.get("content"), "verdict": verdict_for(a.get("content")), "reason": "テスト理由"}
                    for a in a_items
                ],
            }
        return result
    return fake_triage_items


def _seed_meeting_group(pm_db_path):
    conn = _conn(pm_db_path)
    conn.execute(
        "INSERT INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES ('m1','2026-07-01','TestKind',NULL,'議事概要','2026-07-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
        " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("ミーティングのアジェンダを準備する。", "富岳太郎", None, None, "open", "meeting", "m1", "2026-07-01"),
    )
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
        " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("マイルストーンM1に必須の性能評価レポートを作成する。", "富岳次郎", None, None, "open", "meeting", "m1", "2026-07-01"),
    )
    conn.execute(
        "INSERT INTO decisions (content, decided_at, source, meeting_id, extracted_at, deleted)"
        " VALUES (?,?,?,?,?,0)",
        ("会議日程を来週に変更する。", "2026-07-01", "meeting", "m1", "2026-07-01"),
    )
    conn.commit()
    conn.close()


def test_triage_only_drop_in_csv(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    _seed_meeting_group(pm_db_path)
    monkeypatch.setattr(slack_mod, "triage_items", _drop_if_contains("アジェンダ", "会議日程"))

    conn = _conn(pm_db_path)
    _logs, log = _collector()
    output_path = tmp_path / "triage.csv"
    pm_screen.run_triage(conn, None, False, str(output_path), log)
    conn.close()

    text = output_path.read_text(encoding="utf-8")
    assert "マイルストーンM1に必須の性能評価レポートを作成する。" not in text
    assert "アジェンダを準備する" in text
    assert "会議日程を来週に変更する" in text


def test_triage_csv_readable_by_pm_relink_parser(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    _seed_meeting_group(pm_db_path)
    monkeypatch.setattr(slack_mod, "triage_items", _drop_if_contains("アジェンダ", "会議日程"))

    conn = _conn(pm_db_path)
    output_path = tmp_path / "triage.csv"
    pm_screen.run_triage(conn, None, False, str(output_path), lambda *a, **k: None)
    conn.close()

    text = output_path.read_text(encoding="utf-8")
    action_lines, decision_lines = pm_relink._split_sections(text)
    ai_rows = pm_relink._parse_action_rows(action_lines)
    dec_rows = pm_relink._parse_decision_rows(decision_lines)

    assert len(ai_rows) == 1
    assert len(dec_rows) == 1
    for values in ai_rows.values():
        assert values.get("deleted") == 1
    for values in dec_rows.values():
        assert values.get("deleted") == 1


def test_triage_llm_failure_group_skipped(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    conn = _conn(pm_db_path)
    conn.execute(
        "INSERT INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES ('m_fail','2026-07-02','TestKind',NULL,'概要','2026-07-02T00:00:00')"
    )
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
        " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("テスト項目A。", "富岳太郎", None, None, "open", "meeting", "m_fail", "2026-07-02"),
    )
    conn.commit()
    conn.close()

    def boom(*a, **k):
        raise RuntimeError("LLM接続失敗（テスト用）")
    monkeypatch.setattr(slack_mod, "triage_items", boom)

    conn2 = _conn(pm_db_path)
    logs, log = _collector()
    output_path = tmp_path / "triage_fail.csv"
    pm_screen.run_triage(conn2, None, False, str(output_path), log)
    conn2.close()

    assert any("WARN" in line for line in logs)
    text = output_path.read_text(encoding="utf-8")
    assert "テスト項目A" not in text


# --------------------------------------------------------------------------- #
# F1: --triage --output <path> で CSV がログ出力により破壊されないこと
# --------------------------------------------------------------------------- #
def test_main_triage_output_not_corrupted_by_logger(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    _seed_meeting_group(pm_db_path)
    monkeypatch.setattr(slack_mod, "triage_items", _drop_if_contains("アジェンダ", "会議日程"))

    output_path = tmp_path / "triage_main.csv"
    argv = [
        "pm_screen.py", "--triage", "--no-encrypt",
        "--db", str(pm_db_path), "--output", str(output_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    pm_screen.main()

    text = output_path.read_text(encoding="utf-8")
    assert text.startswith("# LLM 判定の一括審査結果")
    assert "アジェンダを準備する" in text
    assert "マイルストーンM1に必須の性能評価レポートを作成する。" not in text


# --------------------------------------------------------------------------- #
# F2: マイルストーン未登録時はトリアージ自体をスキップし全件 KEEP 扱いにする
# --------------------------------------------------------------------------- #
def test_triage_skips_when_no_milestones(pm_db_path, tmp_path, monkeypatch):
    _seed_meeting_group(pm_db_path)  # 意図的に _seed_milestone を呼ばない

    def boom(*a, **k):
        raise AssertionError("マイルストーン未登録時に triage_items が呼ばれてはならない")
    monkeypatch.setattr(slack_mod, "triage_items", boom)

    conn = _conn(pm_db_path)
    logs, log = _collector()
    output_path = tmp_path / "triage_no_milestone.csv"
    pm_screen.run_triage(conn, None, False, str(output_path), log)
    conn.close()

    assert any("マイルストーン未登録" in line for line in logs)
    text = output_path.read_text(encoding="utf-8")
    assert "アジェンダ" not in text
    assert "会議日程" not in text


# --------------------------------------------------------------------------- #
# R2: 1グループ内の1チャンク失敗で他チャンク・他グループの結果まで捨てない
# --------------------------------------------------------------------------- #
def test_triage_partial_chunk_failure_keeps_other_results(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    conn = _conn(pm_db_path)
    conn.execute(
        "INSERT INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES ('m_ok','2026-07-03','TestKind',NULL,'概要','2026-07-03T00:00:00')"
    )
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
        " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("会議のアジェンダを準備する。", "富岳太郎", None, None, "open", "meeting", "m_ok", "2026-07-03"),
    )
    conn.execute(
        "INSERT INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES ('m_bad','2026-07-04','TestKind',NULL,'概要','2026-07-04T00:00:00')"
    )
    conn.execute(
        "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
        " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("障害グループのアジェンダを準備する。", "富岳次郎", None, None, "open", "meeting", "m_bad", "2026-07-04"),
    )
    conn.commit()
    conn.close()

    def flaky(extracted, milestones, *, context_note="", return_verdicts=False, missing_verdict="DROP"):
        if "2026-07-04" in context_note:
            raise RuntimeError("LLM接続失敗（テスト用）")
        return _drop_if_contains("アジェンダ")(
            extracted, milestones, context_note=context_note,
            return_verdicts=return_verdicts, missing_verdict=missing_verdict,
        )

    monkeypatch.setattr(slack_mod, "triage_items", flaky)

    conn2 = _conn(pm_db_path)
    logs, log = _collector()
    output_path = tmp_path / "triage_partial.csv"
    pm_screen.run_triage(conn2, None, False, str(output_path), log)
    conn2.close()

    text = output_path.read_text(encoding="utf-8")
    assert "会議のアジェンダを準備する。" in text  # 成功グループの結果は保持される
    assert "障害グループのアジェンダを準備する。" not in text  # 失敗チャンクはKEEP扱い
    assert any("チャンク" in line and "スキップ" in line for line in logs)


# --------------------------------------------------------------------------- #
# R2強化: 同一 meeting 内で20件バッチ分割された場合、片チャンク失敗でも
# もう片方のチャンクの結果（DROP判定）が保持されること
# --------------------------------------------------------------------------- #
def test_triage_chunk_split_within_single_meeting_keeps_other_chunk_results(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    conn = _conn(pm_db_path)
    conn.execute(
        "INSERT INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES ('m_chunks','2026-07-05','TestKind',NULL,'概要','2026-07-05T00:00:00')"
    )
    # チャンク1（20件、id昇順で先頭20件）: このチャンクは例外発生 → 全件 KEEP 扱い
    for i in range(20):
        conn.execute(
            "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
            " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
            (f"通常項目{i:02d}を実施する。", "富岳太郎", None, None, "open", "meeting", "m_chunks", "2026-07-05"),
        )
    # チャンク2（5件、id昇順で残り）: 正常に審査され、アジェンダ関連のみDROP
    for i in range(3):
        conn.execute(
            "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
            " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
            (f"会議のアジェンダ{i}を準備する。", "富岳次郎", None, None, "open", "meeting", "m_chunks", "2026-07-05"),
        )
    for i in range(2):
        conn.execute(
            "INSERT INTO action_items (content, assignee, due_date, milestone_id, status, source,"
            " meeting_id, extracted_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
            (f"重要作業{i}を完了させる。", "富岳三郎", None, None, "open", "meeting", "m_chunks", "2026-07-05"),
        )
    conn.commit()
    conn.close()

    # チャンクサイズ（20件）で判定してチャンク1のみ失敗させる。
    # 同一 meeting 内の全チャンクが同じ context_note を共有するため、
    # グループ単位ではなくチャンク内件数で失敗チャンクを識別する。
    def flaky_by_chunk_size(extracted, milestones, *, context_note="", return_verdicts=False,
                            missing_verdict="DROP"):
        a_items = extracted.get("action_items") or []
        if len(a_items) == 20:
            raise RuntimeError("LLM接続失敗（テスト用、大チャンク）")
        return _drop_if_contains("アジェンダ")(
            extracted, milestones, context_note=context_note,
            return_verdicts=return_verdicts, missing_verdict=missing_verdict,
        )

    monkeypatch.setattr(slack_mod, "triage_items", flaky_by_chunk_size)

    conn2 = _conn(pm_db_path)
    logs, log = _collector()
    output_path = tmp_path / "triage_chunk_split.csv"
    pm_screen.run_triage(conn2, None, False, str(output_path), log)
    conn2.close()

    text = output_path.read_text(encoding="utf-8")
    # チャンク1（失敗）の項目はDROPされない（CSVに出ない、KEEP扱い）
    for i in range(20):
        assert f"通常項目{i:02d}を実施する。" not in text
    # チャンク2（成功）のアジェンダ関連はDROPされてCSVに出る
    for i in range(3):
        assert f"会議のアジェンダ{i}を準備する。" in text
    # チャンク2の非アジェンダ項目はKEEPされる（CSVに出ない）
    for i in range(2):
        assert f"重要作業{i}を完了させる。" not in text
    assert any("チャンク" in line and "スキップ" in line for line in logs)
