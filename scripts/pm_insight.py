#!/usr/bin/env python3
"""
pm_insight.py

pm.db のデータを統計集計し、LLM でプロジェクトの健全性評価・リスク特定・改善提案を
生成してレポート出力・Canvas投稿する。

Usage:
    python3 scripts/pm_insight.py --db data/pm.db --dry-run
    python3 scripts/pm_insight.py --db data/pm.db --output insight.md
    python3 scripts/pm_insight.py --db data/pm.db --canvas-id F0AAD2494VB

Options:
    --db PATH               pm.db のパス（必須）
    --canvas-id ID          投稿先 Canvas ID（省略時は Canvas 投稿なし）
    --since YYYY-MM-DD      この日付以降のデータのみ対象
    --skip-canvas           Canvas 投稿をスキップ
    --dry-run               Canvas 投稿なし・結果を標準出力のみ
    --output PATH           結果をファイルにも保存
    --no-encrypt            DBを暗号化しない（平文モード）
    --model MODEL           使用する Claude モデル
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import (
    add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg,
    make_logger, load_claude_md,
)

# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD  = REPO_ROOT / "CLAUDE.md"


# --------------------------------------------------------------------------- #
# DB 接続
# --------------------------------------------------------------------------- #
def open_pm_db(db_path: Path, no_encrypt: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(
        db_path,
        encrypt=not no_encrypt,
        migrations=["ALTER TABLE decisions ADD COLUMN acknowledged_at TEXT"],
    )


# --------------------------------------------------------------------------- #
# データ収集
# --------------------------------------------------------------------------- #
def fetch_milestone_progress(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("""
            SELECT m.milestone_id, m.goal_id, m.name, m.due_date, m.area,
                   m.status, m.success_criteria,
                   COUNT(DISTINCT CASE WHEN a.status='open'   THEN a.id END) AS open_count,
                   COUNT(DISTINCT CASE WHEN a.status='closed' THEN a.id END) AS closed_count
            FROM milestones m
            LEFT JOIN action_items a ON a.milestone_id = m.milestone_id
            WHERE m.status = 'active'
            GROUP BY m.milestone_id
            ORDER BY m.due_date ASC NULLS LAST
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def fetch_overdue_items(conn: sqlite3.Connection, today: str, since: str | None) -> list[dict]:
    """期限超過（status='open' かつ due_date < today）のアイテムを取得"""
    query = """
        SELECT id, content, assignee, due_date, milestone_id
        FROM action_items
        WHERE status = 'open' AND due_date IS NOT NULL AND due_date < ?
    """
    params: list = [today]
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    query += " ORDER BY due_date ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def _normalize_assignee(name: str | None) -> str:
    if not name:
        return "未定"
    if re.search(r"[\u3040-\u9fff]", name):
        name = name.replace(" ", "").replace("\u3000", "")
    return name


def fetch_assignee_workload(conn: sqlite3.Connection, today: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT assignee, due_date FROM action_items WHERE status = 'open'"
        ).fetchall()
    except Exception:
        return []
    counts: dict[str, dict] = {}
    for row in rows:
        name = _normalize_assignee(row["assignee"])
        entry = counts.setdefault(name, {"total_open": 0, "overdue": 0, "no_due_date": 0})
        entry["total_open"] += 1
        if row["due_date"] and row["due_date"] < today:
            entry["overdue"] += 1
        if not row["due_date"]:
            entry["no_due_date"] += 1
    result = [{"assignee": k, **v} for k, v in counts.items()]
    result.sort(key=lambda x: (-x["overdue"], -x["total_open"]))
    return result


def fetch_unlinked_items_count(conn: sqlite3.Connection, since: str | None) -> int:
    """milestone_id が未設定の open アイテム数（計画の穴）"""
    query = "SELECT COUNT(*) FROM action_items WHERE status='open' AND milestone_id IS NULL"
    params: list = []
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    return conn.execute(query, params).fetchone()[0]


def fetch_no_assignee_count(conn: sqlite3.Connection, since: str | None) -> int:
    """担当者なしの open アイテム数"""
    query = "SELECT COUNT(*) FROM action_items WHERE status='open' AND (assignee IS NULL OR assignee = '')"
    params: list = []
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    return conn.execute(query, params).fetchone()[0]


