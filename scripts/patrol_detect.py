#!/usr/bin/env python3
"""
patrol_detect.py — Patrol Agent の検出ルール

7つの検出関数を提供する。各関数は PatrolContext を受け取り、
検出結果に基づいて patrol_actions の関数を呼ぶ。

完了シグナル検出は LLM 判定を併用（キーワードマッチ + 自然言語分析の二段構え）。
それ以外の検出器は決定論的ルールのみ（LLM 不使用）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# カテゴリ1: 完了シグナル検出
# --------------------------------------------------------------------------- #
def detect_completion_signals(ctx) -> int:
    """
    Slack メッセージから open AI の完了シグナルを検出し、
    担当者に Block Kit ボタンでクローズ確認を送信する。

    検出は二段構え:
    1. キーワードマッチ（高速・決定論的）
    2. LLM 判定（キーワードで拾えない自然言語表現を検出）

    対象: source='slack' かつ source_ref が Slack パーマリンクの AI のみ。
    Returns: 検出件数
    """
    from pm_sync_canvas import CLOSE_KEYWORDS
    from patrol_actions import send_completion_confirm

    cfg = ctx.config.get("patrol", {}).get("completion_detection", {})
    if not cfg.get("enabled", True):
        return 0

    use_llm = cfg.get("use_llm", True)
    max_age = cfg.get("max_reply_age_days", 7)
    cutoff_date = (date.fromisoformat(ctx.today) - timedelta(days=max_age)).isoformat()

    rows = ctx.conn.execute(
        "SELECT id, content, assignee, due_date, source_ref"
        " FROM action_items"
        " WHERE status = 'open' AND COALESCE(deleted,0)=0"
        "   AND source = 'slack' AND source_ref IS NOT NULL AND source_ref != ''",
    ).fetchall()

    close_kw_lower = {k.lower() for k in CLOSE_KEYWORDS}
    detected = 0

    for row in rows:
        ai_id = row["id"]
        target_key = f"ai:{ai_id}"
        if ctx.state.already_notified("completion_confirm", target_key, cooldown_days=9999):
            continue

        source_ref = row["source_ref"]
        channel_id, thread_ts = _parse_permalink(source_ref)
        if not channel_id or not thread_ts:
            continue

        replies = _get_recent_replies(ctx.data_dir, channel_id, thread_ts, cutoff_date)
        if not replies:
            continue

        # --- 第1段: キーワードマッチ ---
        kw_hit = False
        for reply_text in replies:
            text_lower = reply_text.lower().strip()
            if any(kw in text_lower for kw in close_kw_lower):
                evidence = reply_text[:300]
                send_completion_confirm(ctx, ai_id, dict(row), evidence)
                detected += 1
                kw_hit = True
                break

        if kw_hit:
            continue

        # --- 第2段: LLM 判定 ---
        if not use_llm:
            continue

        llm_result = _llm_judge_completion(row["content"], replies)
        if llm_result:
            evidence = f"[LLM判定] {llm_result}"
            send_completion_confirm(ctx, ai_id, dict(row), evidence)
            detected += 1

    if detected:
        logger.info("完了シグナル検出: %d 件", detected)
    return detected


# --------------------------------------------------------------------------- #
# カテゴリ2: 期限超過リマインダー
# --------------------------------------------------------------------------- #
def detect_overdue_items(ctx) -> int:
    """期限超過の open AI を検出し、担当者にリマインダーを送信する。"""
    from db_utils import fetch_overdue_items
    from patrol_actions import send_reminder

    cfg = ctx.config.get("patrol", {}).get("overdue_reminder", {})
    if not cfg.get("enabled", True):
        return 0

    cooldown = cfg.get("cooldown_days", 7)
    items = fetch_overdue_items(ctx.conn, ctx.today, since=None)

    to_notify: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        target_key = f"ai:{it['id']}"
        if ctx.state.already_notified("overdue_reminder", target_key, cooldown):
            continue
        assignee = it.get("assignee") or "（担当者なし）"
        to_notify[assignee].append(it)

    sent = 0
    for assignee, assignee_items in to_notify.items():
        if send_reminder(ctx, assignee, assignee_items, "overdue"):
            sent += len(assignee_items)

    if sent:
        logger.info("期限超過リマインダー: %d 件 → %d 担当者", sent, len(to_notify))
    return sent


# --------------------------------------------------------------------------- #
# カテゴリ2: 期限前警告
# --------------------------------------------------------------------------- #
def detect_approaching_deadlines(ctx) -> int:
    """期限まで3日以内の open AI を検出し、担当者に警告する。"""
    from patrol_actions import send_reminder

    cfg = ctx.config.get("patrol", {}).get("deadline_warning", {})
    if not cfg.get("enabled", True):
        return 0

    warn_days = cfg.get("warn_days_before", 3)
    deadline = (
        date.fromisoformat(ctx.today) + timedelta(days=warn_days)
    ).isoformat()

    rows = ctx.conn.execute(
        "SELECT id, content, assignee, due_date, milestone_id"
        " FROM action_items"
        " WHERE status = 'open' AND COALESCE(deleted,0)=0"
        "   AND due_date IS NOT NULL AND due_date >= ? AND due_date <= ?",
        (ctx.today, deadline),
    ).fetchall()

    to_notify: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        target_key = f"ai:{row['id']}"
        if ctx.state.already_notified("deadline_warning", target_key, cooldown_days=warn_days):
            continue
        assignee = row["assignee"] or "（担当者なし）"
        to_notify[assignee].append(dict(row))

    sent = 0
    for assignee, items in to_notify.items():
        if send_reminder(ctx, assignee, items, "deadline_warning"):
            sent += len(items)

    if sent:
        logger.info("期限前警告: %d 件 → %d 担当者", sent, len(to_notify))
    return sent


# --------------------------------------------------------------------------- #
# カテゴリ2: 未確認決定事項フォローアップ
# --------------------------------------------------------------------------- #
def detect_unacknowledged_decisions(ctx) -> int:
    """7日以上未確認の決定事項をリーダー会議チャンネルに通知する。"""
    from db_utils import fetch_unacknowledged_decisions
    from patrol_actions import send_channel_alert

    cfg = ctx.config.get("patrol", {}).get("decision_followup", {})
    if not cfg.get("enabled", True):
        return 0

    stale_days = cfg.get("stale_days", 7)
    cooldown = cfg.get("cooldown_days", 7)
    cutoff = (
        date.fromisoformat(ctx.today) - timedelta(days=stale_days)
    ).isoformat()

    decisions = fetch_unacknowledged_decisions(ctx.conn, since=None)
    stale = [
        d for d in decisions
        if d.get("decided_at") and d["decided_at"] <= cutoff
    ]

    to_notify = []
    for d in stale:
        target_key = f"decision:{d['id']}"
        if not ctx.state.already_notified("decision_followup", target_key, cooldown):
            to_notify.append(d)

    if not to_notify:
        return 0

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "C08SXA4M7JT")
    items_text = "\n".join(
        f"• *D#{d['id']}* ({d.get('decided_at', '?')}): {(d.get('content') or '')[:100]}"
        for d in to_notify[:10]
    )
    text = (
        f":clipboard: *未確認の決定事項（{len(to_notify)}件）*\n"
        f"以下の決定事項が{stale_days}日以上確認されていません:\n"
        f"{items_text}\n"
        "レポート Canvas で確認済みチェックを入れてください。"
    )

    if send_channel_alert(ctx, leader_ch, text) and not ctx.dry_run:
        for d in to_notify:
            ctx.state.record_notification(
                "decision_followup", f"decision:{d['id']}", leader_ch
            )

    logger.info("未確認決定事項フォロー: %d 件", len(to_notify))
    return len(to_notify)


# --------------------------------------------------------------------------- #
# カテゴリ4: 長期停滞検出
# --------------------------------------------------------------------------- #
def detect_stale_items(ctx) -> int:
    """14日以上変化のない open AI を検出してアラートする。"""
    from patrol_actions import send_reminder, send_channel_alert

    cfg = ctx.config.get("patrol", {}).get("stale_detection", {})
    if not cfg.get("enabled", True):
        return 0

    stale_days = cfg.get("stale_days", 14)
    cooldown = cfg.get("cooldown_days", 14)
    cutoff = (
        date.fromisoformat(ctx.today) - timedelta(days=stale_days)
    ).isoformat()

    rows = ctx.conn.execute(
        "SELECT id, content, assignee, due_date, milestone_id, extracted_at"
        " FROM action_items"
        " WHERE status = 'open' AND COALESCE(deleted,0)=0",
    ).fetchall()

    stale_items: list[dict] = []
    for row in rows:
        ai_id = row["id"]
        target_key = f"ai:{ai_id}"
        if ctx.state.already_notified("stale_alert", target_key, cooldown):
            continue

        last_change = _get_last_change_date(ctx.conn, ai_id)
        if last_change is None:
            last_change = row["extracted_at"] or ""

        if last_change and last_change <= cutoff:
            stale_items.append(dict(row))

    if not stale_items:
        return 0

    by_assignee: dict[str, list[dict]] = defaultdict(list)
    for it in stale_items:
        assignee = it.get("assignee") or "（担当者なし）"
        by_assignee[assignee].append(it)

    for assignee, items in by_assignee.items():
        send_reminder(ctx, assignee, items, "stale")

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "C08SXA4M7JT")
    summary = (
        f":zzz: *長期停滞アラート（{len(stale_items)}件）*\n"
        f"{stale_days}日以上更新のないアクションアイテムが{len(stale_items)}件あります。\n"
        f"対象担当者: {', '.join(by_assignee.keys())}"
    )
    send_channel_alert(ctx, leader_ch, summary)

    logger.info("長期停滞検出: %d 件", len(stale_items))
    return len(stale_items)


# --------------------------------------------------------------------------- #
# カテゴリ6: マイルストーン健全性
# --------------------------------------------------------------------------- #
def detect_milestone_health(ctx) -> int:
    """マイルストーンの達成率が期限に対して低い場合にアラートする。"""
    from db_utils import fetch_milestone_progress
    from patrol_actions import send_channel_alert

    cfg = ctx.config.get("patrol", {}).get("milestone_health", {})
    if not cfg.get("enabled", True):
        return 0

    threshold = cfg.get("threshold", 0.7)
    cooldown = cfg.get("cooldown_days", 7)
    today_dt = date.fromisoformat(ctx.today)

    milestones = fetch_milestone_progress(ctx.conn)
    alerts: list[str] = []

    for ms in milestones:
        ms_id = ms.get("milestone_id", "")
        target_key = f"milestone:{ms_id}"
        if ctx.state.already_notified("milestone_alert", target_key, cooldown):
            continue

        due_str = ms.get("due_date")
        if not due_str:
            continue

        open_count = ms.get("open_count", 0)
        closed_count = ms.get("closed_count", 0)
        total = open_count + closed_count
        if total == 0:
            continue

        due_dt = date.fromisoformat(due_str)
        completion_rate = closed_count / total

        created_at_str = ms.get("due_date")
        try:
            start_dt = date.fromisoformat(created_at_str) if created_at_str else due_dt - timedelta(days=90)
        except (ValueError, TypeError):
            start_dt = due_dt - timedelta(days=90)

        total_span = max((due_dt - start_dt).days, 1)
        elapsed = max((today_dt - start_dt).days, 0)
        elapsed_ratio = min(elapsed / total_span, 1.0)

        expected = elapsed_ratio * threshold
        if completion_rate < expected and elapsed_ratio > 0.2:
            alerts.append(
                f"• *{ms_id}* ({ms.get('name', '')}): "
                f"完了率 {completion_rate:.0%}（期待 {expected:.0%}）、"
                f"期限まで {(due_dt - today_dt).days} 日"
            )
            if not ctx.dry_run:
                ctx.state.record_notification(
                    "milestone_alert", target_key,
                    ctx.config.get("patrol", {}).get("leader_channel", "C08SXA4M7JT"),
                )

    if not alerts:
        return 0

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "C08SXA4M7JT")
    text = (
        f":chart_with_downwards_trend: *マイルストーン健全性アラート*\n"
        f"以下のマイルストーンの進捗が期待を下回っています:\n"
        + "\n".join(alerts)
    )
    send_channel_alert(ctx, leader_ch, text)

    logger.info("マイルストーン健全性アラート: %d 件", len(alerts))
    return len(alerts)


# --------------------------------------------------------------------------- #
# カテゴリ6: 週次トレンド悪化検出
# --------------------------------------------------------------------------- #
def detect_weekly_trend_alert(ctx) -> int:
    """直近2週の完了数が前2週より50%以上減少した場合にアラートする。"""
    from db_utils import fetch_weekly_trends
    from patrol_actions import send_channel_alert

    cfg = ctx.config.get("patrol", {}).get("weekly_trend", {})
    if not cfg.get("enabled", True):
        return 0

    decline_threshold = cfg.get("decline_threshold", 0.5)

    trends = fetch_weekly_trends(ctx.conn, weeks=4)
    if len(trends) < 4:
        return 0

    prev_closed = trends[0]["closed"] + trends[1]["closed"]
    recent_closed = trends[2]["closed"] + trends[3]["closed"]

    if prev_closed == 0:
        return 0

    decline = 1.0 - (recent_closed / prev_closed)
    if decline < decline_threshold:
        return 0

    target_key = f"trend:{ctx.today}"
    if ctx.state.already_notified("weekly_trend_alert", target_key, cooldown_days=7):
        return 0

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "C08SXA4M7JT")
    text = (
        f":chart_with_downwards_trend: *週次トレンド悪化アラート*\n"
        f"直近2週の完了件数（{recent_closed}件）が前2週（{prev_closed}件）より"
        f" {decline:.0%} 減少しています。\n"
        f"ボトルネックの確認をお勧めします。"
    )

    if send_channel_alert(ctx, leader_ch, text) and not ctx.dry_run:
        ctx.state.record_notification(
            "weekly_trend_alert", target_key, leader_ch
        )

    logger.info("週次トレンド悪化: %.0f%% 減少", decline * 100)
    return 1


# --------------------------------------------------------------------------- #
# 内部ヘルパー
# --------------------------------------------------------------------------- #
_LLM_COMPLETION_PROMPT = """\
あなたはプロジェクト管理アシスタントです。
以下のアクションアイテム（AI）と、そのSlackスレッドの最近の返信を読んで、
このAIが**完了した**と判断できるかを判定してください。

