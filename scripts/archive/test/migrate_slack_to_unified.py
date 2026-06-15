#!/usr/bin/env python3
"""
migrate_slack_to_unified.py — data/C*.db を data/slack.db に統合する一回限りのマイグレーション。

各 C{channel_id}.db に分散していた messages / replies / summaries テーブルを
data/slack.db にマージする。重複は (thread_ts, channel_id) / (msg_ts, channel_id)
で INSERT OR IGNORE。元 DB は --rename を指定すれば .bak にリネーム、
--remove-source で削除（非推奨）。

Usage:
  # 件数確認のみ
  python3 scripts/migrate_slack_to_unified.py --dry-run

  # マージ実行（元DBはそのまま残す）
  python3 scripts/migrate_slack_to_unified.py

  # マージ後に元 DB を .bak にリネーム
  python3 scripts/migrate_slack_to_unified.py --rename

  # 平文DBの場合
  python3 scripts/migrate_slack_to_unified.py --no-encrypt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_utils import open_db


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    thread_ts   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    user_id     TEXT,
    user_name   TEXT,
    text        TEXT,
    timestamp   TEXT,
    permalink   TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (thread_ts, channel_id)
);

CREATE TABLE IF NOT EXISTS replies (
    msg_ts      TEXT NOT NULL,
    thread_ts   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    user_id     TEXT,
    user_name   TEXT,
    text        TEXT,
    timestamp   TEXT,
    permalink   TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (msg_ts, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_replies_channel  ON replies(channel_id);
CREATE INDEX IF NOT EXISTS idx_replies_thread   ON replies(thread_ts, channel_id);
"""


def merge_table(src, dst, table: str, columns: list[str]) -> tuple[int, int, int]:
    """src.table → dst.table に INSERT OR IGNORE。
    return (read_n, inserted_n, skipped_n)
    """
    cols_csv = ", ".join(columns)
    placeholders = ", ".join("?" * len(columns))
    rows = src.execute(f"SELECT {cols_csv} FROM {table}").fetchall()
    read_n = len(rows)
    inserted_n = 0
    for r in rows:
        cur = dst.execute(
            f"INSERT OR IGNORE INTO {table} ({cols_csv}) VALUES ({placeholders})",
            tuple(r[c] for c in columns),
        )
        inserted_n += cur.rowcount or 0
    return read_n, inserted_n, read_n - inserted_n


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data",
                        help="ソース DB のディレクトリ（デフォルト: data）")
    parser.add_argument("--target", default="data/slack.db",
                        help="統合先 DB ファイル（デフォルト: data/slack.db）")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="平文モード（src/dst 両方に適用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB 書き込みなし・件数のみ表示")
    parser.add_argument("--rename", action="store_true",
                        help="マージ完了後にソース DB を .bak にリネーム")
    parser.add_argument("--remove-source", action="store_true",
                        help="マージ完了後にソース DB を削除（非推奨）")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    target_path = Path(args.target).resolve()
    sources = sorted(data_dir.glob("C*.db"))

    if not sources:
        print(f"[ERROR] {data_dir} に C*.db が見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"=== Slack DB 統合: {len(sources)} 個 → {target_path} ===")
    if args.dry_run:
        print("(--dry-run: 書き込みは行いません)")

    # 統合先 DB を作成（dry-run 時はメモリ DB にする）
    if args.dry_run:
        # Just count — open each source briefly
        total_msgs = 0
        total_replies = 0
        for src_path in sources:
            try:
                src = open_db(src_path, encrypt=not args.no_encrypt)
                m = src.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                r = src.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
                total_msgs += m
                total_replies += r
                print(f"  {src_path.name}: messages={m}, replies={r}")
                src.close()
            except Exception as e:
                print(f"  {src_path.name}: ERROR {e}")
        print(f"\n[DRY] 合計: messages={total_msgs}, replies={total_replies}")
        return

    dst = open_db(target_path, encrypt=not args.no_encrypt, schema=SCHEMA)

    # Track running totals for sanity reporting
    grand_msg = grand_msg_ins = 0
    grand_rep = grand_rep_ins = 0
    bad_dbs: list[tuple[str, str]] = []

    msg_cols = ["thread_ts", "channel_id", "user_id", "user_name",
                "text", "timestamp", "permalink", "fetched_at"]
    rep_cols = ["msg_ts", "thread_ts", "channel_id", "user_id", "user_name",
                "text", "timestamp", "permalink", "fetched_at"]

    for src_path in sources:
        try:
            src = open_db(src_path, encrypt=not args.no_encrypt)
        except Exception as e:
            bad_dbs.append((src_path.name, str(e)))
            print(f"  [SKIP] {src_path.name}: 開けません ({e})")
            continue

        try:
            mr, mi, ms = merge_table(src, dst, "messages", msg_cols)
            rr, ri, rs = merge_table(src, dst, "replies", rep_cols)
            grand_msg += mr; grand_msg_ins += mi
            grand_rep += rr; grand_rep_ins += ri
            dst.commit()
            print(f"  {src_path.name}: messages {mi}/{mr} (skip dup {ms}),"
                  f" replies {ri}/{rr} (skip dup {rs})")
        except Exception as e:
            bad_dbs.append((src_path.name, f"merge: {e}"))
            print(f"  [ERROR] {src_path.name}: マージ失敗 ({e})")
            src.close()
            continue
        finally:
            src.close()

        # Rename / remove if requested
        if args.rename:
            bak = src_path.with_suffix(src_path.suffix + ".bak")
            if bak.exists():
                # 既にあれば連番で避ける
                i = 2
                while bak.with_suffix(f".bak{i}").exists():
                    i += 1
                bak = bak.with_suffix(f".bak{i}")
            src_path.rename(bak)
            print(f"    → {bak.name} にリネーム")
        elif args.remove_source:
            src_path.unlink()
            print(f"    → 削除")

    # Summary
    print()
    print("=== 集計 ===")
    print(f"  messages: 読み込み {grand_msg}, INSERT {grand_msg_ins} (重複スキップ {grand_msg - grand_msg_ins})")
    print(f"  replies:  読み込み {grand_rep}, INSERT {grand_rep_ins} (重複スキップ {grand_rep - grand_rep_ins})")
    final_m = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    final_r = dst.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    print(f"  slack.db 最終: messages={final_m}, replies={final_r}")

    if bad_dbs:
        print()
        print(f"[WARN] {len(bad_dbs)} 個の DB で問題:")
        for name, msg in bad_dbs:
            print(f"  - {name}: {msg}")

    dst.close()


if __name__ == "__main__":
    main()
