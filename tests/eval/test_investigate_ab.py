"""scripts/eval/investigate_ab.py のテスト。

LLM 実接続・subprocess 実行なし。scripts/eval は pytest の pythonpath 対象外のため、
import 前に sys.path へ追加する（investigate_ab.py 自身が行っているブートストラップと
同じパス）。
"""
import argparse
import json
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "eval"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import investigate_ab  # noqa: E402 — 上記パス追加後にインポート

# --------------------------------------------------------------------------- #
# load_gold — 必須フィールド検証
# --------------------------------------------------------------------------- #

def _write_gold(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "gold.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "entries": entries}, allow_unicode=True),
                     encoding="utf-8")
    return path


class TestLoadGold:
    def test_loads_valid_entries(self, tmp_path):
        path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q1", "reference": "r1", "since": "2026-01-01"},
        ])
        entries = investigate_ab.load_gold(path)
        assert len(entries) == 1
        assert entries[0]["id"] == "e1"

    def test_missing_question_raises(self, tmp_path):
        path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "reference": "r1", "since": "2026-01-01"},
        ])
        with pytest.raises(ValueError, match="question"):
            investigate_ab.load_gold(path)

    def test_missing_reference_raises(self, tmp_path):
        path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q1", "since": "2026-01-01"},
        ])
        with pytest.raises(ValueError, match="reference"):
            investigate_ab.load_gold(path)

    def test_missing_since_raises(self, tmp_path):
        path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q1", "reference": "r1"},
        ])
        with pytest.raises(ValueError, match="since"):
            investigate_ab.load_gold(path)

    def test_docqa_entry_with_file_field_allowed(self, tmp_path):
        path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "docqa", "question": "q1", "reference": "r1",
             "since": "2026-01-01", "file": "報告書.pdf"},
        ])
        entries = investigate_ab.load_gold(path)
        assert entries[0]["file"] == "報告書.pdf"

    def test_actual_gold_file_loads(self):
        """リポジトリ本体の investigate_gold.yaml が読み込めること
        （search 6件 + docqa 2件の計8件、docqa は file 必須）。"""
        entries = investigate_ab.load_gold(investigate_ab.DEFAULT_GOLD)
        assert len(entries) == 8
        search_entries = [e for e in entries if e["mode"] == "search"]
        docqa_entries = [e for e in entries if e["mode"] == "docqa"]
        assert len(search_entries) == 6
        assert len(docqa_entries) == 2
        for e in docqa_entries:
            assert e.get("file")


# --------------------------------------------------------------------------- #
# build_investigate_cmd
# --------------------------------------------------------------------------- #

class TestBuildInvestigateCmd:
    def test_search_mode_has_no_file_flag(self):
        entry = {"id": "e1", "mode": "search", "question": "質問文", "reference": "r", "since": "2026-01-01"}
        cmd = investigate_ab.build_investigate_cmd(entry, 1200)
        assert "--file" not in cmd
        assert "--investigate" in cmd
        assert "質問文" in cmd
        assert "--since" in cmd
        assert "2026-01-01" in cmd
        assert "--no-intent-header" in cmd

    def test_agent_timeout_passed_as_timeout_flag(self):
        """両アームで調査予算を統一するため --timeout に agent_timeout を明示する。"""
        entry = {"id": "e1", "mode": "search", "question": "質問文", "reference": "r", "since": "2026-01-01"}
        cmd = investigate_ab.build_investigate_cmd(entry, 1200)
        assert "--timeout" in cmd
        idx = cmd.index("--timeout")
        assert cmd[idx + 1] == "1200"

    def test_docqa_mode_with_file_adds_file_flag(self):
        entry = {"id": "e1", "mode": "docqa", "question": "質問文", "reference": "r",
                  "since": "2026-01-01", "file": "報告書.pdf"}
        cmd = investigate_ab.build_investigate_cmd(entry, 1200)
        assert "--file" in cmd
        idx = cmd.index("--file")
        assert cmd[idx + 1] == "報告書.pdf"

    def test_docqa_mode_without_file_has_no_file_flag(self):
        entry = {"id": "e1", "mode": "docqa", "question": "質問文", "reference": "r", "since": "2026-01-01"}
        cmd = investigate_ab.build_investigate_cmd(entry, 1200)
        assert "--file" not in cmd


# --------------------------------------------------------------------------- #
# run_investigate_arm — subprocess.run はモックし env / 戻り値を検証
# --------------------------------------------------------------------------- #

