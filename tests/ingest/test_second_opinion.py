"""第2系統による差分検査（docs/security-architecture.md §4.9 対策3+5）のテスト。"""
from __future__ import annotations

import sqlite3

import pytest
from ingest import slack as ing


class TestFlagSensitiveTerms:
    def test_detects_geopolitical_term(self):
        assert "中国" in ing.flag_sensitive_terms("中国製モデルの採用を検討する")

    def test_detects_english_term(self):
        assert "Taiwan" in ing.flag_sensitive_terms("Taiwan fab capacity")

    def test_detects_vendor_name(self):
        assert "Moonshot" in ing.flag_sensitive_terms("Moonshot の K3 を評価する")

    def test_plain_text_has_no_flags(self):
        assert ing.flag_sensitive_terms("次回までにベンチマーク結果をまとめる") == []

    def test_empty_is_empty(self):
        assert ing.flag_sensitive_terms("") == []

    def test_result_is_sorted_and_deduped(self):
        got = ing.flag_sensitive_terms("中国と中国製、そして China")
        assert got == sorted(set(got))


class TestApplySecondOpinion:
    @pytest.fixture
    def results(self):
        return {
            "action_items": [({"content": "中国製モデルの利用可否を整理する"}, "DROP", "")],
            "decisions": [({"content": "ベンチ環境を更新する"}, "KEEP", "")],
        }

    def test_only_flagged_items_are_sent(self, results, monkeypatch):
        sent = []

        def fake(content, milestones, **kw):
            sent.append(content)
            return "KEEP", "raw"

        monkeypatch.setattr(ing, "second_opinion_verdict", fake)
        ing.apply_second_opinion(results, [], log=lambda *_: None)
        assert sent == ["中国製モデルの利用可否を整理する"]  # フラグ無しの項目は送らない

    def test_disagreement_is_reported_but_not_applied(self, results, monkeypatch):
        monkeypatch.setattr(ing, "second_opinion_verdict", lambda *a, **k: ("KEEP", "raw"))
        dis = ing.apply_second_opinion(results, [], log=lambda *_: None)
        assert len(dis) == 1
        assert dis[0]["primary"] == "DROP" and dis[0]["second"] == "KEEP"
        # 主系統の判定は書き換えない（自動で覆さない）
        assert results["action_items"][0][1] == "DROP"

    def test_agreement_produces_no_disagreement(self, results, monkeypatch):
        monkeypatch.setattr(ing, "second_opinion_verdict", lambda *a, **k: ("DROP", "raw"))
        assert ing.apply_second_opinion(results, [], log=lambda *_: None) == []

    def test_env_flag_disables(self, results, monkeypatch):
        monkeypatch.setenv("ARGUS_SECOND_OPINION", "0")
        monkeypatch.setattr(ing, "second_opinion_verdict",
                            lambda *a, **k: pytest.fail("呼ばれてはいけない"))
        assert ing.apply_second_opinion(results, [], log=lambda *_: None) == []

    def test_failure_of_second_system_skips_item(self, results, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("エンドポイント不通")

        monkeypatch.setattr(ing, "second_opinion_verdict", boom)
        assert ing.apply_second_opinion(results, [], log=lambda *_: None) == []

    def test_records_both_agreement_and_disagreement(self, results, pm_db_path, monkeypatch):
        monkeypatch.setattr(ing, "second_opinion_verdict", lambda *a, **k: ("KEEP", "raw"))
        conn = sqlite3.connect(str(pm_db_path)); conn.row_factory = sqlite3.Row
        ing.apply_second_opinion(results, [], conn=conn, log=lambda *_: None)
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()
        assert len(rows) == 1
        assert rows[0]["agreed"] == 0
        assert "中国" in rows[0]["flagged_terms"]


class TestFailDirection:
    def test_triage_items_defaults_to_keep(self):
        """Phase 4: 判定不能時の fail 方向を KEEP に統一した（欠落を作らない）。"""
        import inspect
        sig = inspect.signature(ing.triage_items)
        assert sig.parameters["missing_verdict"].default == "KEEP"

    def test_batched_also_keeps(self):
        import inspect
        sig = inspect.signature(ing.triage_items_batched)
        assert sig.parameters["missing_verdict"].default == "KEEP"
