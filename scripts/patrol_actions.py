#!/usr/bin/env python3
"""
patrol_actions.py — Patrol Agent のアクション実行レイヤー

Slack 投稿（Block Kit・テキスト）と pm.db 書き込み（audit_log 付き）を提供する。
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patrol_state import PatrolState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 完了確認（Block Kit ボタン付き DM）
# --------------------------------------------------------------------------- #
def send_completion_confirm(ctx, ai_id: int, ai_row: dict, evidence: str) -> bool:
    """
    担当者に DM で Block Kit ボタン付き完了確認メッセージを送信する。

    Returns: 送信成功なら True
    """
    if ctx.dry_run:
        logger.info(
            "[DRY] 完了確認送信: AI #%d (%s) → %s",
            ai_id,
            ai_row.get("content", "")[:40],
            ai_row.get("assignee", "?"),
        )
        return True

    pending_id = ctx.state.create_pending("close_ai", ai_id, evidence)

    assignee = ai_row.get("assignee", "")
    user_id = ctx.user_resolver.resolve(assignee) if assignee else None

    content_preview = (ai_row.get("content") or "")[:200]
    due_date = ai_row.get("due_date") or "未設定"
    evidence_preview = evidence[:300] if evidence else ""

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":white_check_mark: *完了確認*\n"
                    "以下のアクションアイテムに完了を示すメッセージが見つかりました。"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*AI #{ai_id}*: {content_preview}\n"
                    f"*担当者*: {assignee}\n"
                    f"*期限*: {due_date}\n\n"
                    f"*検出根拠*:\n> {evidence_preview}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "完了にする"},
                    "style": "primary",
                    "action_id": "patrol_approve_close",
                    "value": str(pending_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "まだ完了していない"},
                    "style": "danger",
                    "action_id": "patrol_reject_close",
                    "value": str(pending_id),
                },
            ],
        },
    ]

    fallback_text = f"完了確認: AI #{ai_id} {content_preview[:60]}"
    channel_id, message_ts = _send_dm_or_fallback(
        ctx, user_id, assignee, blocks, fallback_text
    )

    if channel_id:
        ctx.state.record_notification(
            "completion_confirm", f"ai:{ai_id}", channel_id, message_ts
        )
        return True
    return False


# --------------------------------------------------------------------------- #
# リマインダー送信
# --------------------------------------------------------------------------- #
def send_reminder(
    ctx, assignee: str, items: list[dict], reminder_type: str
) -> bool:
    """
    担当者にリマインダーメッセージを送信する。

    reminder_type: 'overdue' | 'deadline_warning' | 'stale'
    """
    templates = {
        "overdue": (
            ":warning: *期限超過のアクションアイテム*\n"
            "以下の{n}件が期限を過ぎています:\n{item_list}\n"
            "状況を更新してください。"
        ),
        "deadline_warning": (
            ":hourglass: *期限間近のアクションアイテム*\n"
            "以下の{n}件の期限が3日以内です:\n{item_list}"
        ),
        "stale": (
            ":zzz: *長期停滞のアクションアイテム*\n"
            "以下の{n}件が14日以上更新されていません:\n{item_list}\n"
            "進捗状況を共有してください。"
        ),
    }

    template = templates.get(reminder_type, templates["overdue"])
    item_list = "\n".join(
        f"• *AI #{it['id']}*: {(it.get('content') or '')[:80]} (期限: {it.get('due_date') or '未設定'})"
        for it in items[:5]
    )
    if len(items) > 5:
        item_list += f"\n  …他{len(items) - 5}件"

    text = template.format(n=len(items), item_list=item_list)

    if ctx.dry_run:
        logger.info("[DRY] %s → %s:\n%s", reminder_type, assignee, text)
        return True

    user_id = ctx.user_resolver.resolve(assignee) if assignee else None
    channel_id, _ = _send_dm_or_fallback(ctx, user_id, assignee, text=text)

    if channel_id:
        for it in items:
            ctx.state.record_notification(
                reminder_type, f"ai:{it['id']}", channel_id
            )
        return True
    return False


# --------------------------------------------------------------------------- #
# チャンネル通知
# --------------------------------------------------------------------------- #
def send_channel_alert(ctx, channel_id: str, text: str) -> bool:
    """チャンネルにアラートメッセージを投稿する。"""
    if ctx.dry_run:
        logger.info("[DRY] チャンネル通知 → %s:\n%s", channel_id, text)
        return True

    if not ctx.slack:
        return False

    try:
        ctx.slack.chat_postMessage(channel=channel_id, text=text)
        return True
    except Exception as e:
        logger.error("チャンネル通知失敗 (%s): %s", channel_id, e)
        return False


# --------------------------------------------------------------------------- #
# AI クローズ実行
# --------------------------------------------------------------------------- #
def close_action_item(ctx, ai_id: int, resolved_by: str) -> bool:
    """
    action_items.status を 'closed' に更新し、audit_log に記録する。
    """
    from pm_sync_canvas import write_audit_log

    row = ctx.conn.execute(
        "SELECT status FROM action_items WHERE id = ?", (ai_id,)
    ).fetchone()
    if not row:
        logger.warning("AI #%d が見つかりません", ai_id)
        return False
    if row["status"] == "closed":
        logger.info("AI #%d は既に closed", ai_id)
        return True

    if ctx.dry_run:
        logger.info("[DRY] AI #%d を closed に更新", ai_id)
        return True

    write_audit_log(ctx.conn, ai_id, "status", "open", "closed", "argus_patrol")
    ctx.conn.execute(
        "UPDATE action_items SET status = 'closed' WHERE id = ?", (ai_id,)
    )
    logger.info("AI #%d を closed に更新 (by %s)", ai_id, resolved_by)
    return True


# --------------------------------------------------------------------------- #
# 内部ヘルパー
# --------------------------------------------------------------------------- #
def _send_dm_or_fallback(
    ctx,
    user_id: str | None,
    assignee_name: str,
    blocks: list | None = None,
    text: str = "",
) -> tuple[str, str]:
    """
    user_id が解決できれば DM、できなければリーダー会議チャンネルに投稿。

    Returns: (channel_id, message_ts)。失敗時は ("", "")。
    """
    if not ctx.slack:
        return ("", "")

    leader_ch = ctx.config.get("patrol", {}).get(
        "leader_channel", "C08SXA4M7JT"
    )

    if not text and blocks:
        text = blocks[0].get("text", {}).get("text", "Argus Patrol 通知")

    target_channel = None
    if user_id:
        try:
            resp = ctx.slack.conversations_open(users=[user_id])
            target_channel = resp["channel"]["id"]
        except Exception as e:
            logger.warning("DM open 失敗 (%s): %s", user_id, e)

    if not target_channel:
        target_channel = leader_ch
        if assignee_name:
            text = f"*{assignee_name}* さん宛:\n{text}"

    try:
        kwargs: dict = {"channel": target_channel, "text": text}
        if blocks and target_channel != leader_ch:
            kwargs["blocks"] = blocks
        resp = ctx.slack.chat_postMessage(**kwargs)
        time.sleep(1)
        return (target_channel, resp.get("ts", ""))
    except Exception as e:
        logger.error("メッセージ送信失敗 (%s): %s", target_channel, e)
        return ("", "")
