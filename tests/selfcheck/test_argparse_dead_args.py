"""argparse の飾り引数（受け取るが未参照）を ast で静的検出する。

バグクラス (d): add_argument() で定義された dest が、そのスクリプト内で
一度も args.<dest> / getattr(args, "<dest>") として参照されない場合、
CLI 引数を受け取っても何もしない「飾り引数」である可能性が高い。

対象:
  - tests/selfcheck/test_cli_help_smoke.py と同じ発見ロジックで集めた
    standalone CLI（ArgumentParser( を自前生成し __main__ を持つスクリプト）
  - scripts/ingest/ 配下の IngestPlugin 実装（add_args() で引数を登録し、
    run() で同一ファイル内から参照する。エントリポイントは pm_ingest.py だが
    実際の引数定義・参照は各プラグインファイル内で完結している）

ALLOWLIST は「誤検知と判断したもの」のみに使う。真に未参照と判定された
飾り引数は削除せず ALLOWLIST にも入れず、そのままテストを fail させて
報告する（判断はオーケストレーターに委ねる）。
"""
import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
INGEST_DIR = SCRIPTS_DIR / "ingest"

# 誤検知と判断済みのケース。
#   file (scripts/ からの相対パス) -> [(dest, 理由), ...]
ALLOWLIST: dict[str, list[str]] = {}

# 現状のコードベースで検出された本物の飾り引数。修正済みのものはここから削除する
# （2026-07-27: generate_minutes_local.py --no-stream は call_argus_llm() が
#  local ルートで no_stream=True を常時強制済みで、ストリーミング切替が実装上
#  どこにも存在しないため引数自体を削除して解消）。
_KNOWN_DEAD_ARGS: dict[str, dict[str, str]] = {}


def _dest_from_option_strings(opts: list[str]) -> str:
    long_opts = [o for o in opts if o.startswith("--")]
    chosen = long_opts[0] if long_opts else opts[0]
    return chosen.lstrip("-").replace("-", "_")


def _extract_add_argument_dests(tree: ast.AST) -> list[tuple[str, int]]:
    """add_argument(...) 呼び出しから (dest, lineno) を抽出する。"""
    dests = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name != "add_argument":
            continue

        dest = None
        for kw in node.keywords:
            if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                dest = kw.value.value
        if dest is None:
            opt_strings = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            positional_opts = [o for o in opt_strings if o.startswith("-")]
            if not positional_opts:
                # 位置引数（"input_file" 等）。dest 名の推定が曖昧なため対象外。
                continue
            dest = _dest_from_option_strings(positional_opts)
        dests.append((dest, node.lineno))
    return dests


def _is_referenced(dest: str, source: str) -> bool:
    patterns = [
        rf"args\.{re.escape(dest)}\b",
        rf'getattr\(args,\s*["\']{re.escape(dest)}["\']',
        rf'["\']({re.escape(dest)})["\']\s*in\s+',
    ]
    return any(re.search(pat, source) for pat in patterns)


def _discover_targets() -> list[Path]:
    """dead-args 検査の対象ファイルを集める（standalone CLI + ingest プラグイン）。"""
    candidates = []
    for p in sorted(SCRIPTS_DIR.rglob("*.py")):
        if "__pycache__" in p.parts or "archive" in p.parts:
            continue
        if p.name.endswith(".bak"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "ArgumentParser(" in text and "__main__" in text:
            candidates.append(p)

    by_real: dict[Path, Path] = {}
    for p in candidates:
        by_real.setdefault(p.resolve(), p)
    targets = list(by_real.keys())

    # ingest プラグイン: add_args()/run() が同一ファイル内で完結するため、
    # standalone CLI ではなくても対象に加える（pm_ingest.py / ingest_plugin.py は除く）。
    if INGEST_DIR.is_dir():
        for p in sorted(INGEST_DIR.glob("*.py")):
            if p.name in ("pm_ingest.py", "ingest_plugin.py", "__init__.py"):
                continue
            if "add_argument(" in p.read_text(encoding="utf-8", errors="ignore"):
                targets.append(p.resolve())

    return sorted(set(targets), key=lambda p: str(p.relative_to(SCRIPTS_DIR)))


def _build_cases():
    cases = []
    for path in _discover_targets():
        rel = str(path.relative_to(SCRIPTS_DIR))
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for dest, lineno in _extract_add_argument_dests(tree):
            if dest in ALLOWLIST.get(rel, []):
                continue
            marks = []
            if dest in _KNOWN_DEAD_ARGS.get(rel, {}):
                marks.append(
                    pytest.mark.xfail(
                        reason=_KNOWN_DEAD_ARGS[rel][dest], strict=True
                    )
                )
            cases.append(
                pytest.param(
                    path, dest, source, id=f"{rel}::{dest}(L{lineno})", marks=marks
                )
            )
    return cases


@pytest.mark.parametrize("path,dest,source", _build_cases())
def test_argument_dest_is_referenced(path: Path, dest: str, source: str):
    assert _is_referenced(dest, source), (
        f"{path.relative_to(SCRIPTS_DIR)}: dest={dest!r} が add_argument() で"
        f" 定義されているが、args.{dest} が一度も参照されていません（飾り引数）"
    )
