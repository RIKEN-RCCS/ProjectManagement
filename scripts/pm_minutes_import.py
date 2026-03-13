#!/usr/bin/env python3
"""
pm_minutes_import.py

会議文字起こしから詳細な議事録DBを生成する。
DBは会議名（--meeting-name）ごとに独立した SQLite ファイルとして
data/minutes/{meeting_name}.db に保存される。

pm_meeting_import.py との違い:
  - pm_meeting_import.py → pm.db に決定事項・アクションアイテムを保存（PM管理用）
  - pm_minutes_import.py → 議事録専用DBに詳細な議事内容・背景を保存（アーカイブ用）

各テーブル:
  instances      : 会議開催記録（開催日・ファイルパス等）
  minutes_content: 議題ごとの詳細議事内容（Markdown）
  decisions      : 決定事項 + 決定に至った背景
  action_items   : アクションアイテム + 発生した背景

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
import json
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
    make_logger, load_claude_md, prepare_transcript,
)


# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
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
    content      TEXT NOT NULL,
    decided_at   TEXT,
    background   TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT NOT NULL REFERENCES instances(meeting_id),
    content      TEXT NOT NULL,
    assignee     TEXT,
    due_date     TEXT,
    background   TEXT
);
"""


def init_minutes_db(db_path: Path, no_encrypt: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(db_path, encrypt=not no_encrypt, schema=MINUTES_SCHEMA)


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
# ファイル名ユーティリティ
# --------------------------------------------------------------------------- #
def infer_date_from_filename(file_path: Path) -> str:
    """GMT20260302-032528_Recording.txt → 2026-03-02"""
    name = file_path.stem
    m = re.search(r"GMT(\d{4})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})[_\-](\d{2})[_\-](\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now().strftime("%Y-%m-%d")


def parse_filename(path: Path) -> tuple[str, str] | None:
    """YYYY-MM-DD_{会議名}.md → (held_at, meeting_name)"""
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


def meeting_id_from_path(file_path: Path) -> str:
    return file_path.stem


def db_path_for_kind(minutes_dir: Path, kind: str) -> Path:
    safe_name = re.sub(r"[^\w\-]", "_", kind)
    return minutes_dir / f"{safe_name}.db"


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #
MINUTES_PROMPT_TEMPLATE = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下に示す「プロジェクト文脈」と「会議の文字起こし」を読み、詳細な議事録を作成してください。

## 重要な指示

1. **文字起こしの誤認識補正**: 音声認識（Whisper）の誤認識が含まれています。
   - 「プロジェクト文脈」に記載された固有名詞・用語・人名を優先して正しく解釈してください
   - 例: 「不学ネクスト」→「富岳NEXT」、「フガクネクスト」→「富岳NEXT」、
         「HBCI」→「HPCI」、「ジェニシス」→「GENESIS」、「エンビディア」→「NVIDIA」等
   - ただし**推測は含めないこと**。文字起こしに書かれた内容のみを根拠にすること

2. **忠実な記録**: 発言に明示されていない内容を補完・推測しないこと

3. **背景の記録**: 決定事項・アクションアイテムについては、それが発生した議論の経緯・背景を
   `background` フィールドに記録すること。後から「なぜこの決定をしたか」が分かるように

4. **議事内容**: 議題ごとの議論の流れを `minutes` フィールドに詳細な Markdown で記録すること

5. **出力形式**: 必ず以下のJSON形式で出力すること（余分なテキスト不要）

## 出力JSON形式

```json
{{
  "minutes": "# 議事内容\\n\\n議題ごとの議論の流れを詳細に記述（Markdown形式）。\\n発言者・発言内容・議論の経緯を含め、後から全体像が把握できるレベルで記録する。",
  "decisions": [
    {{
      "content": "決定事項の内容（明示的に決まったこと、合意に至ったこと）",
      "decided_at": "YYYY-MM-DD または null",
      "background": "この決定に至った議論の経緯・理由・前提条件"
    }}
  ],
  "action_items": [
    {{
      "content": "アクションアイテムの内容",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null",
      "background": "このアクションアイテムが発生した理由・経緯・目的"
    }}
  ]
}}
```

---

## プロジェクト文脈

{claude_md}

---

## 会議の文字起こし（開催日: {held_at}）

{transcript}
"""


def build_prompt(transcript: str, held_at: str, claude_md: str) -> str:
    # CLAUDE.md の関連セクションのみ抽出
    sections = []
    capture = False
    for line in claude_md.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)", line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)

    context = "\n".join(sections) if sections else claude_md[:3000]

    return MINUTES_PROMPT_TEMPLATE.format(
        claude_md=context,
        held_at=held_at,
        transcript=transcript,
    )


# --------------------------------------------------------------------------- #
# JSON 抽出
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output:\n{text[:500]}")


# --------------------------------------------------------------------------- #
# DB 保存
# --------------------------------------------------------------------------- #
def save_to_minutes_db(conn, meeting_id: str, held_at: str, kind: str,
                       file_path: str, extracted: dict, force: bool) -> None:
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

    minutes_text = extracted.get("minutes", "")
    if minutes_text:
        conn.execute(
            "INSERT INTO minutes_content (meeting_id, content) VALUES (?, ?)",
            (meeting_id, minutes_text),
        )

    for d in extracted.get("decisions", []):
        conn.execute(
            "INSERT INTO decisions (meeting_id, content, decided_at, background)"
            " VALUES (?, ?, ?, ?)",
            (meeting_id, d["content"], d.get("decided_at"), d.get("background")),
        )

    for a in extracted.get("action_items", []):
        conn.execute(
            "INSERT INTO action_items (meeting_id, content, assignee, due_date, background)"
            " VALUES (?, ?, ?, ?, ?)",
            (meeting_id, a["content"], a.get("assignee"), a.get("due_date"), a.get("background")),
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
        kind_name = db_file.stem
        print(f"\n{'='*70}")
        print(f"  会議名: {kind_name}")
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
                   COUNT(DISTINCT a.id) AS ai_count,
                   (SELECT COUNT(*) FROM minutes_content mc WHERE mc.meeting_id = i.meeting_id) AS mc_count
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
    """
    単一ファイルを処理する。
    Returns: "ok" | "skipped" | "error"
    """
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

    claude_md = load_claude_md(CLAUDE_MD)
    log(f"[INFO] LLMによる議事録作成を開始... (model: {model or 'default'})")

    prompt = build_prompt(transcript, held_at, claude_md)
    try:
        raw_output = call_claude(prompt, model=model)
    except Exception as e:
        log(f"[ERROR] LLM呼び出し失敗: {e}")
        return "error"

    log("[INFO] LLM出力を解析中...")
    try:
        extracted = extract_json(raw_output)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"[ERROR] JSON解析に失敗: {e}")
        return "error"

    # 結果表示
    log("\n" + "=" * 60)
    log("## 議事内容（冒頭200文字）")
    minutes_preview = (extracted.get("minutes") or "（なし）")[:200]
    log(minutes_preview + ("..." if len(extracted.get("minutes", "")) > 200 else ""))

    log("\n## 決定事項")
    for i, d in enumerate(extracted.get("decisions", []), 1):
        date_str = f" [{d.get('decided_at')}]" if d.get("decided_at") else ""
        bg = f"\n     背景: {d['background']}" if d.get("background") else ""
        log(f"  {i}. {d['content']}{date_str}{bg}")

    log("\n## アクションアイテム")
    for i, a in enumerate(extracted.get("action_items", []), 1):
        assignee = a.get("assignee") or "未定"
        due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
        bg = f"\n     背景: {a['background']}" if a.get("background") else ""
        log(f"  {i}. [{assignee}] {a['content']}{due}{bg}")
    log("=" * 60)

    if dry_run:
        log("\n[INFO] --dry-run のため DB保存をスキップしました")
        return "ok"

    conn = init_minutes_db(db_path, no_encrypt=no_encrypt)
    save_to_minutes_db(conn, meeting_id, held_at, kind, str(input_path), extracted, force)
    conn.close()

    log(f"\n[INFO] 議事録DB に保存完了: {db_path}")
    log(f"  - decisions   : {len(extracted.get('decisions', []))} 件")
    log(f"  - action_items: {len(extracted.get('action_items', []))} 件")
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