def fetch_weekly_trends(conn: sqlite3.Connection, weeks: int = 4) -> list[dict]:
    """直近 N 週の「作成件数」と「完了件数」の近似トレンド"""
    today_dt = date.today()
    result = []
    for w in range(weeks, 0, -1):
        week_start = (today_dt - timedelta(weeks=w)).isoformat()
        week_end   = (today_dt - timedelta(weeks=w - 1)).isoformat()
        created = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE extracted_at >= ? AND extracted_at < ?",
            (week_start, week_end),
        ).fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE status='closed' AND extracted_at >= ? AND extracted_at < ?",
            (week_start, week_end),
        ).fetchone()[0]
        result.append({
            "week_start": week_start,
            "week_end": week_end,
            "created": created,
            "closed": closed,
        })
    return result


def fetch_unacknowledged_decisions(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    """未確認（acknowledged_at IS NULL）の決定事項（最大20件）"""
    query = "SELECT id, content, decided_at FROM decisions WHERE acknowledged_at IS NULL"
    params: list = []
    if since:
        query += " AND decided_at >= ?"
        params.append(since)
    query += " ORDER BY decided_at DESC LIMIT 20"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_summary_stats(conn: sqlite3.Connection, since: str | None, today: str) -> dict:
    """全体統計"""
    def _count(query: str, params: list) -> int:
        return conn.execute(query, params).fetchone()[0]

    p_since = [since] if since else []
    since_filter_ai = " AND extracted_at >= ?" if since else ""
    since_filter_d  = " WHERE decided_at >= ?" if since else ""

    return {
        "total_open": _count(
            f"SELECT COUNT(*) FROM action_items WHERE status='open'{since_filter_ai}", p_since
        ),
        "total_closed": _count(
            f"SELECT COUNT(*) FROM action_items WHERE status='closed'{since_filter_ai}", p_since
        ),
        "overdue_count": _count(
            "SELECT COUNT(*) FROM action_items WHERE status='open' AND due_date IS NOT NULL AND due_date < ?",
            [today],
        ),
        "total_decisions": _count(
            f"SELECT COUNT(*) FROM decisions{since_filter_d}", p_since
        ),
        "unacknowledged_decisions": _count(
            "SELECT COUNT(*) FROM decisions WHERE acknowledged_at IS NULL", []
        ),
    }


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #
def load_context_from_claude_md() -> str:
    text = load_claude_md(CLAUDE_MD)
    sections = []
    capture = False
    for line in text.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)", line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else text[:3000]


def _format_milestone_table(milestones: list[dict], today: str) -> str:
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


def _format_overdue_list(items: list[dict]) -> str:
    if not items:
        return "（なし）"
    lines = []
    for it in items[:15]:
        assignee = _normalize_assignee(it.get("assignee"))
        ms = it.get("milestone_id") or "-"
        lines.append(f"- [ID:{it['id']}][期限:{it['due_date']}][担当:{assignee}][MS:{ms}] {it['content'][:80]}")
    if len(items) > 15:
        lines.append(f"（他 {len(items) - 15} 件）")
    return "\n".join(lines)


def _format_assignee_table(workload: list[dict]) -> str:
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


def _format_weekly_trends(trends: list[dict]) -> str:
    if not trends:
        return "（データなし）"
    lines = [
        "| 週 | 作成件数 | 完了件数（近似） |",
        "|----|----------|-----------------|",
    ]
    for t in trends:
        lines.append(f"| {t['week_start']}〜{t['week_end']} | {t['created']} | {t['closed']} |")
    return "\n".join(lines)


def _format_decisions_list(decisions: list[dict]) -> str:
    if not decisions:
        return "（なし）"
    lines = []
    for d in decisions[:10]:
        lines.append(f"- [D:{d['id']}][{d.get('decided_at') or '日付不明'}] {d['content'][:100]}")
    if len(decisions) > 10:
        lines.append(f"（他 {len(decisions) - 10} 件）")
    return "\n".join(lines)