class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="回答本文", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunInvestigateArm:
    _ENTRY = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

    def test_baseline_arm_env_has_no_overrides_added(self, monkeypatch):
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(
            self._ENTRY, investigate_ab.ARMS["baseline"], agent_timeout=10,
        )
        assert result["answer"] == "回答本文"
        assert "ARGUS_TOP_K_RERANK" not in captured["env"]

    def test_expanded_arm_env_carries_all_overrides(self, monkeypatch):
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(
            self._ENTRY, investigate_ab.ARMS["expanded"], agent_timeout=10,
        )
        for key, value in investigate_ab.ARMS["expanded"].items():
            assert captured["env"][key] == value

    def test_kill_timeout_is_agent_timeout_plus_margin(self, monkeypatch):
        """両アーム共通の調査予算（agent_timeout）に対し、subprocess kill タイムアウトは
        _KILL_TIMEOUT_MARGIN 秒の余裕を持たせて自動設定される。"""
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["timeout"] = timeout
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=1200)
        assert captured["timeout"] == 1200 + investigate_ab._KILL_TIMEOUT_MARGIN

    def test_returncode_nonzero_returns_none_answer(self, monkeypatch):
        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)
        assert result["answer"] is None
        assert "returncode=1" in result["error"]
        assert result["budget_truncated"] is False

    def test_timeout_returns_none_answer(self, monkeypatch):
        import subprocess as _subprocess

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            raise _subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=5)
        assert result["answer"] is None
        assert "TimeoutExpired" in result["error"]
        assert result["budget_truncated"] is False


# --------------------------------------------------------------------------- #
# run_investigate_arm — budget_truncated マーカー検出
# --------------------------------------------------------------------------- #

class TestBudgetTruncatedDetection:
    _ENTRY = {"id": "e1", "mode": "docqa", "question": "q", "reference": "r", "since": "2026-01-01"}

    def test_marker_present_sets_budget_truncated_true(self, monkeypatch):
        stdout = (
            "回答本文\n\n## 制限事項\n"
            f"- {investigate_ab._BUDGET_MARKER}: 報告書.pdf 断片5/17"
        )

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout=stdout)
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)
        assert result["budget_truncated"] is True

    def test_marker_absent_sets_budget_truncated_false(self, monkeypatch):
        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="通常の回答本文")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)
        assert result["budget_truncated"] is False


# --------------------------------------------------------------------------- #
# RIVAULT プリフライト
# --------------------------------------------------------------------------- #

class TestRivaultPreflight:
    def test_both_set_returns_true(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")
        assert investigate_ab.rivault_configured() is True

    def test_missing_url_returns_false(self, monkeypatch):
        monkeypatch.delenv("RIVAULT_URL", raising=False)
        monkeypatch.setenv("RIVAULT_TOKEN", "token")
        assert investigate_ab.rivault_configured() is False

    def test_missing_token_returns_false(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.delenv("RIVAULT_TOKEN", raising=False)
        assert investigate_ab.rivault_configured() is False

    def test_empty_string_treated_as_unset(self, monkeypatch):
        """conftest の _isolate_env が既定で空文字列を設定するため、空文字列も未設定扱いにする。"""
        monkeypatch.setenv("RIVAULT_URL", "")
        monkeypatch.setenv("RIVAULT_TOKEN", "")
        assert investigate_ab.rivault_configured() is False

    def test_cmd_run_returns_2_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("RIVAULT_URL", raising=False)
        monkeypatch.delenv("RIVAULT_TOKEN", raising=False)
        rc = investigate_ab.cmd_run(argparse.Namespace())
        assert rc == 2


# --------------------------------------------------------------------------- #
# cmd_run — --save-answers opt-in
# --------------------------------------------------------------------------- #

class TestCmdRunSaveAnswers:
    def _make_args(self, tmp_path: Path, gold_path: Path, *, save_answers: bool) -> argparse.Namespace:
        return argparse.Namespace(
            gold=str(gold_path), entry=None,
            jsonl=str(tmp_path / "out.jsonl"),
            answers_dir=str(tmp_path / "answers"),
            agent_timeout=10, save_answers=save_answers,
            judge_model="Kimi-K2-Thinking", judge_max_tokens=4096, judge_timeout=60,
            seed=7,
        )

    def _patch_common(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")

        def fake_run_arm(entry, arm_env, *, agent_timeout):
            label = "expanded" if arm_env else "baseline"
            return {
                "answer": f"{label}回答", "latency_s": 1.0, "error": "",
                "budget_truncated": False,
            }
        monkeypatch.setattr(investigate_ab, "run_investigate_arm", fake_run_arm)
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "call_judge",
            lambda *a, **kw: ('{"prefer": "tie", "rationale": "同等"}', 100, ""),
        )
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "parse_judge_output",
            lambda raw: {"prefer": "tie", "rationale": "同等"},
        )

    def test_default_does_not_write_answer_files(self, tmp_path, monkeypatch):
        self._patch_common(monkeypatch)
        gold_path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"},
        ])
        args = self._make_args(tmp_path, gold_path, save_answers=False)

        rc = investigate_ab.cmd_run(args)

        assert rc == 0
        answers_dir = Path(args.answers_dir)
        assert not answers_dir.exists()
        record = json.loads(Path(args.jsonl).read_text(encoding="utf-8").strip())
        assert record["answer_path_baseline"] is None
        assert record["answer_path_expanded"] is None
        # 本文はJSONLに入らず文字数のみ
        assert "answer" not in record
        assert record["chars_baseline"] == len("baseline回答")

    def test_save_answers_writes_timestamped_files_and_records_path(self, tmp_path, monkeypatch):
        self._patch_common(monkeypatch)
        gold_path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"},
        ])
        args = self._make_args(tmp_path, gold_path, save_answers=True)

        rc = investigate_ab.cmd_run(args)

        assert rc == 0
        record = json.loads(Path(args.jsonl).read_text(encoding="utf-8").strip())
        for key, expected_label in (
            ("answer_path_baseline", "baseline"),
            ("answer_path_expanded", "expanded"),
        ):
            path = Path(record[key])
            assert path.exists()
            assert path.read_text(encoding="utf-8") == f"{expected_label}回答"
            # ファイル名に run 開始時刻（YYYYmmdd_HHMMSS）と entry id を含む
            assert path.name.endswith(f"_e1_{expected_label}.txt")
            ts_prefix = path.name.split("_e1_")[0]
            assert len(ts_prefix) == len("20260101_000000")


