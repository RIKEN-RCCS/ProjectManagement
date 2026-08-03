"""Read Plane の分離（docs/security-architecture.md §3.2・Phase 5）のテスト。"""
from __future__ import annotations

import json
import subprocess
import threading

import pytest
from argus.pm_read_worker import forbidden_env_present, scrub_env, self_check


class TestScrubEnv:
    def test_removes_slack_and_box_tokens(self):
        src = {"SLACK_BOT_TOKEN": "x", "BOX_CLIENT_ID": "y", "PM_BOX_FOLDER_ID": "z"}
        assert scrub_env(src) == {}

    def test_keeps_db_key_and_llm_tokens(self):
        """DB 鍵と LLM トークンは残す — Read Plane の仕事に要り、read_plane の宛先にしか使えない。"""
        src = {"PM_DB_KEY": "k", "RIKYU_TOKEN": "t", "RIVAULT_TOKEN": "r", "LOCAL_LLM_TOKEN": "l"}
        assert scrub_env(src) == src

    def test_removes_canvas_and_tts_and_github(self):
        src = {"PM_REPORT_CANVAS_ID": "c", "FISH_TTS_HOST": "h", "GITHUB_TOKEN": "g", "HOME": "/h"}
        assert scrub_env(src) == {"HOME": "/h"}


class TestForbiddenDetection:
    def test_detects_leaked_names(self):
        assert forbidden_env_present({"SLACK_USER_TOKEN": "x", "HOME": "/h"}) == ["SLACK_USER_TOKEN"]

    def test_clean_env_is_empty(self):
        assert forbidden_env_present({"HOME": "/h", "PM_DB_KEY": "k"}) == []


class TestSelfCheck:
    def test_raises_when_token_present(self, monkeypatch):
        """親が scrub を忘れても子が止まる（分離を呼び出し側の作法に依存させない）。"""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "leaked")
        with pytest.raises(SystemExit, match="分離が成立していない"):
            self_check(strict=True)

    def test_non_strict_returns_leak_without_raising(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "leaked")
        assert "SLACK_BOT_TOKEN" in self_check(strict=False)

    def test_clean_env_passes(self, monkeypatch):
        for k in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN", "SLACK_APP_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        assert self_check(strict=True) == []


class TestNetGuardPlaneRestriction:
    """`ARGUS_NETGUARD_PLANES` で write_plane を許可集合から外せること。"""

    @pytest.fixture
    def allowlist(self, tmp_path, monkeypatch):
        p = tmp_path / "allow.yaml"
        p.write_text(
            "read_plane:\n"
            "  - host: llm.example\n    port: 443\n"
            "write_plane:\n"
            "  - host: slack.example\n    port: 443\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(p))
        from utils import net_guard
        monkeypatch.setattr(net_guard, "_allowlist_cache", None)
        return net_guard

    def test_without_restriction_both_planes_allowed(self, allowlist, monkeypatch):
        monkeypatch.delenv("ARGUS_NETGUARD_PLANES", raising=False)
        assert allowlist._host_allowed("slack.example", 443)
        assert allowlist._host_allowed("llm.example", 443)

    def test_read_plane_only_blocks_write_plane(self, allowlist, monkeypatch):
        monkeypatch.setenv("ARGUS_NETGUARD_PLANES", "read_plane")
        assert allowlist._host_allowed("llm.example", 443)
        assert not allowlist._host_allowed("slack.example", 443)

    def test_multiple_planes_can_be_listed(self, allowlist, monkeypatch):
        monkeypatch.setenv("ARGUS_NETGUARD_PLANES", "read_plane,write_plane")
        assert allowlist._host_allowed("slack.example", 443)


# --------------------------------------------------------------------------- #
# 進捗の親子中継（子は stdout に JSON 行、親は on_progress へ橋渡し）
# --------------------------------------------------------------------------- #


class TestPrintProgress:
    def test_emits_progress_json_line(self, capsys):
        from argus.pm_read_worker import _print_progress

        _print_progress("STEP 1/3 検索中...")
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {"progress": "STEP 1/3 検索中..."}

    def test_truncates_to_500_chars(self, capsys):
        from argus.pm_read_worker import _print_progress

        _print_progress("x" * 1000)
        out = capsys.readouterr().out.strip()
        assert len(json.loads(out)["progress"]) == 500


class _ScriptedFakeProc:
    """subprocess.Popen の代わりに使う、行を返すだけの偽プロセス。"""

    def __init__(self, stdout_lines, stderr_lines=None):
        self.stdout = iter(stdout_lines)
        self.stderr = iter(stderr_lines or [])
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class _HangingFakeProc:
    """子プロセスが stdout を閉じずにハングした状況を模す。"""

    def __init__(self):
        self._stop = threading.Event()
        self.killed = False

    @property
    def stdout(self):
        return self

    @property
    def stderr(self):
        return iter([])

    def __iter__(self):
        return self

    def __next__(self):
        self._stop.wait(timeout=5)
        raise StopIteration

    def kill(self):
        self.killed = True
        self._stop.set()

    def wait(self, timeout=None):
        return 0


class TestRunInReadPlaneStreaming:
    """`run_in_read_plane` の行単位ストリーミングと progress 中継。"""

    def test_forwards_progress_lines_to_on_progress(self, monkeypatch):
        from argus import pm_read_worker

        lines = [
            json.dumps({"progress": "ステップ1"}) + "\n",
            json.dumps({"progress": "ステップ2"}) + "\n",
            json.dumps({"ok": True, "answer": "回答"}) + "\n",
        ]
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _ScriptedFakeProc(lines))

        received = []
        answer = pm_read_worker.run_in_read_plane(
            "質問", on_progress=received.append, spawn_timeout=5,
        )

        assert answer == "回答"
        assert received == ["ステップ1", "ステップ2"]

    def test_progress_line_after_result_line_still_parses(self, monkeypatch):
        """progress 行が結果行の後に来ても、最後の ok 行を結果として拾えること（回帰検出）。"""
        from argus import pm_read_worker

        lines = [
            json.dumps({"ok": True, "answer": "回答"}) + "\n",
            json.dumps({"progress": "遅延ステップ"}) + "\n",
        ]
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _ScriptedFakeProc(lines))

        received = []
        answer = pm_read_worker.run_in_read_plane(
            "質問", on_progress=received.append, spawn_timeout=5,
        )

        assert answer == "回答"
        assert received == ["遅延ステップ"]

    def test_child_failure_raises_runtime_error(self, monkeypatch):
        from argus import pm_read_worker

        lines = [json.dumps({"ok": False, "error": "boom"}) + "\n"]
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _ScriptedFakeProc(lines))

        with pytest.raises(RuntimeError, match="boom"):
            pm_read_worker.run_in_read_plane("質問", spawn_timeout=5)

    def test_no_output_raises_runtime_error(self, monkeypatch):
        from argus import pm_read_worker

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _ScriptedFakeProc([]))

        with pytest.raises(RuntimeError, match="応答を返しませんでした"):
            pm_read_worker.run_in_read_plane("質問", spawn_timeout=5)

    def test_timeout_kills_hanging_child(self, monkeypatch):
        from argus import pm_read_worker

        fake_proc = _HangingFakeProc()
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake_proc)

        with pytest.raises(RuntimeError, match="タイムアウト"):
            pm_read_worker.run_in_read_plane("質問", spawn_timeout=0.05)

        assert fake_proc.killed
