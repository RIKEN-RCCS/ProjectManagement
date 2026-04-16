"""
format_utils.py — Markdown テーブル整形の共通ユーティリティ

pm_insight.py と pm_argus.py で重複していた整形関数を統合。
"""

from datetime import date

from db_utils import normalize_assignee


def format_milestone_table(milestones: list[dict], today: str) -> str:
    """マイルストーン進捗テーブル（Markdown）"""
    if not milestones:
        return "（マイルストーン未登録）"
    lines = [
        "| ID | 名前 | 期限 | 残日数 | open | closed | 状況 |",
        "|----|------|------|--------|------|--------|------|",
    ]
    for m in milestones:
        due = m.get("due_date") or "未定"
        if m.get("due_date"):
            delta = (date.fromisoformat(m["due_date"]) - date.fromisoformat(today)).days
            remaining = f"{delta}日" if delta >= 0 else f"{abs(delta)}日超過"
        else:
            remaining = "-"
        open_c   = m["open_count"]
        closed_c = m["closed_count"]
        total    = open_c + closed_c
        if m.get("status") == "achieved":
            st = "達成済"
        elif m.get("due_date") and m["due_date"] < today:
            st = "遅延"
        elif total == 0:
            st = "未着手"
        else:
            pct = closed_c / total * 100 if total else 0
            st = f"進行中({pct:.0f}%)"
        lines.append(f"| {m['milestone_id']} | {m['name']} | {due} | {remaining} | {open_c} | {closed_c} | {st} |")
    return "\n".join(lines)


def format_overdue_list(items: list[dict], limit: int = 15) -> str:
    """期限超過アイテムの箇条書き"""
    if not items:
        return "（なし）"
    lines = []
    for it in items[:limit]:
        assignee = normalize_assignee(it.get("assignee")) or "未定"
        ms = it.get("milestone_id") or "-"
        lines.append(f"- [ID:{it['id']}][期限:{it['due_date']}][担当:{assignee}][MS:{ms}] {it['content'][:80]}")
    if len(items) > limit:
        lines.append(f"（他 {len(items) - limit} 件）")
    return "\n".join(lines)


def format_assignee_table(workload: list[dict]) -> str:
    """担当者別負荷テーブル（Markdown）"""
    if not workload:
        return "（データなし）"
    lines = [
        "| 担当者 | open件数 | 期限超過 | 期限未設定 |",
        "|--------|----------|----------|------------|",
    ]
    for w in workload:
        overdue_str = str(w["overdue"]) if w["overdue"] == 0 else f"{w['overdue']}件(超過)"
        lines.append(f"| {w['assignee']} | {w['total_open']} | {overdue_str} | {w['no_due_date']} |")
    return "\n".join(lines)


def format_weekly_trends(trends: list[dict]) -> str:
    """週次トレンドテーブル（Markdown）"""
    if not trends:
        return "（データなし）"
    lines = [
        "| 週 | 作成件数 | 完了件数（近似） |",
        "|----|----------|-----------------|",
    ]
    for t in trends:
        lines.append(f"| {t['week_start']}〜{t['week_end']} | {t['created']} | {t['closed']} |")
    return "\n".join(lines)


def format_decisions_list(decisions: list[dict], limit: int = 10) -> str:
    """未確認決定事項の箇条書き"""
    if not decisions:
        return "（なし）"
    lines = []
    for d in decisions[:limit]:
        lines.append(f"- [D:{d['id']}][{d.get('decided_at') or '日付不明'}] {d['content'][:100]}")
    if len(decisions) > limit:
        lines.append(f"（他 {len(decisions) - limit} 件）")
    return "\n".join(lines)
