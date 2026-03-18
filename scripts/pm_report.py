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
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

from slack_bolt import App
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg, make_logger

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


# --------------------------------------------------------------------------- #
# pm.db 読み込み
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



def _normalize_assignee(name: str | None) -> str:
    """日本語を含む担当者名の姓名間スペース（半角・全角）を除去する"""
    if not name:
        return "未定"
    if re.search(r"[\u3040-\u9fff]", name):
        name = name.replace(" ", "").replace("\u3000", "")
    return name


def fetch_assignee_workload(conn: sqlite3.Connection, today: str) -> list[dict]:
    """担当者別の負荷（オープンアイテム数・期限超過数・期限未設定数）を取得する（LLM不使用）
    既存データの表記ゆれ（姓名間スペース）もPython側で正規化して集計する。"""
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


def format_assignee_workload(workload: list[dict]) -> str:
    """「担当者別負荷」セクションをMarkdown表形式で生成する（LLM不使用）"""
    if not workload:
        return "（データなし）"
    header = "| 担当者 | 合計 | 期限超過 | 期限未設定 |"
    sep    = "|--------|------|----------|------------|"
    rows = [header, sep]
    for w in workload:
        overdue_str = f"**{w['overdue']}**" if w["overdue"] > 0 else "0"
        rows.append(
            f"| {w['assignee']} | {w['total_open']} | {overdue_str} | {w['no_due_date']} |"
        )
    return "\n".join(rows)


def fetch_milestone_progress(conn: sqlite3.Connection) -> list[dict]:
    """マイルストーンごとのアクションアイテム完了率を取得する"""
    try:
        rows = conn.execute(
            """
            SELECT m.milestone_id, m.goal_id, m.name, m.due_date, m.area,
                   m.status, m.success_criteria,
                   COUNT(DISTINCT CASE WHEN a.status='open'   THEN a.id END) AS open_count,
                   COUNT(DISTINCT CASE WHEN a.status='closed' THEN a.id END) AS closed_count
            FROM milestones m
            LEFT JOIN action_items a ON a.milestone_id = m.milestone_id
            WHERE m.status = 'active'
            GROUP BY m.milestone_id
            ORDER BY m.due_date ASC NULLS LAST
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


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
    """Canvas に貼るアクションアイテム表（pm_relink.py --export と列・順序を統一）"""
    if not items:
        return "（なし）"
    header = "| ID | 担当者 | 期限 | マイルストーン | 状況 | 内容 | 対応状況 | 出典 |"
    sep    = "|----|--------|------|----------------|------|------|----------|------|"
    rows = [header, sep]
    for a in items:
        ai_id     = a.get("id", "")
        assignee  = a.get("assignee") or "未定"
        due       = a.get("due_date") or ""
        milestone = a.get("milestone_id") or ""
        status    = a.get("status") or ""
        content   = a.get("content", "").replace("|", "｜").replace("\n", " ").replace("\r", "")
        source    = _format_source(a, permalink_map)
        note      = (a.get("note") or "").replace("\n", " ").replace("\r", "")
        rows.append(f"| {ai_id} | {assignee} | {due} | {milestone} | {status} | {content} | {note} | {source} |")
    return "\n".join(rows)


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
# Slack Canvas 投稿
# --------------------------------------------------------------------------- #
def sanitize_for_canvas(text: str) -> str:
    # 記号・特殊文字を標準的な文字に置換
    replacements = {
        # ダッシュ・ハイフン類
        "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u2212": "-", "\u2011": "-", "\u2010": "-",
        # 波ダッシュ・チルダ類
        "\uff5e": "-", "\u301c": "-",
        # 全角括弧
        "\uff08": "(", "\uff09": ")",
        # 全角記号
        "\uff0c": ",", "\uff0e": ".", "\uff01": "!",
        "\uff1a": ":", "\uff1b": ";", "\uff1f": "?",
        # 引用符類
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
        # 矢印類
        "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
        "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>",
        "\u25b6": ">", "\u25c0": "<",
        # 点・中黒
        "\u30fb": ".", "\u2022": "-", "\u2023": "-",
        "\u25cf": "-", "\u25cb": "-", "\u2027": ".",
        # スペース類
        "\u3000": " ", "\u00a0": " ",
        # その他よく出る記号
        "\u2026": "...", "\u22ef": "...",
        "\u00d7": "x", "\u00f7": "/",
        "\u2605": "*", "\u2606": "*",
        "\u2713": "OK", "\u2714": "OK", "\u2715": "NG", "\u2716": "NG",
        "\u25a0": "-", "\u25a1": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # h4以下の見出しはh3に統一（Canvasで未サポート）
    text = re.sub(r"^#{4,6}\s+", "### ", text, flags=re.MULTILINE)
    # インデントされた番号リストをリストに変換
    text = re.sub(r"^(\s+)\d+\.\s+", r"\1- ", text, flags=re.MULTILINE)
    # ブロッククオート内のリスト項目からブロッククオートを除去
    # (Slack Canvas は blockquote 内の List ブロックをサポートしない)
    text = re.sub(r"^> (-|\*|\d+\.)\s+", r"\1 ", text, flags=re.MULTILINE)

    # 上記で対処できなかった非ASCII・非日本語の特殊記号を除去
    # 日本語(CJK)・英数字・基本記号・改行・スペースは保持
    def keep_char(c: str) -> str:
        cp = ord(c)
        # ASCII printable
        if 0x20 <= cp <= 0x7E:
            return c
        # 改行・タブ
        if c in ("\n", "\t"):
            return c
        # 日本語: ひらがな・カタカナ・漢字・半角カタカナ・記号
        if 0x3000 <= cp <= 0x9FFF:
            return c
        if 0xF900 <= cp <= 0xFAFF:
            return c
        if 0xFF00 <= cp <= 0xFFEF:
            return c
        # latin拡張（アクセント付き文字など）
        if 0x00C0 <= cp <= 0x024F:
            return c
        # それ以外の特殊記号は除去
        return ""

    text = "".join(keep_char(c) for c in text)

    # 連続する空行を1行に圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def post_to_canvas(canvas_id: str, content: str) -> None:
    token = os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("ERROR: SLACK_MCP_XOXB_TOKEN を設定してください",
              file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Canvas投稿コンテンツ: {len(content)} 文字")
    app = App(token=token)

    try:
        app.client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "replace",
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
    parser = argparse.ArgumentParser(description="pm.db → 進捗レポート・アジェンダ生成・Canvas投稿")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="投稿先 Canvas ID")
    add_since_arg(parser)
    parser.add_argument("--skip-canvas", action="store_true", help="Canvas 投稿をスキップ")
    parser.add_argument("--show-acknowledged", action="store_true",
                        help="確認済み決定事項も表示する（デフォルトは非表示）")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
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
    assignee_workload = fetch_assignee_workload(conn, today)
    conn.close()

    minutes_dir = db_path.parent / "minutes"
    permalink_map = _build_permalink_map(action_items + decisions, minutes_dir)
    linked = sum(1 for mid in permalink_map)

    log(f"[INFO] アクションアイテム: {len(action_items)}件 (うちリスク: {len(risk_items)}件)")
    log(f"[INFO] 決定事項          : {len(decisions)}件")
    log(f"[INFO] マイルストーン    : {len(milestone_progress)}件")
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
