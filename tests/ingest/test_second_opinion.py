"""第2系統による差分検査（docs/security-architecture.md §4.9 対策3+5）のテスト。"""
from __future__ import annotations

import difflib
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


class TestExtractionItemContents:
    """LLM が指示した `{"content": ...}` 形式に従わない場合の正規化。

    2026-08-04 に Llama-4-Scout が `{"decisions": ["文字列", ...]}` を返し、
    `item.get("content")` が AttributeError になって**第2系統検査が会議単位で
    丸ごと落ちた**（Console 起動の議事録作成、logs/admin_job_25725851.log）。
    """

    def test_bare_string_items_are_accepted(self):
        primary = {"decisions": [], "action_items": []}
        second = {"decisions": ["計算資源の追加割当を9月末までに行う"], "action_items": []}
        diff = ing.compare_extractions(primary, second)
        assert diff["decisions"] == ["計算資源の追加割当を9月末までに行う"]
        assert diff["second_counts"]["decisions"] == 1

    def test_bare_string_items_still_match_primary(self):
        """素の文字列でも突合は効く（欠落として誤検出しない）。"""
        primary = {"decisions": [{"content": "ベンチ環境を更新する"}], "action_items": []}
        second = {"decisions": ["ベンチ環境の更新"], "action_items": []}
        assert ing.compare_extractions(primary, second)["decisions"] == []

    def test_alternative_content_key_is_read(self):
        second = {"decisions": [{"decision": "会場を来週までに予約する"}], "action_items": []}
        diff = ing.compare_extractions({"decisions": [], "action_items": []}, second)
        assert diff["decisions"] == ["会場を来週までに予約する"]

    def test_dict_without_any_content_key_is_empty_not_crash(self):
        second = {"decisions": [{"owner": "誰か", "due": "9/30"}], "action_items": []}
        diff = ing.compare_extractions({"decisions": [], "action_items": []}, second)
        assert diff["decisions"] == []          # 空 content は突合対象外
        assert diff["second_counts"]["decisions"] == 1  # 件数だけは失わない

    def test_string_instead_of_list_is_split_into_lines(self):
        second = {"decisions": "決定Aを行う\n決定Bを行う", "action_items": []}
        diff = ing.compare_extractions({"decisions": [], "action_items": []}, second)
        assert diff["decisions"] == ["決定Aを行う", "決定Bを行う"]

    def test_non_dict_second_is_treated_as_empty(self):
        """top-level が配列で返っても例外にしない（検査全体を落とさない）。"""
        diff = ing.compare_extractions({"decisions": [], "action_items": []},
                                       [{"content": "何か"}])
        assert diff["decisions"] == [] and diff["action_items"] == []

    def test_none_and_scalar_items_are_skipped(self):
        second = {"decisions": [None, 123, {"content": "有効な決定を行う"}], "action_items": []}
        diff = ing.compare_extractions({"decisions": [], "action_items": []}, second)
        assert diff["decisions"] == ["有効な決定を行う"]

    def test_direct_helper_on_none_is_empty(self):
        assert ing.extraction_item_contents(None) == []


class TestNormalizeForExtractionMatch:
    def test_strips_whitespace_and_punctuation(self):
        a = ing.normalize_for_extraction_match("議事録を確認する。")
        b = ing.normalize_for_extraction_match("議事録を　確認する")
        assert a == b == "議事録を確認する"

    def test_lowercases_ascii(self):
        assert ing.normalize_for_extraction_match("ABC") == "abc"

    def test_empty_is_empty(self):
        assert ing.normalize_for_extraction_match("") == ""
        assert ing.normalize_for_extraction_match(None) == ""


