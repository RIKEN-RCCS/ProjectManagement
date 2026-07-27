"""scripts/bin/*.sh の cron 実行環境契約を静的検査する。

バグクラス (b): cron ラッパーの PATH 欠落（box CLI が cron でだけ
FileNotFoundError、warning 止まりで無音になる）の再発防止。

2026-07-27 の実際の事故（pm_argus_patrol.sh が box CLI の PATH 補正を欠いており、
自動クローズ後の XLSX 再エクスポートが毎回無音で失敗していた）を踏まえ、以下を検査する:

  1. box CLI（Node 製 `box` コマンド）に到達する .sh は、cron 環境でも
     `box` バイナリを見つけられるよう PATH 補正行を持つこと。
  2. python3 を呼ぶ全 .sh は、venv activate を経てから呼んでいること
     （システム python3 には sqlcipher3 が無く暗号化DBを開けない規約）。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BIN_DIR = REPO_ROOT / "scripts" / "bin"

# --------------------------------------------------------------------------- #
# 1. box CLI 依存 → PATH 補正行の必須化
# --------------------------------------------------------------------------- #
# 近似ルール: box CLI (Node 製 `box` コマンド) に到達するスクリプト名を本文に
# 含む .sh、および _lib_sync_canvas.sh を source する .sh を「box CLI 依存」とみなす。
# コメント中の単なる言及（拡張子なし言及）を誤検知しないよう .py / .sh 付きで判定する。
BOX_CLI_PATTERN = re.compile(
    r"pm_minutes_publish\.py|pm_xlsx_sync\.py|pm_xlsx_report\.py"
    r"|pm_box_crawl\.py|pm_minutes_catalog\.py|box_cli|_lib_sync_canvas\.sh"
)
PATH_FIX_PATTERN = re.compile(r"export PATH=.*nvm.*node.*bin", re.IGNORECASE)

# _lib_sync_canvas.sh 自体は shebang を持たない共有ライブラリで、cron から
# 直接起動されることはない（source する側が実行主体）。PATH 補正の責務は
# source する側の .sh にあるため、このファイル自身は対象から除外する。
_LIBRARY_EXEMPT = {"_lib_sync_canvas.sh"}

# 過去に検出された本物のギャップ（box CLI 依存だが PATH 補正が無い）は
# pm_from_recording.sh / pm_from_recording_auto.sh / pm_from_slack.sh に
# PATH 補正行を追加して解消済み（2026-07-27）。
_KNOWN_PATH_GAPS: dict[str, str] = {}


def _bin_scripts() -> list[Path]:
    return sorted(BIN_DIR.glob("*.sh"))


def _box_dependent_scripts() -> list[Path]:
    result = []
    for p in _bin_scripts():
        if p.name in _LIBRARY_EXEMPT:
            continue
        if BOX_CLI_PATTERN.search(p.read_text(encoding="utf-8", errors="ignore")):
            result.append(p)
    return result


def _param_path_check(p: Path):
    marks = []
    if p.name in _KNOWN_PATH_GAPS:
        marks.append(
            pytest.mark.xfail(reason=_KNOWN_PATH_GAPS[p.name], strict=True)
        )
    return pytest.param(p, id=p.name, marks=marks)


@pytest.mark.parametrize(
    "script", [_param_path_check(p) for p in _box_dependent_scripts()]
)
def test_box_dependent_script_has_path_fix(script: Path):
    text = script.read_text(encoding="utf-8", errors="ignore")
    assert PATH_FIX_PATTERN.search(text), (
        f"{script.name} は box CLI に到達するが、cron 実行時に box (Node製) を"
        f" 見つけるための PATH 補正行 (export PATH=...nvm.../node/.../bin:$PATH)"
        f" がありません"
    )


# --------------------------------------------------------------------------- #
# 2. python3 呼び出し → venv activate 必須化
# --------------------------------------------------------------------------- #
_ACTIVATE_RE = re.compile(r"\.venv_(aarch64|x86_64)/bin/activate")
_PY3_RE = re.compile(r"(?<![\w./$-])python3\b(?!\s*=)")
_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")

# 判定困難 / 意図的に system python3 で問題ないケース。
# ファイル単位の免除ではなく「許容する違反行そのもの」を列挙する
# （ファイル全体を免除すると、同じファイルに新しい違反行が増えても検出できない
#  ため、行の集合を厳密に管理し、増えたら fail するようにする）。
_VENV_ACTIVATE_ALLOWLIST: dict[str, dict[str, object]] = {
    "fish_seed_sweep.sh": {
        "reason": (
            "fish-speech TTS の JSON ペイロード構築のみに python3 -c を使う開発用"
            "ツール。DB/sqlcipher3 access なし。"
        ),
        "allowed_lines": {
            'payload=$(python3 -c "',
        },
    },
    "pm_daemon.sh": {
        "reason": (
            "jq 未導入環境向けの ~/.claude/settings.json JSON パースのフォールバック"
            "経路のみ。標準ライブラリの json のみ使用し DB access なし。"
        ),
        "allowed_lines": {
            "if command -v python3 &>/dev/null; then",
            'export $(python3 -c "',
        },
    },
    # _lib_sync_canvas.sh はコメント行のみの誤検知だったため
    # （_PY3_RE のコメント除外により）許容行の登録が不要になった。
}

# 過去に検出された本物のギャップ（pm_from_recording.sh の MEETING_CFG=$(python3 -c ...)
# が system python3 を直接呼んでいた）は $PYTHON3 に置換して解消済み（2026-07-27）。
_KNOWN_VENV_GAPS: dict[str, str] = {}


def _strip_heredocs(text: str) -> str:
    """ヒアドキュメント本体（別ランタイムに渡されるスクリプト文字列）を除去する。

    ヒアドキュメントは singularity コンテナ内部で実行される run.sh 等を組み立てる
    ためのものが多く、ホストの venv 契約の対象外であるため除外する。
    `$((...))` 算術展開の中に `<<`（左シフト）が現れる行はヒアドキュメント開始
    ではないため、誤って以降の行を読み飛ばさないようガードする。
    """
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HEREDOC_START_RE.search(line)
        if m and "$((" not in line:
            delim = m.group(1)
            out.append(line)
            i += 1
            while i < n and lines[i].strip() != delim:
                i += 1
            if i < n:
                i += 1  # 終端デリミタ行自体も読み飛ばす
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _find_venv_violations(text: str) -> list[str]:
    """venv activate より前に生の python3 呼び出しがある行を返す（コメント行は除外）。"""
    stripped = _strip_heredocs(text)
    activate_seen = False
    violations = []
    for line in stripped.split("\n"):
        if _ACTIVATE_RE.search(line):
            activate_seen = True
        if line.lstrip().startswith("#"):
            continue
        for m in _PY3_RE.finditer(line):
            rest = line[m.end():]
            # `local python3; python3="$(detect_python)"` のような
            # 宣言直後同一行での代入イディオムは venv パス変数の定義そのもの
            if re.match(r"^\s*;\s*python3\s*=", rest):
                continue
            if not activate_seen:
                violations.append(line.strip())
    return violations


def _param_venv_check(p: Path):
    marks = []
    if p.name in _KNOWN_VENV_GAPS:
        marks.append(
            pytest.mark.xfail(reason=_KNOWN_VENV_GAPS[p.name], strict=True)
        )
    return pytest.param(p, id=p.name, marks=marks)


@pytest.mark.parametrize(
    "script", [_param_venv_check(p) for p in _bin_scripts()]
)
def test_python3_invocation_follows_venv_activate(script: Path):
    text = script.read_text(encoding="utf-8", errors="ignore")
    violations = _find_venv_violations(text)
    allowed = _VENV_ACTIVATE_ALLOWLIST.get(script.name, {}).get("allowed_lines", set())
    unexpected = [v for v in violations if v not in allowed]
    assert not unexpected, (
        f"{script.name}: venv activate 前に生の python3 呼び出しがあります"
        f"（ALLOWLIST 未登録）: {unexpected}"
    )
