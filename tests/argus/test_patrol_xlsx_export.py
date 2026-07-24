"""_export_xlsx_after_close の純テスト（subprocess monkeypatch、実DB/Box不使用）。

patrol の自動クローズ後に Box XLSX（open のみを載せる）を再エクスポートしないと、
pm_xlsx_sync.py 実行時に古いシート値で closed→open へ巻き戻る恐れがある。
その再エクスポート起動部（成功/失敗/タイムアウト/例外時に巡回を落とさないこと）を検証する。
"""
import logging
import subprocess

from argus import pm_argus_patrol


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_export_success_returns_true(monkeypatch):
    monkeypatch.setattr(
        pm_argus_patrol.subprocess, "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode=0),
    )
    ok = pm_argus_patrol._export_xlsx_after_close(logging.getLogger("test_patrol_export_ok"))
    assert ok is True


def test_export_success_logs_stdout_tail(monkeypatch, caplog):
    monkeypatch.setattr(
        pm_argus_patrol.subprocess, "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode=0, stdout="Done."),
    )
    logger = logging.getLogger("test_patrol_export_stdout")
    with caplog.at_level(logging.INFO, logger=logger.name):
        ok = pm_argus_patrol._export_xlsx_after_close(logger)
    assert ok is True
    assert any("Done." in r.getMessage() for r in caplog.records)


def test_export_nonzero_exit_returns_false_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(
        pm_argus_patrol.subprocess, "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode=1, stderr="boom"),
    )
    logger = logging.getLogger("test_patrol_export_fail")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        ok = pm_argus_patrol._export_xlsx_after_close(logger)
    assert ok is False
    assert any("失敗" in r.getMessage() for r in caplog.records)


def test_export_timeout_returns_false_and_warns(monkeypatch, caplog):
    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pm_minutes_publish.py", timeout=300)

    monkeypatch.setattr(pm_argus_patrol.subprocess, "run", _raise_timeout)
    logger = logging.getLogger("test_patrol_export_timeout")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        ok = pm_argus_patrol._export_xlsx_after_close(logger)
    assert ok is False
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)


def test_export_unexpected_exception_does_not_propagate(monkeypatch, caplog):
    def _raise(*a, **kw):
        raise RuntimeError("予期しない失敗")

    monkeypatch.setattr(pm_argus_patrol.subprocess, "run", _raise)
    logger = logging.getLogger("test_patrol_export_exc")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        ok = pm_argus_patrol._export_xlsx_after_close(logger)
    assert ok is False
    assert any("失敗" in r.getMessage() for r in caplog.records)


def test_export_uses_sys_executable_and_xlsx_only_flag(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw.get("timeout")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(pm_argus_patrol.subprocess, "run", _fake_run)
    pm_argus_patrol._export_xlsx_after_close(logging.getLogger("test_patrol_export_cmd"))

    assert captured["cmd"][0] == pm_argus_patrol.sys.executable
    assert captured["cmd"][-1] == "--xlsx-only"
    assert str(captured["cmd"][1]).endswith("pm_minutes_publish.py")
    assert captured["timeout"] == 300