class TestCompareExtractionsContainment:
    """突合の偽陽性対策（ratio単独では長さの差に強く罰点を与える）のテスト。
    実測: 保存済み29件の偽陽性のうち7件（③抽出表にもある）・2件（②本文にはある）が
    ratio<0.6 のため従来ロジックでは不一致（=欠落候補）と誤判定されていた。"""

    def test_long_and_short_same_item_matches_via_containment(self):
        """K3のように主系統より詳しく長く書いた同一項目は ratio<0.6 になるが、
        包含判定（正規化した短い方の12文字窓が長い方の70%以上に出現）で救える
        ことを確認する（③の再現）。"""
        primary = {
            "decisions": [{"content": "計算資源を9月末までに追加割当する"}],
            "action_items": [],
        }
        second = {
            "decisions": [{
                "content": "関係者間で複数回にわたり議論した結果、計算資源を9月末までに"
                           "追加割当することが正式に決定した",
            }],
            "action_items": [],
        }
        ratio = difflib.SequenceMatcher(
            None, primary["decisions"][0]["content"], second["decisions"][0]["content"],
        ).ratio()
        assert ratio < ing._EXTRACTION_MATCH_RATIO_THRESHOLD  # 従来ロジックでは不一致になる値
        diff = ing.compare_extractions(primary, second)
        assert diff["decisions"] == []  # 包含判定により一致、未一致（欠落候補）に出ない

    def test_punctuation_and_whitespace_only_difference_matches(self):
        primary = {"decisions": [], "action_items": [{"content": "議事録を確認する"}]}
        second = {"decisions": [], "action_items": [{"content": "議事録を　確認する。"}]}
        diff = ing.compare_extractions(primary, second)
        assert diff["action_items"] == []

    def test_distinct_items_with_short_shared_phrase_do_not_match(self):
        """短い共通句（「〜について確認する」）を持つが内容が異なる別項目まで
        包含判定で同一視してしまわないことを確認する。"""
        primary = {
            "decisions": [], "action_items": [{"content": "システムAの構成変更について確認する"}],
        }
        second = {
            "decisions": [], "action_items": [{"content": "手順書Bの改訂内容について確認する"}],
        }
        diff = ing.compare_extractions(primary, second)
        assert diff["action_items"] == ["手順書Bの改訂内容について確認する"]

    def test_extra_haystack_excludes_item_present_in_body(self):
        """本文（extra_haystack）にはあるが抽出表に無い項目は、欠落として報告しない
        （②の再現）。"""
        primary = {"decisions": [], "action_items": []}
        second = {
            "decisions": [], "action_items": [{"content": "備品を来週までに発注する"}],
        }
        haystack = "## 議事内容\n今回の会議では備品を来週までに発注することを合意した。"
        diff = ing.compare_extractions(primary, second, extra_haystack=haystack)
        assert diff["action_items"] == []
        assert diff["matched_in_haystack_counts"]["action_items"] == 1

    def test_without_extra_haystack_default_behavior_is_unchanged(self):
        """extra_haystack を渡さない既存呼び出し（Slack Pass 1 抽出・Box）の挙動は
        変わらない。本文相当の情報が無いため、抽出表に無ければ欠落として報告する。"""
        primary = {"decisions": [], "action_items": []}
        second = {
            "decisions": [], "action_items": [{"content": "備品を来週までに発注する"}],
        }
        diff = ing.compare_extractions(primary, second)
        assert diff["action_items"] == ["備品を来週までに発注する"]


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


class TestSecondOpinionExtractionPromptContent:
    """第2系統プロンプトの粒度調整（2026-08）: 主系統と同じ3ゲート基準・出力形式制約が
    含まれること。route="rivault"（既定）・route="k3" のいずれでも同じ基準が入る
    こと（両方の読み手が同じ基準で拾うべき、という要求）。"""

    def test_rivault_route_prompt_includes_triage_gates(self, monkeypatch):
        captured = {}

        def fake_call_rivault(prompt, **kw):
            captured["prompt"] = prompt
            return '{"decisions": [], "action_items": []}'

        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", fake_call_rivault)

        ing._call_second_opinion_extraction("スレッド本文", route="rivault")
        prompt = captured["prompt"]
        assert "ゲート1" in prompt
        assert "ゲート2" in prompt
        assert "ゲート3" in prompt
        # 基準文言を複製せず _TRIAGE_GATES_SECTION を参照していること
        assert ing._TRIAGE_GATES_SECTION in prompt

    def test_rivault_route_prompt_forbids_speaker_and_org_names(self, monkeypatch):
        captured = {}

        def fake_call_rivault(prompt, **kw):
            captured["prompt"] = prompt
            return '{"decisions": [], "action_items": []}'

        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_rivault", fake_call_rivault)

        ing._call_second_opinion_extraction("スレッド本文", route="rivault")
        prompt = captured["prompt"]
        assert "話者名・組織名を含めない" in prompt

    def test_k3_route_prompt_includes_same_triage_gates(self, monkeypatch):
        captured = {}

        def fake_call_local_llm(prompt, **kw):
            captured["prompt"] = prompt
            return '{"decisions": [], "action_items": []}'

        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:9999/v1")
        import utils.llm as llm_mod
        monkeypatch.setattr(llm_mod, "call_local_llm", fake_call_local_llm)

        ing._call_second_opinion_extraction("スレッド本文", route="k3")
        prompt = captured["prompt"]
        assert ing._TRIAGE_GATES_SECTION in prompt
        assert "話者名・組織名を含めない" in prompt


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
