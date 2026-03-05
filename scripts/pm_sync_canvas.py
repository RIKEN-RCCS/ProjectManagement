#!/usr/bin/env python3
"""
pm_sync_canvas.py

Slack Canvas の「未完了アクションアイテム」表から「対応状況」列を収集し、
pm.db を更新する。

会議後のワークフロー:
  1. pm_report.py でアクションアイテム表をCanvas投稿（対応状況列は空）
  2. 会議中にメンバーがCanvas上の「対応状況」列に記入
  3. 会議後に本スクリプトを実行してpm.dbを更新

完了判定キーワード（status='closed' に更新）:
  完了, done, 済, 対応済, 解決, closed, finish, finished

上記以外の記入がある場合: note列に保存（status は 'open' のまま）

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
from pathlib import Path

from slack_bolt import App
from slack_sdk.errors import SlackApiError

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

    戻り値: [{"id": 5, "note": "完了"}, ...]
             対応状況が空のものは除外
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

        id_idx = headers.index("ID")
        note_idx = headers.index("対応状況")

        # データ行を解析
        for row_html in rows[1:]:
            cells_html = re.findall(r"<td>.*?</td>", row_html, re.DOTALL)
            cells = [strip_html_tags(c) for c in cells_html]

            if len(cells) <= max(id_idx, note_idx):
                continue

            raw_id = cells[id_idx].strip()
            # ゼロ幅スペース等の不可視文字を除去して実質的な内容を判定
            raw_note = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", cells[note_idx]).strip()

            if not raw_id.isdigit():
                continue

            ai_id = int(raw_id)
            if ai_id in seen_ids:
                continue  # 重複テーブルによる同一IDのスキップ
            seen_ids.add(ai_id)

            if raw_note:
                results.append({"id": ai_id, "note": raw_note})

    return results


def is_close_keyword(note: str) -> bool:
    return note.lower().strip() in {k.lower() for k in CLOSE_KEYWORDS}


# --------------------------------------------------------------------------- #
# pm.db 更新
# --------------------------------------------------------------------------- #
def open_pm_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # マイグレーション: note列がなければ追加
    cols = [r[1] for r in conn.execute("PRAGMA table_info(action_items)").fetchall()]
    if "note" not in cols:
        conn.execute("ALTER TABLE action_items ADD COLUMN note TEXT")
        conn.commit()
        print("[INFO] action_items に note 列を追加しました")
    return conn


def update_action_item(
    conn: sqlite3.Connection,
    ai_id: int,
    note: str,
    dry_run: bool,
) -> str:
    """
    アクションアイテムを更新する。
    戻り値: 'closed' / 'noted' / 'not_found'
    """
    row = conn.execute(
        "SELECT id, content, status FROM action_items WHERE id = ?", (ai_id,)
    ).fetchone()

    if not row:
        return "not_found"

    if is_close_keyword(note):
        new_status = "closed"
        new_note = note
    else:
        new_status = row["status"]  # status は変更しない
        new_note = note

    if not dry_run:
        conn.execute(
            "UPDATE action_items SET status = ?, note = ? WHERE id = ?",
            (new_status, new_note, ai_id),
        )
        conn.commit()

    return "closed" if new_status == "closed" else "noted"


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canvas の「対応状況」列を読み取り pm.db を更新する"
    )
    parser.add_argument("--canvas-id", default=DEFAULT_CANVAS_ID, help="対象 Canvas ID")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--dry-run", action="store_true", help="DB保存なし・結果を標準出力のみ")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_PM_DB

    print(f"[INFO] Canvas ID : {args.canvas_id}")
    print(f"[INFO] pm.db     : {db_path}")
    if args.dry_run:
        print("[INFO] --dry-run モード（DB更新なし）")

    print("\n[INFO] Canvas を取得中...")
    content = fetch_canvas_content(args.canvas_id)

    if not content:
        print("ERROR: Canvas の内容を取得できませんでした", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 取得完了 ({len(content)} 文字)")

    items = parse_action_items_table(content)
    print(f"[INFO] 対応状況が記入されたアクションアイテム: {len(items)} 件")

    if not items:
        print("更新対象なし。終了します。")
        return

    conn = open_pm_db(db_path)

    closed_count = noted_count = not_found_count = 0

    for item in items:
        ai_id = item["id"]
        note = item["note"]
        result = update_action_item(conn, ai_id, note, args.dry_run)

        if result == "closed":
            print(f"  [完了] ID={ai_id} → status='closed'  note='{note}'")
            closed_count += 1
        elif result == "noted":
            print(f"  [メモ] ID={ai_id} → note='{note}'")
            noted_count += 1
        else:
            print(f"  [未検出] ID={ai_id} は pm.db に存在しません")
            not_found_count += 1

    conn.close()

    print(f"\n完了: 完了マーク={closed_count}件, メモ保存={noted_count}件, 未検出={not_found_count}件")
    if args.dry_run:
        print("[INFO] --dry-run のため DB保存をスキップしました")


if __name__ == "__main__":
    main()