INSIGHT_PROMPT = """\
あなたは富岳NEXTプロジェクトのシニアプロジェクトマネージャーです。
以下のプロジェクト統計データを分析し、プロジェクトの健全性評価・リスク特定・改善提案を行ってください。

## プロジェクト文脈

{context}

## 集計日: {today}（集計範囲: {since_note}）

## 全体統計

- オープンアクションアイテム: {total_open}件
- 完了アクションアイテム: {total_closed}件
- 期限超過（open）: {overdue_count}件
- 決定事項総数: {total_decisions}件（未確認: {unacknowledged_decisions}件）
- マイルストーン未紐づけのopenアイテム: {unlinked_count}件
- 担当者なしのopenアイテム: {no_assignee_count}件

## マイルストーン進捗

{milestone_table}

## 期限超過アイテム（上位15件）

{overdue_list}

## 担当者別負荷

{assignee_table}

## 週次トレンド（直近4週）

{weekly_trends}

## 未確認決定事項（上位10件）

{decisions_list}

---

上記データを分析し、以下のJSON形式のみで回答してください（前後の説明テキスト不要）:

```json
{{
  "health_score": "A/B/C/D のいずれか（A:良好、B:概ね順調、C:要注意、D:危機的）",
  "health_summary": "プロジェクト全体の現状を200字以内で日本語で説明",
  "milestone_assessments": [
    {{
      "milestone_id": "M1",
      "assessment": "進捗の定性的評価（50字以内）",
      "concern": "懸念事項（なければ null）"
    }}
  ],
  "risks": [
    {{
      "priority": "H/M/L",
      "category": "delay/unowned/bottleneck/decision/resource/other",
      "description": "リスクの説明（80字以内）",
      "recommended_action": "推奨対応（80字以内）"
    }}
  ],
  "recommendations": [
    {{
      "priority": 1,
      "action": "具体的なアクション（80字以内）",
      "rationale": "理由・根拠（80字以内）"
    }}
  ]
}}
```
"""


def build_analysis_prompt(
    data: dict,
    context: str,
    today: str,
    since: str | None,
) -> str:
    stats = data["stats"]
    since_note = f"{since} 以降" if since else "全期間"
    return INSIGHT_PROMPT.format(
        context=context,
        today=today,
        since_note=since_note,
        total_open=stats["total_open"],
        total_closed=stats["total_closed"],
        overdue_count=stats["overdue_count"],
        total_decisions=stats["total_decisions"],
        unacknowledged_decisions=stats["unacknowledged_decisions"],
        unlinked_count=data["unlinked_count"],
        no_assignee_count=data["no_assignee_count"],
        milestone_table=_format_milestone_table(data["milestones"], today),
        overdue_list=_format_overdue_list(data["overdue_items"]),
        assignee_table=_format_assignee_table(data["assignee_workload"]),
        weekly_trends=_format_weekly_trends(data["weekly_trends"]),
        decisions_list=_format_decisions_list(data["unacknowledged_decisions"]),
    )


# --------------------------------------------------------------------------- #
# LLM 呼び出し
# --------------------------------------------------------------------------- #
def call_claude(prompt: str, model: str | None = None, timeout: int = 300) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = ["claude"]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


# --------------------------------------------------------------------------- #
# レポート整形
# --------------------------------------------------------------------------- #
_HEALTH_ICONS = {"A": "[A]", "B": "[B]", "C": "[C]", "D": "[D]"}
_PRIORITY_ICONS = {"H": "高", "M": "中", "L": "低"}


