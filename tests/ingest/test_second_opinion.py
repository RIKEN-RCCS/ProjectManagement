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


# --------------------------------------------------------------------------- #
# 第2系統による Pass 1 抽出の差分検査（R8 / Phase 4）
# --------------------------------------------------------------------------- #


class TestCompareExtractions:
    def test_synonymous_item_above_threshold_is_not_missing(self):
        """ratio >= 0.6（表記ゆれ）は一致とみなし、未一致に出さない。"""
        primary = {"decisions": [{"content": "ベンチ環境を更新する"}], "action_items": []}
        second = {"decisions": [{"content": "ベンチ環境の更新"}], "action_items": []}
        diff = ing.compare_extractions(primary, second)
        assert diff["decisions"] == []

    def test_dissimilar_item_below_threshold_is_missing(self):
        """ratio < 0.6 は別項目とみなし、未一致として報告する。"""
        primary = {"decisions": [{"content": "会場を予約する"}], "action_items": []}
        second = {"decisions": [{"content": "会場の予約を行う"}], "action_items": []}
        diff = ing.compare_extractions(primary, second)
        assert diff["decisions"] == ["会場の予約を行う"]

    def test_empty_primary_reports_all_second_items(self):
        primary = {"decisions": [], "action_items": []}
        second = {
            "decisions": [{"content": "決定A"}, {"content": "決定B"}],
            "action_items": [],
        }
        diff = ing.compare_extractions(primary, second)
        assert diff["decisions"] == ["決定A", "決定B"]
        assert diff["primary_counts"]["decisions"] == 0
        assert diff["second_counts"]["decisions"] == 2


class TestSecondOpinionExtraction:
    def test_broken_json_returns_empty_dict_without_raising(self, monkeypatch):
        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", lambda *a, **k: "これはJSONではない")
        result = ing.second_opinion_extraction("スレッド本文")
        assert result == {"decisions": [], "action_items": []}

    def test_call_failure_returns_empty_dict_without_raising(self, monkeypatch):
        import utils.llm as llm_mod

        def boom(*a, **k):
            raise RuntimeError("エンドポイント不通")

        monkeypatch.setattr(llm_mod, "call_rivault", boom)
        result = ing.second_opinion_extraction("スレッド本文")
        assert result == {"decisions": [], "action_items": []}


class TestApplySecondOpinionExtraction:
    def test_no_flagged_terms_skips_second_system(self, monkeypatch):
        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake_call(*a, **k):
            calls["n"] += 1
            return '{"decisions": [], "action_items": []}'

        monkeypatch.setattr(llm_mod, "call_rivault", fake_call)
        result = ing.apply_second_opinion_extraction(
            "次回までにベンチマーク結果をまとめる", {"decisions": [], "action_items": []},
            [], log=lambda *_: None,
        )
        assert result == []
        assert calls["n"] == 0

    def test_disagreement_is_recorded_with_expected_kind(self, pm_db_path, monkeypatch):
        import sqlite3

        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault",
            lambda *a, **k: '{"decisions": [{"content": "中国製モデルの採用を決定した"}], "action_items": []}',
        )
        conn = sqlite3.connect(str(pm_db_path))
        conn.row_factory = sqlite3.Row
        ing.apply_second_opinion_extraction(
            "中国製モデルについて議論した", {"decisions": [], "action_items": []},
            [], conn=conn, log=lambda *_: None,
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM triage_second_opinion")]
        conn.close()
        assert len(rows) == 1
        assert rows[0]["kind"] == "decisions_extraction"
        assert rows[0]["primary_verdict"] == "MISSING"
        assert rows[0]["second_verdict"] == "PRESENT"

    def test_env_flag_disables(self, monkeypatch):
        monkeypatch.setenv("ARGUS_SECOND_OPINION", "0")
        import utils.llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "call_rivault", lambda *a, **k: pytest.fail("呼ばれてはいけない"),
        )
        result = ing.apply_second_opinion_extraction(
            "中国製モデルについて議論した", {"decisions": [], "action_items": []},
            [], log=lambda *_: None,
        )
        assert result == []

    def test_cap_stops_further_calls_and_warns(self, monkeypatch):
        monkeypatch.setattr(
            ing, "_load_second_opinion_config",
            lambda: {
                "second_opinion": {"model": "test-model", "max_flagged_per_run": 1},
                "terms": {"geopolitical": ["中国"]},
            },
        )
        import utils.llm as llm_mod
        calls = {"n": 0}

        def fake_call(*a, **k):
            calls["n"] += 1
            return '{"decisions": [], "action_items": []}'

        monkeypatch.setattr(llm_mod, "call_rivault", fake_call)
        logs: list[str] = []
        state: dict = {}
        thread_text = "中国製モデルについて議論した"
        extracted = {"decisions": [], "action_items": []}

        ing.apply_second_opinion_extraction(
            thread_text, extracted, [], conn=None, log=logs.append, state=state,
        )
        ing.apply_second_opinion_extraction(
            thread_text, extracted, [], conn=None, log=logs.append, state=state,
        )
        assert calls["n"] == 1
        assert any("[WARN]" in m and "上限" in m for m in logs)
