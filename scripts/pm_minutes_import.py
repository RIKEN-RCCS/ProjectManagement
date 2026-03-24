#!/usr/bin/env python3
"""
pm_minutes_import.py

会議文字起こしから詳細な議事録DBを生成する。
DBは会議名（--meeting-name）ごとに独立した SQLite ファイルとして
data/minutes/{meeting_name}.db に保存される。

処理後は pm_minutes_to_pm.py で pm.db に転記する（LLM不使用）。

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

    # 詳細表示
    python3 scripts/pm_minutes_import.py --show 2026-03-10_Leader_Meeting

    # Slack にアップロード（Files タブに表示）
    python3 scripts/pm_minutes_import.py \\
        --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 -c C08SXA4M7JT

    # 特定スレッドにアップロード（スレッドに集約、Files タブには表示されない）
    python3 scripts/pm_minutes_import.py \\
        --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \\
        -c C08SXA4M7JT --thread-ts 1741234567.123456

    # 議事録DBから削除
    python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting
    python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting --meeting-name Leader_Meeting

    # DB内容をMarkdownにエクスポート（人間が修正するための叩き台を出力）
    python3 scripts/pm_minutes_import.py --export 2026-03-10_Leader_Meeting --output corrected.md

    # 人間が修正したMarkdownをLLM不使用でインポート（--force で上書き）
    python3 scripts/pm_minutes_import.py corrected.md \\
        --meeting-name Leader_Meeting --held-at 2026-03-10 --no-llm --force

Options:
    input_file              文字起こしファイル（.txt / .md）（単一ファイルモード）
    --meeting-name NAME     会議種別名（DBファイル名に使用。省略時はファイル名から推定）
    --held-at DATE          開催日（YYYY-MM-DD）。省略時はファイル名から推定
    --bulk                  一括処理モード（meetings/ ディレクトリ内を全て処理）
    --meetings-dir DIR      議事録 .md ファイルの検索ディレクトリ（デフォルト: meetings/）
    --minutes-dir DIR       議事録DBの保存ディレクトリ（デフォルト: data/minutes/）
    --since YYYY-MM-DD      一括処理・--list 時に対象を絞る
    --model MODEL           使用する Claude モデル。省略時は CLI デフォルト
    --force                 既存レコードを上書き
    --dry-run               DB保存なし・結果を標準出力のみ
    --output PATH           出力をファイルにも保存（単一ファイルモードのみ）
    --no-encrypt            DBを暗号化しない（平文モード）
    --no-llm                LLMを呼ばず入力ファイルを構造化Markdownとして直接解析（人間修正版インポート用）
    --list                  議事録DBの内容を表示して終了
    --show MEETING_ID       指定した meeting_id の詳細を表示して終了
    --export MEETING_ID     DB内容を構造化Markdownでエクスポート（人間による修正の叩き台として出力）
    --delete MEETING_ID     指定した meeting_id を議事録DBから削除して終了
    --post-to-slack         議事録ファイルを Slack チャンネルにアップロード
    -c / --channel ID       アップロード先チャンネルID（--post-to-slack 時に必須）
    --thread-ts TS          投稿先スレッドTS（省略時: チャンネル直接投稿 / 指定時: スレッド集約）
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import open_db
from cli_utils import (
    add_output_arg, add_no_encrypt_arg, add_dry_run_arg, add_since_arg,
    make_logger, prepare_transcript, call_claude,
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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id     TEXT NOT NULL REFERENCES instances(meeting_id),
    content        TEXT NOT NULL,
    source_context TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT NOT NULL REFERENCES instances(meeting_id),
    content      TEXT NOT NULL,
    assignee     TEXT,
    due_date     TEXT
);
"""

