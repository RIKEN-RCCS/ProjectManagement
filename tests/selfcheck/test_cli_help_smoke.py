"""scripts/ 配下の argparse CLI 全数に対する --help スモークテスト。

バグクラス (a): --help が ValueError で死ぬ（help 文字列の生 %）・import エラー
の再発を pre-commit 時に検出する。

対象の集め方:
  - scripts/ 以下の *.py（archive/ 除外、.bak 除外、__pycache__ 除外）のうち
    「ArgumentParser( を含み、かつ __main__ ブロックを持つ」ものを CLI とみなす。
  - 2026-06-16 のリファクタで scripts/ 配下がサブディレクトリ化され、旧パスに
    symlink が残っている（例: scripts/pm_relink.py -> quality/pm_relink.py）。
    symlink 実体は同一ファイルなので resolve() して重複除去するが、
    一部スクリプト（pm_api.py / pm_relink.py / pm_screen.py 等）は
    `Path(__file__).parent`（resolve なし）で同階層の同居モジュール
    （cli_utils.py / db_utils.py の symlink）を前提に import しており、
    実体パス（scripts/web/pm_api.py 等）から直接起動すると
    ModuleNotFoundError になる。実際の起動経路（cron / pm_daemon.sh 等）は
    旧パスの symlink 経由のため、symlink が存在する場合はそちらを起動対象にする。
"""
import platform
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PYTHON3 = str(Path.home() / f".venv_{platform.machine()}" / "bin" / "python3")

# torch 等を module レベルで import し、GPU 初期化コストにより --help が
# 数秒〜timeout 超まで揺れるスクリプト。@pytest.mark.slow に分離する。
_SLOW_SCRIPTS = {
    "recording/whisper_vad.py",
}

# 現状のコードベースで検出された本物の import バグ。修正済みのものはここから
# 削除する（2026-07-27: db_utils.py の sys.path 修正で解消）。
_KNOWN_XFAIL: dict[str, str] = {}


def _discover_cli_scripts() -> list[Path]:
    """scripts/ 配下の argparse CLI を実体パスで重複除去して返す。

    top-level に旧パス symlink が存在する場合はそちらを起動対象として選ぶ
    （実際の cron / pm_daemon.sh 呼び出し経路と一致させるため）。
    """
    candidates = []
    for p in sorted(SCRIPTS_DIR.rglob("*.py")):
        if "__pycache__" in p.parts or "archive" in p.parts:
            continue
        if p.name.endswith(".bak"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "ArgumentParser(" in text and "__main__" in text:
            candidates.append(p)

    by_real: dict[Path, Path] = {}
    for p in candidates:
        real = p.resolve()
        by_real.setdefault(real, p)

    result = []
    for real in by_real:
        top_level_symlink = SCRIPTS_DIR / real.name
        if (
            top_level_symlink.is_symlink()
            and top_level_symlink.resolve() == real
        ):
            result.append(top_level_symlink)
        else:
            result.append(real)
    return sorted(result, key=lambda p: str(p.relative_to(SCRIPTS_DIR)))


_CLI_SCRIPTS = _discover_cli_scripts()


def _test_id(p: Path) -> str:
    return str(p.relative_to(SCRIPTS_DIR))


def _param(p: Path):
    rel = _test_id(p)
    marks = []
    if rel in _SLOW_SCRIPTS:
        marks.append(pytest.mark.slow)
    if rel in _KNOWN_XFAIL:
        marks.append(pytest.mark.xfail(reason=_KNOWN_XFAIL[rel], strict=True))
    return pytest.param(p, id=rel, marks=marks)


@pytest.mark.parametrize("script", [_param(p) for p in _CLI_SCRIPTS])
def test_help_smoke(script: Path):
    rel = _test_id(script)
    timeout = 60 if rel in _SLOW_SCRIPTS else 20
    r = subprocess.run(
        [PYTHON3, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert r.returncode == 0, (
        f"{rel} --help が失敗しました (rc={r.returncode})\n"
        f"stdout={r.stdout[-500:]}\nstderr={r.stderr[-1500:]}"
    )


def test_discovered_at_least_one_script():
    assert len(_CLI_SCRIPTS) > 10, "CLI スクリプト検出ロジックが機能していない可能性"
