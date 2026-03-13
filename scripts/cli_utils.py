#!/usr/bin/env python3
"""
cli_utils.py

PM支援スクリプト共通の CLI ユーティリティ。
argparse ヘルパー関数・make_logger() を提供する。
"""

import argparse
import re
from pathlib import Path


# --------------------------------------------------------------------------- #
# argparse ヘルパー
# --------------------------------------------------------------------------- #

def add_output_arg(parser: argparse.ArgumentParser) -> None:
    """--output PATH を parser に追加する"""
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="出力をファイルにも保存")


def add_no_encrypt_arg(parser: argparse.ArgumentParser) -> None:
    """--no-encrypt を parser に追加する"""
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DBを暗号化しない（平文モード）")


def add_dry_run_arg(parser: argparse.ArgumentParser) -> None:
    """--dry-run を parser に追加する"""
    parser.add_argument("--dry-run", action="store_true",
                        help="DB保存なし・結果を標準出力のみ")


def add_since_arg(parser: argparse.ArgumentParser, help_suffix: str = "") -> None:
    """--since YYYY-MM-DD を parser に追加する"""
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help=f"この日付以降のデータのみ対象{help_suffix}")


def add_db_arg(parser: argparse.ArgumentParser, default: str = "data/pm.db") -> None:
    """--db PATH を parser に追加する"""
    parser.add_argument("--db", default=None, metavar="PATH",
                        help=f"pm.db のパス（デフォルト: {default}）")


# --------------------------------------------------------------------------- #
# ロガーユーティリティ
# --------------------------------------------------------------------------- #

def make_logger(output_path: str | None):
    """
    (log, close) のタプルを返す。

    Parameters
    ----------
    output_path : str | None
        ファイルに出力する場合はパス文字列。None なら標準出力のみ。

    Returns
    -------
    log : Callable[[str], None]
        print(msg) + output_file.write(msg + "\\n") を行う関数
    close : Callable[[], None]
        output_file を閉じる関数（output_path が None の場合は何もしない）
    """
    output_file = open(output_path, "w", encoding="utf-8") if output_path else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    def close() -> None:
        if output_file:
            output_file.close()

    return log, close


# --------------------------------------------------------------------------- #
# CLAUDE.md ローダー
# --------------------------------------------------------------------------- #

def load_claude_md(claude_md_path: Path) -> str:
    """
    CLAUDE.md を読み込み、`@path` 参照を再帰的に展開して返す。

    Claude Code は `@docs/project.md` のような行を自動的に展開するが、
    スクリプトがファイルを直接読む場合は展開されない。
    本関数はその差異を吸収し、参照先ファイルの内容をインラインに結合する。
    """
    if not claude_md_path.exists():
        return ""
    base_dir = claude_md_path.parent
    return _expand_at_refs(claude_md_path.read_text(encoding="utf-8"), base_dir, depth=0)


def _expand_at_refs(text: str, base_dir: Path, depth: int) -> str:
    if depth > 5:  # 循環参照ガード
        return text
    lines = []
    for line in text.splitlines():
        m = re.match(r"^@(.+)$", line.strip())
        if m:
            ref_path = base_dir / m.group(1).strip()
            if ref_path.exists():
                included = _expand_at_refs(
                    ref_path.read_text(encoding="utf-8"), ref_path.parent, depth + 1
                )
                lines.append(included)
            # 参照先が存在しない場合はその行をスキップ
        else:
            lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# パスユーティリティ
# --------------------------------------------------------------------------- #

def resolve_db_path(arg_db: str | None, default: Path) -> Path:
    """--db 引数からパスを解決する"""
    return Path(arg_db) if arg_db else default
