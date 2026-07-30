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
        （search 15件 + docqa 2件の計17件。2026-07-30 に多段設問 mh- 9件を追加、
        docqa は file 必須）。"""
        entries = investigate_ab.load_gold(investigate_ab.DEFAULT_GOLD)
        assert len(entries) == 17
        search_entries = [e for e in entries if e["mode"] == "search"]
        docqa_entries = [e for e in entries if e["mode"] == "docqa"]
        assert len(search_entries) == 15
        assert len(docqa_entries) == 2
        mh_entries = [e for e in entries if e["id"].startswith("mh-")]
        assert len(mh_entries) == 9
        for e in docqa_entries:
            assert e.get("file")


# --------------------------------------------------------------------------- #
# ARM_PRESETS — プリセット定義の検証
# --------------------------------------------------------------------------- #

class TestArmPresets:
    def test_baseline_and_expanded_unchanged_from_legacy(self):
        """旧 ARMS 相当の baseline/expanded env が変わっていないこと。"""
        assert investigate_ab.ARM_PRESETS["baseline"]["env"] == {}
        assert investigate_ab.ARM_PRESETS["expanded"]["env"] == {
            "ARGUS_TOP_K_RERANK": "10",
            "ARGUS_SEARCH_EXCERPT_CHARS": "1200",
            "ARGUS_RERANK_PREVIEW_CHARS": "800",
            "ARGUS_DOC_QA_WINDOW": "150000",
        }

    def test_new_presets_present(self):
        for name in ("glm-loop", "glm-oneshot", "k3-loop", "k3-oneshot"):
            assert name in investigate_ab.ARM_PRESETS

    def test_k3_presets_disable_rivault_and_set_local_refs(self):
        for name in ("k3-loop", "k3-oneshot"):
            env = investigate_ab.ARM_PRESETS[name]["env"]
            assert env["RIVAULT_URL"] == ""
            assert env["RIVAULT_TOKEN"] == ""
            assert env["LOCAL_LLM_URL"] == "${RIKYU_URL}"
            assert env["LOCAL_LLM_MODEL"] == "kimi-k3"

    def test_k3_loop_sets_preserve_reasoning(self):
        assert investigate_ab.ARM_PRESETS["k3-loop"]["env"]["ARGUS_PRESERVE_REASONING"] == "1"

    def test_oneshot_presets_set_oneshot_flags(self):
        assert investigate_ab.ARM_PRESETS["glm-oneshot"]["env"]["ARGUS_ONESHOT"] == "1"
        assert investigate_ab.ARM_PRESETS["k3-oneshot"]["env"]["ARGUS_ONESHOT"] == "1"


# --------------------------------------------------------------------------- #
# _expand_env_refs
# --------------------------------------------------------------------------- #

class TestExpandEnvRefs:
    def test_plain_values_pass_through(self):
        result = investigate_ab._expand_env_refs({"FOO": "bar"})
        assert result == {"FOO": "bar"}

    def test_empty_string_literal_passes_through(self):
        result = investigate_ab._expand_env_refs({"RIVAULT_URL": ""})
        assert result == {"RIVAULT_URL": ""}

    def test_var_ref_expanded_from_parent_env(self, monkeypatch):
        monkeypatch.setenv("RIKYU_URL", "http://rikyu.example")
        result = investigate_ab._expand_env_refs({"LOCAL_LLM_URL": "${RIKYU_URL}"})
        assert result == {"LOCAL_LLM_URL": "http://rikyu.example"}

    def test_unset_var_ref_raises(self, monkeypatch):
        monkeypatch.delenv("RIKYU_URL", raising=False)
        with pytest.raises(ValueError, match="RIKYU_URL"):
            investigate_ab._expand_env_refs({"LOCAL_LLM_URL": "${RIKYU_URL}"})

    def test_empty_var_ref_raises(self, monkeypatch):
        monkeypatch.setenv("RIKYU_URL", "")
        with pytest.raises(ValueError, match="RIKYU_URL"):
            investigate_ab._expand_env_refs({"LOCAL_LLM_URL": "${RIKYU_URL}"})

    def test_mixed_dict_expands_only_ref_values(self, monkeypatch):
        monkeypatch.setenv("RIKYU_TOKEN", "secret-token")
        result = investigate_ab._expand_env_refs({
            "RIVAULT_URL": "",
            "LOCAL_LLM_TOKEN": "${RIKYU_TOKEN}",
            "LOCAL_LLM_MODEL": "kimi-k3",
        })
        assert result == {
            "RIVAULT_URL": "",
            "LOCAL_LLM_TOKEN": "secret-token",
            "LOCAL_LLM_MODEL": "kimi-k3",
        }


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

    def test_extra_args_appended_at_end(self):
        entry = {"id": "e1", "mode": "search", "question": "質問文", "reference": "r", "since": "2026-01-01"}
        cmd = investigate_ab.build_investigate_cmd(entry, 1200, extra_args=["--foo", "bar"])
        assert cmd[-2:] == ["--foo", "bar"]

    def test_no_extra_args_default_is_unaffected(self):
        entry = {"id": "e1", "mode": "search", "question": "質問文", "reference": "r", "since": "2026-01-01"}
        cmd_default = investigate_ab.build_investigate_cmd(entry, 1200)
        cmd_empty = investigate_ab.build_investigate_cmd(entry, 1200, extra_args=())
        assert cmd_default == cmd_empty


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
            self._ENTRY, investigate_ab.ARM_PRESETS["baseline"]["env"], agent_timeout=10,
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
            self._ENTRY, investigate_ab.ARM_PRESETS["expanded"]["env"], agent_timeout=10,
        )
        for key, value in investigate_ab.ARM_PRESETS["expanded"]["env"].items():
            assert captured["env"][key] == value

    def test_extra_args_propagated_to_cmd(self, monkeypatch):
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["cmd"] = cmd
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(
            self._ENTRY, {}, agent_timeout=10, extra_args=["--extra-flag"],
        )
        assert "--extra-flag" in captured["cmd"]

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

    def test_has_sources_section_detected(self, monkeypatch):
        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="回答本文\n\n## 出典\n- [1] foo")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)
        assert result["has_sources_section"] is True

    def test_has_sources_section_absent(self, monkeypatch):
        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="回答本文のみ")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)
        assert result["has_sources_section"] is False


# --------------------------------------------------------------------------- #
# run_investigate_arm — suspect_short_answer（B-2 最小回答長ガード）
# --------------------------------------------------------------------------- #

class TestSuspectShortAnswer:
    def test_short_answer_in_search_mode_flagged(self, monkeypatch):
        entry = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="短い回答")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(entry, {}, agent_timeout=10)
        assert result["suspect_short_answer"] is True

    def test_normal_length_answer_not_flagged(self, monkeypatch):
        entry = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="長い回答本文。" * 50)
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(entry, {}, agent_timeout=10)
        assert result["suspect_short_answer"] is False

    def test_docqa_mode_short_answer_not_flagged(self, monkeypatch):
        """mode=="search" 限定のガードのため docqa は対象外。"""
        entry = {"id": "e1", "mode": "docqa", "question": "q", "reference": "r",
                 "since": "2026-01-01", "file": "報告書.pdf"}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="短い回答")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(entry, {}, agent_timeout=10)
        assert result["suspect_short_answer"] is False

    def test_short_answer_warns_to_stderr(self, monkeypatch, capsys):
        entry = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(stdout="短い回答")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(entry, {}, agent_timeout=10)
        captured = capsys.readouterr()
        assert "[WARN]" in captured.err
        assert "short answer" in captured.err

    def test_error_path_suspect_short_answer_false(self, monkeypatch):
        """error 扱いにはしない — returncode 非0 の場合は answer=None、suspect_short_answer=False。"""
        entry = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            return _FakeCompletedProcess(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        result = investigate_ab.run_investigate_arm(entry, {}, agent_timeout=10)
        assert result["suspect_short_answer"] is False


# --------------------------------------------------------------------------- #
# run_investigate_arm — アーム間 env 汚染の打ち消し（M1）
# --------------------------------------------------------------------------- #

class TestRunInvestigateArmEnvPollution:
    _ENTRY = {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"}

    def test_controlled_env_keys_stripped_from_parent_before_overlay(self, monkeypatch):
        """親シェルに残った ARGUS_* が、arm_env で上書きされない限り subprocess に渡らない。"""
        for key in investigate_ab._ARM_CONTROLLED_ENV_KEYS:
            monkeypatch.setenv(key, "polluted")
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)

        for key in investigate_ab._ARM_CONTROLLED_ENV_KEYS:
            assert key not in captured["env"]

    def test_arm_env_can_still_set_controlled_keys(self, monkeypatch):
        """arm_env が明示的に指定したキーは、親環境の汚染除去後もそのまま反映される。"""
        monkeypatch.setenv("ARGUS_ONESHOT", "polluted")
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(
            self._ENTRY, {"ARGUS_ONESHOT": "1"}, agent_timeout=10,
        )
        assert captured["env"]["ARGUS_ONESHOT"] == "1"

    # S3: expanded/k3-oneshot 系アームが使う検索パラメータ・one-shot override・
    # investigate 予算の env キーが _ARM_CONTROLLED_ENV_KEYS から漏れていた不足分。
    _NEWLY_CONTROLLED_KEYS = (
        "ARGUS_TOP_K_RERANK",
        "ARGUS_SEARCH_EXCERPT_CHARS",
        "ARGUS_RERANK_PREVIEW_CHARS",
        "ARGUS_DOC_QA_WINDOW",
        "ARGUS_ONESHOT_LLM_URL",
        "ARGUS_ONESHOT_LLM_TOKEN",
        "ARGUS_ONESHOT_LLM_MODEL",
        "ARGUS_ONESHOT_LLM_TEMPERATURE",
        "ARGUS_INVESTIGATE_TIMEOUT",
    )

    def test_newly_controlled_keys_are_in_whitelist(self):
        """不足していた9キーが _ARM_CONTROLLED_ENV_KEYS に含まれること（回帰防止）。"""
        for key in self._NEWLY_CONTROLLED_KEYS:
            assert key in investigate_ab._ARM_CONTROLLED_ENV_KEYS

    def test_newly_controlled_keys_stripped_from_parent_pollution(self, monkeypatch):
        """親シェルが expanded/k3-oneshot 由来の値で汚染されていても、
        アーム env で明示指定しない限り subprocess に渡らない。"""
        for key in self._NEWLY_CONTROLLED_KEYS:
            monkeypatch.setenv(key, "polluted")
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        investigate_ab.run_investigate_arm(self._ENTRY, {}, agent_timeout=10)

        for key in self._NEWLY_CONTROLLED_KEYS:
            assert key not in captured["env"]

    def test_newly_controlled_keys_can_still_be_set_by_arm_env(self, monkeypatch):
        """親汚染除去後も、arm_env が明示指定したこれらのキーは反映される。"""
        for key in self._NEWLY_CONTROLLED_KEYS:
            monkeypatch.setenv(key, "polluted")
        captured = {}

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured["env"] = env
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)

        arm_env = {key: "clean" for key in self._NEWLY_CONTROLLED_KEYS}
        investigate_ab.run_investigate_arm(self._ENTRY, arm_env, agent_timeout=10)

        for key in self._NEWLY_CONTROLLED_KEYS:
            assert captured["env"][key] == "clean"


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
# _extract_run_metrics — stderr ログ文言のパース
# --------------------------------------------------------------------------- #

class TestExtractRunMetrics:
    _SAMPLE_STDERR = "\n".join([
        "[INFO] call_argus_llm: route_order=local think=False fallback=True",
        "[STEP 1/20] LLM 応答 512 chars, 2件のツール呼び出し (3.2s)",
        "[STEP 1/20] ツール2件を実行中...",
        "[INFO] call_argus_llm: route_order=local think=False fallback=True",
        "[STEP 2/20] LLM 応答 1024 chars, 3件のツール呼び出し (4.1s)",
        "re-rankエラー: boom. 日付降順フォールバックを使用",
        "[forced-synthesis] 5件のツール結果から最終回答生成",
        "[WARN] コンテキスト長超過。max_tokens 16384 → 8192 に縮小再試行",
        "[oneshot] retrieved=200 packed=48 context_chars=118234 prompt_chars=125000",
    ])

    def test_tool_calls_total_summed(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["tool_calls_total"] == 5

    def test_steps_used_is_max_step_number(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["steps_used"] == 2

    def test_forced_synthesis_detected(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["forced_synthesis"] is True

    def test_forced_synthesis_absent_when_not_present(self):
        metrics = investigate_ab._extract_run_metrics("[STEP 1/20] LLM 応答 10 chars, 0件のツール呼び出し (1s)")
        assert metrics["forced_synthesis"] is False

    def test_rerank_fallback_counted(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["rerank_fallbacks"] == 1

    def test_rerank_head_fallback_counted(self):
        stderr = "re-rank: 有効な番号が得られず先頭件数で代替\n" * 2
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["rerank_head_fallbacks"] == 2

    def test_embedding_errors_counted(self):
        stderr = "embedding 取得エラー: Connection refused\n"
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["embedding_errors"] == 1

    def test_oneshot_context_and_prompt_chars_extracted(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["oneshot_chunks"] == 48
        assert metrics["oneshot_context_chars"] == 118234
        assert metrics["oneshot_prompt_chars"] == 125000

    def test_oneshot_prompt_chars_optional_for_legacy_log_format(self):
        """prompt_chars 追加前の旧ログ形式でも oneshot_chunks/context_chars は抽出できる。"""
        stderr = "[oneshot] retrieved=200 packed=48 context_chars=118234"
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["oneshot_chunks"] == 48
        assert metrics["oneshot_context_chars"] == 118234
        assert metrics["oneshot_prompt_chars"] is None

    def test_oneshot_absent_when_no_line(self):
        metrics = investigate_ab._extract_run_metrics("[STEP 1/20] LLM 応答 10 chars, 0件のツール呼び出し (1s)")
        assert metrics["oneshot_chunks"] is None
        assert metrics["oneshot_context_chars"] is None
        assert metrics["oneshot_prompt_chars"] is None

    def test_vector_leg_empty_and_sources_missing_and_ctx_shrink_separated(self):
        """degraded_events の合算をやめ、劣化イベントの種別ごとに個別カウントする。"""
        stderr = "\n".join([
            "[oneshot][DEGRADED] vector leg empty (EMBED_API_BASE?)",
            "[oneshot][DEGRADED] sources section missing",
            "[WARN] コンテキスト長超過。max_tokens 8192 → 4096 に縮小再試行",
            "[WARN] コンテキスト長超過。max_tokens 4096 → 2048 に縮小再試行",
        ])
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["vector_leg_empty"] == 1
        assert metrics["sources_missing"] == 1
        assert metrics["ctx_shrink_retries"] == 2
        assert "degraded_events" not in metrics

    def test_route_orders_counted(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["route_orders"] == 2

    def test_oneshot_llm_fallbacks_counted(self):
        """S5: pm_argus_agent.py の [oneshot][FALLBACK] 発生回数を計上する。"""
        stderr = "\n".join([
            "[oneshot][FALLBACK] override LLM failed (TimeoutError), falling back to default route",
            "[oneshot][FALLBACK] override LLM failed (TimeoutError), falling back to default route",
        ])
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["oneshot_llm_fallbacks"] == 2

    def test_oneshot_llm_fallbacks_absent_when_not_present(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["oneshot_llm_fallbacks"] == 0

    def test_hybrid_fts_excluded_counted(self):
        """S5: retrieval.py の RRF 遮断ログ（date_fallback / like 共通）を計上する。"""
        stderr = "\n".join([
            "[hybrid] FTS date_fallback excluded from RRF (vector-only)",
            "[hybrid] FTS like excluded from RRF (vector-only)",
        ])
        metrics = investigate_ab._extract_run_metrics(stderr)
        assert metrics["hybrid_fts_excluded"] == 2

    def test_hybrid_fts_excluded_absent_when_not_present(self):
        metrics = investigate_ab._extract_run_metrics(self._SAMPLE_STDERR)
        assert metrics["hybrid_fts_excluded"] == 0

    def test_empty_stderr_returns_zeros(self):
        metrics = investigate_ab._extract_run_metrics("")
        assert metrics["tool_calls_total"] == 0
        assert metrics["steps_used"] == 0
        assert metrics["forced_synthesis"] is False
        assert metrics["rerank_fallbacks"] == 0
        assert metrics["rerank_head_fallbacks"] == 0
        assert metrics["embedding_errors"] == 0
        assert metrics["vector_leg_empty"] == 0
        assert metrics["sources_missing"] == 0
        assert metrics["ctx_shrink_retries"] == 0
        assert metrics["route_orders"] == 0
        assert metrics["initial_search_calls"] == 0
        assert metrics["oneshot_llm_fallbacks"] == 0
        assert metrics["hybrid_fts_excluded"] == 0


# --------------------------------------------------------------------------- #
# _extract_run_metrics — initial_search_calls（B-1: tool_calls_total 0/3 固定の調査・修正）
#
# 実測（data/eval/investigate_k3.jsonl の glm-loop 側 metrics、51件全数）で
# tool_calls_total=0 が 51/51 だった。原因は STEP ループに入る前の事前検索
# （pm_argus_agent.py の _rewrite_query による初回 search_text 並列実行、
# "[initial-search] ..." ログ）が tool_calls_total（STEP ループ内のみを数える
# "N件のツール呼び出し" ログに由来）に含まれないこと。以下のフィクスチャの
# ログ文言は pm_argus_agent.py の該当 logger.info() 呼び出しから採録した
# 実際のログ形式（"[initial-search] rewrite クエリN件を事前検索" /
# "[initial-search] 完了 (X.Xs, M件)"）。
# --------------------------------------------------------------------------- #

class TestExtractRunMetricsInitialSearch:
    _GLM_LOOP_STDERR = "\n".join([
        "[INFO] call_argus_llm: route_order=local think=False fallback=True",
        "[rewrite] LLM応答 320 chars, 1.2s",
        "[initial-search] rewrite クエリ4件を事前検索",
        "[initial-search] 完了 (2.3s, 4件)",
        "[STEP 1/20] LLM 応答 210 chars, 0件のツール呼び出し (1.1s)",
        "[STEP 2/20] LLM 応答 198 chars, 0件のツール呼び出し (1.0s)",
        "[STEP 3/20] LLM 応答 205 chars, 0件のツール呼び出し (0.9s)",
        "[forced-synthesis] 4件のツール結果から最終回答生成",
    ])

    def test_initial_search_calls_counted_independently_of_tool_calls_total(self):
        """initial-search の実行本数（4件）は tool_calls_total とは別に計上される。"""
        metrics = investigate_ab._extract_run_metrics(self._GLM_LOOP_STDERR)
        assert metrics["initial_search_calls"] == 4
        assert metrics["tool_calls_total"] == 0

    def test_steps_used_reflects_actual_step_loop_progress(self):
        """steps_used=3 はパース漏れではなく、STEP ループが実際に3回進んだ結果。"""
        metrics = investigate_ab._extract_run_metrics(self._GLM_LOOP_STDERR)
        assert metrics["steps_used"] == 3
        assert metrics["forced_synthesis"] is True

    def test_initial_search_absent_returns_zero(self):
        metrics = investigate_ab._extract_run_metrics(
            "[STEP 1/20] LLM 応答 10 chars, 0件のツール呼び出し (1s)",
        )
        assert metrics["initial_search_calls"] == 0


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
# cmd_run — アーム env 展開失敗（${VAR} 未設定）
# --------------------------------------------------------------------------- #

class TestCmdRunArmEnvValidation:
    def test_unset_ref_var_returns_2_before_any_subprocess(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")
        monkeypatch.delenv("RIKYU_URL", raising=False)

        calls = []

        def fake_run_arm(entry, arm_env, *, agent_timeout, extra_args=()):
            calls.append(entry)
            return {"answer": "x", "latency_s": 1.0, "error": "", "budget_truncated": False}
        monkeypatch.setattr(investigate_ab, "run_investigate_arm", fake_run_arm)

        gold_path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"},
        ])
        args = argparse.Namespace(
            gold=str(gold_path), entry=None,
            jsonl=str(tmp_path / "out.jsonl"), answers_dir=str(tmp_path / "answers"),
            arm_a="glm-loop", arm_b="k3-oneshot",
            agent_timeout=10, save_answers=False,
            judge_model="Kimi-K2-Thinking", judge_max_tokens=4096, judge_timeout=60,
            seed=7,
        )
        rc = investigate_ab.cmd_run(args)
        assert rc == 2
        assert calls == []  # 展開失敗時は subprocess を1つも起動しない


# --------------------------------------------------------------------------- #
# cmd_run — --arm-a/--arm-b 既定値の後方互換
# --------------------------------------------------------------------------- #

class TestCmdRunDefaultArmsBackwardCompat:
    def test_default_arms_send_legacy_env_and_no_extra_args(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")
        captured_calls = []

        def fake_run(cmd, cwd, env, capture_output, text, timeout):
            captured_calls.append({"cmd": cmd, "env": env})
            return _FakeCompletedProcess()
        monkeypatch.setattr(investigate_ab.subprocess, "run", fake_run)
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "call_judge",
            lambda *a, **kw: ('{"prefer": "tie", "rationale": "同等"}', 100, ""),
        )
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "parse_judge_output",
            lambda raw: {"prefer": "tie", "rationale": "同等"},
        )

        gold_path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"},
        ])
        args = argparse.Namespace(
            gold=str(gold_path), entry=None,
            jsonl=str(tmp_path / "out.jsonl"), answers_dir=str(tmp_path / "answers"),
            agent_timeout=10, save_answers=False,
            judge_model="Kimi-K2-Thinking", judge_max_tokens=4096, judge_timeout=60,
            seed=7,
            # arm_a/arm_b は意図的に未指定 -> getattr既定 baseline/expanded を使う
        )
        rc = investigate_ab.cmd_run(args)
        assert rc == 0
        assert len(captured_calls) == 2

        env_a, env_b = captured_calls[0]["env"], captured_calls[1]["env"]
        assert "ARGUS_TOP_K_RERANK" not in env_a
        for key, value in investigate_ab.ARM_PRESETS["expanded"]["env"].items():
            assert env_b[key] == value

        record = json.loads(Path(args.jsonl).read_text(encoding="utf-8").strip())
        assert record["arm_a"] == "baseline"
        assert record["arm_b"] == "expanded"
        assert record["compare"] == "baseline_vs_expanded"


# --------------------------------------------------------------------------- #
# cmd_run — model_a/model_b・arm_config_a/b の記録（S2、機密非流出）
# --------------------------------------------------------------------------- #

class TestCmdRunRecordModelAndArmConfig:
    def test_model_and_arm_config_recorded_without_leaking_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")
        monkeypatch.setenv("RIKYU_URL", "http://rikyu.example")
        monkeypatch.setenv("RIKYU_TOKEN", "rikyu-secret-token")
        monkeypatch.setenv("EMBED_API_BASE", "http://embed.example")
        monkeypatch.setenv("EMBED_API_KEY", "embed-secret-key")
        monkeypatch.setenv("EMBED_MODEL", "bge-m3:567m")

        def fake_run_arm(entry, arm_env, *, agent_timeout, extra_args=()):
            return {"answer": "回答", "latency_s": 1.0, "error": "", "budget_truncated": False}
        monkeypatch.setattr(investigate_ab, "run_investigate_arm", fake_run_arm)
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "call_judge",
            lambda *a, **kw: ('{"prefer": "tie", "rationale": "同等"}', 100, ""),
        )
        monkeypatch.setattr(
            investigate_ab.argus_ab_judge, "parse_judge_output",
            lambda raw: {"prefer": "tie", "rationale": "同等"},
        )

        gold_path = _write_gold(tmp_path, [
            {"id": "e1", "mode": "search", "question": "q", "reference": "r", "since": "2026-01-01"},
        ])
        args = argparse.Namespace(
            gold=str(gold_path), entry=None,
            jsonl=str(tmp_path / "out.jsonl"), answers_dir=str(tmp_path / "answers"),
            arm_a="glm-loop", arm_b="k3-oneshot",
            agent_timeout=10, save_answers=False,
            judge_model="Kimi-K2-Thinking", judge_max_tokens=4096, judge_timeout=60,
            seed=7,
        )
        rc = investigate_ab.cmd_run(args)
        assert rc == 0

        record = json.loads(Path(args.jsonl).read_text(encoding="utf-8").strip())
        assert record["model_a"] == "(inherited)"
        assert record["model_b"] == "kimi-k3"
        assert record["arm_config_a"] == {}
        assert record["arm_config_b"] == {
            "LOCAL_LLM_MODEL": "kimi-k3",
            "ARGUS_ONESHOT": "1",
            "ARGUS_ONESHOT_TOP_K": "50",
        }
        record_text = json.dumps(record, ensure_ascii=False)
        assert "rikyu-secret-token" not in record_text
        assert "embed-secret-key" not in record_text


# --------------------------------------------------------------------------- #
# cmd_run — --save-answers opt-in
# --------------------------------------------------------------------------- #

class TestCmdRunSaveAnswers:
    def _make_args(self, tmp_path: Path, gold_path: Path, *, save_answers: bool) -> argparse.Namespace:
        return argparse.Namespace(
            gold=str(gold_path), entry=None,
            jsonl=str(tmp_path / "out.jsonl"),
            answers_dir=str(tmp_path / "answers"),
            arm_a="baseline", arm_b="expanded",
            agent_timeout=10, save_answers=save_answers,
            judge_model="Kimi-K2-Thinking", judge_max_tokens=4096, judge_timeout=60,
            seed=7,
        )

    def _patch_common(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://example")
        monkeypatch.setenv("RIVAULT_TOKEN", "token")

        def fake_run_arm(entry, arm_env, *, agent_timeout, extra_args=()):
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
        assert record["answer_path_a"] is None
        assert record["answer_path_b"] is None
        # 本文はJSONLに入らず文字数のみ
        assert "answer" not in record
        assert record["chars_a"] == len("baseline回答")
        assert record["chars_b"] == len("expanded回答")

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
            ("answer_path_a", "baseline"),
            ("answer_path_b", "expanded"),
        ):
            path = Path(record[key])
            assert path.exists()
            assert path.read_text(encoding="utf-8") == f"{expected_label}回答"
            # ファイル名に run 開始時刻（YYYYmmdd_HHMMSS）と entry id を含む
            assert path.name.endswith(f"_e1_{expected_label}.txt")
            ts_prefix = path.name.split("_e1_")[0]
            assert len(ts_prefix) == len("20260101_000000")


# --------------------------------------------------------------------------- #
# _normalize_record — 旧形式レコードの新キー写像
# --------------------------------------------------------------------------- #

class TestNormalizeRecord:
    def test_legacy_record_mapped_to_new_keys(self):
        legacy = {
            "id": "e1", "mode": "search", "compare": "search_expansion",
            "latency_baseline_s": 10.0, "latency_expanded_s": 20.0,
            "chars_baseline": 100, "chars_expanded": 200,
            "budget_truncated_baseline": False, "budget_truncated_expanded": True,
            "answer_path_baseline": None, "answer_path_expanded": None,
            "prefer_arm": "expanded",
        }
        normalized = investigate_ab._normalize_record(legacy)
        assert normalized["arm_a"] == "baseline"
        assert normalized["arm_b"] == "expanded"
        assert normalized["latency_a_s"] == 10.0
        assert normalized["latency_b_s"] == 20.0
        assert normalized["chars_a"] == 100
        assert normalized["chars_b"] == 200
        assert normalized["budget_truncated_a"] is False
        assert normalized["budget_truncated_b"] is True
        assert normalized["answer_path_a"] is None
        assert normalized["answer_path_b"] is None
        # 旧キーも保持される（非破壊）
        assert normalized["latency_baseline_s"] == 10.0

    def test_new_format_record_is_idempotent(self):
        new_rec = {
            "id": "e1", "mode": "search", "compare": "glm-loop_vs_k3-oneshot",
            "arm_a": "glm-loop", "arm_b": "k3-oneshot",
            "latency_a_s": 5.0, "latency_b_s": 6.0,
            "prefer_arm": "k3-oneshot",
        }
        normalized = investigate_ab._normalize_record(new_rec)
        assert normalized == new_rec

    def test_missing_legacy_keys_do_not_error(self):
        rec = {"id": "e1", "mode": "search", "prefer_arm": "tie"}
        normalized = investigate_ab._normalize_record(rec)
        assert normalized["arm_a"] == "baseline"
        assert normalized["arm_b"] == "expanded"
        assert "latency_a_s" not in normalized


# --------------------------------------------------------------------------- #
# _check_oneshot_metrics_consistency — アーム名と metrics の矛盾検出（M1 二重防御）
# --------------------------------------------------------------------------- #

class TestCheckOneshotMetricsConsistency:
    def test_oneshot_arm_without_oneshot_metrics_warns(self):
        records = [{
            "id": "e1", "arm_a": "glm-loop", "arm_b": "k3-oneshot",
            "metrics_a": {"oneshot_chunks": None}, "metrics_b": {"oneshot_chunks": None},
        }]
        warnings = investigate_ab._check_oneshot_metrics_consistency(records)
        assert len(warnings) == 1
        assert "arm_b=k3-oneshot" in warnings[0]
        assert "矛盾" in warnings[0]

    def test_loop_arm_with_oneshot_metrics_warns(self):
        records = [{
            "id": "e1", "arm_a": "k3-loop", "arm_b": "glm-oneshot",
            "metrics_a": {"oneshot_chunks": 48}, "metrics_b": {"oneshot_chunks": 48},
        }]
        warnings = investigate_ab._check_oneshot_metrics_consistency(records)
        assert len(warnings) == 1
        assert "arm_a=k3-loop" in warnings[0]

    def test_consistent_records_produce_no_warnings(self):
        records = [{
            "id": "e1", "arm_a": "glm-loop", "arm_b": "k3-oneshot",
            "metrics_a": {"oneshot_chunks": None}, "metrics_b": {"oneshot_chunks": 48},
        }]
        assert investigate_ab._check_oneshot_metrics_consistency(records) == []

    def test_missing_metrics_skipped_without_error(self):
        records = [{"id": "e1", "arm_a": "baseline", "arm_b": "expanded"}]
        assert investigate_ab._check_oneshot_metrics_consistency(records) == []

    def test_cmd_report_prints_warning_to_stderr(self, tmp_path, capsys):
        jsonl_path = tmp_path / "out.jsonl"
        record = {
            "id": "e1", "mode": "search", "arm_a": "glm-loop", "arm_b": "k3-oneshot",
            "compare": "glm-loop_vs_k3-oneshot", "prefer_arm": "tie",
            "metrics_a": {"oneshot_chunks": None}, "metrics_b": {"oneshot_chunks": None},
        }
        jsonl_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        args = argparse.Namespace(jsonl=str(jsonl_path))
        rc = investigate_ab.cmd_report(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "[WARN]" in captured.err
        assert "矛盾" in captured.err


# --------------------------------------------------------------------------- #
# aggregate_report — error/parse_failed の分母除外を含む集計検証
# --------------------------------------------------------------------------- #

class TestAggregateReport:
    def test_win_tie_rate_excludes_error_and_parse_failed(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "expanded",
             "latency_a_s": 10.0, "latency_b_s": 12.0},
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "baseline",
             "latency_a_s": 8.0, "latency_b_s": 9.0},
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "tie",
             "latency_a_s": 5.0, "latency_b_s": 5.0},
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "error",
             "latency_a_s": 1.0, "latency_b_s": None},
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "parse_failed",
             "latency_a_s": 3.0, "latency_b_s": 4.0},
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["total"] == 5
        # valid = expanded(1) + baseline(1) + tie(1) = 3 (error/parse_failed 除外)
        assert stats["valid"] == 3
        assert stats["win_tie_rate"] == pytest.approx(2 / 3)

    def test_pass_at_threshold(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "expanded"}
            for _ in range(6)
        ] + [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "baseline"}
            for _ in range(4)
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["win_tie_rate"] == pytest.approx(0.6)
        assert stats["passed"] is True

    def test_fail_below_threshold(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "expanded"}
            for _ in range(5)
        ] + [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "baseline"}
            for _ in range(5)
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["win_tie_rate"] == pytest.approx(0.5)
        assert stats["passed"] is False

    def test_all_error_gives_zero_valid_and_none_rate(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "error"}
            for _ in range(3)
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["valid"] == 0
        assert stats["win_tie_rate"] is None
        assert stats["passed"] is False

    def test_modes_are_stratified_separately(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "expanded"},
            {"mode": "docqa", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "baseline"},
        ]
        report = investigate_ab.aggregate_report(records)
        assert set(report.keys()) == {
            ("baseline_vs_expanded", "search"), ("baseline_vs_expanded", "docqa"),
        }
        assert report[("baseline_vs_expanded", "search")]["counts"] == {"expanded": 1}
        assert report[("baseline_vs_expanded", "docqa")]["counts"] == {"baseline": 1}

    def test_compares_are_stratified_separately(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded",
             "compare": "baseline_vs_expanded", "prefer_arm": "expanded"},
            {"mode": "search", "arm_a": "glm-loop", "arm_b": "k3-oneshot",
             "compare": "glm-loop_vs_k3-oneshot", "prefer_arm": "k3-oneshot"},
        ]
        report = investigate_ab.aggregate_report(records)
        assert set(report.keys()) == {
            ("baseline_vs_expanded", "search"), ("glm-loop_vs_k3-oneshot", "search"),
        }

    def test_avg_latency_computed_per_arm(self):
        records = [
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "expanded",
             "latency_a_s": 10.0, "latency_b_s": 20.0},
            {"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": "baseline",
             "latency_a_s": 20.0, "latency_b_s": 30.0},
        ]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["avg_latency_a_s"] == pytest.approx(15.0)
        assert stats["avg_latency_b_s"] == pytest.approx(25.0)

    def test_missing_prefer_arm_treated_as_parse_failed(self):
        records = [{"mode": "search", "arm_a": "baseline", "arm_b": "expanded", "prefer_arm": None}]
        report = investigate_ab.aggregate_report(records)
        stats = report[("baseline_vs_expanded", "search")]
        assert stats["counts"] == {"parse_failed": 1}
        assert stats["valid"] == 0

    def test_legacy_jsonl_records_aggregate_equivalently_after_normalize(self):
        """既存 data/eval/investigate_ab.jsonl 相当（旧キー・compare=search_expansion）を
        _normalize_record してから aggregate_report に掛けても、mode 単位の集計が
        従来（mode のみでグループ化していた頃）と同じ人数・勝率になること。"""
        legacy_records = [
            {"id": "e1", "mode": "search", "compare": "search_expansion",
             "latency_baseline_s": 10.0, "latency_expanded_s": 12.0, "prefer_arm": "expanded"},
            {"id": "e2", "mode": "search", "compare": "search_expansion",
             "latency_baseline_s": 8.0, "latency_expanded_s": 9.0, "prefer_arm": "baseline"},
            {"id": "e3", "mode": "docqa", "compare": "search_expansion",
             "latency_baseline_s": 5.0, "latency_expanded_s": 5.0, "prefer_arm": "tie"},
        ]
        normalized = [investigate_ab._normalize_record(r) for r in legacy_records]
        report = investigate_ab.aggregate_report(normalized)
        # 全レコードが compare="search_expansion" 固定なので (compare, mode) でグループ化しても
        # mode のみでグループ化していた旧仕様と同じ分割・件数になる
        search_stats = report[("search_expansion", "search")]
        docqa_stats = report[("search_expansion", "docqa")]
        assert search_stats["total"] == 2
        assert search_stats["counts"] == {"expanded": 1, "baseline": 1}
        assert docqa_stats["total"] == 1
        assert docqa_stats["counts"] == {"tie": 1}
