#!/usr/bin/env python3
"""
pm_minutes_import.py

会議文字起こしから詳細な議事録DBを生成する。
DBは会議名（--meeting-name）ごとに独立した SQLite ファイルとして
data/minutes/{meeting_name}.db に保存される。

pm_meeting_import.py との違い:
  - pm_meeting_import.py → pm.db に決定事項・アクションアイテムを保存（PM管理用）
  - pm_minutes_import.py → 議事録専用DBに詳細な議事内容を保存（アーカイブ用）

各テーブル:
  instances      : 会議開催記録（開催日・ファイルパス等）
  minutes_content: 議事内容（Markdown）
  decisions      : 決定事項
  action_items   : アクションアイテム

Usage:
    # 単一ファイル
    python3 scripts/pm_minutes_import.py meetings/2026-03-10_Leader_Meeting.md \\
        --meeting-name Leader_Meeting --held-at 2026-03-10

    # 一括処理（meetings/ ディレクトリ内を全て処理）
    python3 scripts/pm_minutes_import.py --bulk [--meetings-dir DIR] [--since DATE]

    # 指定会議の格納内容を一覧表示
    python3 scripts/pm_minutes_import.py --list --meeting-name Leader_Meeting

    # 全会議名の概要一覧
    python3 scripts/pm_minutes_import.py --list

Options:
    input_file              文字起こしファイル（.txt / .md）（単一ファイルモード）
    --meeting-name NAME     会議種別名（DBファイル名に使用。省略時はファイル名から推定）
    --held-at DATE          開催日（YYYY-MM-DD）。省略時はファイル名から推定
    --bulk                  一括処理モード（meetings/ ディレクトリ内を全て処理）
    --meetings-dir DIR      一括処理時の議事録ディレクトリ（デフォルト: meetings/）
    --minutes-dir DIR       議事録DBの保存ディレクトリ（デフォルト: data/minutes/）
    --since YYYY-MM-DD      一括処理・--list 時に対象を絞る
    --model MODEL           使用する Claude モデル。省略時は CLI デフォルト
    --force                 既存レコードを上書き
    --dry-run               DB保存なし・結果を標準出力のみ
    --output PATH           出力をファイルにも保存（単一ファイルモードのみ）
    --no-encrypt            DBを暗号化しない（平文モード）
    --list                  議事録DBの内容を表示して終了
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import (
    add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg,
    make_logger, prepare_transcript,
)


# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEETINGS_DIR = REPO_ROOT / "meetings"
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"


# --------------------------------------------------------------------------- #
# DB スキーマ
# --------------------------------------------------------------------------- #
MINUTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    meeting_id   TEXT PRIMARY KEY,
    held_at      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    file_path    TEXT,
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes_content (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT NOT NULL REFERENCES instances(meeting_id),
    content      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT NOT NULL REFERENCES instances(meeting_id),
    content      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT NOT NULL REFERENCES instances(meeting_id),
    content      TEXT NOT NULL
);
"""


