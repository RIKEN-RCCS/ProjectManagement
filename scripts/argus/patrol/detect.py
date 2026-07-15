#!/usr/bin/env python3
"""
patrol_detect.py — Patrol Agent の検出ルール

8つの検出関数を提供する。各関数は PatrolContext を受け取り、
検出結果に基づいて patrol_actions の関数を呼ぶ。

完了シグナル検出は LLM 判定を併用（キーワードマッチ + 自然言語分析の二段構え）。
第2段は同一スレッド以外（会議・別チャンネル・qa_index に索引済みの資料）での
完了報告も証拠として扱う。`auto_close_enabled` かつ確信度が
`auto_close_min_confidence`（既定 HIGH）以上の場合のみ自動クローズし、
それ以外（auto_close 無効、または確信度不足）は従来どおり担当者へ
承認ボタン付き完了確認 DM を送信する
（`patrol.completion_detection.auto_close_*` で段階的に有効化）。
それ以外の検出器は決定論的ルールのみ（LLM 不使用）。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
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
    open AI の完了シグナルを検出する。

    検出は二段構え:
    1. キーワードマッチ（Slack 同一スレッド返信、高速・決定論的）
       → 担当者に Block Kit ボタンでクローズ確認を送信する
    2. LLM 判定（同一スレッドの返信に加え、qa_index に索引済みの
       会議議事録・別チャンネル等での完了報告も証拠として分析）
       → 確信度が閾値以上かつ `auto_close_enabled` の場合のみ自動クローズする。
         それ以外は従来どおり担当者へ承認ボタン付き完了確認 DM を送信する。

    対象: source IN ('slack', 'meeting') の open AI。
    Returns: 実際にアクション（クローズ確認送信 or 自動クローズ）を行った件数
    """
    from pm_sync_canvas import CLOSE_KEYWORDS

    from .actions import close_action_item, send_channel_alert, send_completion_confirm

    cfg = ctx.config.get("patrol", {}).get("completion_detection", {})
    if not cfg.get("enabled", True):
        return 0

    use_llm = cfg.get("use_llm", True)
    max_age = cfg.get("max_reply_age_days", 7)
    cutoff_date = (date.fromisoformat(ctx.today) - timedelta(days=max_age)).isoformat()

    rows = ctx.conn.execute(
        "SELECT id, content, assignee, due_date, source_ref, source, extracted_at, note"
        " FROM action_items"
        " WHERE status = 'open' AND COALESCE(deleted,0)=0"
        "   AND source IN ('slack', 'meeting')",
    ).fetchall()

    close_kw_lower = {k.lower() for k in CLOSE_KEYWORDS}
    auto_close_enabled = cfg.get("auto_close_enabled", False)
    min_confidence = cfg.get("auto_close_min_confidence", "HIGH")
    post_close_notify = cfg.get("post_close_notify", True)
    detected = 0

    for row in rows:
        ai_id = row["id"]
        target_key = f"ai:{ai_id}"
        if (
            ctx.state.already_notified("completion_confirm", target_key, cooldown_days=9999)
            or ctx.state.already_notified("auto_close", target_key, cooldown_days=9999)
        ):
            continue

        # --- 第1段: Slack 同一スレッド返信のキーワードマッチ（高速パス） ---
        evidence_list: list[dict] = []
        kw_hit = False
        if row["source"] == "slack" and row["source_ref"]:
            channel_id, thread_ts = _parse_permalink(row["source_ref"])
            if channel_id and thread_ts:
                replies = _get_recent_replies(ctx.data_dir, channel_id, thread_ts, cutoff_date)
                for reply_text in replies:
                    text_lower = reply_text.lower().strip()
                    if any(kw in text_lower for kw in close_kw_lower):
                        evidence = reply_text[:300]
                        send_completion_confirm(ctx, ai_id, dict(row), evidence)
                        detected += 1
                        kw_hit = True
                        break
                if not kw_hit:
                    evidence_list = [
                        {
                            "source_type": "slack_thread",
                            "held_at": "",
                            "source_ref": row["source_ref"],
                            "content": r,
                        }
                        for r in replies
                    ]

        if kw_hit:
            continue

        # --- 第2段: 出典を跨いだ証拠収集 + LLM 判定 ---
        if not use_llm:
            continue

        evidence_list += _get_activity_evidence(ctx, dict(row))
        if not evidence_list:
            continue

        judged = _llm_judge_completion(row["content"], evidence_list)
        if judged is None:
            continue
        is_complete, confidence, reason = judged
        if not is_complete:
            continue

        conf_ok = (
            _CONFIDENCE_ORDER.get(confidence, 0)
            >= _CONFIDENCE_ORDER.get(min_confidence, 1)
        )
        evidence_text = f"[LLM判定/{confidence}] {reason}"

        if auto_close_enabled and conf_ok:
            if not close_action_item(ctx, ai_id, "argus_auto", note=evidence_text):
                continue
            detected += 1
            if not ctx.dry_run:
                # pm.db のクローズ確定を先に commit してから state.db に記録する
                # （順序を逆にすると、途中でプロセスが落ちた際に
                #  「state.db は記録済みだが pm.db は open のまま」の
                #  スタック状態になり得る）
                ctx.conn.commit()
                ctx.state.record_notification("auto_close", target_key)

            if post_close_notify:
                leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
                text = (
                    ":white_check_mark: *自動クローズ*\n"
                    f"*AI #{ai_id}*: {(row['content'] or '')[:200]}\n"
                    f"根拠: {reason[:300]}\n"
                    "誤りであれば Web UI で再オープンできます。"
                )
                send_channel_alert(ctx, leader_ch, text)
            continue

        # 確信度不足、または auto_close_enabled=false:
        # 従来どおり担当者へ承認ボタン付き完了確認 DM を送る
        # （record_notification("completion_confirm", ...) は
        #  send_completion_confirm 内で送信成功時に記録される）
        logger.info(
            "AI #%d: LLM完了判定（確信度=%s, auto_close_enabled=%s）→ 承認確認DMを送信: %s",
            ai_id, confidence, auto_close_enabled, reason[:80],
        )
        send_completion_confirm(ctx, ai_id, dict(row), evidence_text)
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

    from .actions import send_reminder

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
    from .actions import send_reminder

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

    from .actions import send_channel_alert

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

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
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
    from .actions import send_channel_alert, send_reminder

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

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
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

    from .actions import send_channel_alert

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
                    ctx.config.get("patrol", {}).get("leader_channel", ""),
                )

    if not alerts:
        return 0

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
    text = (
        ":chart_with_downwards_trend: *マイルストーン健全性アラート*\n"
        "以下のマイルストーンの進捗が期待を下回っています:\n"
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

    from .actions import send_channel_alert

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

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
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
# カテゴリ8: 外部シグナル検出（Argus 垂直軸 機能1: 上位意思・外界取り込み）
# --------------------------------------------------------------------------- #
def detect_external_signals(ctx) -> int:
    """ledger_assumptions.monitor_target に関連する外部Web記事を検出し、
    設計書§5の着地処理（前提の確信度更新／既存決定への警告／監視継続）を行う。

    キーワードマッチ（ルールベース）で候補を絞った後、LLM で
    confirms/contradicts/neutral を判定する（`use_llm` で無効化可）。
    neutral はキーワード一致のみのノイズとみなし通知を抑制する
    （設計書§5「通知過多の抑制」）。confirms/contradicts のみ通知し、
    前提の confidence/last_reviewed_at を更新する。LLM 失敗時・無効時は
    従来通りキーワード一致のみで通知する（自動判定なしの安全側にフォールバック）。
    """
    from .actions import send_channel_alert

    cfg = ctx.config.get("patrol", {}).get("external_signal", {})
    if not cfg.get("enabled", True):
        return 0

    lookback_days = cfg.get("lookback_days", 14)
    cooldown = cfg.get("cooldown_days", 14)
    max_per_run = cfg.get("max_per_run", 5)
    use_llm = cfg.get("use_llm", True)
    cutoff = (date.fromisoformat(ctx.today) - timedelta(days=lookback_days)).isoformat()

    assumptions = ctx.conn.execute(
        "SELECT id, content, monitor_target FROM ledger_assumptions"
        " WHERE monitor_target IS NOT NULL AND TRIM(monitor_target) != ''"
        "   AND state = 'active'"
    ).fetchall()
    if not assumptions:
        return 0

    articles = _get_recent_articles(ctx.data_dir, cutoff)
    if not articles:
        return 0

    matches: list[tuple] = []
    for a in assumptions:
        terms = _split_monitor_terms(a["monitor_target"])
        if not terms:
            continue
        for art in articles:
            haystack = f"{art.get('title') or ''} {art.get('summary') or ''} {art.get('content') or ''}".lower()
            if any(term.lower() in haystack for term in terms):
                matches.append((a, art))

    if not matches:
        return 0

    leader_ch = ctx.config.get("patrol", {}).get("leader_channel", "")
    sent = 0
    for a, art in matches:
        if sent >= max_per_run:
            break
        target_key = f"assumption:{a['id']}:article:{art['id']}"
        if ctx.state.already_notified("external_signal", target_key, cooldown):
            continue

        judgment = None
        if use_llm:
            judgment = _llm_judge_external_signal(
                a["content"], a["monitor_target"],
                art.get("title") or "", art.get("summary") or art.get("content") or "",
            )

        # neutral 判定はノイズとみなし、通知せず cooldown も消費しない
        if judgment and judgment["verdict"] == "neutral":
            continue

        label = art.get("title") or art.get("url") or "(タイトルなし)"
        when = art.get("published_at") or art.get("fetched_at") or "?"

        if judgment:
            verdict_label = {"confirms": "裏付け", "contradicts": "否定"}[judgment["verdict"]]
            judgment_line = f"\nLLM判定: *{verdict_label}* — {judgment['reason']}"
        else:
            judgment_line = "\n（自動判定なし。手動で確認してください）"

        # 依拠する決定への警告（設計書§5 作用3。depends_on 辺が無ければ該当なし）
        dependents = ctx.conn.execute(
            "SELECT from_id FROM ledger_edges"
            " WHERE edge_type = 'depends_on' AND to_kind = 'assumption' AND to_id = ?"
            "   AND state = 'active'",
            (str(a["id"]),),
        ).fetchall()
        dependents_line = ""
        if dependents and judgment and judgment["verdict"] == "contradicts":
            ids = "、".join(f"d:{d['from_id']}" for d in dependents)
            dependents_line = f"\n:warning: この前提に依拠する決定に影響の可能性: {ids}"

        text = (
            f":mag: *前提の監視対象に関連する外部記事を検出*\n"
            f"前提 #{a['id']}: {a['content']}\n"
            f"監視対象: {a['monitor_target']}\n"
            f"該当記事: <{art.get('url', '')}|{label}>"
            f"（{art.get('source_name', '?')}, {when}）"
            f"{judgment_line}{dependents_line}\n"
            "この前提が今も妥当か確認してください。"
        )
        if send_channel_alert(ctx, leader_ch, text) and not ctx.dry_run:
            ctx.state.record_notification("external_signal", target_key, leader_ch)
            if judgment and judgment["verdict"] in ("confirms", "contradicts"):
                _update_assumption_confidence(ctx.conn, a["id"], judgment["verdict"])
        sent += 1

    if sent:
        logger.info("外部シグナル検出: %d 件", sent)
    return sent


_LLM_EXTERNAL_SIGNAL_PROMPT = """\
あなたはプロジェクト管理アシスタントです。以下の「前提」と、それに関連しそうな
外部記事を読んで、記事がこの前提を裏付けるか、否定するか、それとも
キーワードが一致しただけで実質的な関連性が薄いかを判定してください。

## 前提
{assumption_content}
（監視対象: {monitor_target}）

## 外部記事
タイトル: {article_title}
本文/要約: {article_summary}

## 判定基準
- 記事が前提の内容を積極的に裏付ける具体的な情報を含む → CONFIRMS
- 記事が前提と矛盾する、または前提を覆しうる情報を含む → CONTRADICTS
- キーワードが一致しただけで、前提の真偽に関する実質的な情報がない → NEUTRAL

## 出力
以下のいずれか1つを先頭に出力し、コロンの後に根拠を1文で述べてください。
CONFIRMS: （根拠）
CONTRADICTS: （根拠）
NEUTRAL: （根拠）
"""


def _llm_judge_external_signal(
    assumption_content: str, monitor_target: str, article_title: str, article_summary: str
) -> dict | None:
    """LLM に前提と外部記事を比較させ、confirms/contradicts/neutral を判定する。

    Returns: {"verdict": "confirms"|"contradicts"|"neutral", "reason": str} 、
    LLM 利用不可・パース失敗時は None（呼び出し側は自動判定なしにフォールバックする）。
    """
    try:
        from cli_utils import call_argus_llm
    except ImportError:
        logger.debug("cli_utils が利用不可。外部シグナルのLLM判定をスキップ。")
        return None

    prompt = _LLM_EXTERNAL_SIGNAL_PROMPT.format(
        assumption_content=assumption_content[:500],
        monitor_target=monitor_target[:200],
        article_title=article_title[:200],
        article_summary=article_summary[:800],
    )

    try:
        result = call_argus_llm(prompt, timeout=30, max_tokens=200).strip()
        for verdict in ("CONFIRMS", "CONTRADICTS", "NEUTRAL"):
            if result.upper().startswith(verdict):
                reason = result[len(verdict):].strip(": 　").strip() or "(根拠なし)"
                return {"verdict": verdict.lower(), "reason": reason}
        logger.warning("外部シグナル LLM 判定: 期待した形式で応答が得られず: %s", result[:100])
    except Exception as e:
        logger.warning("外部シグナル LLM 判定エラー: %s", e)

    return None


def _update_assumption_confidence(conn, assumption_id: int, verdict: str) -> None:
    """LLM判定結果に基づき ledger_assumptions を更新する（設計書§5 作用2）。

    confirms: 鮮度を更新するのみ（confidence が未設定なら medium とする）。
    contradicts: confidence を low に下げ、state を review にして次回以降の
    自動監視対象から外す（人による再確認・状態リセットを促す）。
    """
    now = datetime.now().isoformat()
    if verdict == "confirms":
        conn.execute(
            "UPDATE ledger_assumptions SET last_reviewed_at = ?,"
            " confidence = COALESCE(NULLIF(TRIM(confidence), ''), 'medium')"
            " WHERE id = ?",
            (now, assumption_id),
        )
    elif verdict == "contradicts":
        conn.execute(
            "UPDATE ledger_assumptions SET last_reviewed_at = ?, confidence = 'low',"
            " state = 'review' WHERE id = ?",
            (now, assumption_id),
        )
    conn.commit()


_MONITOR_TERM_STOPWORDS = {"する", "こと", "ため", "もの", "よる", "おく", "れる", "いる", "ある"}


def _split_monitor_terms(monitor_target: str) -> list[str]:
    """monitor_target を検索語に分割する。

    区切り文字（, 、 ・ / 空白）での単純分割だと、日本語の自由文（例:
    「KDDIによるGB200 NVL72サービスの正式な提供開始時期」）はほぼ分割されず
    1つの長い文字列になってしまい、記事本文と一致しなくなる（2026-07-03に
    depends_on 辺検証中に発覚。経緯はLOG.md参照）。
    英数字の固有名詞（製品名・企業名等）は正規表現でそのまま抽出し、
    日本語部分は `retrieval.sudachi_tokenize_query()`（既存のFTS5検索と同じ
    形態素解析）で名詞・動詞等を抽出して補う。SudachiPy利用不可時は
    区切り文字分割のみにフォールバックする。
    """
    alnum_terms = re.findall(r"[A-Za-zＡ-Ｚａ-ｚ0-9]{2,}", monitor_target)

    try:
        from argus.retrieval import _init_sudachi, sudachi_tokenize_query

        _init_sudachi()
        noun_terms = sudachi_tokenize_query(monitor_target)
    except Exception:
        noun_terms = re.split(r"[,、・/\s]+", monitor_target.strip())

    terms: list[str] = []
    seen: set[str] = set()
    for t in alnum_terms + noun_terms:
        key = t.lower()
        if len(t) >= 2 and key not in seen and t not in _MONITOR_TERM_STOPWORDS:
            seen.add(key)
            terms.append(t)
    return terms


def _get_recent_articles(data_dir: Path, cutoff_date: str) -> list[dict]:
    """data/web_articles.db（平文 sqlite3、暗号化なし）から
    cutoff_date 以降に取得された記事を返す。
    """
    from db_utils import open_db_plain

    db_path = data_dir / "web_articles.db"
    if not db_path.exists():
        return []
    try:
        conn = open_db_plain(db_path)
        rows = conn.execute(
            "SELECT id, source_name, url, title, published_at, fetched_at, content, summary"
            " FROM articles WHERE fetched_at >= ? ORDER BY fetched_at DESC",
            (cutoff_date,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# 内部ヘルパー
# --------------------------------------------------------------------------- #
_CONFIDENCE_ORDER = {"LOW": 0, "HIGH": 1}

_LLM_COMPLETION_PROMPT = """\
あなたはプロジェクト管理アシスタントです。
以下のアクションアイテム（AI）と、それに関連しそうな複数の証拠（Slackスレッドの
返信、会議議事録、別チャンネルでの報告など、出典が異なるものを含む）を読んで、
このAIが**完了した**と判断できるかを判定してください。

## アクションアイテム
{ai_content}

## 証拠（出典付き）
{evidence_text}

## 判定基準
- 同一スレッドの返信に限らず、別の場（会議・別チャンネル・レポート等）での
  成果物提出・完了報告・対応済み報告も完了の根拠として扱ってよい
- 明示的な完了報告（「完了」「done」等）だけでなく、成果物の提出・報告・対応済みの報告なども完了とみなす
- 「検討中」「確認します」「対応予定」等は未完了
- 部分的な進捗報告は未完了（全体が完了していない限り）
- 証拠の内容がAIの内容と無関係な場合は未完了
- 単に関連する文書・議事録が検索でヒットしただけ、話題が言及されただけでは
  完了とみなさない。担当者本人または関係者による「成果物の提出・提供」
  「対応済み・完了した」という明確な報告がある場合のみ完了とする。
  根拠が間接的・推測に留まる場合は確信度を LOW にすること
- 判定理由には、根拠とした証拠の出典（source_type・日時・source_ref/会議名）を引用すること

## 確信度
- 本人／関係者による明確な完了報告など、直接的な証拠がある → HIGH
- 完了を示唆するが間接的、または推測を伴う → LOW

## 出力
完了と判断できる場合:
YES|HIGH: （根拠と出典を1文で）
YES|LOW: （根拠を1文で）
完了と判断できない場合:
NO
"""

_LLM_COMPLETION_RE = re.compile(
    r"YES\s*\|\s*(HIGH|LOW)\s*:?\s*(.*)", re.IGNORECASE | re.DOTALL
)
_LLM_NO_RE = re.compile(r"\bNO\b", re.IGNORECASE)


def _llm_judge_completion(
    ai_content: str, evidence: list[dict]
) -> tuple[bool, str | None, str] | None:
    """LLM に出典付きの証拠を分析させ、AI が完了したか確信度付きで判定する。

    evidence: {"source_type", "held_at", "source_ref", "content"} の辞書のリスト。
    Returns: (is_complete, confidence, reason) のタプル。confidence は
    完了時のみ "HIGH"|"LOW"、未完了時は None。LLM 利用不可・パース失敗時は
    None（呼び出し側は自動判定なしとして扱う）。
    """
    if not evidence:
        return None
    try:
        from cli_utils import call_argus_llm
    except ImportError:
        logger.debug("cli_utils が利用不可。LLM 判定をスキップ。")
        return None

    evidence_text = "\n".join(
        f"- [出典: {e.get('source_type', '?')} / {e.get('held_at') or '?'} /"
        f" {e.get('source_ref') or '?'}] {(e.get('content') or '')[:400]}"
        for e in evidence[:10]
    )
    prompt = _LLM_COMPLETION_PROMPT.format(
        ai_content=ai_content[:500],
        evidence_text=evidence_text,
    )

    try:
        # rivault(Kimi-K2-Thinking, thinking無効化不可)が優先ルートの場合、
        # thinking で数千トークンを消費するため max_tokens は多めに確保する。
        result = call_argus_llm(prompt, timeout=60, max_tokens=4096).strip()
        # think ブロックは call_argus_llm 内で除去済みだが、念のため前置き文言が
        # 残っていてもマッチするよう search でテキスト全体から探す。
        m = _LLM_COMPLETION_RE.search(result)
        if m:
            confidence = m.group(1).upper()
            reason = m.group(2).strip(": 　").strip() or "LLMが完了と判定"
            return (True, confidence, reason)
        if _LLM_NO_RE.search(result):
            return (False, None, "")
        logger.warning("完了シグナル LLM 判定: 期待した形式で応答が得られず: %s", result[:100])
    except Exception as e:
        logger.warning("完了シグナル LLM 判定エラー: %s", e)

    return None


def _get_activity_evidence(ctx, ai_row: dict) -> list[dict]:
    """qa_index.db から、同一スレッド以外での完了報告・成果物提出等の証拠を検索する。

    config `evidence_from_index`（既定 false）が有効な場合のみ動作する
    （段階ロールアウト用。qa_index 未構築・検索失敗時は Patrol 全体を落とさない
    よう例外を握りつぶし [] を返す）。
    """
    cfg = ctx.config.get("patrol", {}).get("completion_detection", {})
    if not cfg.get("evidence_from_index", False):
        return []

    try:
        from enrich.knowledge_context import extract_topic_keywords

        content = ai_row.get("content") or ""
        keywords = extract_topic_keywords(content)
        query = " ".join(keywords) if keywords else content
        if not query.strip():
            return []

        qa_index_path = ctx.data_dir / "qa_index.db"
        if not qa_index_path.exists():
            return []

        since_date = None
        if cfg.get("evidence_since_extracted", True):
            extracted_at = ai_row.get("extracted_at") or ""
            if extracted_at:
                since_date = extracted_at[:10]

        from argus.retrieval import retrieve_chunks_hybrid

        chunks = retrieve_chunks_hybrid(
            query, qa_index_path,
            k=cfg.get("evidence_k", 6),
            since_date=since_date,
            index_name=cfg.get("evidence_index_name"),
        )
        return [
            {
                "source_type": c.get("source_type", "?"),
                "held_at": c.get("held_at", ""),
                "source_ref": c.get("source_ref", ""),
                "content": c.get("content", ""),
            }
            for c in chunks
        ]
    except Exception as e:
        logger.debug("AI #%s の qa_index 証拠取得エラー: %s", ai_row.get("id"), e)
        return []


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
    """統合 Slack DB (data/slack.db) からスレッドの最新返信テキストを取得する。"""
    from db_utils import open_pm_db

    db_path = data_dir / "slack.db"
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
