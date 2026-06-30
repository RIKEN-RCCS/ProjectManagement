#!/usr/bin/env python3
"""
goals_print.py

goals.yaml を人間が読みやすい形式でターミナルに出力する。
ゴール別・月別に集計して表示。各マイルストーンは id / エリア / 達成基準 の順。

Usage:
    python3 scripts/reporting/goals_print.py
    python3 scripts/reporting/goals_print.py --markdown
    python3 scripts/reporting/goals_print.py --no-color
    python3 scripts/reporting/goals_print.py --goals-file path/to/goals.yaml

Options:
    --goals-file PATH   goals.yaml のパス（デフォルト: goals.yaml）
    --markdown          Markdown 形式で出力（ファイル保存・共有向け）
    --no-color          ANSIカラーを無効化（パイプ・ログ向け）
    --output PATH       出力をファイルにも保存
    --area AREA         指定エリアのマイルストーンのみ表示
    --overdue-only      期限超過のマイルストーンのみ表示
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# ANSI カラー
# ---------------------------------------------------------------------------

class Color:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[31m"
    YELLOW = "\033[33m"
    GREEN  = "\033[32m"
    CYAN   = "\033[36m"
    BLUE   = "\033[34m"
    GRAY   = "\033[90m"

    def __init__(self, enabled: bool = True):
        self._on = enabled

    def __getattr__(self, name: str) -> str:
        if not self._on:
            return ""
        return getattr(Color, name, "")


# ---------------------------------------------------------------------------
# 期限ラベル
# ---------------------------------------------------------------------------

def due_label(due_str: str | None, c: Color) -> str:
    if not due_str:
        return f"{c.GRAY}未定{c.RESET}"
    try:
        due = date.fromisoformat(str(due_str))
    except ValueError:
        return str(due_str)

    today = date.today()
    delta = (due - today).days
    formatted = due.strftime("%Y-%m-%d")

    if delta < 0:
        return f"{c.RED}{formatted} (超過 {-delta}日){c.RESET}"
    elif delta <= 14:
        return f"{c.YELLOW}{formatted} (残 {delta}日){c.RESET}"
    elif delta <= 60:
        return f"{c.CYAN}{formatted} (残 {delta}日){c.RESET}"
    else:
        return f"{c.GREEN}{formatted} (残 {delta}日){c.RESET}"


def due_label_md(due_str: str | None) -> str:
    if not due_str:
        return "未定"
    try:
        due = date.fromisoformat(str(due_str))
    except ValueError:
        return str(due_str)

    today = date.today()
    delta = (due - today).days
    formatted = due.strftime("%Y-%m-%d")

    if delta < 0:
        return f"{formatted} ⚠️ 超過 {-delta}日"
    elif delta <= 14:
        return f"{formatted} 🟡 残 {delta}日"
    elif delta <= 60:
        return f"{formatted} 🔵 残 {delta}日"
    else:
        return f"{formatted} ✅ 残 {delta}日"


# ---------------------------------------------------------------------------
# 成功基準パース
# ---------------------------------------------------------------------------

def _extract_criterion(item) -> str:
    if isinstance(item, dict):
        return " / ".join(str(v) for v in item.values() if v)
    return str(item)


def parse_success_criteria(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [_extract_criterion(s) for s in raw if s]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [_extract_criterion(s) for s in parsed if s]
        except (json.JSONDecodeError, ValueError):
            pass
        return [raw.strip()] if raw.strip() else []
    return []


# ---------------------------------------------------------------------------
# 月別グループ化
# ---------------------------------------------------------------------------

def group_by_month(ms_list: list) -> dict[str | None, list]:
    """due_date の年月でグループ化。未定・パース不能は None キー"""
    groups: dict[str | None, list] = {}
    for ms in ms_list:
        due_str = ms.get("due_date")
        key = None
        if due_str:
            try:
                key = date.fromisoformat(str(due_str)).strftime("%Y-%m")
            except ValueError:
                pass
        groups.setdefault(key, []).append(ms)
    return groups


def sorted_month_keys(groups: dict) -> list[str | None]:
    dated = sorted(k for k in groups if k is not None)
    return dated + ([None] if None in groups else [])


# ---------------------------------------------------------------------------
# ターミナル出力
# ---------------------------------------------------------------------------

def _ms_oneliner(ms: dict, c: Color) -> str:
    mid = ms["id"]
    area = ms.get("area") or "-"
    criteria = parse_success_criteria(ms.get("success_criteria"))
    sc_text = "  \n    ".join(criteria) if criteria else ms.get("name", "")
    return (
        f"{c.BOLD}{c.CYAN}[{mid}]{c.RESET}"
        f"{c.CYAN}[{area}]{c.RESET}"
        f"[{sc_text}]"
    )


def print_terminal(goals: list, milestones: list, c: Color, area_filter: str | None, overdue_only: bool) -> str:
    lines = []

    ms_by_goal: dict[str | None, list] = {}
    for ms in milestones:
        ms_by_goal.setdefault(ms.get("goal_id"), []).append(ms)

    def should_show(ms) -> bool:
        if area_filter and ms.get("area", "") != area_filter:
            return False
        if overdue_only:
            due_str = ms.get("due_date")
            if not due_str:
                return False
            try:
                return date.fromisoformat(str(due_str)) < date.today()
            except ValueError:
                return False
        return True

    today_str = date.today().strftime("%Y-%m-%d")
    lines.append(f"{c.BOLD}{c.BLUE}{'='*60}{c.RESET}")
    lines.append(f"{c.BOLD}{c.BLUE}  ゴール・マイルストーン一覧  ({today_str}){c.RESET}")
    lines.append(f"{c.BOLD}{c.BLUE}{'='*60}{c.RESET}")
    lines.append("")

    for goal in goals:
        gid = goal["id"]
        lines.append(f"{c.BOLD}{c.CYAN}■ [{gid}] {goal['name']}{c.RESET}")
        if goal.get("description"):
            lines.append(f"    {c.GRAY}{goal['description']}{c.RESET}")
        lines.append("")

        ms_list = [m for m in ms_by_goal.get(gid, []) if should_show(m)]
        if not ms_list:
            lines.append(f"    {c.GRAY}（このゴールのマイルストーンなし）{c.RESET}")
            lines.append("")
            continue

        monthly = group_by_month(ms_list)
        for month_key in sorted_month_keys(monthly):
            month_ms = monthly[month_key]
            label = month_key if month_key else "未定"
            lines.append(f"  {c.BOLD}{c.BLUE}◆ {label}{c.RESET}")
            for ms in month_ms:
                lines.append(f"  {_ms_oneliner(ms, c)}")
            lines.append("")

    orphans = [m for m in ms_by_goal.get(None, []) if should_show(m)]
    if orphans:
        lines.append(f"{c.BOLD}{c.YELLOW}■ [未分類] ゴール未紐付けのマイルストーン{c.RESET}")
        lines.append("")
        monthly = group_by_month(orphans)
        for month_key in sorted_month_keys(monthly):
            month_ms = monthly[month_key]
            label = month_key if month_key else "未定"
            lines.append(f"  {c.BOLD}{c.BLUE}◆ {label}{c.RESET}")
            for ms in month_ms:
                lines.append(f"  {_ms_oneliner(ms, c)}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown 出力
# ---------------------------------------------------------------------------

def print_markdown(goals: list, milestones: list, area_filter: str | None, overdue_only: bool) -> str:
    lines = []

    ms_by_goal: dict[str | None, list] = {}
    for ms in milestones:
        ms_by_goal.setdefault(ms.get("goal_id"), []).append(ms)

    def should_show(ms) -> bool:
        if area_filter and ms.get("area", "") != area_filter:
            return False
        if overdue_only:
            due_str = ms.get("due_date")
            if not due_str:
                return False
            try:
                return date.fromisoformat(str(due_str)) < date.today()
            except ValueError:
                return False
        return True

    today_str = date.today().strftime("%Y-%m-%d")
    lines.append(f"# ゴール・マイルストーン一覧 ({today_str})\n")

    for goal in goals:
        gid = goal["id"]
        lines.append(f"## [{gid}] {goal['name']}\n")
        if goal.get("description"):
            lines.append(f"> {goal['description']}\n")

        ms_list = [m for m in ms_by_goal.get(gid, []) if should_show(m)]
        if not ms_list:
            lines.append("_このゴールのマイルストーンなし_\n")
            continue

        monthly = group_by_month(ms_list)
        for month_key in sorted_month_keys(monthly):
            month_ms = monthly[month_key]
            label = month_key if month_key else "未定"
            lines.append(f"### {label}\n")
            for ms in month_ms:
                mid = ms["id"]
                area = ms.get("area") or "-"
                criteria = parse_success_criteria(ms.get("success_criteria"))
                sc_text = "  \n".join(criteria) if criteria else ms.get("name", "")
                lines.append(f"`[{mid}][{area}]` {sc_text}  ")
            lines.append("")

    orphans = [m for m in ms_by_goal.get(None, []) if should_show(m)]
    if orphans:
        lines.append("## [未分類] ゴール未紐付けのマイルストーン\n")
        monthly = group_by_month(orphans)
        for month_key in sorted_month_keys(monthly):
            month_ms = monthly[month_key]
            label = month_key if month_key else "未定"
            lines.append(f"### {label}\n")
            for ms in month_ms:
                mid = ms["id"]
                area = ms.get("area") or "-"
                criteria = parse_success_criteria(ms.get("success_criteria"))
                sc_text = "  \n".join(criteria) if criteria else ms.get("name", "")
                lines.append(f"`[{mid}][{area}]` {sc_text}  ")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="goals.yaml を人間が読みやすい形式で表示")
    parser.add_argument("--goals-file", default="goals.yaml", help="goals.yaml のパス")
    parser.add_argument("--markdown", action="store_true", help="Markdown 形式で出力")
    parser.add_argument("--no-color", action="store_true", help="ANSI カラーを無効化")
    parser.add_argument("--output", help="出力をファイルにも保存")
    parser.add_argument("--area", help="指定エリアのマイルストーンのみ表示")
    parser.add_argument("--overdue-only", action="store_true", help="期限超過のみ表示")
    args = parser.parse_args()

    goals_path = Path(args.goals_file)
    if not goals_path.exists():
        print(f"Error: {goals_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    with goals_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    goals = data.get("goals") or []
    milestones = data.get("milestones") or []

    # 期限順にソート（未定は末尾）
    milestones.sort(key=lambda m: (m.get("due_date") is None, m.get("due_date") or ""))

    if args.markdown:
        output = print_markdown(goals, milestones, args.area, args.overdue_only)
    else:
        c = Color(enabled=not args.no_color and sys.stdout.isatty() or not args.no_color)
        output = print_terminal(goals, milestones, c, args.area, args.overdue_only)

    print(output)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\n→ {args.output} に保存しました", file=sys.stderr)


if __name__ == "__main__":
    main()