def init_minutes_db(db_path: Path, no_encrypt: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(db_path, encrypt=not no_encrypt, schema=MINUTES_SCHEMA)


# --------------------------------------------------------------------------- #
# プロンプト
# --------------------------------------------------------------------------- #
PROMPT_TEMPLATE = """\
以下の会議の文字起こしテキストから、構造化された議事録を作成してください。

## 議事録作成ルール

- 文字起こしテキストの内容に忠実に従い、推測を含めない
- Whisperの書き起こし誤認識による不自然な表現は自然な日本語に修正してよいが、事実は変えない
- プロジェクト固有の用語はCLAUDE.mdの用語集を参照して正しく表記する
- 必ず以下のフォーマットのみで出力すること。フォーマット外の説明・コメントは不要

## 出力フォーマット

# 議事録

## 決定事項

- （会議で確定した事項を箇条書きで記載。なければ「（なし）」）

## アクションアイテム

- （担当者・内容を明記したタスクを箇条書きで記載。なければ「（なし）」）

## 議事内容

（議論の流れを要旨としてまとめて記載）

---

## 文字起こしテキスト

{transcript}
"""


# --------------------------------------------------------------------------- #
# claude CLI 呼び出し
# --------------------------------------------------------------------------- #
def call_claude(prompt: str, timeout: int = 300, model: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Markdown パース
# --------------------------------------------------------------------------- #
def _extract_section(text: str, heading: str) -> str:
    """## heading 以降の次の ## までのテキストを返す"""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _parse_bullets(section_text: str) -> list[str]:
    """箇条書き行（- または * で始まる）を抽出してリストで返す"""
    items = []
    for line in section_text.splitlines():
        line = line.strip()
        if re.match(r"^[-*]\s+", line):
            content = re.sub(r"^[-*]\s+", "", line).strip()
            if content and content not in ("（なし）", "(なし)"):
                items.append(content)
    return items


def parse_minutes_output(text: str) -> dict:
    """LLM が出力した Markdown 議事録をパースして辞書で返す"""
    decisions_text   = _extract_section(text, "決定事項")
    action_items_text = _extract_section(text, "アクションアイテム")
    minutes_text     = _extract_section(text, "議事内容")

    return {
        "minutes":      minutes_text,
        "decisions":    _parse_bullets(decisions_text),
        "action_items": _parse_bullets(action_items_text),
    }


# --------------------------------------------------------------------------- #
# ファイル名ユーティリティ
# --------------------------------------------------------------------------- #
def infer_date_from_filename(file_path: Path) -> str:
    name = file_path.stem
    m = re.search(r"GMT(\d{4})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})[_\-](\d{2})[_\-](\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now().strftime("%Y-%m-%d")


def parse_filename(path: Path) -> tuple[str, str] | None:
    name = path.stem
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", name)
    if not m:
        return None
    return m.group(1), m.group(2)


def collect_files(meetings_dir: Path, since: str | None) -> list[Path]:
    files = []
    for p in sorted(meetings_dir.glob("*.md")):
        if p.name.endswith("_parsed.md"):
            continue
        parsed = parse_filename(p)
        if parsed is None:
            print(f"[SKIP] ファイル名の形式が不正: {p.name}")
            continue
        held_at, _ = parsed
        if since and held_at < since:
            continue
        files.append(p)
    return files


def db_path_for_kind(minutes_dir: Path, kind: str) -> Path:
    safe_name = re.sub(r"[^\w\-]", "_", kind)
    return minutes_dir / f"{safe_name}.db"


# --------------------------------------------------------------------------- #
# DB 保存
# --------------------------------------------------------------------------- #
def save_to_minutes_db(conn, meeting_id: str, held_at: str, kind: str,
                       file_path: str, parsed: dict, force: bool) -> None:
    now = datetime.now().isoformat()

    if force:
        conn.execute("DELETE FROM minutes_content WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM instances WHERE meeting_id = ?", (meeting_id,))

    conn.execute(
        "INSERT OR IGNORE INTO instances (meeting_id, held_at, kind, file_path, imported_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (meeting_id, held_at, kind, file_path, now),
    )

    if parsed["minutes"]:
        conn.execute(
            "INSERT INTO minutes_content (meeting_id, content) VALUES (?, ?)",
            (meeting_id, parsed["minutes"]),
        )

    for content in parsed["decisions"]:
        conn.execute(
            "INSERT INTO decisions (meeting_id, content) VALUES (?, ?)",
            (meeting_id, content),
        )

    for content in parsed["action_items"]:
        conn.execute(
            "INSERT INTO action_items (meeting_id, content) VALUES (?, ?)",
            (meeting_id, content),
        )

    conn.commit()


# --------------------------------------------------------------------------- #
# 一覧表示
# --------------------------------------------------------------------------- #
def list_minutes(minutes_dir: Path, kind_filter: str | None,
                 since: str | None, no_encrypt: bool) -> None:
    if not minutes_dir.exists():
        print(f"[INFO] 議事録DBディレクトリが存在しません: {minutes_dir}")
        return

    db_files = sorted(minutes_dir.glob("*.db"))
    if not db_files:
        print("[INFO] 議事録DBが見つかりません")
        return

    if kind_filter:
        safe = re.sub(r"[^\w\-]", "_", kind_filter)
        db_files = [f for f in db_files if f.stem == safe]
        if not db_files:
            print(f"[INFO] '{kind_filter}' の議事録DBが見つかりません")
            return

    for db_file in db_files:
        print(f"\n{'='*70}")
        print(f"  会議名: {db_file.stem}")
        print(f"  DB    : {db_file}")
        print(f"{'='*70}")

        try:
            conn = open_db(db_file, encrypt=not no_encrypt)
        except Exception as e:
            print(f"  [ERROR] DB接続失敗: {e}")
            continue

        query = """
            SELECT i.meeting_id, i.held_at, i.imported_at,
                   COUNT(DISTINCT d.id) AS d_count,
                   COUNT(DISTINCT a.id) AS ai_count
            FROM instances i
            LEFT JOIN decisions d ON d.meeting_id = i.meeting_id
            LEFT JOIN action_items a ON a.meeting_id = i.meeting_id
        """
        params: list = []
        if since:
            query += " WHERE i.held_at >= ?"
            params.append(since)
        query += " GROUP BY i.meeting_id ORDER BY i.held_at DESC"

        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            print("  （該当するレコードなし）")
            continue

        print(f"  {'開催日':<12} {'決定':>4} {'AI':>4} {'登録日時':<20}  meeting_id")
        print(f"  {'-'*65}")
        for r in rows:
            print(f"  {r['held_at']:<12} {r['d_count']:>4} {r['ai_count']:>4}"
                  f"  {(r['imported_at'] or '')[:19]:<20}  {r['meeting_id']}")
        print(f"\n  合計: {len(rows)} 件")


# --------------------------------------------------------------------------- #
# 単一ファイル処理（コア）
# --------------------------------------------------------------------------- #
def process_file(
    input_path: Path,
    held_at: str,
    kind: str,
    minutes_dir: Path,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
    model: str | None = None,
    log=print,
) -> str:
    """Returns: "ok" | "skipped" | "error" """
    meeting_id = input_path.stem
    db_path = db_path_for_kind(minutes_dir, kind)

    log(f"[INFO] 入力ファイル : {input_path}")
    log(f"[INFO] 開催日       : {held_at}")
    log(f"[INFO] 会議種別     : {kind}")
    log(f"[INFO] meeting_id   : {meeting_id}")
    log(f"[INFO] 議事録DB     : {db_path}")

    raw_transcript = input_path.read_text(encoding="utf-8")
    transcript, is_whisper = prepare_transcript(raw_transcript)
    log(f"[INFO] 文字起こし形式: {'Whisper (話者・タイムスタンプ付き)' if is_whisper else '平文テキスト'}")

    # インポート済みチェック（LLM呼び出し前）
    if not dry_run:
        conn_check = init_minutes_db(db_path, no_encrypt=no_encrypt)
        existing = conn_check.execute(
            "SELECT meeting_id FROM instances WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
        conn_check.close()
        if existing and not force:
            log(f"[SKIP] meeting_id '{meeting_id}' は既にDBに存在します。--force で上書き可能")
            return "skipped"

    log(f"[INFO] LLMによる議事録作成を開始... (model: {model or 'default'})")

    prompt = PROMPT_TEMPLATE.format(transcript=transcript)
    try:
        minutes_text = call_claude(prompt, model=model)
    except Exception as e:
        log(f"[ERROR] LLM呼び出し失敗: {e}")
        return "error"

    parsed = parse_minutes_output(minutes_text)

    # 結果表示
    log("\n" + "=" * 60)
    log(minutes_text)
    log("=" * 60)

    if dry_run:
        log("\n[INFO] --dry-run のため DB保存をスキップしました")
        return "ok"

    conn = init_minutes_db(db_path, no_encrypt=no_encrypt)
    save_to_minutes_db(conn, meeting_id, held_at, kind, str(input_path), parsed, force)
    conn.close()

    log(f"\n[INFO] 議事録DB に保存完了: {db_path}")
    log(f"  - decisions   : {len(parsed['decisions'])} 件")
    log(f"  - action_items: {len(parsed['action_items'])} 件")
    return "ok"


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="会議文字起こし → 議事録DB（data/minutes/{meeting_name}.db）への詳細保存",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 単一ファイル
  python3 scripts/pm_minutes_import.py meetings/2026-03-10_Leader_Meeting.md \\
      --meeting-name Leader_Meeting --held-at 2026-03-10

  # 一括処理
  python3 scripts/pm_minutes_import.py --bulk
  python3 scripts/pm_minutes_import.py --bulk --since 2026-01-01 --force

  # 一覧表示
  python3 scripts/pm_minutes_import.py --list
  python3 scripts/pm_minutes_import.py --list --meeting-name Leader_Meeting
""",
    )
    parser.add_argument("input_file", nargs="?",
                        help="文字起こしファイル（.txt / .md）（単一ファイルモード）")
    parser.add_argument("--meeting-name", default=None,
                        help="会議種別名（DBファイル名に使用。省略時はファイル名から推定）")
    parser.add_argument("--held-at", default=None,
                        help="開催日 YYYY-MM-DD（省略時はファイル名から推定）")
    parser.add_argument("--bulk", action="store_true",
                        help="一括処理モード（meetings/ ディレクトリ内を全て処理）")
    parser.add_argument("--meetings-dir", default=None,
                        help="一括処理時の議事録ディレクトリ（デフォルト: meetings/）")
    parser.add_argument("--minutes-dir", default=None,
                        help="議事録DBの保存ディレクトリ（デフォルト: data/minutes/）")
    add_since_arg(parser, "（--bulk / --list 時）")
    parser.add_argument("--model", default=None, metavar="MODEL",
                        help="使用する Claude モデル（例: claude-haiku-4-5-20251001）。省略時は CLI デフォルト")
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true",
                        help="議事録DBの内容を表示して終了")
    args = parser.parse_args()

    minutes_dir = Path(args.minutes_dir) if args.minutes_dir else DEFAULT_MINUTES_DIR

    # --- list ---
    if args.list:
        list_minutes(minutes_dir, args.meeting_name, args.since, args.no_encrypt)
        return

    # --- bulk ---
    if args.bulk:
        meetings_dir = Path(args.meetings_dir) if args.meetings_dir else DEFAULT_MEETINGS_DIR
        if not meetings_dir.exists():
            print(f"ERROR: ディレクトリが見つかりません: {meetings_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"[INFO] 議事録ディレクトリ: {meetings_dir}")
        print(f"[INFO] 議事録DB保存先    : {minutes_dir}")
        if args.since:
            print(f"[INFO] since            : {args.since}")
        if args.dry_run:
            print("[INFO] --dry-run モード（DB保存なし）")

        files = collect_files(meetings_dir, args.since)
        print(f"[INFO] 対象ファイル     : {len(files)} 件\n")

        if not files:
            print("対象ファイルなし。終了します。")
            return

        ok = skipped = failed = 0
        for i, file_path in enumerate(files, 1):
            parsed = parse_filename(file_path)
            if parsed is None:
                continue
            held_at, meeting_name = parsed
            kind = args.meeting_name or meeting_name
            print(f"[{i}/{len(files)}] {file_path.name}")
            status = process_file(
                file_path, held_at, kind, minutes_dir,
                force=args.force, dry_run=args.dry_run,
                no_encrypt=args.no_encrypt, model=args.model,
            )
            if status == "ok":
                ok += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
            print()

        print(f"完了: 処理={ok}件, スキップ={skipped}件, 失敗={failed}件")
        return

    # --- single file ---
    if not args.input_file:
        parser.print_help()
        sys.exit(1)

    input_path = Path(args.input_file).resolve()
    if not input_path.exists():
        print(f"ERROR: ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    held_at = args.held_at or infer_date_from_filename(input_path)
    kind = args.meeting_name or (parse_filename(input_path) or (None, "不明"))[1]

    log, close_log = make_logger(args.output)

    status = process_file(
        input_path, held_at, kind, minutes_dir,
        force=args.force, dry_run=args.dry_run,
        no_encrypt=args.no_encrypt, model=args.model,
        log=log,
    )

    close_log()

    if status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
