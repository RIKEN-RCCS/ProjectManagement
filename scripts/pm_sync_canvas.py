#!/usr/bin/env python3
"""
pm_sync_canvas.py

Slack Canvas の「未完了アクションアイテム」表を読み取り pm.db を更新する。

対応状況列だけでなく、担当者・内容・期限の変更もDBに反映する。
会議中に誤記や期限修正が発生した場合に対応できる。

会議後のワークフロー:
  1. pm_report.py でアクションアイテム表をCanvas投稿
  2. 会議中にメンバーがCanvas上の各列を直接編集
  3. 会議後に本スクリプトを実行してpm.dbを更新

完了判定キーワード（status='closed' に更新）:
  完了, done, 済, 対応済, 解決, closed, finish, finished

上記以外の対応状況記入: note列に保存（status は 'open' のまま）
担当者・内容・期限: Canvas値が非空かつDB値と異なる場合のみ上書き

Usage:
    python3 scripts/pm_sync_canvas.py
    python3 scripts/pm_sync_canvas.py --canvas-id F0AAD2494VB
    python3 scripts/pm_sync_canvas.py --dry-run

Options:
    --canvas-id ID      対象 Canvas ID（デフォルト: F0AAD2494VB）
    --db PATH           pm.db のパス（デフォルト: data/pm.db）
    --dry-run           DB保存なし・結果を標準出力のみ
"""

import argparse
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from slack_bolt import App
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, make_logger

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PM_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_CANVAS_ID = "F0AAD2494VB"

CLOSE_KEYWORDS = {"完了", "done", "済", "対応済", "解決", "closed", "finish", "finished"}


# --------------------------------------------------------------------------- #
# Canvas 読み込み
# --------------------------------------------------------------------------- #
def fetch_canvas_content(canvas_id: str) -> str:
    """Slack Canvas の全テキスト内容を取得して返す"""
    token = os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN または SLACK_MCP_XOXB_TOKEN を設定してください",
              file=sys.stderr)
        sys.exit(1)

    app = App(token=token)

    # 1. canvases_sections_lookup（テーブル行を含むセクション）
    try:
        sections_resp = app.client.canvases_sections_lookup(
            canvas_id=canvas_id,
            criteria={"contains_text": "|"},
        )
        sections = sections_resp.get("sections", [])
        content = "\n".join(sec.get("content", "") for sec in sections)
        if content.strip():
            return content
    except SlackApiError as e:
        print(f"[DEBUG] canvases_sections_lookup failed: {e.response['error']}")

    # 2. files.info の url_private をダウンロード
    try:
        resp = app.client.files_info(file=canvas_id)
        file_info = resp.get("file", {})
        url = file_info.get("url_private") or file_info.get("url_private_download", "")
        if url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req) as r:
                raw = r.read().decode("utf-8", errors="replace")
            print(f"[DEBUG] url_private download: {len(raw)} bytes, content-type hint from url: {url[:80]}")
            if raw.strip():
                return raw
    except Exception as e:
        print(f"[DEBUG] url_private download failed: {e}")

    return ""


