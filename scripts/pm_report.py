#!/usr/bin/env python3
"""
pm_report.py

pm.db から決定事項・アクションアイテム・会議情報を読み込み、
LLMで週次進捗レポートを生成して Slack Canvas に投稿する。

レポート構成:
  サマリー → 直近の決定事項 → 要注意事項 → 未完了アクションアイテム（表形式）

Usage:
    python3 scripts/pm_report.py
    python3 scripts/pm_report.py --since 2026-01-01
    python3 scripts/pm_report.py --dry-run --output report.md

Options:
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --canvas-id ID          投稿先 Canvas ID
    --since YYYY-MM-DD      この日付以降のデータのみ対象
    --skip-canvas           Canvas 投稿をスキップ
    --dry-run               結果を標準出力のみ（Canvas投稿なし）
    --output PATH           標準出力の内容をファイルにも保存
"""

import argparse
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db, open_pm_db, fetch_milestone_progress, fetch_assignee_workload
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg, make_logger
from canvas_utils import sanitize_for_canvas, post_to_canvas

# --------------------------------------------------------------------------- #
# 定数・パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_CANVAS_ID = "F0AAD2494VB"  # 20_1_リーダ会議メンバ Canvas

RISK_KEYWORDS = [
    "問題", "障害", "遅延", "困難", "難しい", "間に合わない",
    "ブロック", "懸念", "リスク", "未解決", "未定", "不明",
    "issue", "blocker", "delay", "risk", "concern",
]


def fetch_open_action_items(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    query = """
        SELECT a.id, a.content, a.assignee, a.due_date, a.status,
               a.note, a.source, a.source_ref, a.extracted_at, a.meeting_id,
               a.milestone_id,
               m.kind as meeting_kind, m.held_at as meeting_held_at
        FROM action_items a
        LEFT JOIN meetings m ON a.meeting_id = m.meeting_id
        WHERE a.status = 'open'
    """
    params: list = []
    if since:
        query += " AND COALESCE(m.held_at, a.extracted_at) >= ?"
        params.append(since)
    query += " ORDER BY a.due_date ASC NULLS LAST, a.extracted_at ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_recent_decisions(
    conn: sqlite3.Connection,
    since: str | None,
    show_acknowledged: bool = False,
) -> list[dict]:
    query = """
        SELECT d.id, d.content, d.decided_at, d.source, d.source_ref,
               d.meeting_id, d.acknowledged_at,
               m.kind as meeting_kind, m.held_at as meeting_held_at
        FROM decisions d
        LEFT JOIN meetings m ON d.meeting_id = m.meeting_id
        WHERE 1=1
    """
    params: list = []
    if not show_acknowledged:
        query += " AND d.acknowledged_at IS NULL"
    if since:
        query += " AND d.decided_at >= ?"
        params.append(since)
    if show_acknowledged:
        # 未確認を先に、確認済みを後に
        query += " ORDER BY (d.acknowledged_at IS NOT NULL), d.decided_at DESC"
    else:
        query += " ORDER BY d.decided_at DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]



def format_assignee_workload(workload: list[dict]) -> str:
    """「担当者別負荷」セクションをリスト形式で生成する（LLM不使用・テーブルなし）"""
    if not workload:
        return "（データなし）"
    lines = []
    for w in workload:
        overdue_str = f"**{w['overdue']}**" if w["overdue"] > 0 else "0"
        lines.append(
            f"- **{w['assignee']}**: {w['total_open']}件"
            f"（期限超過:{overdue_str}件 / 期限未設定:{w['no_due_date']}件）"
        )
    return "\n".join(lines)


def format_milestone_progress(milestones: list[dict], today: str) -> str:
    """「プロジェクトの現在地」セクションをMarkdown形式で生成する（LLM不使用）"""
    if not milestones:
        return "（goals.yaml が未定義です。pm_goals_import.py を実行してください）"

    lines = []
    for m in milestones:
        mid      = m["milestone_id"]
        name     = m["name"]
        due      = m["due_date"] or "未定"
        open_c   = m["open_count"]
        closed_c = m["closed_count"]
        total    = open_c + closed_c

        # 状況判定
        if m["status"] == "achieved":
            status_label = "達成済"
            icon = "OK"
        elif not m["due_date"]:
            status_label = "未着手" if total == 0 else "進行中"
            icon = "-"
        elif m["due_date"] < today:
            status_label = "遅延"
            icon = "!!"
        elif total == 0:
            status_label = "未着手"
            icon = "-"
        else:
            pct = closed_c / total * 100
            if pct >= 80:
                status_label = "進行中"
                icon = "OK"
            elif m["due_date"] <= (date.today().replace(day=1).isoformat()):
                status_label = "要注意"
                icon = "!"
            else:
                status_label = "進行中"
                icon = "-"

        ratio = f"{closed_c}/{total}" if total else "0/0"
        lines.append(f"- [{icon}] **{mid}: {name}**  期限: {due}  状況: {status_label}  完了: {ratio}")

        # 達成条件
        try:
            import json as _json
            criteria = _json.loads(m.get("success_criteria") or "[]")
            for c in criteria:
                lines.append(f"  - {c}")
        except Exception:
            pass

    return "\n".join(lines)