def format_insight_report(insight: dict, data: dict, today: str, since: str | None) -> str:
    since_note = f"（{since} 以降）" if since else "（全期間）"
    stats = data["stats"]
    score = insight.get("health_score", "?")
    icon = _HEALTH_ICONS.get(score, score)

    sections = [f"# 富岳NEXT プロジェクトインサイト（{today}）\n\n集計範囲: {since_note}"]

    # --- 総合評価 ---
    summary = insight.get("health_summary", "")
    sections.append(
        f"## 総合評価: {icon}\n\n{summary}\n\n"
        f"- オープン: {stats['total_open']}件 / 完了: {stats['total_closed']}件"
        f" / 期限超過: {stats['overdue_count']}件 / 未確認決定事項: {stats['unacknowledged_decisions']}件"
    )

    # --- マイルストーン別評価 ---
    ms_assessments = insight.get("milestone_assessments", [])
    if ms_assessments:
        ms_map = {m["milestone_id"]: m for m in data["milestones"]}
        lines = []
        for ma in ms_assessments:
            mid       = ma.get("milestone_id", "")
            ms_name   = ms_map.get(mid, {}).get("name", mid)
            due       = ms_map.get(mid, {}).get("due_date") or "未定"
            assessment = ma.get("assessment", "")
            concern   = ma.get("concern")
            status_mark = "[!]" if concern else "[OK]"
            lines.append(f"- {status_mark} **{mid}: {ms_name}** （期限: {due}）")
            lines.append(f"  - {assessment}")
            if concern:
                lines.append(f"  - 懸念: {concern}")
        sections.append("## マイルストーン別評価\n\n" + "\n".join(lines))

    # --- リスク・課題 ---
    risks = insight.get("risks", [])
    if risks:
        by_priority: dict[str, list] = {"H": [], "M": [], "L": []}
        for r in risks:
            by_priority.setdefault(r.get("priority", "M"), []).append(r)
        risk_lines = []
        for prio in ("H", "M", "L"):
            items = by_priority.get(prio, [])
            if not items:
                continue
            label = {"H": "高優先度", "M": "中優先度", "L": "低優先度"}[prio]
            risk_lines.append(f"### [{_PRIORITY_ICONS[prio]}] {label}")
            for r in items:
                desc   = r.get("description", "")
                action = r.get("recommended_action", "")
                risk_lines.append(f"- {desc}")
                if action:
                    risk_lines.append(f"  - 推奨対応: {action}")
        sections.append("## リスク・課題\n\n" + "\n".join(risk_lines))
    else:
        sections.append("## リスク・課題\n\n特になし")

    # --- 改善提案 ---
    recs = insight.get("recommendations", [])
    if recs:
        rec_lines = []
        for i, r in enumerate(recs, 1):
            action    = r.get("action", "")
            rationale = r.get("rationale", "")
            rec_lines.append(f"{i}. {action}")
            if rationale:
                rec_lines.append(f"   - 根拠: {rationale}")
        sections.append("## 改善提案\n\n" + "\n".join(rec_lines))

    return "\n\n".join(sections)