## アクションアイテム
{ai_content}

## スレッドの返信（新しい順）
{replies_text}

## 判定基準
- 明示的な完了報告（「完了」「done」等）だけでなく、成果物の提出・報告・対応済みの報告なども完了とみなす
- 「検討中」「確認します」「対応予定」等は未完了
- 部分的な進捗報告は未完了（全体が完了していない限り）
- 返信内容がAIの内容と無関係な場合は未完了

## 出力
完了と判断できる場合: YES: （根拠を1文で）
完了と判断できない場合: NO
"""


def _llm_judge_completion(ai_content: str, replies: list[str]) -> str | None:
    """LLM にスレッド返信を分析させ、AI が完了したか判定する。

    Returns: 完了と判定された場合は根拠テキスト、それ以外は None。
    """
    try:
        from cli_utils import call_argus_llm
    except ImportError:
        logger.debug("cli_utils が利用不可。LLM 判定をスキップ。")
        return None

    replies_text = "\n---\n".join(r[:500] for r in replies[:10])
    prompt = _LLM_COMPLETION_PROMPT.format(
        ai_content=ai_content[:500],
        replies_text=replies_text,
    )

    try:
        result = call_argus_llm(prompt, timeout=30, max_tokens=200)
        result = result.strip()
        if result.upper().startswith("YES"):
            return result[4:].strip(": 　").strip() or "LLMが完了と判定"
    except Exception as e:
        logger.warning("完了シグナル LLM 判定エラー: %s", e)

    return None


_PERMALINK_RE = re.compile(
    r"/archives/(C[A-Z0-9]+)/p(\d{10})(\d{6})"
)


def _parse_permalink(permalink: str) -> tuple[str, str]:
    """Slack パーマリンクからチャンネルID と thread_ts を抽出する。"""
    m = _PERMALINK_RE.search(permalink)
    if not m:
        return ("", "")
    channel_id = m.group(1)
    thread_ts = f"{m.group(2)}.{m.group(3)}"
    return (channel_id, thread_ts)


def _get_recent_replies(
    data_dir: Path, channel_id: str, thread_ts: str, cutoff_date: str
) -> list[str]:
    """Slack DB からスレッドの最新返信テキストを取得する。"""
    from db_utils import open_pm_db

    db_path = data_dir / f"{channel_id}.db"
    if not db_path.exists():
        return []

    try:
        conn = open_pm_db(db_path)
        rows = conn.execute(
            "SELECT text FROM replies"
            " WHERE thread_ts = ? AND channel_id = ?"
            "   AND timestamp >= ?"
            " ORDER BY msg_ts DESC LIMIT 20",
            (thread_ts, channel_id, cutoff_date),
        ).fetchall()
        conn.close()
        return [r["text"] for r in rows if r["text"]]
    except Exception:
        return []


def _get_last_change_date(conn, ai_id: int) -> str | None:
    """audit_log から AI の最終変更日を取得する。"""
    try:
        row = conn.execute(
            "SELECT MAX(changed_at) as last_change FROM audit_log"
            " WHERE table_name = 'action_items' AND record_id = ?",
            (str(ai_id),),
        ).fetchone()
        if row and row["last_change"]:
            return row["last_change"][:10]
    except Exception:
        pass
    return None
