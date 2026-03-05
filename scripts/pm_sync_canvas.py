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

    # Canvas はファイルとして files.info で取得できる
    try:
        resp = app.client.files_info(file=canvas_id)
    except SlackApiError as e:
        print(f"ERROR: Canvas 取得失敗: {e.response['error']}", file=sys.stderr)
        sys.exit(1)

    file_info = resp.get("file", {})

    # Canvas の本文は "plain_text" または "preview" フィールドに入る場合がある
    # まず canvases_sections_lookup で全セクションを取得する
    try:
        sections_resp = app.client.canvases_sections_lookup(
            canvas_id=canvas_id,
            criteria={"contains_text": "|"},  # テーブル行は | を含む
        )
        sections = sections_resp.get("sections", [])
        lines = []
        for sec in sections:
            lines.append(sec.get("content", ""))
        return "\n".join(lines)
    except SlackApiError:
        pass

    # fallback: files.info の plain_text
    plain = file_info.get("plain_text") or file_info.get("preview", "")
    return plain


# --------------------------------------------------------------------------- #
# テーブルパース
# --------------------------------------------------------------------------- #
def parse_action_items_table(content: str) -> list[dict]:
    """
    Canvas 内の以下形式のMarkdownテーブルを解析する:
    | ID | 担当者 | 内容 | 期限 | ソース | 対応状況 |
    |----|--------|------|------|--------|----------|
    | 5  | 井上 晃 | ... | ...  | ...    | 完了     |

    戻り値: [{"id": 5, "note": "完了"}, ...]
             対応状況が空のものは除外
    """
    results = []
    in_table = False
    header_found = False

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                in_table = False
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]

        # ヘッダー行を検出
        if not header_found:
            if "ID" in cells and "対応状況" in cells:
                header_found = True
                in_table = True
                # 列インデックスを取得
                id_idx = cells.index("ID")
                note_idx = cells.index("対応状況")
            continue

        # セパレーター行をスキップ
        if all(re.fullmatch(r"[-:]+", c) for c in cells if c):
            continue

        if in_table and len(cells) > max(id_idx, note_idx):
            raw_id = cells[id_idx].strip()
            raw_note = cells[note_idx].strip()

            # IDが数値でなければデータ行ではない
            if not raw_id.isdigit():
                continue

            if raw_note:  # 対応状況が記入されているものだけ
                results.append({"id": int(raw_id), "note": raw_note})

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
