#!/usr/bin/env python3
"""
pm_meeting_import.py

会議文字起こし（Whisper出力Markdown）を解析し、pm.db および議事録DB に保存する。
単一ファイルモードと一括処理モード（--bulk）に対応。

【議事録DB】
会議名ごとに独立した SQLite DB を data/minutes/{会議名}.db に作成する。
pm.db（アクションアイテム・決定事項の統合管理）とは別に、詳細な議事内容・
決定経緯・アクションアイテム発生経緯を保存する。

Usage:
    # 単一ファイル
    python3 scripts/pm_meeting_import.py meetings/2026-03-10_Leader_Meeting.md \\
        [--meeting-name NAME] [--held-at DATE]

    # 一括処理（meetings/ ディレクトリ内のファイルを全て処理）
    python3 scripts/pm_meeting_import.py --bulk [--meetings-dir DIR] [--since DATE]

    # インポート済み一覧表示
    python3 scripts/pm_meeting_import.py --list [--since YYYY-MM-DD]

    # 議事録削除
    python3 scripts/pm_meeting_import.py --delete MEETING_ID [--dry-run]

Options:
    input_file              文字起こしファイル（.txt / .md）（単一ファイルモード）
    --meeting-name NAME     会議種別名（単一ファイルモード、省略時は "不明"）
    --held-at DATE          開催日（YYYY-MM-DD）。省略時はファイル名から推定（単一ファイルモード）
    --bulk                  一括処理モード（meetings/ ディレクトリ内を全て処理）
    --meetings-dir DIR      一括処理時の議事録ディレクトリ（デフォルト: meetings/）
    --since YYYY-MM-DD      一括処理・--list 時に対象を絞る
    --db PATH               pm.db のパス（デフォルト: data/pm.db）
    --minutes-dir DIR       議事録DB の保存先ディレクトリ（デフォルト: data/minutes/）
    --skip-minutes-db       議事録DB への保存をスキップする
    --force                 既存レコードを上書き
    --dry-run               DB保存なし・結果を標準出力のみ
    --output PATH           出力をファイルにも保存（単一ファイルモードのみ）
    --skip-parsed           LLM抽出結果の *_parsed.md 保存をスキップする
    --no-encrypt            DBを暗号化しない（平文モード）
    --list                  pm.db にインポート済みの議事録一覧を表示して終了
    --delete MEETING_ID     指定した meeting_id の議事録をDBから削除する
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
from cli_utils import add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg, make_logger, load_claude_md


def normalize_assignee(name: str | None) -> str | None:
    """日本語を含む担当者名の姓名間スペース（半角・全角）を除去する"""
    if not name:
        return name
    if re.search(r"[\u3040-\u9fff]", name):
        name = name.replace(" ", "").replace("\u3000", "")
    return name


# --------------------------------------------------------------------------- #
# パス解決
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEFAULT_DB = REPO_ROOT / "data" / "pm.db"
DEFAULT_MEETINGS_DIR = REPO_ROOT / "meetings"
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"


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
        migrations=[
            "ALTER TABLE action_items ADD COLUMN note TEXT",
            "ALTER TABLE action_items ADD COLUMN milestone_id TEXT",
        ],
    )
    return conn


def fetch_milestones(conn: sqlite3.Connection) -> list[dict]:
    """pm.db からアクティブなマイルストーン一覧を取得する"""
    try:
        rows = conn.execute(
            "SELECT milestone_id, name, due_date, area FROM milestones WHERE status='active' ORDER BY due_date"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []



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
    """
    YYYY-MM-DD_{会議名}.md → (held_at, meeting_name)
    パースできない場合は None を返す。
    """
    name = path.stem
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", name)
    if not m:
        return None
    return m.group(1), m.group(2)


def collect_files(meetings_dir: Path, since: str | None) -> list[Path]:
    """対象ファイルを収集してソートして返す"""
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

3. **マイルストーン紐づけ**: 各アクションアイテムについて、下記「マイルストーン一覧」の
   いずれかに明らかに関連する場合は milestone_id を記入すること。判断できない場合は null。

4. **出力形式**: 必ず以下のJSON形式で出力すること（余分なテキスト不要）

5. **議事内容（minutes）の記述粒度**:
   - 議題ごとにセクションを分け、議論の流れを詳細に記述すること
   - 誰が何を発言・提案・質問したかを含めること（発言者名が文字起こしにある場合）
   - 決定事項やアクションアイテムが生じた文脈（何が課題で、どう議論されたか）が
     後から読んでわかる粒度にすること
   - Markdown形式で出力すること

6. **background フィールド**:
   - decisions の background: その決定に至った議論・前提条件・却下された代替案等
   - action_items の background: そのタスクが発生した経緯・課題・誰が提起したか等

## マイルストーン一覧

{milestones}

## 出力JSON形式

```json
{{
  "summary": "会議全体の要点を3〜7行で記述（誰が何を話し合ったか）",
  "minutes": "議題ごとのセクションに分けた詳細な議事内容（Markdown形式）",
  "decisions": [
    {{
      "content": "決定事項の内容（明示的に決まったこと、合意に至ったこと）",
      "decided_at": "YYYY-MM-DD または null",
      "background": "この決定に至った議論・経緯・前提条件（1〜3文）"
    }}
  ],
  "action_items": [
    {{
      "content": "アクションアイテムの内容",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null",
      "milestone_id": "マイルストーンID（M1等）または null",
      "background": "このタスクが発生した経緯・課題・提起者（1〜3文）"
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


def format_milestones_for_prompt(milestones: list[dict]) -> str:
    if not milestones:
        return "（マイルストーン未登録。goals.yaml を定義して pm_goals_import.py を実行してください）"
    lines = ["| ID | マイルストーン名 | 期限 | エリア |",
             "|----|----------------|------|--------|"]
    for m in milestones:
        lines.append(f"| {m['milestone_id']} | {m['name']} | {m.get('due_date') or '未定'} | {m.get('area') or ''} |")
    return "\n".join(lines)


def build_prompt(transcript: str, held_at: str, claude_md: str, milestones: list[dict]) -> str:
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
        milestones=format_milestones_for_prompt(milestones),
    )


# --------------------------------------------------------------------------- #
# JSON 抽出
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict:
    """LLM出力からJSONブロックを抽出してパース"""
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found in LLM output:\n{text[:500]}")


# --------------------------------------------------------------------------- #
# _parsed.md 保存
# --------------------------------------------------------------------------- #
def save_parsed_md(input_path: Path, held_at: str, kind: str, extracted: dict) -> Path:
    """LLM抽出結果を {stem}_parsed.md として入力ファイルと同じディレクトリに保存する"""
    lines = [
        f"# {kind} ({held_at})",
        "",
        "## 会議要旨",
        "",
        extracted.get("summary", "(なし)"),
    ]

    # 詳細議事内容
    if extracted.get("minutes"):
        lines += ["", "## 議事内容", "", extracted["minutes"]]

    # 決定事項（経緯付き）
    lines += ["", "## 決定事項", ""]
    for i, d in enumerate(extracted.get("decisions", []), 1):
        date_str = f" [{d.get('decided_at')}]" if d.get("decided_at") else ""
        lines.append(f"{i}. {d['content']}{date_str}")
        if d.get("background"):
            lines.append(f"   > 経緯: {d['background']}")

    # アクションアイテム（経緯付き）
    lines += ["", "## アクションアイテム", ""]
    for i, a in enumerate(extracted.get("action_items", []), 1):
        assignee = a.get("assignee") or "未定"
        due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
        ms = f" [MS: {a['milestone_id']}]" if a.get("milestone_id") else ""
        lines.append(f"{i}. [{assignee}] {a['content']}{due}{ms}")
        if a.get("background"):
            lines.append(f"   > 経緯: {a['background']}")

    out_path = input_path.parent / f"{input_path.stem}_parsed.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# 議事録DB（会議名ごとに独立）
# --------------------------------------------------------------------------- #
MINUTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    held_at     TEXT NOT NULL,
    source_file TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes_content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    content     TEXT NOT NULL,
    decided_at  TEXT,
    background  TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    content     TEXT NOT NULL,
    assignee    TEXT,
    due_date    TEXT,
    status      TEXT DEFAULT 'open',
    milestone_id TEXT,
    background  TEXT
);
"""