def detect_risk_items(action_items: list[dict]) -> list[dict]:
    """リスクキーワードを含むアクションアイテムを抽出"""
    risk_items = []
    for item in action_items:
        content = item.get("content", "").lower()
        if any(kw.lower() in content for kw in RISK_KEYWORDS):
            risk_items.append(item)
    return risk_items




def _build_permalink_map(rows: list[dict], minutes_dir: Path) -> dict[str, str]:
    """
    meeting_id → slack_file_permalink の辞書を返す。
    source == "meeting" の行を kind ごとにグループ化し、
    data/minutes/{kind}.db の instances テーブルから一括取得する。
    DB が存在しない・開けない場合は該当 kind をスキップする（エラーなし）。
    """
    kind_to_ids: dict[str, list[str]] = {}
    for r in rows:
        if r.get("source") != "meeting":
            continue
        kind = r.get("meeting_kind") or ""
        mid  = r.get("meeting_id") or ""
        if kind and mid:
            kind_to_ids.setdefault(kind, []).append(mid)

    permalink_map: dict[str, str] = {}
    for kind, meeting_ids in kind_to_ids.items():
        safe     = re.sub(r"[^\w\-]", "_", kind)
        db_path  = minutes_dir / f"{safe}.db"
        if not db_path.exists():
            continue
        try:
            conn_m = open_db(db_path)  # 暗号化はデフォルト（pm_report は --no-encrypt なし想定）
            placeholders = ",".join("?" * len(meeting_ids))
            for r in conn_m.execute(
                f"SELECT meeting_id, slack_file_permalink FROM instances"
                f" WHERE meeting_id IN ({placeholders})"
                f"   AND slack_file_permalink IS NOT NULL",
                meeting_ids,
            ).fetchall():
                permalink_map[r["meeting_id"]] = r["slack_file_permalink"]
            conn_m.close()
        except Exception:
            pass  # DB が開けない場合は無視

    return permalink_map


def _format_source(a: dict, permalink_map: dict[str, str] | None = None) -> str:
    """アクションアイテム・決定事項の出典を人が読める形式に変換する"""
    if a.get("source") == "meeting":
        kind  = a.get("meeting_kind") or ""
        held  = a.get("meeting_held_at") or ""
        label = f"{kind} ({held})" if held else kind
        mid   = a.get("meeting_id") or ""
        url   = (permalink_map or {}).get(mid)
        return f"[{label}]({url})" if url else label
    # Slack の場合は source_ref にパーマリンクが入っている
    ref = a.get("source_ref") or ""
    if ref.startswith("http"):
        return f"[Slack]({ref})"
    return ref if ref else "Slack"


def format_action_items(items: list[dict],
                        permalink_map: dict[str, str] | None = None) -> str:
    """Canvas に貼るアクションアイテムリスト（テーブルなし・IDの下に属性を箇条書き）

    形式:
      - [ ] **#1** 内容テキスト全文
        - 担当者:井上 | 期限:2026-03-31 | MS:M1
        - 対応状況:
        - 出典:Leader_Meeting (2026-03-10)

    pm_sync_canvas.py の parse_action_items_list() が全 <li> をフラットに収集して
    #N を持つ <li> を起点にグループ化することでフィールドを解析する。
    """
    if not items:
        return "（なし）"
    lines = []
    for a in items:
        ai_id     = a.get("id", "")
        assignee  = a.get("assignee") or "未定"
        due       = a.get("due_date") or "-"
        milestone = a.get("milestone_id") or "-"
        content   = a.get("content", "").replace("|", "｜").replace("\n", " ").replace("\r", "")
        source    = _format_source(a, permalink_map).replace("|", "｜")
        note      = (a.get("note") or "").replace("|", "｜").replace("\n", " ").replace("\r", "")
        lines.append(f"- [ ] **#{ai_id}** {content}")
        lines.append(f"  - 担当者:{assignee} | 期限:{due} | MS:{milestone}")
        lines.append(f"  - 対応状況:{note}")
        lines.append(f"  - 出典:{source}")
    return "\n".join(lines)