# --------------------------------------------------------------------------- #
# テーブルパース（HTML形式）
# --------------------------------------------------------------------------- #
def strip_html_tags(text: str) -> str:
    """HTMLタグを除去してテキストのみ返す"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&").replace("&nbsp;", " ")
    return text.strip()


def parse_action_items_table(content: str) -> list[dict]:
    """
    Canvas から取得したHTML内の<table>を解析する。
    ヘッダー行に「ID」と「対応状況」を含むテーブルを対象とする。

    戻り値: [{"id": 5, "assignee": "...", "content": "...", "due_date": "...", "note": "..."}, ...]
             ID が有効な全行を返す（空フィールドは空文字列）
    """
    results = []
    seen_ids: set[int] = set()  # Canvas全体で重複排除

    # <table>...</table> を全て抽出
    tables = re.findall(r"<table>.*?</table>", content, re.DOTALL)

    for table_html in tables:
        # <tr>...</tr> を抽出
        rows = re.findall(r"<tr>.*?</tr>", table_html, re.DOTALL)
        if not rows:
            continue

        # ヘッダー行を解析して列インデックスを特定
        header_cells = re.findall(r"<td>.*?</td>", rows[0], re.DOTALL)
        headers = [strip_html_tags(c) for c in header_cells]

        if "ID" not in headers or "対応状況" not in headers:
            continue

        id_idx        = headers.index("ID")
        note_idx      = headers.index("対応状況")
        assignee_idx  = headers.index("担当者")      if "担当者"      in headers else None
        content_idx   = headers.index("内容")        if "内容"        in headers else None
        due_idx       = headers.index("期限")        if "期限"        in headers else None
        milestone_idx = headers.index("マイルストーン") if "マイルストーン" in headers else None

        required_max = max(
            idx for idx in [id_idx, note_idx, assignee_idx, content_idx, due_idx, milestone_idx]
            if idx is not None
        )

        def get_cell(cells: list[str], idx: int | None) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", cells[idx]).strip()

        # データ行を解析
        for row_html in rows[1:]:
            cells_html = re.findall(r"<td>.*?</td>", row_html, re.DOTALL)
            cells = [strip_html_tags(c) for c in cells_html]

            if len(cells) <= required_max:
                continue

            raw_id = cells[id_idx].strip()
            if not raw_id.isdigit():
                continue

            ai_id = int(raw_id)
            if ai_id in seen_ids:
                continue  # 重複テーブルによる同一IDのスキップ
            seen_ids.add(ai_id)

            results.append({
                "id":           ai_id,
                "assignee":     get_cell(cells, assignee_idx),
                "content":      get_cell(cells, content_idx),
                "due_date":     get_cell(cells, due_idx),
                "milestone_id": get_cell(cells, milestone_idx),
                "note":         get_cell(cells, note_idx),
            })

    return results


def is_close_keyword(note: str) -> bool:
    return note.lower().strip() in {k.lower() for k in CLOSE_KEYWORDS}


# --------------------------------------------------------------------------- #
# pm.db 更新
# --------------------------------------------------------------------------- #
_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,
    source     TEXT
)"""


def open_pm_db(db_path: Path, no_encrypt: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(
        db_path,
        encrypt=not no_encrypt,
        migrations=[
            "ALTER TABLE action_items ADD COLUMN note TEXT",
            _AUDIT_LOG_DDL,
        ],
    )


def write_audit_log(
    conn: sqlite3.Connection,
    record_id: int,
    field: str,
    old_value,
    new_value,
    source: str,
) -> None:
    """変更前の値を audit_log に記録する（dry_run 時は呼ばない）"""
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES ('action_items', ?, ?, ?, ?, ?, ?)",
        (
            str(record_id),
            field,
            str(old_value) if old_value is not None else None,
            str(new_value) if new_value is not None else None,
            datetime.now(timezone.utc).isoformat(),
            source,
        ),
    )


