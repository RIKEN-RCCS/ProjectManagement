#!/usr/bin/env python3
"""patrol/audit.py — Patrol Agent の LLM 判定・自動クローズを監査台帳へ記録する

pm.db の tool_calls / reasoning_traces（ハッシュ連鎖付き・追記専用の監査台帳、
docs/security-architecture.md §4.4）へ Patrol の判定内容を残す。

Patrol は pm_argus_agent.py（ThreadPool 並列）と違って**単一スレッド**で動作する。
そのため `ctx.conn`（PatrolContext.conn プロパティ、実体は conns[0]）が既に
開いていればそれをそのまま使い、別接続は開かない。`db_utils.record_tool_call` は
`conn.in_transaction` が True のときは自前で BEGIN/commit をせず、呼び出し側の
未コミットのトランザクションに相乗りする設計になっており、この経路を使う。

**設計上の代償**（実測で確認済み。`tests/argus/test_patrol_audit.py` 参照）:
`ctx.conn` に相乗りするため、**監査行は呼び出し側が commit するまで永続化されない**。
つまり `close_action_item` のような書き込みの直後、呼び出し元がまだ commit して
いない間は、対象の変更（例: action_items の status）も監査行（tool_calls）も
別接続からは見えない。呼び出し側が commit した時点で両方が同じコミット境界で
一緒に確定する。Patrol は `run_patrol()` の末尾（および detect.py 側の
`close_action_item` 呼び出し後）で `conn.commit()` するため実運用では問題にならない。
代償として、**巻き戻された操作（例外で処理を打ち切った場合）の監査行は
残らない可能性がある** — Patrol には実質的な巻き戻し経路が無く（検出器内の
例外は握って処理を継続し、巡回の区切りでまとめて commit する）ため、これは許容する。
別接続で毎回開閉する方式も試したが、`ctx.conn` に未コミットの書き込みが残った
まま同一ファイルへ `BEGIN IMMEDIATE` すると busy_timeout 満了で恒久的に失敗する
ことを実測で確認した（fail-open のため例外は出ないが、記録が一切残らない）。
**記録されない監査より、呼び出し側と運命を共にする監査のほうがましである。**

`ctx.conn` が None の場合（単体テスト等、conns が空のとき）だけ、従来どおり
`ctx.audit_db`（省略時 `ctx.data_dir / "pm.db"`）を開き、使い終わったら閉じる。

**循環 import に注意**: pm_argus_patrol.py は module レベルで patrol.detect を
import しているため、このモジュールから pm_argus_patrol / patrol.detect /
patrol.actions を import してはならない（型注釈のみ TYPE_CHECKING で参照する）。
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pm_argus_patrol import PatrolContext

logger = logging.getLogger("argus_patrol")

# 単一スレッド前提が崩れた場合の保険として残す（pm_argus_agent.py と同じ作法）。
_AUDIT_LOCK = threading.Lock()


def record_call(
    ctx: PatrolContext, tool_name: str, args: dict, outcome: str, *,
    plane: str = "read", result: str | None = None,
    block_reason: str | None = None,
    reasoning_sha256: str | None = None,
) -> None:
    """Patrol の判定・アクションを pm.db の tool_calls に追記する（§4.4）。

    `ctx.session_id` が空なら何もしない（dry-run 等の退避路）。
    **記録の失敗は本番処理を止めない（fail-open）。** ただし例外は必ずログに出す
    （P6：静かに失敗させない）。
    """
    if not ctx.session_id:
        return
    try:
        from db_utils import open_db, record_tool_call

        with _AUDIT_LOCK:
            ctx.tool_seq += 1
            conn = getattr(ctx, "conn", None)
            owns_conn = conn is None
            if owns_conn:
                db_path = ctx.audit_db or (ctx.data_dir / "pm.db")
                conn = open_db(db_path, encrypt=True)
            try:
                record_tool_call(
                    conn, session_id=ctx.session_id, seq=ctx.tool_seq,
                    plane=plane, tool_name=tool_name, args=args,
                    outcome=outcome, result=result, block_reason=block_reason,
                    model=ctx.model, model_revision=ctx.model_revision,
                    reasoning_sha256=reasoning_sha256,
                )
            finally:
                # ctx.conn の所有権は呼び出し元にある。close() してはならない。
                if owns_conn:
                    conn.close()
    except Exception:
        logger.exception(
            "[AUDIT] tool_calls への記録に失敗しました（実行は継続）: %s", tool_name
        )


def record_reasoning(ctx: PatrolContext, step: int, trace: str) -> str | None:
    """思考トレースを reasoning_traces に保存し sha256 を返す（§4.4）。

    `ctx.session_id` が空、または trace が空なら何もしない。
    **記録の失敗は本番処理を止めない（fail-open）。**
    セッションの最初のステップで保持期間を過ぎた分を掃除する（既定90日）。
    """
    if not ctx.session_id or not trace:
        return None
    try:
        from db_utils import open_db, purge_reasoning_traces, record_reasoning_trace

        with _AUDIT_LOCK:
            conn = getattr(ctx, "conn", None)
            owns_conn = conn is None
            if owns_conn:
                db_path = ctx.audit_db or (ctx.data_dir / "pm.db")
                conn = open_db(db_path, encrypt=True)
            try:
                sha = record_reasoning_trace(
                    conn, session_id=ctx.session_id, step=step, trace=trace,
                    model=ctx.model, model_revision=ctx.model_revision,
                )
                if step <= 1:
                    n = purge_reasoning_traces(conn)
                    if n:
                        logger.info(
                            "[AUDIT] 保持期間切れの思考トレースを %d 件削除しました", n
                        )
                return sha
            finally:
                # ctx.conn の所有権は呼び出し元にある。close() してはならない。
                if owns_conn:
                    conn.close()
    except Exception:
        logger.exception("[AUDIT] reasoning_traces への記録に失敗しました（実行は継続）")
        return None
