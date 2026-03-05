#!/usr/bin/env python3
"""
meeting_parser.py

会議文字起こし（Whisper出力Markdown）を解析し、pm.db に保存する。

Usage:
    python3 scripts/meeting_parser.py meetings/GMT20260302-032528_Recording.txt [options]

Options:
    --meeting-name NAME     会議種別名（CLAUDE.md の「会議の種類」参照）
    --held-at DATE          開催日（YYYY-MM-DD）。省略時はファイル名から推定
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --force                 既存レコードを上書き
    --dry-run               DB保存せず結果を標準出力のみ
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db

# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEFAULT_DB = REPO_ROOT / "data" / "pm.db"


# --------------------------------------------------------------------------- #
# DB 初期化
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   TEXT PRIMARY KEY,
    held_at      TEXT,
    kind         TEXT,
    file_path    TEXT,
    summary      TEXT,
    parsed_at    TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    assignee     TEXT,
    due_date     TEXT,
    status       TEXT DEFAULT 'open',
    note         TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    decided_at   TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);
"""


def init_db(db_path: Path, no_encrypt: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(
        db_path,
        encrypt=not no_encrypt,
        schema=SCHEMA,
        migrations=["ALTER TABLE action_items ADD COLUMN note TEXT"],
    )
    return conn


# --------------------------------------------------------------------------- #
# CLAUDE.md 読み込み
# --------------------------------------------------------------------------- #
def load_claude_md() -> str:
    if CLAUDE_MD.exists():
        return CLAUDE_MD.read_text(encoding="utf-8")
    return ""


# --------------------------------------------------------------------------- #
# claude CLI 呼び出し
# --------------------------------------------------------------------------- #
def call_claude(prompt: str, timeout: int = 300) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# ファイル名から開催日を推定
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


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT_TEMPLATE = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下に示す「プロジェクト文脈」と「会議の文字起こし」を読み、指示に従って情報を抽出してください。

## 重要な指示

1. **文字起こしの誤認識補正**: 音声認識（Whisper）の誤認識が含まれています。
   - 「プロジェクト文脈」に記載された固有名詞・用語・人名を優先して正しく解釈してください
   - 例: 「不学ネクスト」→「富岳NEXT」、「フガクネクスト」→「富岳NEXT」、
         「HBCI」→「HPCI」、「ジェニシス」→「GENESIS」、「エンビディア」→「NVIDIA」等
   - ただし**推測は含めないこと**。文字起こしに書かれた内容のみを根拠にすること

2. **忠実な抽出**: 発言に明示されていない内容を補完・推測しないこと

3. **出力形式**: 必ず以下のJSON形式で出力すること（余分なテキスト不要）

## 出力JSON形式

```json
{{
  "summary": "会議全体の要点を3〜7行で記述（誰が何を話し合ったか）",
  "decisions": [
    {{
      "content": "決定事項の内容（明示的に決まったこと、合意に至ったこと）",
      "decided_at": "YYYY-MM-DD または null"
    }}
  ],
  "action_items": [
    {{
      "content": "アクションアイテムの内容",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null"
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
    # CLAUDE.mdは長いので「プロジェクト固有の用語」「ステークホルダー」「主なプロジェクト参加者」セクションのみ抽出
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

    return EXTRACT_PROMPT_TEMPLATE.format(
        claude_md=context,
        held_at=held_at,
        transcript=transcript,
    )


# --------------------------------------------------------------------------- #
# JSON 抽出
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict:
    """LLM出力からJSONブロックを抽出してパース"""
    # ```json ... ``` ブロックを優先
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    # { ... } 全体を探す
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output:\n{text[:500]}")


# --------------------------------------------------------------------------- #
# DB 保存
# --------------------------------------------------------------------------- #
def save_to_db(
    conn: sqlite3.Connection,
    meeting_id: str,
    held_at: str,
    kind: str,
    file_path: str,
    extracted: dict,
    force: bool,
) -> None:
    now = datetime.now().isoformat()

    # meetings
    if force:
        conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))

    conn.execute(
        """
        INSERT OR IGNORE INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (meeting_id, held_at, kind, file_path, extracted.get("summary", ""), now),
    )

    # decisions
    for d in extracted.get("decisions", []):
        conn.execute(
            """
            INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref, extracted_at)
            VALUES (?, ?, ?, 'meeting', ?, ?)
            """,
            (meeting_id, d["content"], d.get("decided_at"), file_path, now),
        )

    # action_items
    for a in extracted.get("action_items", []):
        conn.execute(
            """
            INSERT INTO action_items (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at)
            VALUES (?, ?, ?, ?, 'open', 'meeting', ?, ?)
            """,
            (meeting_id, a["content"], a.get("assignee"), a.get("due_date"), file_path, now),
        )

    conn.commit()


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="会議文字起こし → pm.db への解析・保存")
    parser.add_argument("input_file", help="文字起こしファイル（.txt / .md）")
    parser.add_argument("--meeting-name", default=None, help="会議種別名")
    parser.add_argument("--held-at", default=None, help="開催日 YYYY-MM-DD")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    parser.add_argument("--dry-run", action="store_true", help="DB保存なし・結果を標準出力のみ")
    parser.add_argument("--output", default=None, help="標準出力の内容を保存するファイルパス")
    parser.add_argument("--no-encrypt", action="store_true", help="DBを暗号化しない（平文モード）")
    args = parser.parse_args()

    input_path = Path(args.input_file).resolve()
    if not input_path.exists():
        print(f"ERROR: ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db) if args.db else DEFAULT_DB
    held_at = args.held_at or infer_date_from_filename(input_path)
    meeting_id = input_path.stem  # ファイル名（拡張子なし）をIDとして使用
    kind = args.meeting_name or "不明"

    output_file = open(args.output, "w", encoding="utf-8") if args.output else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    log(f"[INFO] 入力ファイル : {input_path}")
    log(f"[INFO] 開催日       : {held_at}")
    log(f"[INFO] 会議種別     : {kind}")
    log(f"[INFO] meeting_id   : {meeting_id}")

    transcript = input_path.read_text(encoding="utf-8")
    claude_md = load_claude_md()

    log("[INFO] CLAUDE.mdを読み込みました")
    log("[INFO] LLMによる抽出を開始...")

    prompt = build_prompt(transcript, held_at, claude_md)
    raw_output = call_claude(prompt)

    log("[INFO] LLM出力を解析中...")
    try:
        extracted = extract_json(raw_output)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: JSON解析に失敗しました: {e}", file=sys.stderr)
        print("--- LLM raw output ---", file=sys.stderr)
        print(raw_output, file=sys.stderr)
        if output_file:
            output_file.close()
        sys.exit(1)

    # 結果表示
    log("\n" + "=" * 60)
    log("## 会議要旨")
    log(extracted.get("summary", "(なし)"))

    log("\n## 決定事項")
    for i, d in enumerate(extracted.get("decisions", []), 1):
        date_str = f" [{d.get('decided_at')}]" if d.get("decided_at") else ""
        log(f"  {i}. {d['content']}{date_str}")

    log("\n## アクションアイテム")
    for i, a in enumerate(extracted.get("action_items", []), 1):
        assignee = a.get("assignee") or "未定"
        due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
        log(f"  {i}. [{assignee}] {a['content']}{due}")
    log("=" * 60)

    if args.dry_run:
        log("\n[INFO] --dry-run のため DB保存をスキップしました")
        if output_file:
            output_file.close()
        return

    conn = init_db(db_path, no_encrypt=args.no_encrypt)

    existing = conn.execute(
        "SELECT meeting_id FROM meetings WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    if existing and not args.force:
        log(f"\n[WARN] meeting_id '{meeting_id}' は既にDBに存在します。上書きする場合は --force を指定してください。")
        conn.close()
        if output_file:
            output_file.close()
        return

    save_to_db(conn, meeting_id, held_at, kind, str(input_path), extracted, args.force)
    conn.close()

    log(f"\n[INFO] pm.db に保存完了: {db_path}")
    log(f"  - decisions   : {len(extracted.get('decisions', []))} 件")
    log(f"  - action_items: {len(extracted.get('action_items', []))} 件")

    if output_file:
        output_file.close()


if __name__ == "__main__":
    main()