_MINUTES_MIGRATIONS = [
    "ALTER TABLE action_items ADD COLUMN assignee TEXT",
    "ALTER TABLE action_items ADD COLUMN due_date TEXT",
    "ALTER TABLE decisions ADD COLUMN source_context TEXT",
    "ALTER TABLE instances ADD COLUMN posted_to_slack_at TEXT",
    "ALTER TABLE instances ADD COLUMN slack_thread_ts TEXT",
    "ALTER TABLE instances ADD COLUMN slack_channel_id TEXT",
    "ALTER TABLE instances ADD COLUMN slack_decisions_thread_ts TEXT",
    "ALTER TABLE instances ADD COLUMN slack_file_permalink TEXT",
]


def init_minutes_db(db_path: Path, no_encrypt: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(db_path, encrypt=not no_encrypt, schema=MINUTES_SCHEMA,
                   migrations=_MINUTES_MIGRATIONS)


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

- 決定事項の内容 [出典: 根拠となった議論・発言の要約（1〜2文）]

※ 決定事項の形式の例:
  - NVLinkアプリ性能測定を今年度キャンセルする [出典: NVIDIAから今年度実施困難との連絡があり、リーダー会議で対応を協議した]
  - FFBをGitHubの公開リポジトリでOSS公開する [出典: 安藤から、開発者との議論でOSS化が決定したと報告があった]
※ 根拠・背景が不明な場合は [出典: なし] とする
※ 決定事項がなければ「（なし）」

## アクションアイテム

- [担当者名|未定] 内容 (期限: YYYY-MM-DD|なし)

※ アクションアイテムの形式の例:
  - [井上] AI for Scienceの対応資料を作成する (期限: 2026-03-18)
  - [未定] NVIDIAへの回答を検討する (期限: なし)
  - [佐野・上野] アーキテクチャ仕様書を更新する (期限: 2026-03-31)
※ 担当者が不明な場合は「未定」を記入すること
※ 期限が「3月18日」等の形式の場合は開催日（{held_at}）を参考に YYYY-MM-DD に変換すること
※ アクションアイテムがなければ「（なし）」

## 議事内容

（議論の流れを要旨としてまとめて記載）

---

## 文字起こしテキスト（開催日: {held_at}）

{transcript}
"""


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


# 決定事項: 内容 [出典: 出典テキスト]
_DECISION_RE = re.compile(r"^(.+?)\s+\[出典:\s*(.+?)\]$")

# [担当者] 内容 (期限: YYYY-MM-DD) または [担当者] 内容 (期限: なし)
_AI_RE = re.compile(
    r"^\[([^\]]+)\]\s+(.+?)(?:\s+\(期限:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|なし)\))?$"
)


def _parse_decisions(section_text: str) -> list[dict]:
    """
    決定事項箇条書きを構造化して返す。
    各要素: {"content": str, "source_context": str|None}
    フォーマット: - 内容 [出典: 出典テキスト]
    """
    items = []
    for line in section_text.splitlines():
        line = line.strip()
        if not re.match(r"^[-*]\s+", line):
            continue
        text = re.sub(r"^[-*]\s+", "", line).strip()
        if not text or text in ("（なし）", "(なし)"):
            continue
        m = _DECISION_RE.match(text)
        if m:
            content = m.group(1).strip()
            raw_ctx = m.group(2).strip()
            source_context = None if raw_ctx in ("なし", "") else raw_ctx
            items.append({"content": content, "source_context": source_context})
        else:
            items.append({"content": text, "source_context": None})
    return items


def _parse_action_items(section_text: str) -> list[dict]:
    """
    アクションアイテム箇条書きを構造化して返す。
    各要素: {"content": str, "assignee": str|None, "due_date": str|None}
    フォーマット外の行はフォールバックとして content のみ設定する。
    """
    items = []
    for line in section_text.splitlines():
        line = line.strip()
        if not re.match(r"^[-*]\s+", line):
            continue
        text = re.sub(r"^[-*]\s+", "", line).strip()
        if not text or text in ("（なし）", "(なし)"):
            continue
        m = _AI_RE.match(text)
        if m:
            raw_assignee, content, raw_due = m.group(1), m.group(2).strip(), m.group(3)
            assignee = None if raw_assignee.strip() in ("未定", "") else raw_assignee.strip()
            due_date = None if (not raw_due or raw_due == "なし") else raw_due
            items.append({"content": content, "assignee": assignee, "due_date": due_date})
        else:
            # フォールバック: フォーマット外
            items.append({"content": text, "assignee": None, "due_date": None})
    return items


def parse_minutes_output(text: str) -> dict:
    """LLM が出力した Markdown 議事録をパースして辞書で返す"""
    decisions_text    = _extract_section(text, "決定事項")
    action_items_text = _extract_section(text, "アクションアイテム")
    minutes_text      = _extract_section(text, "議事内容")

    return {
        "minutes":      minutes_text,
        "decisions":    _parse_decisions(decisions_text),
        "action_items": _parse_action_items(action_items_text),
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

    for d in parsed["decisions"]:
        conn.execute(
            "INSERT INTO decisions (meeting_id, content, source_context) VALUES (?, ?, ?)",
            (meeting_id, d["content"], d.get("source_context")),
        )

    for a in parsed["action_items"]:
        conn.execute(
            "INSERT INTO action_items (meeting_id, content, assignee, due_date) VALUES (?, ?, ?, ?)",
            (meeting_id, a["content"], a.get("assignee"), a.get("due_date")),
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
# 詳細表示
# --------------------------------------------------------------------------- #
def show_meeting(minutes_dir: Path, meeting_id: str,
                 kind_filter: str | None, no_encrypt: bool) -> None:
    """指定 meeting_id の議事録詳細を表示する"""
    if not minutes_dir.exists():
        print(f"[ERROR] 議事録DBディレクトリが存在しません: {minutes_dir}", file=sys.stderr)
        return

    db_files = sorted(minutes_dir.glob("*.db"))
    if kind_filter:
        safe = re.sub(r"[^\w\-]", "_", kind_filter)
        db_files = [f for f in db_files if f.stem == safe]

    found = False
    for db_file in db_files:
        try:
            conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
        except Exception as e:
            print(f"[ERROR] DB接続失敗: {db_file}: {e}", file=sys.stderr)
            continue

        inst = conn.execute(
            "SELECT meeting_id, held_at, kind, file_path, imported_at,"
            "       posted_to_slack_at, slack_channel_id, slack_thread_ts, slack_file_permalink"
            " FROM instances WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()

        if not inst:
            conn.close()
            continue

        found = True
        print(f"\n{'='*70}")
        print(f"  meeting_id : {inst['meeting_id']}")
        print(f"  開催日     : {inst['held_at']}")
        print(f"  会議種別   : {inst['kind']}")
        print(f"  ファイル   : {inst['file_path'] or '(なし)'}")
        print(f"  登録日時   : {(inst['imported_at'] or '')[:19]}")
        if inst['posted_to_slack_at']:
            print(f"  Slack投稿  : {inst['posted_to_slack_at'][:19]}"
                  f"  チャンネル: {inst['slack_channel_id'] or '-'}"
                  f"  スレッドTS: {inst['slack_thread_ts'] or '(チャンネル直接)'}")
            if inst['slack_file_permalink']:
                print(f"  ファイルURL: {inst['slack_file_permalink']}")
        else:
            print(f"  Slack投稿  : 未投稿")
        print(f"{'='*70}")

        decisions = conn.execute(
            "SELECT content, source_context FROM decisions WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
        print(f"\n## 決定事項 ({len(decisions)} 件)")
        if decisions:
            for i, d in enumerate(decisions, 1):
                print(f"  {i}. {d['content']}")
                if d["source_context"]:
                    print(f"     [出典: {d['source_context']}]")
        else:
            print("  （なし）")

        action_items = conn.execute(
            "SELECT content, assignee, due_date FROM action_items"
            " WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
        print(f"\n## アクションアイテム ({len(action_items)} 件)")
        if action_items:
            for i, a in enumerate(action_items, 1):
                assignee = a["assignee"] or "未定"
                due = f" (期限: {a['due_date']})" if a["due_date"] else ""
                print(f"  {i}. [{assignee}] {a['content']}{due}")
        else:
            print("  （なし）")

        mc = conn.execute(
            "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1",
            (meeting_id,),
        ).fetchone()
        print("\n## 議事内容")
        if mc:
            print(mc["content"])
        else:
            print("  （なし）")

        conn.close()
        break

    if not found:
        scope = f"'{kind_filter}'" if kind_filter else "全DB"
        print(f"[ERROR] meeting_id '{meeting_id}' が {scope} に見つかりません。"
              f" --list で一覧を確認してください。", file=sys.stderr)


# --------------------------------------------------------------------------- #
# エクスポート（人間修正用）
# --------------------------------------------------------------------------- #
def cmd_export(minutes_dir: Path, meeting_id: str, kind_filter: str | None,
               no_encrypt: bool, output_path: str | None) -> None:
    """DB内容を構造化Markdownでエクスポートする（人間による修正の叩き台）"""
    if not minutes_dir.exists():
        print(f"[ERROR] 議事録DBディレクトリが存在しません: {minutes_dir}", file=sys.stderr)
        sys.exit(1)

    db_files = sorted(minutes_dir.glob("*.db"))
    if kind_filter:
        safe = re.sub(r"[^\w\-]", "_", kind_filter)
        db_files = [f for f in db_files if f.stem == safe]

    for db_file in db_files:
        try:
            conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
        except Exception as e:
            print(f"[ERROR] DB接続失敗: {db_file}: {e}", file=sys.stderr)
            continue

        inst = conn.execute(
            "SELECT meeting_id, held_at, kind FROM instances WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()

        if not inst:
            conn.close()
            continue

        held_at = inst["held_at"]
        kind = inst["kind"]

        mc_row = conn.execute(
            "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1",
            (meeting_id,),
        ).fetchone()
        data = {
            "decisions": [dict(r) for r in conn.execute(
                "SELECT content, source_context FROM decisions WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            ).fetchall()],
            "action_items": [dict(r) for r in conn.execute(
                "SELECT content, assignee, due_date FROM action_items WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            ).fetchall()],
            "minutes_content": mc_row["content"] if mc_row else None,
        }
        conn.close()

        md = _reconstruct_minutes_md(held_at, kind, data)

        if output_path:
            Path(output_path).write_text(md, encoding="utf-8")
            print(f"[INFO] エクスポート完了: {output_path}")
            print(f"[INFO] 修正後に以下のコマンドで再インポートしてください:")
            print(f"  python3 scripts/pm_minutes_import.py {output_path} \\")
            print(f"      --meeting-name {kind} --held-at {held_at} --no-llm --force")
        else:
            print(md)
        return

    scope = f"'{kind_filter}'" if kind_filter else "全DB"
    print(f"[ERROR] meeting_id '{meeting_id}' が {scope} に見つかりません。"
          f" --list で一覧を確認してください。", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# 削除
# --------------------------------------------------------------------------- #
def delete_meeting(minutes_dir: Path, meeting_id: str,
                   kind_filter: str | None, no_encrypt: bool,
                   dry_run: bool) -> None:
    """指定 meeting_id を議事録DBから削除する"""
    if not minutes_dir.exists():
        print(f"[ERROR] 議事録DBディレクトリが存在しません: {minutes_dir}", file=sys.stderr)
        sys.exit(1)

    db_files = sorted(minutes_dir.glob("*.db"))
    if kind_filter:
        safe = re.sub(r"[^\w\-]", "_", kind_filter)
        db_files = [f for f in db_files if f.stem == safe]

    for db_file in db_files:
        try:
            conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
        except Exception as e:
            print(f"[ERROR] DB接続失敗: {db_file}: {e}", file=sys.stderr)
            continue

        inst = conn.execute(
            "SELECT meeting_id, held_at FROM instances WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()

        if not inst:
            conn.close()
            continue

        print(f"[INFO] 削除対象: {meeting_id} ({inst['held_at']}) @ {db_file}")
        if dry_run:
            print("[INFO] --dry-run のため削除をスキップしました")
            conn.close()
            return

        conn.execute("DELETE FROM minutes_content WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM instances WHERE meeting_id = ?", (meeting_id,))
        conn.commit()
        conn.close()
        print(f"[INFO] {meeting_id} を {db_file} から削除しました")
        return

    scope = f"'{kind_filter}'" if kind_filter else "全DB"
    print(f"[ERROR] meeting_id '{meeting_id}' が {scope} に見つかりません", file=sys.stderr)
    sys.exit(1)


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
    no_llm: bool = False,
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

    # インポート済みチェック（LLM呼び出し前）: 同じ開催日・会議名が議事録DBに存在するか確認
    if not dry_run and not force:
        conn_check = init_minutes_db(db_path, no_encrypt=no_encrypt)
        existing = conn_check.execute(
            "SELECT meeting_id FROM instances WHERE held_at = ? AND kind = ?", (held_at, kind)
        ).fetchone()
        conn_check.close()
        if existing:
            log(f"[SKIP] {held_at}/{kind} は既に議事録DBに存在します。--force で上書き可能")
            return "skipped"

    if no_llm:
        # 入力ファイルを構造化Markdownとして直接パース（LLM不使用）
        log("[INFO] --no-llm: 入力ファイルを構造化Markdownとして直接解析します")
        minutes_text = input_path.read_text(encoding="utf-8")
    else:
        raw_transcript = input_path.read_text(encoding="utf-8")
        transcript, is_whisper = prepare_transcript(raw_transcript)
        log(f"[INFO] 文字起こし形式: {'Whisper (話者・タイムスタンプ付き)' if is_whisper else '平文テキスト'}")

        if dry_run:
            log("[INFO] --dry-run のため LLM呼び出し・DB保存をスキップしました")
            return "ok"

        log(f"[INFO] LLMによる議事録作成を開始... (model: {model or 'default'})")
        prompt = PROMPT_TEMPLATE.format(transcript=transcript, held_at=held_at)
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
    log(f"  決定事項: {len(parsed['decisions'])} 件 / アクションアイテム: {len(parsed['action_items'])} 件")

    if dry_run:
        log("[INFO] --dry-run のため DB保存をスキップしました")
        return "ok"

    conn = init_minutes_db(db_path, no_encrypt=no_encrypt)
    save_to_minutes_db(conn, meeting_id, held_at, kind, str(input_path), parsed, force)
    conn.close()

    log(f"\n[INFO] 議事録DB に保存完了: {db_path}")
    log(f"  - decisions   : {len(parsed['decisions'])} 件")
    log(f"  - action_items: {len(parsed['action_items'])} 件")
    assigned = sum(1 for a in parsed['action_items'] if a.get('assignee'))
    dated    = sum(1 for a in parsed['action_items'] if a.get('due_date'))
    log(f"    (担当者あり: {assigned}件, 期限あり: {dated}件)")
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

  # 詳細表示（Slack 投稿済み状況も含む）
  python3 scripts/pm_minutes_import.py --show 2026-03-10_Leader_Meeting
  python3 scripts/pm_minutes_import.py --show 2026-03-10_Leader_Meeting --meeting-name Leader_Meeting

  # DB内容を修正用Markdownにエクスポート（MEETING_ID で一意に特定できるため --meeting-name 不要）
  python3 scripts/pm_minutes_import.py --export 2026-03-10_Leader_Meeting --output corrected.md

  # 人間が修正したMarkdownをLLM不使用でインポート
  python3 scripts/pm_minutes_import.py corrected.md \\
      --meeting-name Leader_Meeting --held-at 2026-03-10 --no-llm --force

  # 削除
  python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting
  python3 scripts/pm_minutes_import.py --delete 2026-03-10_Leader_Meeting --meeting-name Leader_Meeting

  # Slack にアップロード（Files タブに表示）
  python3 scripts/pm_minutes_import.py \\
      --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 -c C08SXA4M7JT

  # 特定スレッドにアップロード（スレッドに集約、Files タブには表示されない）
  python3 scripts/pm_minutes_import.py \\
      --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \\
      -c C08SXA4M7JT --thread-ts 1741234567.123456

  # 確認のみ（Slack API 呼び出しなし）
  python3 scripts/pm_minutes_import.py \\
      --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \\
      -c C08SXA4M7JT --dry-run

  # 再アップロード（投稿済みフラグを無視）
  python3 scripts/pm_minutes_import.py \\
      --post-to-slack --meeting-name Leader_Meeting --held-at 2026-03-10 \\
      -c C08SXA4M7JT --force
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
    parser.add_argument("--no-llm", action="store_true", default=False,
                        help="LLMを呼ばず入力ファイルを構造化Markdownとして直接解析してDBに保存"
                             "（人間が修正した議事録の再インポート用。--force と併用して上書き）")
    parser.add_argument("--force", action="store_true", help="既存レコードを上書き")
    add_dry_run_arg(parser)
    add_output_arg(parser)
    add_no_encrypt_arg(parser)
    parser.add_argument("--list", action="store_true",
                        help="議事録DBの内容を表示して終了")
    parser.add_argument("--show", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id の詳細（決定事項・AI・議事内容）を表示して終了")
    parser.add_argument("--export", default=None, metavar="MEETING_ID",
                        help="DB内容を構造化Markdownでエクスポート（人間による修正の叩き台）。"
                             "--output で保存先を指定しない場合は標準出力に表示")
    parser.add_argument("--delete", default=None, metavar="MEETING_ID",
                        help="指定した meeting_id を議事録DBから削除して終了")
    parser.add_argument("--post-to-slack", action="store_true",
                        help="議事録ファイルを Slack チャンネルにアップロード"
                             "（--meeting-name・--held-at・-c が必須）")
    parser.add_argument("-c", "--channel", default=None, metavar="CHANNEL_ID",
                        help="アップロード先チャンネルID（--post-to-slack 時に必須）")
    parser.add_argument("--thread-ts", default=None, metavar="TS",
                        help="投稿先スレッドTS（省略時: チャンネル直接投稿で Files タブに表示 / "
                             "指定時: スレッドにリプライ投稿で Files タブには表示されない）")
    args = parser.parse_args()

    minutes_dir = Path(args.minutes_dir) if args.minutes_dir else DEFAULT_MINUTES_DIR

    # --- delete ---
    if args.delete:
        delete_meeting(minutes_dir, args.delete, args.meeting_name,
                       args.no_encrypt, args.dry_run)
        return

    # --- show ---
    if args.show:
        show_meeting(minutes_dir, args.show, args.meeting_name, args.no_encrypt)
        return

    # --- export ---
    if args.export:
        cmd_export(minutes_dir, args.export, args.meeting_name,
                   args.no_encrypt, args.output)
        return

    # --- list ---
    if args.list:
        list_minutes(minutes_dir, args.meeting_name, args.since, args.no_encrypt)
        return

    # --- post-to-slack ---
    if args.post_to_slack:
        if not args.meeting_name or not args.held_at:
            parser.error("--post-to-slack には --meeting-name と --held-at が必須です")
        if not args.channel:
            parser.error("--post-to-slack には -c CHANNEL_ID が必須です")
        meetings_dir = Path(args.meetings_dir) if args.meetings_dir else DEFAULT_MEETINGS_DIR
        log, close_log = make_logger(args.output)
        log(f"[INFO] 会議種別  : {args.meeting_name}")
        log(f"[INFO] 開催日    : {args.held_at}")
        log(f"[INFO] 議事録DB  : {minutes_dir}")
        cmd_post_to_slack(args, minutes_dir, meetings_dir, log)
        close_log()
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
                no_llm=args.no_llm,
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
        no_llm=args.no_llm,
        log=log,
    )

    close_log()

    if status == "error":
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Slack アップロード
# --------------------------------------------------------------------------- #
def _get_slack_token() -> tuple[str, str]:
    """
    Returns: (token, kind_label)
    """
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("[ERROR] SLACK_USER_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    kind_label = "ユーザートークン (xoxp-)" if token.startswith("xoxp-") else "ボットトークン (xoxb-)"
    return token, kind_label


def _reconstruct_minutes_md(held_at: str, kind: str, data: dict) -> str:
    """
    DB から取得した decisions・action_items・minutes_content を Markdown に再構築する。
    元の .md ファイルが削除済みの場合のフォールバック用。
    """
    lines = [f"# {held_at} {kind} 議事録", ""]

    lines.append("## 決定事項")
    lines.append("")
    if data["decisions"]:
        for d in data["decisions"]:
            ctx = f" [出典: {d['source_context']}]" if d.get("source_context") else ""
            lines.append(f"- {d['content']}{ctx}")
    else:
        lines.append("（なし）")
    lines.append("")

    lines.append("## アクションアイテム")
    lines.append("")
    if data["action_items"]:
        for a in data["action_items"]:
            assignee = a.get("assignee") or "未定"
            due = f" (期限: {a['due_date']})" if a.get("due_date") else " (期限: なし)"
            lines.append(f"- [{assignee}] {a['content']}{due}")
    else:
        lines.append("（なし）")
    lines.append("")

    if data.get("minutes_content"):
        lines.append("## 議事内容")
        lines.append("")
        lines.append(data["minutes_content"])
        lines.append("")

    return "\n".join(lines)


def _upload_md_file(client, channel_id: str, md_path: Path | None,
                    held_at: str, kind: str, log,
                    fallback_content: str | None = None,
                    thread_ts: str | None = None) -> str | None:
    """
    .md ファイルを Slack にアップロードする。
    thread_ts 指定時  → スレッドにリプライ投稿（Files タブには表示されない）
    thread_ts 省略時  → チャンネルに直接投稿（Files タブに表示される）
    md_path が存在しない場合は fallback_content を tempfile に書き出してアップロード。
    Returns: ファイルの permalink（str）、スキップ時は None。
    """
    from slack_sdk.errors import SlackApiError

    title = f"{held_at} {kind} 議事録"

    if md_path and md_path.exists():
        upload_path = md_path
        filename = md_path.name
        tmp_to_delete = None
    elif fallback_content:
        log("[INFO] 議事録 .md ファイルが見つかりません。DBから議事録を再構築してアップロードします")
        tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        )
        tf.write(fallback_content)
        tf.close()
        upload_path = Path(tf.name)
        filename = f"{held_at}_{kind}.md"
        tmp_to_delete = upload_path
    else:
        log("[WARN] 議事録 .md ファイルも再構築コンテンツもありません。アップロードをスキップします")
        return None

    dest = f"スレッド {thread_ts}" if thread_ts else f"チャンネル #{channel_id}"
    log(f"[INFO] ファイルをアップロード: {filename} → {dest}")
    kwargs = dict(channel=channel_id, file=str(upload_path), filename=filename, title=title)
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        resp = client.files_upload_v2(**kwargs)
        # レスポンスから permalink を取得（"file" キーが dict またはリストの場合に対応）
        file_obj = resp.get("file") or (resp.get("files") or [None])[0]
        permalink = file_obj.get("permalink") if file_obj else None
    except SlackApiError as e:
        print(f"[ERROR] ファイルアップロード失敗: {e.response['error']}", file=sys.stderr)
        if tmp_to_delete:
            tmp_to_delete.unlink(missing_ok=True)
        sys.exit(1)
    finally:
        if tmp_to_delete:
            tmp_to_delete.unlink(missing_ok=True)
    return permalink


def cmd_post_to_slack(args, minutes_dir: Path, meetings_dir: Path, log) -> None:
    """議事録ファイルを Slack チャンネルにアップロードする"""
    from slack_sdk import WebClient

    db_path = db_path_for_kind(minutes_dir, args.meeting_name)
    if not db_path.exists():
        print(f"[ERROR] 議事録DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = init_minutes_db(db_path, no_encrypt=args.no_encrypt)
    inst = conn.execute(
        "SELECT meeting_id, held_at, kind, file_path,"
        "       posted_to_slack_at, slack_thread_ts, slack_channel_id"
        " FROM instances WHERE held_at = ? AND kind = ?",
        (args.held_at, args.meeting_name),
    ).fetchone()

    if not inst:
        conn.close()
        print(f"[ERROR] {args.held_at} / {args.meeting_name} のレコードが議事録DBに見つかりません。"
              f" pm_minutes_import.py でインポートされているか確認してください。",
              file=sys.stderr)
        sys.exit(1)

    inst = dict(inst)

    # 投稿済みチェック
    if inst.get("posted_to_slack_at") and not args.force:
        thread_info = (f"\n  スレッド TS      : {inst['slack_thread_ts']}"
                       if inst.get("slack_thread_ts") else "")
        print(
            f"[ERROR] {args.held_at} / {args.meeting_name} は既にアップロード済みです。\n"
            f"  アップロード日時 : {inst['posted_to_slack_at']}\n"
            f"  チャンネル       : {inst['slack_channel_id']}"
            f"{thread_info}\n"
            f"再アップロードするには --force を指定してください。",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    # DB から決定事項・AI・議事内容を取得
    meeting_id = inst["meeting_id"]
    mc_row = conn.execute(
        "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1",
        (meeting_id,),
    ).fetchone()
    data = {
        "decisions": [dict(r) for r in conn.execute(
            "SELECT content, source_context FROM decisions WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()],
        "action_items": [dict(r) for r in conn.execute(
            "SELECT content, assignee, due_date FROM action_items WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()],
        "minutes_content": mc_row["content"] if mc_row else None,
    }

    # 常に DB から再構築する（.md ファイルは生の文字起こしの可能性があるため使用しない）
    md_path: Path | None = None
    log("[INFO] 議事録ファイルが見つかりません。DBから再構築します")

    thread_ts = getattr(args, "thread_ts", None)

    if args.dry_run:
        fallback = _reconstruct_minutes_md(args.held_at, args.meeting_name, data)
        dest = f"スレッド {thread_ts}" if thread_ts else f"チャンネル #{args.channel}"
        log("\n" + "=" * 60)
        log(f"[dry-run] アップロード先: {dest}")
        log(f"[dry-run] ファイル名    : {args.held_at}_{args.meeting_name}.md")
        log(f"[dry-run] 再構築コンテンツ先頭:\n{fallback[:400]}...")
        log("=" * 60)
        log("[INFO] --dry-run のため Slack アップロードをスキップしました")
        conn.close()
        return

    token, token_kind = _get_slack_token()
    log(f"[INFO] トークン種別: {token_kind}")
    client = WebClient(token=token)

    fallback = _reconstruct_minutes_md(args.held_at, args.meeting_name, data) if not md_path else None
    permalink = _upload_md_file(client, args.channel, md_path, args.held_at, args.meeting_name, log,
                                fallback_content=fallback, thread_ts=thread_ts)

    if permalink:
        log(f"[INFO] ファイルパーマリンク: {permalink}")
    else:
        log("[WARN] パーマリンクを取得できませんでした")

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE instances SET posted_to_slack_at = ?, slack_channel_id = ?,"
        "  slack_thread_ts = ?, slack_file_permalink = ?"
        " WHERE meeting_id = ?",
        (now, args.channel, thread_ts, permalink, meeting_id),
    )
    conn.commit()
    log(f"[INFO] instances テーブルを更新しました (meeting_id: {meeting_id})")
    conn.close()


if __name__ == "__main__":
    main()
