#!/usr/bin/env python3
"""
patrol_confirm.py — Block Kit ボタンハンドラ（承認/却下）

pm_qa_server.py の app.action() から呼ばれる。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patrol_state import PatrolState

logger = logging.getLogger(__name__)


def handle_approve_close(
    body: dict,
    client,
    state: PatrolState,
    conn: sqlite3.Connection,
) -> None:
    """
    「完了にする」ボタン押下時のハンドラ。

    1. pending_confirmations を 'approved' に更新
    2. action_items.status を 'closed' に更新（audit_log 付き）
    3. 元メッセージを更新（ボタンを削除して完了テキストに置換）
    """
    from pm_sync_canvas import write_audit_log

    action = _extract_action(body)
    if not action:
        return

    pending_id = int(action["value"])
    user_id = body.get("user", {}).get("id", "unknown")

    pending = state.get_pending(pending_id)
    if not pending:
        logger.warning("pending_id=%d が見つかりません", pending_id)
        _update_message(client, body, ":warning: この確認リクエストは既に処理済みです。")
        return

    if pending["status"] != "pending":
        _update_message(client, body, ":warning: この確認リクエストは既に処理済みです。")
        return

    ai_id = pending["target_id"]

    row = conn.execute(
        "SELECT status FROM action_items WHERE id = ?", (ai_id,)
    ).fetchone()
    if not row:
        logger.warning("AI #%d が見つかりません", ai_id)
        state.resolve_pending(pending_id, "approved", user_id)
        _update_message(client, body, f":warning: AI #{ai_id} が見つかりません。")
        return

    if row["status"] != "closed":
        write_audit_log(conn, ai_id, "status", row["status"], "closed", "argus_patrol")
        conn.execute(
            "UPDATE action_items SET status = 'closed' WHERE id = ?", (ai_id,)
        )

    state.resolve_pending(pending_id, "approved", user_id)
    logger.info("AI #%d を closed に更新 (approved by %s)", ai_id, user_id)

    _update_message(
        client, body,
        f":white_check_mark: AI #{ai_id} を完了にしました。（<@{user_id}> が承認）",
    )


def handle_reject_close(
    body: dict,
    client,
    state: PatrolState,
) -> None:
    """
    「まだ完了していない」ボタン押下時のハンドラ。

    1. pending_confirmations を 'rejected' に更新
    2. 元メッセージを更新（ボタンを削除して却下テキストに置換）
    """
    action = _extract_action(body)
    if not action:
        return

    pending_id = int(action["value"])
    user_id = body.get("user", {}).get("id", "unknown")

    pending = state.get_pending(pending_id)
    if not pending:
        _update_message(client, body, ":warning: この確認リクエストは既に処理済みです。")
        return

    if pending["status"] != "pending":
        _update_message(client, body, ":warning: この確認リクエストは既に処理済みです。")
        return

    state.resolve_pending(pending_id, "rejected", user_id)
    logger.info(
        "AI #%d のクローズを却下 (rejected by %s)",
        pending["target_id"],
        user_id,
    )

    _update_message(
        client, body,
        f":x: AI #{pending['target_id']} のクローズを却下しました。（<@{user_id}>）",
    )


# --------------------------------------------------------------------------- #
# 内部ヘルパー
# --------------------------------------------------------------------------- #
def _extract_action(body: dict) -> dict | None:
    """body から押されたアクションを抽出する。"""
    actions = body.get("actions", [])
    if not actions:
        logger.warning("actions が空です")
        return None
    return actions[0]


def _update_message(client, body: dict, text: str) -> None:
    """元メッセージをテキストのみに更新（ボタンを削除）する。"""
    try:
        channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id", "")
        ts = body.get("message", {}).get("ts") or body.get("container", {}).get("message_ts", "")
        if channel and ts:
            client.chat_update(
                channel=channel,
                ts=ts,
                text=text,
                blocks=[],
            )
    except Exception as e:
        logger.error("メッセージ更新失敗: %s", e)