def format_action_items_text(items: list[dict],
                             permalink_map: dict[str, str] | None = None) -> str:
    """LLMプロンプト用テキスト形式（表ではなく箇条書き）"""
    if not items:
        return "（なし）"
    lines = []
    for a in items:
        assignee = a.get("assignee") or "未定"
        due = f" 期限:{a['due_date']}" if a.get("due_date") else ""
        src = _format_source(a, permalink_map)
        source = f" 出典:{src}" if src else ""
        lines.append(f"- [ID:{a.get('id','')}][{assignee}]{due}{source} {a['content']}")
    return "\n".join(lines)


def format_decisions(items: list[dict],
                     permalink_map: dict[str, str] | None = None) -> str:
    if not items:
        return "（なし）"
    lines = []
    for d in items:
        source = _format_source(d, permalink_map)
        source_str = f" （{source}）" if source else ""
        check = "x" if d.get("acknowledged_at") else " "
        lines.append(f"- [{check}] [D:{d['id']}] {d['content']}{source_str}")
    return "\n".join(lines)


def build_report(
    action_items: list[dict],
    decisions: list[dict],
    risk_items: list[dict],
    milestone_progress: list[dict],
    assignee_workload: list[dict],
    today: str,
    permalink_map: dict[str, str] | None = None,
    since: str | None = None,
) -> str:
    since_note = f"（{since} 以降）" if since else "（全期間）"
    sections = [f"# 富岳NEXT プロジェクト進捗レポート（{today}）\n\n集計範囲: {since_note}"]

    if milestone_progress:
        ms_text = format_milestone_progress(milestone_progress, today)
        sections.append(f"## プロジェクトの現在地\n\n{ms_text}")

    risk_text = format_action_items_text(risk_items, permalink_map) if risk_items else "特になし"
    sections.append(f"## 要注意事項\n\n{risk_text}")

    sections.append(f"## 直近の決定事項\n\n{format_decisions(decisions, permalink_map)}")

    sections.append(f"## 未完了アクションアイテム\n\n{format_action_items(action_items, permalink_map)}")

    if assignee_workload:
        wl_text = format_assignee_workload(assignee_workload)
        sections.append(f"## 担当者別負荷\n\n{wl_text}")

    return "\n\n".join(sections)




# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="pm.db → 進捗レポート・アジェンダ生成・Canvas投稿")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="投稿先 Canvas ID")
    add_since_arg(parser)
    parser.add_argument("--skip-canvas", action="store_true", help="Canvas 投稿をスキップ")
    parser.add_argument("--show-acknowledged", action="store_true",
                        help="確認済み決定事項も表示する（デフォルトは非表示）")
    parser.add_argument("--show-workload", action="store_true",
                        help="担当者別負荷セクションを出力する（デフォルトは非表示）")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    if not args.db:
        print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
        print("  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db", file=sys.stderr)
        sys.exit(1)
    db_path = Path(args.db)
    today = date.today().isoformat()

    log, close_log = make_logger(args.output)

    log(f"[INFO] pm.db     : {db_path}")
    log(f"[INFO] since     : {args.since or '全期間'}")
    log(f"[INFO] 生成日    : {today}")

    conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)
    action_items = fetch_open_action_items(conn, args.since)
    decisions = fetch_recent_decisions(conn, args.since, show_acknowledged=args.show_acknowledged)
    risk_items = detect_risk_items(action_items)
    milestone_progress = fetch_milestone_progress(conn)
    assignee_workload = fetch_assignee_workload(conn, today) if args.show_workload else []
    conn.close()

    minutes_dir = db_path.parent / "minutes"
    permalink_map = _build_permalink_map(action_items + decisions, minutes_dir)
    linked = sum(1 for mid in permalink_map)

    log(f"[INFO] アクションアイテム: {len(action_items)}件 (うちリスク: {len(risk_items)}件)")
    log(f"[INFO] 決定事項          : {len(decisions)}件")
    log(f"[INFO] マイルストーン    : {len(milestone_progress)}件")
    if args.show_workload:
        log(f"[INFO] 担当者            : {len(assignee_workload)}名")
    log(f"[INFO] Slackリンク対応   : {linked}件の会議がクリッカブルリンク化")

    report = build_report(action_items, decisions, risk_items, milestone_progress,
                          assignee_workload, today, permalink_map, since=args.since)
    report = sanitize_for_canvas(report)
    log("\n" + "=" * 60)
    log(report)
    log("=" * 60)

    if args.dry_run or args.skip_canvas:
        log("[INFO] Canvas 投稿をスキップしました")
        close_log()
        return

    post_to_canvas(args.canvas_id, report)
    close_log()


if __name__ == "__main__":
    main()