def update_action_item(
    conn: sqlite3.Connection,
    ai_id: int,
    note: str,
    canvas_assignee: str,
    canvas_content: str,
    canvas_due_date: str,
    canvas_milestone_id: str,
    dry_run: bool,
) -> tuple[str, list[str]]:
    """
    アクションアイテムを更新する。
    - note: 対応状況（完了キーワードなら status='closed'）
    - canvas_assignee/content/due_date: Canvas上で変更があれば上書き（空欄は無視）

    戻り値: (result_str, changed_fields)
        result_str   : 'closed' / 'noted' / 'unchanged' / 'not_found'
        changed_fields: 変更されたフィールド名のリスト
    """
    row = conn.execute(
        "SELECT id, content, assignee, due_date, status, milestone_id FROM action_items WHERE id = ?",
        (ai_id,),
    ).fetchone()

    if not row:
        return "not_found", []

    updates: dict[str, object] = {}
    changed_fields: list[str] = []

    # 対応状況（note / status）
    if note:
        new_status = "closed" if is_close_keyword(note) else row["status"]
        updates["status"] = new_status
        updates["note"] = note
        changed_fields.append("対応状況")
    else:
        new_status = row["status"]

    # 担当者・内容・期限・マイルストーン — Canvas値が非空かつDB値と異なる場合のみ更新
    if canvas_assignee and canvas_assignee != (row["assignee"] or ""):
        updates["assignee"] = canvas_assignee
        changed_fields.append("担当者")
    if canvas_content and canvas_content != (row["content"] or ""):
        updates["content"] = canvas_content
        changed_fields.append("内容")
    if canvas_due_date and canvas_due_date != (row["due_date"] or ""):
        updates["due_date"] = canvas_due_date
        changed_fields.append("期限")
    if canvas_milestone_id and canvas_milestone_id != (row["milestone_id"] or ""):
        updates["milestone_id"] = canvas_milestone_id
        changed_fields.append("マイルストーン")

    if not updates:
        return "unchanged", []

    if not dry_run:
        for field, new_val in updates.items():
            try:
                old_val = row[field]
            except (IndexError, KeyError):
                old_val = None
            write_audit_log(conn, ai_id, field, old_val, new_val, "canvas_sync")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [ai_id]
        conn.execute(f"UPDATE action_items SET {set_clause} WHERE id = ?", values)
        conn.commit()

    if new_status == "closed":
        return "closed", changed_fields
    if note:
        return "noted", changed_fields
    return "updated", changed_fields


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canvas の「対応状況」列を読み取り pm.db を更新する"
    )
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="対象 Canvas ID")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    add_dry_run_arg(parser)
    add_no_encrypt_arg(parser)
    add_output_arg(parser)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB
    log, close_log = make_logger(args.output)

    log(f"[INFO] Canvas ID : {args.canvas_id}")
    log(f"[INFO] pm.db     : {db_path}")
    if args.dry_run:
        log("[INFO] --dry-run モード（DB更新なし）")

    log("\n[INFO] Canvas を取得中...")
    content = fetch_canvas_content(args.canvas_id)

    if not content:
        print("ERROR: Canvas の内容を取得できませんでした", file=sys.stderr)
        close_log()
        sys.exit(1)

    log(f"[INFO] 取得完了 ({len(content)} 文字)")

    items = parse_action_items_table(content)
    log(f"[INFO] テーブルから読み込んだアクションアイテム: {len(items)} 件")

    if not items:
        log("更新対象なし。終了します。")
        close_log()
        return

    conn = open_pm_db(db_path, no_encrypt=args.no_encrypt)

    closed_count = noted_count = updated_count = not_found_count = unchanged_count = 0

    for item in items:
        ai_id = item["id"]
        result, changed = update_action_item(
            conn,
            ai_id,
            note=item["note"],
            canvas_assignee=item["assignee"],
            canvas_content=item["content"],
            canvas_due_date=item["due_date"],
            canvas_milestone_id=item.get("milestone_id", ""),
            dry_run=args.dry_run,
        )

        if result == "closed":
            log(f"  [完了] ID={ai_id} → status='closed'  note='{item['note']}'  変更={changed}")
            closed_count += 1
        elif result == "noted":
            log(f"  [メモ] ID={ai_id} → note='{item['note']}'  変更={changed}")
            noted_count += 1
        elif result == "updated":
            log(f"  [更新] ID={ai_id} → 変更={changed}")
            updated_count += 1
        elif result == "unchanged":
            unchanged_count += 1
        else:
            log(f"  [未検出] ID={ai_id} は pm.db に存在しません")
            not_found_count += 1

    conn.close()

    log(
        f"\n完了: 完了マーク={closed_count}件, メモ保存={noted_count}件, "
        f"フィールド更新={updated_count}件, 変更なし={unchanged_count}件, 未検出={not_found_count}件"
    )
    if args.dry_run:
        log("[INFO] --dry-run のため DB保存をスキップしました")
    close_log()


if __name__ == "__main__":
    main()