def init_minutes_db(minutes_dir: Path, kind: str, no_encrypt: bool = False) -> sqlite3.Connection:
    """会議名に対応する議事録DBを開く（なければ作成）"""
    minutes_dir.mkdir(parents=True, exist_ok=True)
    db_path = minutes_dir / f"{kind}.db"
    return open_db(db_path, encrypt=not no_encrypt, schema=MINUTES_SCHEMA)


def save_to_minutes_db(
    conn: sqlite3.Connection,
    held_at: str,
    source_file: str,
    extracted: dict,
    force: bool,
) -> None:
    now = datetime.now().isoformat()

    # 同じ開催日のインスタンスが既にある場合の処理
    existing = conn.execute(
        "SELECT id FROM instances WHERE held_at = ?", (held_at,)
    ).fetchone()

    if existing:
        if not force:
            return  # スキップ（呼び出し元でログ出力済み）
        instance_id = existing["id"]
        conn.execute("DELETE FROM minutes_content WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM decisions       WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM action_items    WHERE instance_id = ?", (instance_id,))
        conn.execute("UPDATE instances SET source_file=?, imported_at=? WHERE id=?",
                     (source_file, now, instance_id))
    else:
        cur = conn.execute(
            "INSERT INTO instances (held_at, source_file, imported_at) VALUES (?, ?, ?)",
            (held_at, source_file, now),
        )
        instance_id = cur.lastrowid

    if extracted.get("minutes"):
        conn.execute(
            "INSERT INTO minutes_content (instance_id, content) VALUES (?, ?)",
            (instance_id, extracted["minutes"]),
        )

    for d in extracted.get("decisions", []):
        conn.execute(
            "INSERT INTO decisions (instance_id, content, decided_at, background) VALUES (?, ?, ?, ?)",
            (instance_id, d["content"], d.get("decided_at"), d.get("background")),
        )

    for a in extracted.get("action_items", []):
        conn.execute(
            """
            INSERT INTO action_items
                (instance_id, content, assignee, due_date, milestone_id, background)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id, a["content"], normalize_assignee(a.get("assignee")),
                a.get("due_date"), a.get("milestone_id"), a.get("background"),
            ),
        )

    conn.commit()


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

    for d in extracted.get("decisions", []):
        conn.execute(
            """
            INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref, extracted_at)
            VALUES (?, ?, ?, 'meeting', ?, ?)
            """,
            (meeting_id, d["content"], d.get("decided_at"), file_path, now),
        )

    for a in extracted.get("action_items", []):
        conn.execute(
            """
            INSERT INTO action_items
                (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at, milestone_id)
            VALUES (?, ?, ?, ?, 'open', 'meeting', ?, ?, ?)
            """,
            (meeting_id, a["content"], normalize_assignee(a.get("assignee")), a.get("due_date"),
             file_path, now, a.get("milestone_id")),
        )

    conn.commit()


# --------------------------------------------------------------------------- #
# 一覧表示 / 削除
# --------------------------------------------------------------------------- #
def _open_db_or_exit(db_path: Path, no_encrypt: bool) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


def list_imported(db_path: Path, since: str | None, no_encrypt: bool) -> None:
    """pm.db にインポート済みの議事録一覧を表示する"""
    conn = _open_db_or_exit(db_path, no_encrypt)

    query = """
        SELECT
            m.meeting_id,
            m.held_at,
            m.kind,
            m.file_path,
            m.parsed_at,
            COUNT(DISTINCT a.id) AS action_items,
            COUNT(DISTINCT d.id) AS decisions
        FROM meetings m
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id
        LEFT JOIN decisions d    ON d.meeting_id = m.meeting_id
    """
    params: list = []
    if since:
        query += " WHERE m.held_at >= ?"
        params.append(since)
    query += " GROUP BY m.meeting_id ORDER BY m.held_at DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("インポート済み議事録はありません。")
        return

    print(f"{'開催日':<12} {'AI':>3} {'決定':>3}  {'登録日時':<20}  {'meeting_id'}")
    print("-" * 90)
    for r in rows:
        held_at    = r["held_at"]    or ""
        parsed_at  = (r["parsed_at"] or "")[:19]
        meeting_id = r["meeting_id"] or ""
        ai_count   = r["action_items"]
        d_count    = r["decisions"]
        print(f"{held_at:<12} {ai_count:>3} {d_count:>3}  {parsed_at:<20}  {meeting_id}")

    print(f"\n合計: {len(rows)} 件")
    print("\n※ 削除は: python3 scripts/pm_meeting_import.py --delete <meeting_id>")


def delete_meeting(
    db_path: Path, meeting_id: str, dry_run: bool, force: bool, no_encrypt: bool
) -> None:
    """指定した meeting_id の議事録を pm.db から削除する"""
    conn = _open_db_or_exit(db_path, no_encrypt)

    row = conn.execute(
        """
        SELECT m.meeting_id, m.held_at, m.kind, m.file_path,
               COUNT(DISTINCT a.id) AS ai_count,
               COUNT(DISTINCT d.id) AS d_count
        FROM meetings m
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id
        LEFT JOIN decisions d    ON d.meeting_id = m.meeting_id
        WHERE m.meeting_id = ?
        GROUP BY m.meeting_id
        """,
        (meeting_id,),
    ).fetchone()

    if not row:
        print(f"ERROR: meeting_id '{meeting_id}' は pm.db に存在しません。", file=sys.stderr)
        print("  --list で一覧を確認してください。", file=sys.stderr)
        conn.close()
        sys.exit(1)

    print(f"削除対象:")
    print(f"  meeting_id : {row['meeting_id']}")
    print(f"  開催日     : {row['held_at']}")
    print(f"  会議種別   : {row['kind']}")
    print(f"  ファイル   : {row['file_path']}")
    print(f"  アクションアイテム: {row['ai_count']} 件")
    print(f"  決定事項          : {row['d_count']} 件")

    if dry_run:
        print("\n[INFO] --dry-run のため削除をスキップしました")
        conn.close()
        return

    if not force:
        answer = input("\n本当に削除しますか？ [y/N]: ").strip().lower()
        if answer != "y":
            print("削除をキャンセルしました。")
            conn.close()
            return

    conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM decisions    WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meetings     WHERE meeting_id = ?", (meeting_id,))
    conn.commit()
    conn.close()

    print(f"\n✓ meeting_id '{meeting_id}' を削除しました。")


# --------------------------------------------------------------------------- #
# 単一ファイル処理（コア）
# --------------------------------------------------------------------------- #
def process_file(
    input_path: Path,
    held_at: str,
    kind: str,
    db_path: Path,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
    save_parsed: bool = True,
    minutes_dir: Path | None = None,
    log=print,
) -> str:
    """
    単一ファイルを処理する。
    Returns: "ok" | "skipped" | "error"
    """
    meeting_id = input_path.stem

    log(f"[INFO] 入力ファイル : {input_path}")
    log(f"[INFO] 開催日       : {held_at}")
    log(f"[INFO] 会議種別     : {kind}")
    log(f"[INFO] meeting_id   : {meeting_id}")

    transcript = input_path.read_text(encoding="utf-8")
    claude_md = load_claude_md(CLAUDE_MD)

    # マイルストーン取得 + インポート済みチェック（LLM呼び出し前）
    conn_for_ms = init_db(db_path, no_encrypt=no_encrypt)
    milestones = fetch_milestones(conn_for_ms)
    if not dry_run:
        existing = conn_for_ms.execute(
            "SELECT meeting_id FROM meetings WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
        if existing and not force:
            conn_for_ms.close()
            log(f"[SKIP] meeting_id '{meeting_id}' は既にDBに存在します。--force で上書き可能")
            return "skipped"
    conn_for_ms.close()

    log(f"[INFO] マイルストーン: {len(milestones)} 件")
    log("[INFO] LLMによる抽出を開始...")

    prompt = build_prompt(transcript, held_at, claude_md, milestones)
    try:
        raw_output = call_claude(prompt)
    except Exception as e:
        log(f"[ERROR] LLM呼び出し失敗: {e}")
        return "error"

    log("[INFO] LLM出力を解析中...")
    try:
        extracted = extract_json(raw_output)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"[ERROR] JSON解析に失敗: {e}")
        return "error"

    # _parsed.md 保存
    if save_parsed:
        parsed_path = save_parsed_md(input_path, held_at, kind, extracted)
        log(f"[INFO] 抽出結果を保存: {parsed_path}")

    # 結果表示
    log("\n" + "=" * 60)
    log("## 会議要旨")
    log(extracted.get("summary", "(なし)"))

    log("\n## 決定事項")
    for i, d in enumerate(extracted.get("decisions", []), 1):
        date_str = f" [{d.get('decided_at')}]" if d.get("decided_at") else ""
        bg = f"\n     経緯: {d['background']}" if d.get("background") else ""
        log(f"  {i}. {d['content']}{date_str}{bg}")

    log("\n## アクションアイテム")
    for i, a in enumerate(extracted.get("action_items", []), 1):
        assignee = a.get("assignee") or "未定"
        due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
        bg = f"\n     経緯: {a['background']}" if a.get("background") else ""
        log(f"  {i}. [{assignee}] {a['content']}{due}{bg}")
    log("=" * 60)

    if dry_run:
        log("\n[INFO] --dry-run のため DB保存をスキップしました")
        return "ok"

    # pm.db に保存
    conn = init_db(db_path, no_encrypt=no_encrypt)
    save_to_db(conn, meeting_id, held_at, kind, str(input_path), extracted, force)
    conn.close()
    log(f"\n[INFO] pm.db に保存完了: {db_path}")
    log(f"  - decisions   : {len(extracted.get('decisions', []))} 件")
    log(f"  - action_items: {len(extracted.get('action_items', []))} 件")

    # 議事録DB に保存
    if minutes_dir is not None:
        mconn = init_minutes_db(minutes_dir, kind, no_encrypt=no_encrypt)
        existing = mconn.execute(
            "SELECT id FROM instances WHERE held_at = ?", (held_at,)
        ).fetchone()
        if existing and not force:
            log(f"[SKIP] 議事録DB: {kind}.db に {held_at} の記録が既に存在します。--force で上書き可能")
        else:
            save_to_minutes_db(mconn, held_at, str(input_path), extracted, force)
            minutes_db_path = minutes_dir / f"{kind}.db"
            log(f"[INFO] 議事録DB に保存完了: {minutes_db_path}")
            log(f"  - minutes     : {'あり' if extracted.get('minutes') else 'なし'}")
            log(f"  - decisions   : {len(extracted.get('decisions', []))} 件")
            log(f"  - action_items: {len(extracted.get('action_items', []))} 件")
        mconn.close()

    return "ok"


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="会議文字起こし → pm.db への解析・保存（単一ファイル / 一括処理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 単一ファイル
  python3 scripts/pm_meeting_import.py meetings/2026-03-10_Leader_Meeting.md \\
      --meeting-name Leader_Meeting --held-at 2026-03-10

  # 一括処理
  python3 scripts/pm_meeting_import.py --bulk
  python3 scripts/pm_meeting_import.py --bulk --since 2026-01-01 --force

  # 一覧 / 削除
  python3 scripts/pm_meeting_import.py --list
  python3 scripts/pm_meeting_import.py --delete 2026-03-10_Leader_Meeting
""",
    )
    parser.add_argument("input_file", nargs="?",
                        help="文字起こしファイル（.txt / .md）（単一ファイルモード）")
    parser.add_argument("--meeting-name", default=None,
                        help="会議種別名（単一ファイルモード）")
    parser.add_argument("--held-at", default=None,
                        help="開催日 YYYY-MM-DD（単一ファイルモード、省略時はファイル名から推定）")
    parser.add_argument("--bulk", action="store_true",
                        help="一括処理モード（meetings/ ディレクトリ内を全て処理）")
    parser.add_argument("--meetings-dir", default=None,
                        help="一括処理時の議事録ディレクトリ（デフォルト: meetings/）")
    add_since_arg(parser, "（--bulk / --list 時）")
    parser.add_argument("--db", default=None, help="pm.db のパス")
    parser.add_argument("--minutes-dir", default=None,
                        help="議事録DB の保存先ディレクトリ（デフォルト: data/minutes/）")
    parser.add_argument("--skip-minutes-db", action="store_true",
                        help="議事録DB への保存をスキップする")
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    parser.add_argument("--skip-parsed", action="store_true",
                        help="LLM抽出結果の *_parsed.md 保存をスキップする")
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true",
                        help="インポート済み議事録一覧を表示して終了")
    parser.add_argument("--delete", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id の議事録を削除する")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_DB
    minutes_dir = None if args.skip_minutes_db else (
        Path(args.minutes_dir) if args.minutes_dir else DEFAULT_MINUTES_DIR
    )

    # --- list ---
    if args.list:
        list_imported(db_path, args.since, args.no_encrypt)
        return

    # --- delete ---
    if args.delete:
        delete_meeting(db_path, args.delete, args.dry_run, args.force, args.no_encrypt)
        return

    # --- bulk ---
    if args.bulk:
        meetings_dir = Path(args.meetings_dir) if args.meetings_dir else DEFAULT_MEETINGS_DIR
        if not meetings_dir.exists():
            print(f"ERROR: ディレクトリが見つかりません: {meetings_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"[INFO] 議事録ディレクトリ: {meetings_dir}")
        print(f"[INFO] pm.db            : {db_path}")
        if minutes_dir:
            print(f"[INFO] 議事録DB保存先   : {minutes_dir}")
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
            print(f"[{i}/{len(files)}] {file_path.name}")
            status = process_file(
                file_path, held_at, meeting_name, db_path,
                force=args.force, dry_run=args.dry_run,
                no_encrypt=args.no_encrypt,
                save_parsed=not args.skip_parsed,
                minutes_dir=minutes_dir,
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
    kind = args.meeting_name or "不明"

    log, close_log = make_logger(args.output)

    status = process_file(
        input_path, held_at, kind, db_path,
        force=args.force, dry_run=args.dry_run,
        no_encrypt=args.no_encrypt,
        save_parsed=not args.skip_parsed,
        minutes_dir=minutes_dir,
        log=log,
    )

    close_log()

    if status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