# --------------------------------------------------------------------------- #
# aggregate_report — error/parse_failed の分母除外を含む集計検証
# --------------------------------------------------------------------------- #

class TestAggregateReport:
    def test_win_tie_rate_excludes_error_and_parse_failed(self):
        records = [
            {"mode": "search", "prefer_arm": "expanded", "latency_baseline_s": 10.0, "latency_expanded_s": 12.0},
            {"mode": "search", "prefer_arm": "baseline", "latency_baseline_s": 8.0, "latency_expanded_s": 9.0},
            {"mode": "search", "prefer_arm": "tie", "latency_baseline_s": 5.0, "latency_expanded_s": 5.0},
            {"mode": "search", "prefer_arm": "error", "latency_baseline_s": 1.0, "latency_expanded_s": None},
            {"mode": "search", "prefer_arm": "parse_failed", "latency_baseline_s": 3.0, "latency_expanded_s": 4.0},
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report["search"]
        assert stats["total"] == 5
        # valid = expanded(1) + baseline(1) + tie(1) = 3 (error/parse_failed 除外)
        assert stats["valid"] == 3
        assert stats["win_tie_rate"] == pytest.approx(2 / 3)

    def test_pass_at_threshold(self):
        records = [{"mode": "search", "prefer_arm": "expanded"} for _ in range(6)] + \
                  [{"mode": "search", "prefer_arm": "baseline"} for _ in range(4)]
        report = investigate_ab.aggregate_report(records)
        assert report["search"]["win_tie_rate"] == pytest.approx(0.6)
        assert report["search"]["passed"] is True

    def test_fail_below_threshold(self):
        records = [{"mode": "search", "prefer_arm": "expanded"} for _ in range(5)] + \
                  [{"mode": "search", "prefer_arm": "baseline"} for _ in range(5)]
        report = investigate_ab.aggregate_report(records)
        assert report["search"]["win_tie_rate"] == pytest.approx(0.5)
        assert report["search"]["passed"] is False

    def test_all_error_gives_zero_valid_and_none_rate(self):
        records = [{"mode": "search", "prefer_arm": "error"} for _ in range(3)]
        report = investigate_ab.aggregate_report(records)
        stats = report["search"]
        assert stats["valid"] == 0
        assert stats["win_tie_rate"] is None
        assert stats["passed"] is False

    def test_modes_are_stratified_separately(self):
        records = [
            {"mode": "search", "prefer_arm": "expanded"},
            {"mode": "docqa", "prefer_arm": "baseline"},
        ]
        report = investigate_ab.aggregate_report(records)
        assert set(report.keys()) == {"search", "docqa"}
        assert report["search"]["counts"] == {"expanded": 1}
        assert report["docqa"]["counts"] == {"baseline": 1}

    def test_avg_latency_computed_per_arm(self):
        records = [
            {"mode": "search", "prefer_arm": "expanded", "latency_baseline_s": 10.0, "latency_expanded_s": 20.0},
            {"mode": "search", "prefer_arm": "baseline", "latency_baseline_s": 20.0, "latency_expanded_s": 30.0},
        ]
        report = investigate_ab.aggregate_report(records)
        assert report["search"]["avg_latency_baseline_s"] == pytest.approx(15.0)
        assert report["search"]["avg_latency_expanded_s"] == pytest.approx(25.0)

    def test_missing_prefer_arm_treated_as_parse_failed(self):
        records = [{"mode": "search", "prefer_arm": None}]
        report = investigate_ab.aggregate_report(records)
        assert report["search"]["counts"] == {"parse_failed": 1}
        assert report["search"]["valid"] == 0