def sanitize_for_canvas(text: str) -> str:
    """Canvas向けMarkdown変換（特殊文字・見出しレベルの正規化）"""
    replacements = {
        "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u2212": "-", "\u2011": "-", "\u2010": "-",
        "\uff5e": "-", "\u301c": "-",
        "\uff08": "(", "\uff09": ")",
        "\uff0c": ",", "\uff0e": ".", "\uff01": "!",
        "\uff1a": ":", "\uff1b": ";", "\uff1f": "?",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
        "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
        "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>",
        "\u25b6": ">", "\u25c0": "<",
        "\u30fb": ".", "\u2022": "-", "\u2023": "-",
        "\u25cf": "-", "\u25cb": "-", "\u2027": ".",
        "\u3000": " ", "\u00a0": " ",
        "\u2026": "...", "\u22ef": "...",
        "\u00d7": "x", "\u00f7": "/",
        "\u2605": "*", "\u2606": "*",
        "\u2713": "OK", "\u2714": "OK", "\u2715": "NG", "\u2716": "NG",
        "\u25a0": "-", "\u25a1": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?<![<(\[])https?://[^\s<>）」\]]+[^\s<>）」\].,;:!?、。]",
                  lambda m: f"<{m.group(0)}>", text)
    text = re.sub(r"^#{4,6}\s+", "### ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s+)\d+\.\s+", r"\1- ", text, flags=re.MULTILINE)
    text = re.sub(r"^> (-|\*|\d+\.)\s+", r"\1 ", text, flags=re.MULTILINE)

    def keep_char(c: str) -> str:
        cp = ord(c)
        if 0x20 <= cp <= 0x7E:
            return c
        if c in ("\n", "\t"):
            return c
        if 0x3000 <= cp <= 0x9FFF:
            return c
        if 0xF900 <= cp <= 0xFAFF:
            return c
        if 0xFF00 <= cp <= 0xFFEF:
            return c
        if 0x00C0 <= cp <= 0x024F:
            return c
        return ""

    text = "".join(keep_char(c) for c in text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# --------------------------------------------------------------------------- #
# Canvas 投稿
# --------------------------------------------------------------------------- #
_PAT_TAG_WITH_ID = re.compile(
    r"<(h[1-6]|p|div|ul|ol|li|blockquote|pre|hr|table|tbody|thead|tr|td|th)\b[^>]*\sid=['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_PAT_DATA_BLOCK = re.compile(r'data-block-id=["\']([^"\']+)["\']')
_PAT_DATA_SEC   = re.compile(r'data-section-id=["\']([^"\']+)["\']')

_DELETE_MAX_WORKERS = 8
_DELETE_MAX_RETRY   = 3


def _collect_section_ids(client: WebClient, canvas_id: str) -> list[str]:
    token = os.getenv("SLACK_USER_TOKEN", "")
    try:
        resp = client.files_info(file=canvas_id)
        file_info = resp.get("file", {})
        url = file_info.get("url_private") or file_info.get("url_private_download", "")
        if not url:
            return []
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] url_private 取得失敗: {e}", file=sys.stderr)
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for m in _PAT_TAG_WITH_ID.finditer(html):
        sid = m.group(2)
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)
    for pat in (_PAT_DATA_BLOCK, _PAT_DATA_SEC):
        for m in pat.finditer(html):
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def _delete_one(token: str, canvas_id: str, sid: str) -> None:
    c = WebClient(token=token)
    for _ in range(_DELETE_MAX_RETRY):
        try:
            c.canvases_edit(
                canvas_id=canvas_id,
                changes=[{"operation": "delete", "section_id": sid}],
            )
            return
        except SlackApiError as e:
            if e.response.get("error") == "ratelimited":
                wait = int(e.response.headers.get("Retry-After", 5))
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"rate limit retry exhausted: {sid}")


def _delete_sections_parallel(token: str, canvas_id: str,
                               section_ids: list[str]) -> tuple[int, list[str]]:
    """section_ids を MAX_WORKERS 並列で削除する。(ok件数, 失敗IDリスト) を返す。"""
    total = len(section_ids)
    ok = done = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=_DELETE_MAX_WORKERS) as pool:
        futures = {pool.submit(_delete_one, token, canvas_id, sid): sid for sid in section_ids}
        for future in as_completed(futures):
            sid = futures[future]
            done += 1
            try:
                future.result()
                ok += 1
            except SlackApiError as e:
                print(f"\n[WARN] {sid} 削除失敗: {e.response.get('error')}", file=sys.stderr)
                failed.append(sid)
            except Exception as e:
                print(f"\n[WARN] {sid} 削除失敗: {e}", file=sys.stderr)
                failed.append(sid)
            if done % 10 == 0 or done == total:
                print(f"\r  進捗: {done}/{total} 件", end="", flush=True)
    print()
    return ok, failed


def _delete_sections_sequential(token: str, canvas_id: str,
                                 section_ids: list[str],
                                 delay: float = 1.0) -> tuple[int, list[str]]:
    """失敗セクションを1件ずつ順次リトライする。(ok件数, 依然失敗のIDリスト) を返す。"""
    ok = 0
    still_failed: list[str] = []
    for sid in section_ids:
        time.sleep(delay)
        try:
            _delete_one(token, canvas_id, sid)
            ok += 1
        except SlackApiError as e:
            print(f"[WARN] {sid} 再試行も失敗: {e.response.get('error')}", file=sys.stderr)
            still_failed.append(sid)
        except Exception as e:
            print(f"[WARN] {sid} 再試行も失敗: {e}", file=sys.stderr)
            still_failed.append(sid)
    return ok, still_failed


