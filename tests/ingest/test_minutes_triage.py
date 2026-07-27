"""ingest/minutes.py の転記時トリアージ（transfer_meeting の triage 引数）のテスト。

LLM 実接続なし（ingest.slack.triage_items を monkeypatch）。人名はダミー
（富岳太郎等）のみを使う。実データ（data/pm.db 等）には書き込まない
（pm_db_path フィクスチャの一時 DB のみを使用する）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ingest import minutes as minutes_mod
from ingest import slack as slack_mod

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------- #
# フィクスチャ用ヘルパ
# --------------------------------------------------------------------------- #
def _init_minutes_db(tmp_path: Path, name: str = "TestKind") -> Path:
    from pm_minutes_import import init_minutes_db
    db_file = tmp_path / f"{name}.db"
    conn = init_minutes_db(db_file, no_encrypt=True)
    conn.close()
    return db_file


def _open_minutes_conn(db_file: Path):
    from pm_minutes_import import init_minutes_db
    return init_minutes_db(db_file, no_encrypt=True)


def _open_pm_conn(pm_db_path: Path):
    conn = sqlite3.connect(str(pm_db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_milestone(pm_db_path: Path) -> None:
    """fetch_milestones() が空を返すとトリアージ自体がスキップされる（F2）ため、
    トリアージが実際に走ることを前提とするテストでは事前に1件登録する。"""
    conn = sqlite3.connect(str(pm_db_path))
    conn.execute(
        "INSERT INTO milestones (milestone_id, name, due_date, area, status)"
        " VALUES ('M1', 'テストマイルストーン', '2026-12-31', 'テスト', 'active')"
    )
    conn.commit()
    conn.close()


def _insert_instance(db_file: Path, meeting_id: str, held_at: str, kind: str,
                     content: str = "議事の概要本文。") -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO instances (meeting_id, held_at, kind, file_path, imported_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (meeting_id, held_at, kind, None, "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO minutes_content (meeting_id, content) VALUES (?, ?)",
        (meeting_id, content),
    )
    conn.commit()
    conn.close()


def _insert_decision(db_file: Path, meeting_id: str, content: str) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO decisions (meeting_id, content, source_context, rationale, trade_off, reversal_condition)"
        " VALUES (?, ?, NULL, NULL, NULL, NULL)",
        (meeting_id, content),
    )
    conn.commit()
    conn.close()


def _insert_action_item(db_file: Path, meeting_id: str, content: str,
                        assignee: str = "富岳太郎", due_date: str | None = None) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO action_items (meeting_id, content, assignee, due_date) VALUES (?, ?, ?, ?)",
        (meeting_id, content, assignee, due_date),
    )
    conn.commit()
    conn.close()


def _drop_fake(reason: str = "マイルストーン非関連"):
    def fake_triage_items(extracted, milestones, *, context_note="", return_verdicts=False,
                          missing_verdict="DROP"):
        d_items = extracted.get("decisions") or []
        a_items = extracted.get("action_items") or []
        result = {"decisions": [], "action_items": []}
        if return_verdicts:
            result["verdicts"] = {
                "decisions": [{"content": d.get("content"), "verdict": "DROP", "reason": reason} for d in d_items],
                "action_items": [{"content": a.get("content"), "verdict": "DROP", "reason": reason} for a in a_items],
            }
        return result
    return fake_triage_items


def _keep_fake():
    def fake_triage_items(extracted, milestones, *, context_note="", return_verdicts=False,
                          missing_verdict="DROP"):
        d_items = extracted.get("decisions") or []
        a_items = extracted.get("action_items") or []
        result = {"decisions": list(d_items), "action_items": list(a_items)}
        if return_verdicts:
            result["verdicts"] = {
                "decisions": [{"content": d.get("content"), "verdict": "KEEP", "reason": ""} for d in d_items],
                "action_items": [{"content": a.get("content"), "verdict": "KEEP", "reason": ""} for a in a_items],
            }
        return result
    return fake_triage_items


def _capturing_fake(calls: list, drop_substrings: tuple[str, ...] = ()):
    def fake_triage_items(extracted, milestones, *, context_note="", return_verdicts=False,
                          missing_verdict="DROP"):
        calls.append(extracted)
        d_items = extracted.get("decisions") or []
        a_items = extracted.get("action_items") or []

        def verdict_for(content: str | None) -> str:
            return "DROP" if any(s in (content or "") for s in drop_substrings) else "KEEP"

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


# --------------------------------------------------------------------------- #
# DROP → deleted=1 で INSERT + audit_log
# --------------------------------------------------------------------------- #
def test_drop_inserted_as_deleted_with_audit_log(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_drop"
    _insert_instance(db_file, meeting_id, "2026-07-01", "TestKind")
    _insert_decision(db_file, meeting_id, "会議日程を調整する。")
    _insert_action_item(db_file, meeting_id, "スライドをBoxにアップロードする。")

    monkeypatch.setattr(slack_mod, "triage_items", _drop_fake("マイルストーン非関連"))

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-01", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()

    assert status == "ok"

    dec_row = pm_conn.execute(
        "SELECT deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert dec_row["deleted"] == 1
    ai_row = pm_conn.execute(
        "SELECT deleted FROM action_items WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert ai_row["deleted"] == 1

    audit_rows = pm_conn.execute(
        "SELECT table_name, field, old_value, new_value FROM audit_log"
        " WHERE source='minutes_triage' ORDER BY id"
    ).fetchall()
    fields = {r["field"] for r in audit_rows}
    assert "deleted" in fields
    assert "triage_reason" in fields
    deleted_rows = [r for r in audit_rows if r["field"] == "deleted"]
    assert all(r["old_value"] == "0" and r["new_value"] == "1" for r in deleted_rows)
    reason_rows = [r for r in audit_rows if r["field"] == "triage_reason"]
    assert all(r["new_value"] == "マイルストーン非関連" for r in reason_rows)
    # decisions と action_items 両方について記録されていること
    assert {r["table_name"] for r in audit_rows} == {"decisions", "action_items"}

    pm_conn.close()


# --------------------------------------------------------------------------- #
# KEEP → 通常INSERT（deleted=0）
# --------------------------------------------------------------------------- #
def test_keep_inserted_normally(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_keep"
    _insert_instance(db_file, meeting_id, "2026-07-03", "TestKind")
    _insert_decision(db_file, meeting_id, "重要な方針を決定した。")
    _insert_action_item(db_file, meeting_id, "重要な資料を作成する。")

    monkeypatch.setattr(slack_mod, "triage_items", _keep_fake())

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-03", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()

    assert status == "ok"
    dec_row = pm_conn.execute(
        "SELECT deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert dec_row["deleted"] == 0
    ai_row = pm_conn.execute(
        "SELECT deleted FROM action_items WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert ai_row["deleted"] == 0

    n_audit = pm_conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE source='minutes_triage'"
    ).fetchone()["c"]
    assert n_audit == 0

    pm_conn.close()


# --------------------------------------------------------------------------- #
# triage_items が例外 → 全件 INSERT（フェイルオープン）+ WARN ログ
# --------------------------------------------------------------------------- #
def test_triage_exception_fails_open(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_fail"
    _insert_instance(db_file, meeting_id, "2026-07-06", "TestKind")
    _insert_decision(db_file, meeting_id, "何らかの決定事項。")

    def boom(*a, **k):
        raise RuntimeError("LLM接続失敗（テスト用）")
    monkeypatch.setattr(slack_mod, "triage_items", boom)

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    logs: list[str] = []
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-06", "TestKind", None,
        force=False, dry_run=False, log=logs.append,
    )
    minutes_conn.close()

    assert status == "ok"
    dec_row = pm_conn.execute(
        "SELECT deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert dec_row["deleted"] == 0
    assert any("[WARN]" in line for line in logs)

    pm_conn.close()


# --------------------------------------------------------------------------- #
# triage=False → triage_items が呼ばれない
# --------------------------------------------------------------------------- #
def test_triage_false_skips_triage_items(pm_db_path, tmp_path, monkeypatch):
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_notriage"
    _insert_instance(db_file, meeting_id, "2026-07-07", "TestKind")
    _insert_decision(db_file, meeting_id, "何らかの決定事項2。")

    def boom(*a, **k):
        raise AssertionError("triage_items は triage=False では呼ばれてはならない")
    monkeypatch.setattr(slack_mod, "triage_items", boom)

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-07", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
        triage=False,
    )
    minutes_conn.close()

    assert status == "ok"
    dec_row = pm_conn.execute(
        "SELECT deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert dec_row["deleted"] == 0

    pm_conn.close()


# --------------------------------------------------------------------------- #
# ARGUS_DISABLE_MINUTES_TRIAGE=1 → 呼ばれない
# --------------------------------------------------------------------------- #
def test_env_disable_skips_triage_items(pm_db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DISABLE_MINUTES_TRIAGE", "1")
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_envdisable"
    _insert_instance(db_file, meeting_id, "2026-07-08", "TestKind")
    _insert_decision(db_file, meeting_id, "何らかの決定事項3。")

    def boom(*a, **k):
        raise AssertionError("triage_items は ARGUS_DISABLE_MINUTES_TRIAGE=1 では呼ばれてはならない")
    monkeypatch.setattr(slack_mod, "triage_items", boom)

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-08", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()

    assert status == "ok"
    pm_conn.close()


# --------------------------------------------------------------------------- #
# dry_run=True → DB変化なし（トリアージ自体は呼ばれる）
# --------------------------------------------------------------------------- #
def test_dry_run_no_db_changes_but_triage_runs(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_dryrun"
    _insert_instance(db_file, meeting_id, "2026-07-09", "TestKind")
    _insert_decision(db_file, meeting_id, "何らかの決定事項4。")

    calls = {"n": 0}

    def fake(extracted, milestones, *, context_note="", return_verdicts=False, missing_verdict="DROP"):
        calls["n"] += 1
        return _drop_fake()(extracted, milestones, context_note=context_note, return_verdicts=return_verdicts,
                           missing_verdict=missing_verdict)

    monkeypatch.setattr(slack_mod, "triage_items", fake)

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-09", "TestKind", None,
        force=False, dry_run=True, log=lambda *a, **k: None,
    )
    minutes_conn.close()

    assert status == "ok"
    assert calls["n"] == 1
    assert pm_conn.execute("SELECT COUNT(*) c FROM meetings").fetchone()["c"] == 0
    assert pm_conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 0

    pm_conn.close()


# --------------------------------------------------------------------------- #
# force=True 再転記 → DROP項目が復活せず、deleted セット内候補は再審査対象外
# --------------------------------------------------------------------------- #
def test_force_reinsert_excludes_deleted_from_triage_and_no_revival(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_force"
    _insert_instance(db_file, meeting_id, "2026-07-10", "TestKind")
    content_a = "決定A（DROP対象・マイルストーン非関連）"
    content_b = "決定B（KEEP対象）"
    _insert_decision(db_file, meeting_id, content_a)
    _insert_decision(db_file, meeting_id, content_b)

    calls: list[dict] = []
    monkeypatch.setattr(slack_mod, "triage_items", _capturing_fake(calls, drop_substrings=(content_a,)))

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-10", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()
    assert status == "ok"

    assert len(calls) == 1
    assert len(calls[0]["decisions"]) == 2  # 初回は両方トリアージ対象

    rows = pm_conn.execute(
        "SELECT content, deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchall()
    by_content = {r["content"]: r["deleted"] for r in rows}
    assert by_content[content_a] == 1
    assert by_content[content_b] == 0

    # --- force 再転記 ---
    calls.clear()
    minutes_conn = _open_minutes_conn(db_file)
    status2 = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-10", "TestKind", None,
        force=True, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()
    assert status2 == "ok"

    # deleted セット内の候補（content_a）はトリアージ対象から除外される
    assert len(calls) == 1
    assert len(calls[0]["decisions"]) == 1
    assert calls[0]["decisions"][0]["content"] == content_b

    rows2 = pm_conn.execute(
        "SELECT content, deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchall()
    by_content2 = {r["content"]: r["deleted"] for r in rows2}
    assert by_content2[content_a] == 1  # 復活していない
    assert by_content2[content_b] == 0
    # S2: force 再転記で行が増殖していないこと（回帰固定）
    assert len(rows2) == 2

    pm_conn.close()


# --------------------------------------------------------------------------- #
# S1: force 再転記時、人間が Web UI で deleted=0 に復元した項目は
# トリアージ対象から除外され、無条件 KEEP のまま再挿入される（LLMが人間の
# 最終判断を覆さない）
# --------------------------------------------------------------------------- #
def test_force_reinsert_respects_human_restored_item(pm_db_path, tmp_path, monkeypatch):
    _seed_milestone(pm_db_path)
    db_file = _init_minutes_db(tmp_path)
    meeting_id = "m_human_kept"
    _insert_instance(db_file, meeting_id, "2026-07-11", "TestKind")
    content_a = "決定A（当初DROP・後に人間が復元）"
    content_b = "決定B（通常KEEP）"
    _insert_decision(db_file, meeting_id, content_a)
    _insert_decision(db_file, meeting_id, content_b)

    calls: list[dict] = []
    monkeypatch.setattr(slack_mod, "triage_items", _capturing_fake(calls, drop_substrings=(content_a,)))

    pm_conn = _open_pm_conn(pm_db_path)
    minutes_conn = _open_minutes_conn(db_file)
    status = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-11", "TestKind", None,
        force=False, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()
    assert status == "ok"

    row_a = pm_conn.execute(
        "SELECT id, deleted FROM decisions WHERE meeting_id=? AND content=?",
        (meeting_id, content_a),
    ).fetchone()
    assert row_a["deleted"] == 1

    # --- 人間が Web UI で復元（web_utils.audit() と同じ形式で記録） ---
    pm_conn.execute("UPDATE decisions SET deleted=0 WHERE id=?", (row_a["id"],))
    pm_conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES ('decisions', ?, 'deleted', '1', '0', ?, 'web_ui')",
        (str(row_a["id"]), "2026-07-11T12:00:00+00:00"),
    )
    pm_conn.commit()

    # --- force 再転記 ---
    calls.clear()
    minutes_conn = _open_minutes_conn(db_file)
    status2 = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-11", "TestKind", None,
        force=True, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()
    assert status2 == "ok"

    # 人間が復元した content_a はトリアージ対象から除外される（content_b のみ渡る）
    assert len(calls) == 1
    assert len(calls[0]["decisions"]) == 1
    assert calls[0]["decisions"][0]["content"] == content_b

    rows = pm_conn.execute(
        "SELECT content, deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchall()
    by_content = {r["content"]: r["deleted"] for r in rows}
    assert by_content[content_a] == 0  # 人間の判断が保持されている（LLMに再びDROPされていない）
    assert by_content[content_b] == 0
    assert len(rows) == 2  # S2: 行数不変（増殖していない）

    # --- 2回目の force 再転記 ---
    # force は対象行を DELETE→INSERT するため content_a の record_id は変わっている。
    # human_kept 保護が新しい record_id に付け替えられていなければ、この3回目の
    # force で content_a が再びトリアージ対象に含まれ、LLM に DROP されてしまう
    # （実際に発生した回帰。_write_human_kept_audit() が修正）。
    calls.clear()
    minutes_conn = _open_minutes_conn(db_file)
    status3 = minutes_mod.transfer_meeting(
        pm_conn, minutes_conn, meeting_id, "2026-07-11", "TestKind", None,
        force=True, dry_run=False, log=lambda *a, **k: None,
    )
    minutes_conn.close()
    assert status3 == "ok"

    assert len(calls) == 1
    assert len(calls[0]["decisions"]) == 1
    assert calls[0]["decisions"][0]["content"] == content_b

    rows3 = pm_conn.execute(
        "SELECT content, deleted FROM decisions WHERE meeting_id=?", (meeting_id,)
    ).fetchall()
    by_content3 = {r["content"]: r["deleted"] for r in rows3}
    assert by_content3[content_a] == 0  # 2回目の force でも人間の判断が保持されている
    assert by_content3[content_b] == 0
    assert len(rows3) == 2

    pm_conn.close()


# --------------------------------------------------------------------------- #
# pm_minutes_publish.py 経由の呼び出しが triage=False であること
# --------------------------------------------------------------------------- #
def test_publish_calls_transfer_meeting_with_triage_false():
    src_path = REPO_ROOT / "scripts" / "minutes" / "pm_minutes_publish.py"
    text = src_path.read_text(encoding="utf-8")
    idx = text.index("transfer_meeting(")
    block = text[idx: idx + 400]
    assert "triage=False" in block
