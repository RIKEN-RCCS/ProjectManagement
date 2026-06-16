#!/usr/bin/env python3
"""
pm_users_sync.py

Slack users.list で全ユーザーを取得し、argus_config.yaml の `user_names:`
セクション（user_id → 表示名）を更新する。
slack.db の messages.user_name にも user_id がそのまま入っているレコードが
多数あるため、表示名の正本を yaml に集約することが目的。

使い方:
  # API で全件取得して更新（既存値は保護）
  source ~/.secrets/slack_tokens.sh
  python3 scripts/pm_users_sync.py

  # 既存値を含めて完全に上書き
  python3 scripts/pm_users_sync.py --force

  # API を使わず slack.db から解決済み user_name だけ反映
  python3 scripts/pm_users_sync.py --from-db

  # 書き込まずに差分だけ表示
  python3 scripts/pm_users_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = REPO_ROOT / "data" / "argus_config.yaml"
SLACK_DB = REPO_ROOT / "data" / "slack.db"


# --------------------------------------------------------------------------- #
# Slack API
# --------------------------------------------------------------------------- #
def _pick_display_name(user: dict) -> str | None:
    """users.list の 1 ユーザーから表示用の名前を選ぶ。"""
    if user.get("deleted") or user.get("is_bot"):
        # bot は表示候補から除外（USLACKBOT は別だが今回は無視）
        return None
    profile = user.get("profile") or {}
    candidates = [
        profile.get("display_name_normalized"),
        profile.get("display_name"),
        profile.get("real_name_normalized"),
        profile.get("real_name"),
        user.get("real_name"),
        user.get("name"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and c.strip():
            return c.strip()
    return None


def fetch_users_via_api() -> dict[str, str]:
    """SLACK_USER_TOKEN で users.list を叩いて user_id → 表示名 dict を返す。"""
    try:
        from slack_sdk import WebClient
    except ImportError:
        print("[ERROR] slack_sdk が見つかりません。pip install slack_sdk", file=sys.stderr)
        sys.exit(1)

    token = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_MCP_XOXB_TOKEN")
    if not token:
        print("[ERROR] SLACK_USER_TOKEN が設定されていません。"
              "source ~/.secrets/slack_tokens.sh してから実行してください。",
              file=sys.stderr)
        sys.exit(1)

    client = WebClient(token=token)
    out: dict[str, str] = {}
    cursor = None
    page = 0
    while True:
        page += 1
        resp = client.users_list(cursor=cursor, limit=200)
        if not resp.get("ok"):
            print(f"[ERROR] users.list 失敗: {resp.get('error')}", file=sys.stderr)
            sys.exit(1)
        members = resp.get("members") or []
        for u in members:
            uid = u.get("id")
            if not uid or not isinstance(uid, str) or not uid.startswith("U"):
                continue
            name = _pick_display_name(u)
            if name:
                out[uid] = name
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
        print(f"[INFO] users.list page={page}: {len(members)} 件 (cumulative valid={len(out)})",
              file=sys.stderr)
        if not cursor:
            break
        # Tier 2 は 20+/min なので 2s 間隔で十分
        time.sleep(2)
    return out


def fetch_users_from_db() -> dict[str, str]:
    """slack.db から user_id → user_name dict を構築（user_name=user_id 行は除外）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from db_utils import open_db  # type: ignore

    out: dict[str, str] = {}
    if not SLACK_DB.exists():
        print(f"[WARN] {SLACK_DB} が存在しません", file=sys.stderr)
        return out
    conn = open_db(SLACK_DB, encrypt=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id, user_name FROM messages"
            " WHERE user_id IS NOT NULL AND user_id LIKE 'U%'"
            "   AND user_name IS NOT NULL AND user_name != ''"
            "   AND user_name != user_id AND user_name NOT LIKE 'U0%'"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        out[r[0]] = r[1]
    return out


# --------------------------------------------------------------------------- #
# yaml の user_names: セクションをテキスト置換で更新
# --------------------------------------------------------------------------- #
_BLOCK_HEADER_RE = re.compile(r"^user_names:[ \t]*$", re.MULTILINE)
# 空マッピング（user_names: {}）も検出
_EMPTY_BLOCK_RE = re.compile(r"^user_names:[ \t]*\{\s*\}[ \t]*$", re.MULTILINE)
# トップレベルキー（行頭が英字＋コロン）の検出
_TOP_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:", re.MULTILINE)


def _parse_existing_block(text: str) -> tuple[dict[str, str], int, int]:
    """既存の user_names: ブロックを (dict, start, end) で返す。
    存在しない場合は ({}, -1, -1)。
    end は当該ブロックの末尾改行位置（次のトップレベルキー直前）。
    """
    m = _EMPTY_BLOCK_RE.search(text)
    if m:
        return {}, m.start(), m.end()
    m = _BLOCK_HEADER_RE.search(text)
    if not m:
        return {}, -1, -1

    block_start = m.start()
    body_start = m.end()
    # body_start 以降から、次のトップレベルキーを探す
    rest = text[body_start:]
    next_top = None
    for tm in _TOP_KEY_RE.finditer(rest):
        # 行頭マッチが必要。ブロックヘッダ自身を再ヒットしないよう offset で進む
        # tm.start() は body_start からの相対位置
        # 「自分の直後の改行直後」から始まるトップレベルキーをすべて拾うため、
        # tm.start() == 0 の場合（ヘッダの直後にいきなり別キーが来るケース）も含めて採用
        next_top = tm
        break
    if next_top is None:
        block_end = len(text)
    else:
        block_end = body_start + next_top.start()

    body = text[body_start:block_end]
    # `  Uxxx: 名前` 形式の行を抽出
    parsed: dict[str, str] = {}
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # キーは U 始まりの英数字
        line_m = re.match(r"^\s+([UW][A-Z0-9]{5,})\s*:\s*(.+?)\s*$", line)
        if not line_m:
            continue
        key = line_m.group(1)
        val = line_m.group(2).strip().strip('"').strip("'")
        if val:
            parsed[key] = val
    return parsed, block_start, block_end


def _format_block(user_names: dict[str, str]) -> str:
    """user_names: ブロックを文字列化する。値はクオートして安全側に倒す。"""
    if not user_names:
        return "user_names: {}\n"
    lines = ["user_names:"]
    for uid in sorted(user_names.keys()):
        name = user_names[uid]
        # ダブルクオートで囲み、内部のダブルクオートはエスケープ
        safe = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  {uid}: "{safe}"')
    return "\n".join(lines) + "\n"


def update_yaml(
    config_path: Path,
    new_users: dict[str, str],
    *,
    force: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    """argus_config.yaml の user_names: ブロックを更新する。

    Returns: (added, updated, kept)
    """
    if not config_path.exists():
        print(f"[ERROR] {config_path} が存在しません", file=sys.stderr)
        sys.exit(1)

    text = config_path.read_text(encoding="utf-8")
    existing, b_start, b_end = _parse_existing_block(text)

    # マージ
    merged = dict(existing)
    added = updated = kept = 0
    for uid, name in new_users.items():
        if uid not in merged:
            merged[uid] = name
            added += 1
        elif force and merged[uid] != name:
            merged[uid] = name
            updated += 1
        else:
            kept += 1

    new_block = _format_block(merged)

    if b_start >= 0:
        # 既存ブロック置換
        new_text = text[:b_start] + new_block + text[b_end:]
    else:
        # 末尾追記（直前に空行を 1 行確保）
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new_text = text + sep + new_block

    print(f"[INFO] 既存 {len(existing)} 件 → 統合後 {len(merged)} 件 "
          f"(新規 {added} / 上書き {updated} / 保持 {kept})", file=sys.stderr)

    if dry_run:
        print("[INFO] --dry-run のため書き込みをスキップしました", file=sys.stderr)
        # 差分表示
        for uid in sorted(set(new_users) - set(existing)):
            print(f"  + {uid}: {new_users[uid]}")
        if force:
            for uid in sorted(set(new_users) & set(existing)):
                if existing[uid] != new_users[uid]:
                    print(f"  ~ {uid}: {existing[uid]!r} -> {new_users[uid]!r}")
        return added, updated, kept

    config_path.write_text(new_text, encoding="utf-8")
    print(f"[INFO] {config_path} を更新しました", file=sys.stderr)
    return added, updated, kept


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(
        description="Slack users.list を叩いて argus_config.yaml の user_names: を更新",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from-db", action="store_true",
                     help="API を使わず slack.db から解決済み user_name を読み込む")
    src.add_argument("--from-api", action="store_true",
                     help="API のみ使う（デフォルト）")
    p.add_argument("--force", action="store_true",
                   help="既存値を上書きする（デフォルトは保護）")
    p.add_argument("--dry-run", action="store_true",
                   help="差分表示のみ。yaml には書き込まない")
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help=f"argus_config.yaml のパス (default: {DEFAULT_CONFIG})")
    args = p.parse_args()

    if args.from_db:
        users = fetch_users_from_db()
        print(f"[INFO] slack.db から {len(users)} 件取得", file=sys.stderr)
    else:
        users = fetch_users_via_api()
        print(f"[INFO] users.list から {len(users)} 件取得", file=sys.stderr)

    if not users:
        print("[WARN] 取得結果が空です", file=sys.stderr)
        return 1

    update_yaml(
        Path(args.config), users,
        force=args.force, dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