def post_to_canvas(canvas_id: str, content: str) -> None:
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("ERROR: SLACK_USER_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Canvas投稿コンテンツ: {len(content)} 文字")
    client = WebClient(token=token)
    try:
        section_ids = _collect_section_ids(client, canvas_id)
        if section_ids:
            print(f"[INFO] 既存セクション {len(section_ids)} 件を削除中...")
            ok, failed_ids = _delete_sections_parallel(token, canvas_id, section_ids)
            if failed_ids:
                print(f"[INFO] 失敗 {len(failed_ids)} 件を順次リトライ中...")
                retry_ok, still_failed = _delete_sections_sequential(token, canvas_id, failed_ids)
                ok += retry_ok
                fail = len(still_failed)
            else:
                fail = 0
            print(f"[INFO] 削除完了: {ok}件成功 / {fail}件失敗")
        client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "insert_at_start",
                "document_content": {"type": "markdown", "markdown": content},
            }],
        )
        print(f"✓ Canvas 更新成功: {canvas_id}")
    except SlackApiError as e:
        print(f"Slack API エラー: {e.response['error']}", file=sys.stderr)
        print(f"レスポンス詳細: {e.response}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="pm.db → LLM インサイト生成・Canvas投稿")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--canvas-id", default=None,
                        help="投稿先 Canvas ID（省略時は Canvas 投稿なし）")
    add_since_arg(parser)
    parser.add_argument("--skip-canvas", action="store_true", help="Canvas 投稿をスキップ")
    parser.add_argument("--model", default=None, help="使用する Claude モデル")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    if not args.db:
        print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
        print("  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db", file=sys.stderr)
        sys.exit(1)
    db_path = Path(args.db)
    today   = date.today().isoformat()

    log, close_log = make_logger(args.output)
    log(f"[INFO] pm.db  : {db_path}")
    log(f"[INFO] since  : {args.since or '全期間'}")
    log(f"[INFO] 集計日 : {today}")

    # --- データ収集 ---
    conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    data = {
        "milestones":             fetch_milestone_progress(conn),
        "overdue_items":          fetch_overdue_items(conn, today, args.since),
        "assignee_workload":      fetch_assignee_workload(conn, today),
        "unlinked_count":         fetch_unlinked_items_count(conn, args.since),
        "no_assignee_count":      fetch_no_assignee_count(conn, args.since),
        "weekly_trends":          fetch_weekly_trends(conn),
        "unacknowledged_decisions": fetch_unacknowledged_decisions(conn, args.since),
        "stats":                  fetch_summary_stats(conn, args.since, today),
    }
    conn.close()

    stats = data["stats"]
    log(f"[INFO] open: {stats['total_open']}件 / closed: {stats['total_closed']}件"
        f" / 期限超過: {stats['overdue_count']}件")
    log(f"[INFO] 未紐づけ: {data['unlinked_count']}件 / 担当者なし: {data['no_assignee_count']}件")
    log(f"[INFO] 未確認決定事項: {stats['unacknowledged_decisions']}件")
    log(f"[INFO] マイルストーン: {len(data['milestones'])}件")

    # --- LLM 呼び出し ---
    context = load_context_from_claude_md()
    prompt  = build_analysis_prompt(data, context, today, args.since)
    log("[INFO] LLM にインサイト分析を依頼中...")
    try:
        raw = call_claude(prompt, model=args.model)
    except Exception as e:
        print(f"[ERROR] LLM 呼び出し失敗: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        insight = extract_json(raw)
    except ValueError as e:
        print(f"[ERROR] LLM レスポンスの JSON 解析失敗: {e}", file=sys.stderr)
        print("[RAW]\n" + raw, file=sys.stderr)
        sys.exit(1)

    # --- レポート整形・出力 ---
    report = format_insight_report(insight, data, today, args.since)
    report = sanitize_for_canvas(report)

    log("\n" + "=" * 60)
    log(report)
    log("=" * 60)

    # --- Canvas 投稿 ---
    if args.dry_run or args.skip_canvas or not args.canvas_id:
        if not args.canvas_id and not args.dry_run and not args.skip_canvas:
            log("[INFO] --canvas-id 未指定のため Canvas 投稿をスキップしました")
        else:
            log("[INFO] Canvas 投稿をスキップしました")
        close_log()
        return

    post_to_canvas(args.canvas_id, report)
    close_log()


if __name__ == "__main__":
    main()
