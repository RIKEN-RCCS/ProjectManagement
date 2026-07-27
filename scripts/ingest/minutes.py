#!/usr/bin/env python3
"""
ingest_minutes.py

議事録DB（data/minutes/{kind}.db）→ pm.db への転記プラグイン。
pm_ingest.py minutes 経由で呼び出される。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_utils import normalize_assignee, open_db

from ingest.ingest_plugin import IngestContext

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MINUTES_DIR = REPO_ROOT / "data" / "minutes"


# --------------------------------------------------------------------------- #
# 議事録 DB 接続
# --------------------------------------------------------------------------- #
def init_minutes_db(db_file: Path, no_encrypt: bool = False):
    from pm_minutes_import import init_minutes_db as _init
    return _init(db_file, no_encrypt=no_encrypt)


# --------------------------------------------------------------------------- #
# 転記コア
# --------------------------------------------------------------------------- #
def _write_triage_audit(pm_conn, table_name: str, record_id: int, reason: str) -> None:
    """minutes_triage による deleted=1 挿入の証跡を audit_log に2行記録する。

    audit_log の changed_at は他の書き手（pm_relink.py 等）と同様に UTC aware で
    記録する（naive local との混在を避ける）。
    """
    now = datetime.now(UTC).isoformat()
    pm_conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, 'deleted', '0', '1', ?, 'minutes_triage')",
        (table_name, str(record_id), now),
    )
    pm_conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, 'triage_reason', NULL, ?, ?, 'minutes_triage')",
        (table_name, str(record_id), reason, now),
    )


def _write_human_kept_audit(pm_conn, table_name: str, record_id: int) -> None:
    """force 再転記で human_kept 項目を再INSERTした直後に、新しい record_id に対して
    「人間による復元」の証跡を audit_log に記録する。

    force 再転記は対象行を DELETE→INSERT するため record_id（連番）が変わる。
    human_kept セットの収集クエリは audit_log.record_id を旧INSERT行のIDで
    突合しているため、この記録を打たないと次回の force 再転記時に
    human_kept として認識されず、人間の復元判断が再び LLM の審査対象に
    戻ってしまう（= 保護が1回の force しか持続しない）。
    source は 'minutes_triage' 以外にする必要があるため 'minutes_human_kept' を使う
    （既存の human_kept 収集クエリの `source != 'minutes_triage'` 条件でそのまま拾われる）。
    """
    now = datetime.now(UTC).isoformat()
    pm_conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field, old_value, new_value, changed_at, source)"
        " VALUES (?, ?, 'deleted', '1', '0', ?, 'minutes_human_kept')",
        (table_name, str(record_id), now),
    )


def _run_minutes_triage(
    pm_conn,
    decisions: list,
    action_items: list,
    deleted_decisions: set,
    deleted_actions: set,
    human_kept_decisions: set,
    human_kept_actions: set,
    force: bool,
    kind: str,
    held_at: str,
    mc_row,
    meeting_id: str,
    log=print,
) -> dict[str, dict[str, tuple[str, str]]]:
    """議事録DBから取得した decisions/action_items を3ゲートでトリアージする。

    force 時は以下をトリアージ対象から除外する（無駄な再審査を避ける・人間の判断を
    LLM が覆さない）:
      - deleted セット: 既にユーザーが手動削除済みの候補（再挿入自体もしない）
      - human_kept セット: 人間が Web UI 等で deleted=0 に復元した候補
        （再挿入はするが、LLM には諮らず無条件 KEEP とする）

    マイルストーンが未登録の場合、ゲート1（マイルストーン関連性）で理論上
    ほぼ全件 DROP されてしまうため、トリアージ自体をスキップして全件 KEEP 扱いにする。

    戻り値: {"decisions": {content: (verdict, reason)}, "action_items": {content: (verdict, reason)}}
    失敗時は空dict（呼び出し側は KEEP フォールバックとして扱う）。
    """
    d_candidates = [
        dict(d) for d in decisions
        if not (force and (d["content"] in deleted_decisions or d["content"] in human_kept_decisions))
    ]
    a_candidates = [
        dict(a) for a in action_items
        if not (force and (a["content"] in deleted_actions or a["content"] in human_kept_actions))
    ]
    if not d_candidates and not a_candidates:
        return {"decisions": {}, "action_items": {}}

    try:
        from ingest.slack import fetch_milestones, triage_items_batched

        milestones = fetch_milestones(pm_conn)
        if not milestones:
            log("  [WARN] マイルストーン未登録のためトリアージをスキップします（全件 KEEP 扱い）")
            return {"decisions": {}, "action_items": {}}

        content_1500 = ((mc_row["content"][:1500] if mc_row else "") or "")
        context_note = (
            "### 会議コンテキスト\n"
            f"会議種別: {kind} / 開催日: {held_at}\n"
            f"議事概要: {content_1500}"
        )
        batched = triage_items_batched(
            a_candidates, d_candidates, milestones,
            context_note=context_note,
            missing_verdict="KEEP",
            log=log,
            group_label=f"meeting={meeting_id}",
        )
    except Exception as e:
        log(f"  [WARN] 転記時トリアージに失敗、全件 KEEP で継続します: {e}")
        return {"decisions": {}, "action_items": {}}

    if batched["n_skipped_chunks"]:
        log(
            f"  [WARN] meeting={meeting_id}: {batched['n_skipped_chunks']}/{batched['n_chunks']} "
            "チャンクがLLM障害でスキップされました（該当項目は KEEP 扱い）"
        )

    verdicts_d: dict[str, tuple[str, str]] = {
        item["content"]: (verdict, reason) for item, verdict, reason in batched["decisions"]
    }
    verdicts_a: dict[str, tuple[str, str]] = {
        item["content"]: (verdict, reason) for item, verdict, reason in batched["action_items"]
    }
    return {"decisions": verdicts_d, "action_items": verdicts_a}


def transfer_meeting(
    pm_conn,
    minutes_conn,
    meeting_id: str,
    held_at: str,
    kind: str,
    file_path: str | None,
    force: bool,
    dry_run: bool,
    log=print,
    *,
    triage: bool = True,
) -> str:
    """Returns: "ok" | "skipped"

    重複判定は meeting_id 単位で行う（held_at/kind 単位ではない）。再生成等で
    同じ日付・種別の会議が新しい meeting_id で minutes.db に追加された場合、
    (held_at, kind) 単位で判定すると「既にある」と誤ってスキップしてしまい、
    しかもスキップは正常終了扱いのため気付きにくい（2026-07-03 に実際に発生し、
    65件中6件が無言でスキップされていた。LOG.md 参照）。

    triage=True（既定）の場合、pm.db へのINSERT前に3ゲートトリアージ
    （ingest.slack.triage_items_batched、20件ずつバッチ分割）を実行し、DROP判定の
    項目は deleted=1 で INSERTする（内容は保持、audit_log に記録）。環境変数
    ARGUS_DISABLE_MINUTES_TRIAGE=1 で無効化できる。マイルストーン未登録時・
    トリアージ自体の失敗はフェイルオープン（全件 KEEP）。--force 再転記時は
    人間が Web UI で deleted=0 に復元した項目もトリアージ対象から除外し
    無条件 KEEP のまま再挿入する。
    """
    existing = pm_conn.execute(
        "SELECT meeting_id FROM meetings WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()

    if existing and not force:
        log(f"  [SKIP] {meeting_id} は既に pm.db に存在します（--force で上書き可能）")
        return "skipped"

    # 同一 (held_at, kind) の別 meeting_id が残っている場合の後始末。
    # 内容が空（decisions/action_items 共に0件）なら失敗インポートの残骸とみなし
    # 自動削除する。内容がある場合は誤って実データを消さないよう警告のみに留める
    # （人手での判断・pm_relink.py 等での整理を促す）。
    if not dry_run:
        stale_rows = pm_conn.execute(
            "SELECT meeting_id FROM meetings WHERE held_at = ? AND kind = ? AND meeting_id != ?",
            (held_at, kind, meeting_id),
        ).fetchall()
        for stale in stale_rows:
            stale_id = stale["meeting_id"]
            # COALESCE(deleted,0)=0 の件数のみ数える。トリアージで全件 DROP された
            # 会議（deleted=1 のみ残る）を「内容を保持したまま残っています」と
            # 誤警告しないため。
            d_count = pm_conn.execute(
                "SELECT COUNT(*) c FROM decisions WHERE meeting_id = ? AND COALESCE(deleted,0)=0",
                (stale_id,),
            ).fetchone()["c"]
            a_count = pm_conn.execute(
                "SELECT COUNT(*) c FROM action_items WHERE meeting_id = ? AND COALESCE(deleted,0)=0",
                (stale_id,),
            ).fetchone()["c"]
            if d_count == 0 and a_count == 0:
                pm_conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (stale_id,))
                log(f"  [CLEANUP] 同一日付・種別の空の旧レコードを削除: {stale_id}")
            else:
                log(
                    f"  [WARN] 同一日付・種別の別レコードが内容を保持したまま残っています: "
                    f"{stale_id}（decisions={d_count}, action_items={a_count}）。"
                    "重複の可能性があるため手動確認を推奨します"
                )

    mc_row = minutes_conn.execute(
        "SELECT content FROM minutes_content WHERE meeting_id = ? LIMIT 1", (meeting_id,)
    ).fetchone()
    summary = (mc_row["content"][:500] if mc_row else "") or ""

    decisions = minutes_conn.execute(
        "SELECT content, source_context, rationale, trade_off, reversal_condition"
        " FROM decisions WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()

    action_items = minutes_conn.execute(
        "SELECT content, assignee, due_date FROM action_items WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()

    log(f"  decisions   : {len(decisions)} 件")
    for d in decisions:
        tags = ""
        if d["source_context"]:
            tags += f" [出典: {d['source_context']}]"
        if d["rationale"]:
            tags += f" [根拠: {d['rationale']}]"
        if d["trade_off"]:
            tags += f" [捨てた案: {d['trade_off']}]"
        if d["reversal_condition"]:
            tags += f" [覆す条件: {d['reversal_condition']}]"
        log(f"    - {d['content']}{tags}")
    log(f"  action_items: {len(action_items)} 件")
    for a in action_items:
        assignee = a["assignee"] or "未定"
        due = f" (期限: {a['due_date']})" if a["due_date"] else ""
        log(f"    [{assignee}] {a['content']}{due}")

    # 手動削除(deleted=1)されたレコードは残し、それ以外を削除してからINSERTする。
    # 削除済みレコードの内容を収集しておき、同一内容の再INSERTを防ぐ
    # （--force 再転記時のみ意味を持つが、トリアージ除外判定にも使うため
    # dry_run でも force なら先に集めておく）。
    #
    # human_kept: 人間が Web UI 等で deleted=0 に復元した（= minutes_triage 以外の
    # ソースで deleted→0 の変更履歴がある）候補。force 再転記時に再挿入はするが、
    # LLM には諮らず無条件 KEEP とする（人間の最終判断を LLM が覆さないため。S1）。
    deleted_decisions: set = set()
    deleted_actions: set = set()
    human_kept_decisions: set = set()
    human_kept_actions: set = set()
    if force:
        for row in pm_conn.execute(
            "SELECT content FROM decisions WHERE meeting_id = ? AND COALESCE(deleted,0)=1",
            (meeting_id,),
        ).fetchall():
            deleted_decisions.add(row["content"])
        for row in pm_conn.execute(
            "SELECT content FROM action_items WHERE meeting_id = ? AND COALESCE(deleted,0)=1",
            (meeting_id,),
        ).fetchall():
            deleted_actions.add(row["content"])

        for row in pm_conn.execute(
            "SELECT d.content FROM decisions d"
            " JOIN audit_log al ON al.table_name='decisions' AND al.record_id = CAST(d.id AS TEXT)"
            " WHERE d.meeting_id = ? AND al.field='deleted' AND al.new_value='0'"
            " AND (al.source IS NULL OR al.source != 'minutes_triage')",
            (meeting_id,),
        ).fetchall():
            human_kept_decisions.add(row["content"])
        for row in pm_conn.execute(
            "SELECT a.content FROM action_items a"
            " JOIN audit_log al ON al.table_name='action_items' AND al.record_id = CAST(a.id AS TEXT)"
            " WHERE a.meeting_id = ? AND al.field='deleted' AND al.new_value='0'"
            " AND (al.source IS NULL OR al.source != 'minutes_triage')",
            (meeting_id,),
        ).fetchall():
            human_kept_actions.add(row["content"])

    triage_enabled = triage and os.environ.get("ARGUS_DISABLE_MINUTES_TRIAGE") != "1"
    triage_verdicts: dict[str, dict[str, tuple[str, str]]] = {"decisions": {}, "action_items": {}}
    if triage_enabled and (decisions or action_items):
        triage_verdicts = _run_minutes_triage(
            pm_conn, decisions, action_items, deleted_decisions, deleted_actions,
            human_kept_decisions, human_kept_actions,
            force, kind, held_at, mc_row, meeting_id, log=log,
        )

    if dry_run:
        for d in decisions:
            if force and d["content"] in deleted_decisions:
                continue
            verdict, reason = triage_verdicts["decisions"].get(d["content"], ("KEEP", ""))
            if verdict == "DROP":
                log(f"    [TRIAGE] DROP decision: {d['content'][:80]}… — 理由: {reason}")
        for a in action_items:
            if force and a["content"] in deleted_actions:
                continue
            verdict, reason = triage_verdicts["action_items"].get(a["content"], ("KEEP", ""))
            if verdict == "DROP":
                log(f"    [TRIAGE] DROP action_item: {a['content'][:80]}… — 理由: {reason}")
        return "ok"

    now = datetime.now().isoformat()
    source_ref = file_path or ""

    if force:
        pm_conn.execute(
            "DELETE FROM decisions WHERE meeting_id = ? AND COALESCE(deleted,0)=0",
            (meeting_id,),
        )
        pm_conn.execute(
            "DELETE FROM action_items WHERE meeting_id = ? AND COALESCE(deleted,0)=0",
            (meeting_id,),
        )

    pm_conn.execute(
        "INSERT OR IGNORE INTO meetings (meeting_id, held_at, kind, file_path, summary, parsed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (meeting_id, held_at, kind, source_ref, summary, now),
    )

    for d in decisions:
        if force and d["content"] in deleted_decisions:
            log(f"    [SKIP] 削除済みの決定事項をスキップ: {d['content'][:60]}")
            continue
        verdict, reason = triage_verdicts["decisions"].get(d["content"], ("KEEP", ""))
        deleted_flag = 1 if verdict == "DROP" else 0
        cur = pm_conn.execute(
            "INSERT INTO decisions"
            " (meeting_id, content, decided_at, source, source_ref, source_context, extracted_at,"
            " rationale, trade_off, reversal_condition, deleted)"
            " VALUES (?, ?, ?, 'meeting', ?, ?, ?, ?, ?, ?, ?)",
            (meeting_id, d["content"], held_at, source_ref, d["source_context"], held_at,
             d["rationale"], d["trade_off"], d["reversal_condition"], deleted_flag),
        )
        if deleted_flag:
            log(f"    [TRIAGE] DROP decision（deleted=1で登録): {d['content'][:80]}… — 理由: {reason}")
            _write_triage_audit(pm_conn, "decisions", cur.lastrowid, reason)
        elif force and d["content"] in human_kept_decisions:
            # human_kept 保護を新しい record_id に付け替える（force は DELETE→INSERT
            # で id が変わるため、旧 id を指した audit_log では次回 force で保護が
            # 効かなくなる）
            _write_human_kept_audit(pm_conn, "decisions", cur.lastrowid)

    for a in action_items:
        if force and a["content"] in deleted_actions:
            log(f"    [SKIP] 削除済みのアクションアイテムをスキップ: {a['content'][:60]}")
            continue
        verdict, reason = triage_verdicts["action_items"].get(a["content"], ("KEEP", ""))
        deleted_flag = 1 if verdict == "DROP" else 0
        cur = pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref, extracted_at, deleted)"
            " VALUES (?, ?, ?, ?, 'open', 'meeting', ?, ?, ?)",
            (meeting_id, a["content"], normalize_assignee(a["assignee"]), a["due_date"],
             source_ref, held_at, deleted_flag),
        )
        if deleted_flag:
            log(f"    [TRIAGE] DROP action_item（deleted=1で登録): {a['content'][:80]}… — 理由: {reason}")
            _write_triage_audit(pm_conn, "action_items", cur.lastrowid, reason)
        elif force and a["content"] in human_kept_actions:
            _write_human_kept_audit(pm_conn, "action_items", cur.lastrowid)

    pm_conn.commit()
    return "ok"


# --------------------------------------------------------------------------- #
# 1つの議事録 DB を処理
# --------------------------------------------------------------------------- #
def process_minutes_db(
    db_file: Path,
    pm_conn,
    since: str | None,
    force: bool,
    dry_run: bool,
    no_encrypt: bool,
    meeting_id_filter: str | None = None,
    log=print,
    triage: bool = True,
) -> tuple[int, int]:
    """Returns: (ok_count, skipped_count)"""
    kind = db_file.stem

    try:
        minutes_conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
    except Exception as e:
        log(f"[ERROR] DB接続失敗: {db_file}: {e}")
        return 0, 0

    query = "SELECT meeting_id, held_at, file_path FROM instances"
    params: list = []
    wheres: list = []
    if since:
        wheres.append("held_at >= ?")
        params.append(since)
    if meeting_id_filter:
        wheres.append("meeting_id = ?")
        params.append(meeting_id_filter)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " ORDER BY held_at"

    instances = minutes_conn.execute(query, params).fetchall()
    minutes_conn.close()

    ok = skipped = 0
    for inst in instances:
        meeting_id = inst["meeting_id"]
        held_at    = inst["held_at"]
        file_path  = inst["file_path"]

        log(f"\n[{kind}] {meeting_id} ({held_at})")

        minutes_conn = init_minutes_db(db_file, no_encrypt=no_encrypt)
        status = transfer_meeting(
            pm_conn, minutes_conn, meeting_id, held_at, kind, file_path,
            force=force, dry_run=dry_run, log=log,
            triage=triage,
        )
        minutes_conn.close()

        if status == "ok":
            ok += 1
        else:
            skipped += 1

    return ok, skipped


# --------------------------------------------------------------------------- #
# pm.db 削除
# --------------------------------------------------------------------------- #
def delete_from_pm(pm_conn, meeting_id: str, dry_run: bool) -> None:
    existing = pm_conn.execute(
        "SELECT meeting_id, held_at, kind FROM meetings WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()

    if not existing:
        print(f"[ERROR] meeting_id '{meeting_id}' が pm.db に見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 削除対象: {meeting_id} ({existing['held_at']}, {existing['kind']})")

    if dry_run:
        print("[INFO] --dry-run のため削除をスキップしました")
        return

    pm_conn.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    pm_conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
    pm_conn.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
    pm_conn.commit()
    print(f"[INFO] {meeting_id} を pm.db から削除しました")


# --------------------------------------------------------------------------- #
# pm.db 一覧表示
# --------------------------------------------------------------------------- #
def list_pm(db_path: Path, kind_filter: str | None, since: str | None, no_encrypt: bool) -> None:
    if not db_path.exists():
        print(f"[ERROR] pm.db が見つかりません: {db_path}", file=sys.stderr)
        return

    conn = open_db(db_path, encrypt=not no_encrypt)

    query = """
        SELECT
            m.meeting_id,
            m.held_at,
            m.kind,
            m.parsed_at,
            COUNT(DISTINCT d.id)  AS d_count,
            COUNT(DISTINCT a.id)  AS ai_count
        FROM meetings m
        LEFT JOIN decisions    d ON d.meeting_id = m.meeting_id AND d.source = 'meeting'
        LEFT JOIN action_items a ON a.meeting_id = m.meeting_id AND a.source = 'meeting'
    """
    params: list = []
    wheres: list = []
    if kind_filter:
        wheres.append("m.kind = ?")
        params.append(kind_filter)
    if since:
        wheres.append("m.held_at >= ?")
        params.append(since)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " GROUP BY m.meeting_id ORDER BY m.held_at DESC, m.kind"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("（該当するレコードなし）")
        return

    print(f"{'開催日':<12} {'会議名':<25} {'決定数':>5} {'AI数':>5}  {'登録日時':<19}  meeting_id")
    print("-" * 95)
    for r in rows:
        parsed_at = (r["parsed_at"] or "")[:19]
        print(
            f"{r['held_at']:<12} {(r['kind'] or ''):<25} {r['d_count']:>5} {r['ai_count']:>5}"
            f"  {parsed_at:<19}  {r['meeting_id']}"
        )
    print(f"\n合計: {len(rows)} 件")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class MinutesIngestPlugin:
    source_name = "minutes"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--minutes-name", default=None,
            metavar="NAME",
            help="特定の会議名のみ処理（minutes ソース用、省略時は全DBを対象）",
        )
        parser.add_argument(
            "--minutes-dir", default=None,
            metavar="DIR",
            help="議事録DBのディレクトリ（minutes ソース用、デフォルト: data/minutes/）",
        )
        parser.add_argument(
            "--minutes-force", action="store_true",
            help="既存レコードを上書き（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-list", action="store_true",
            help="pm.db の転記済み会議一覧を表示して終了（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-delete", default=None,
            metavar="MEETING_ID",
            help="指定した meeting_id を pm.db から削除して終了（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-meeting-id", default=None,
            metavar="MEETING_ID",
            help="特定の meeting_id のみ転記（minutes ソース用）",
        )
        parser.add_argument(
            "--minutes-no-triage", action="store_true",
            help="転記時トリアージ（抽出候補の3ゲート審査）を無効化（デフォルト: 有効）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        minutes_dir = (
            Path(args.minutes_dir) if getattr(args, "minutes_dir", None)
            else DEFAULT_MINUTES_DIR
        )

        if getattr(args, "minutes_list", False):
            list_pm(ctx.pm_db_path, getattr(args, "minutes_name", None), ctx.since, ctx.no_encrypt)
            return

        if getattr(args, "minutes_delete", None):
            delete_from_pm(ctx.pm_conn, args.minutes_delete, ctx.dry_run)
            return

        if not minutes_dir.exists():
            print(f"ERROR: 議事録DBディレクトリが見つかりません: {minutes_dir}", file=sys.stderr)
            sys.exit(1)

        meeting_name = getattr(args, "minutes_name", None)
        if meeting_name:
            safe = re.sub(r"[^\w\-]", "_", meeting_name)
            db_files = [minutes_dir / f"{safe}.db"]
            if not db_files[0].exists():
                print(f"ERROR: 議事録DBが見つかりません: {db_files[0]}", file=sys.stderr)
                sys.exit(1)
        else:
            db_files = sorted(minutes_dir.glob("*.db"))
            if not db_files:
                ctx.log("[INFO] 議事録DBが見つかりません。処理を終了します。")
                return

        ctx.log(f"[INFO] 議事録DB   : {minutes_dir}")
        ctx.log(f"[INFO] 対象DB数   : {len(db_files)} 件")
        if ctx.since:
            ctx.log(f"[INFO] since      : {ctx.since}")
        if ctx.dry_run:
            ctx.log("[INFO] --dry-run モード（DB保存なし）")
        force = ctx.force or getattr(args, "minutes_force", False)
        meeting_id_filter = getattr(args, "minutes_meeting_id", None)
        triage = not getattr(args, "minutes_no_triage", False)
        if force:
            ctx.log("[INFO] --force モード（既存レコードを上書き）")
        if meeting_id_filter:
            ctx.log(f"[INFO] meeting_id: {meeting_id_filter} のみ処理")
        if not triage:
            ctx.log("[INFO] トリアージ無効: --minutes-no-triage")

        total_ok = total_skipped = 0
        for db_file in db_files:
            ctx.log(f"\n{'='*60}")
            ctx.log(f"  会議名: {db_file.stem}")
            ctx.log(f"{'='*60}")
            ok, skipped = process_minutes_db(
                db_file, ctx.pm_conn,
                since=ctx.since, force=force, dry_run=ctx.dry_run,
                no_encrypt=ctx.no_encrypt, log=ctx.log,
                meeting_id_filter=meeting_id_filter,
                triage=triage,
            )
            total_ok      += ok
            total_skipped += skipped

        ctx.log(f"\n完了: 転記={total_ok}件, スキップ={total_skipped}件")
        if ctx.dry_run:
            ctx.log("（--dry-run のため実際には保存されていません）")
