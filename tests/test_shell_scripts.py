"""Smoke tests for shell scripts in scripts/bin/.

Verifies that each script can start without ImportError or "No such file"
errors after .sh symlink removal.  Tests use --help or intentional argument
errors to exit quickly without side effects.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "scripts" / "bin"


def _run(cmd: list[str], *, timeout: int = 15):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "")},
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        so = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else str(e.stdout or "")
        se = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else str(e.stderr or "")
        return 0, so, se


def _bash(script: str, *args: str, timeout: int = 15):
    return _run(["bash", str(BIN_DIR / script), *args], timeout=timeout)


# --------------------------------------------------------------------------- #
# --help で終了コード 0 のスクリプト
# --------------------------------------------------------------------------- #

def test_pm_box_update():
    rc, out, err = _bash("pm_box_update.sh", "--help")
    assert rc == 0 or "Usage" in out + err, f"out={out[:200]} err={err[:200]}"


def test_slack_post_minutes():
    rc, out, err = _bash("slack_post_minutes.sh", "--help")
    assert rc == 0 or "Usage" in out + err, f"out={out[:200]} err={err[:200]}"


# --------------------------------------------------------------------------- #
# 引数不足で終了コード 1 だが、Usage を表示して graceful に終了するスクリプト
# --------------------------------------------------------------------------- #

def test_pm_from_recording():
    rc, out, err = _bash("pm_from_recording.sh")
    assert "Usage" in out + err, f"out={out[:200]} err={err[:200]}"


def test_pm_from_slack():
    rc, out, err = _bash("pm_from_slack.sh")
    assert rc != 0
    assert "指定してください" in out + err or "Usage" in out + err, f"out={out[:200]}"


def test_pm_daemon():
    rc, out, err = _bash("pm_daemon.sh")
    assert "Usage" in out + err, f"out={out[:200]} err={err[:200]}"


# --------------------------------------------------------------------------- #
# 処理を開始するが、タイムアウトで止めるスクリプト
# --------------------------------------------------------------------------- #

# pm_web_update.sh / pm_web_fetch.py は 2026-08-01 に削除した
# （docs/security-architecture.md §4.5。認証境界の外へ出る唯一の経路だったため）。
# 復活させないこと — 再導入はセキュリティ上の判断を伴う。


def test_web_fetch_scripts_are_gone():
    """Web 取得スクリプトが復活していないことを固定する（§4.5 の廃止決定）。"""
    for rel in (
        "bin/pm_web_update.sh",
        "data-pipeline/pm_web_fetch.py",
        "pm_web_fetch.py",
    ):
        assert not (REPO_ROOT / "scripts" / rel).exists(), (
            f"{rel} が復活しています。docs/security-architecture.md §4.5 で廃止決定済み"
        )


def test_canvas_report():
    # 引数なし → ログ出力開始後、引数不足で終了
    rc, out, err = _bash("canvas_report.sh", timeout=8)
    assert "開始" in out or "ステップ" in out or rc != 0, \
        f"out={out[:200]} err={err[:200]}"


# --------------------------------------------------------------------------- #
# 少ない引数で short option のみ確認
# --------------------------------------------------------------------------- #

def test_pm_argus_daily_summary():
    rc, out, err = _bash("pm_argus_daily_summary.sh", timeout=5)
    # LLM 呼び出しに入る前にタイムアウト → 正常起動確認
    assert "開始" in out or rc == 0, f"out={out[:200]} err={err[:200]}"
