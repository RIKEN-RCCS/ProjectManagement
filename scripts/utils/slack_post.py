"""Slack の整形ユーティリティと**投稿ファネル**。

GitHub Flavored Markdown を Slack mrkdwn に変換し、Slack section block の
文字数制限内に分割する。あわせて **Slack 投稿の唯一の通り道**（`post_message` /
`post_ephemeral` / `update_message` / `upload_file`）を提供する。

**このファイルは以前は整形ヘルパだけで、輸送層ではなかった**（設計文書 §4.2 が
「`slack_post.py` は輸送層ではない」と実測で指摘した箇所）。SDK 直叩き 25 箇所を
ここへ移送して、送信前の検査と egress ログを1箇所に集約する。
"""
import logging
import re

logger = logging.getLogger(__name__)

# Slack section block の text は 3000 文字上限。超過するとブロック全体が無音で破棄される。
_SLACK_SECTION_LIMIT = 2900  # 安全マージン


def _to_slack_mrkdwn(text: str) -> str:
    """GitHub Flavored Markdown を Slack mrkdwn に変換。

    - `## heading` / `### heading` → `*heading*`
    - `**bold**` → `*bold*`
    - 入れ子箇条書き (`- ` / `  - ` / `    - `) は section block では
      先頭スペースが消えてフラット表示になるため、Unicode のブレット文字と
      NBSP (　) インデントに置換して階層感を保つ:
        `- item`     → `• item`
        `  - item`   → `　　◦ item`
        `    - item` → `　　　　▪ item`
    """
    # ヘッダー (## ... / ### ...) を太字に変換
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*([^*\n]+?)\*\*', r'*\1*', text)

    # 箇条書きを Unicode ブレット + NBSP インデントに変換
    def _bullet(m: re.Match) -> str:
        leading = m.group(1)
        spaces = leading.replace("\t", "    ")
        depth = len(spaces) // 2
        if depth >= 2:
            marker = "▪"
        elif depth == 1:
            marker = "◦"
        else:
            marker = "•"
        indent = "　　" * depth
        return f"{indent}{marker} "
    text = re.sub(r'^([ \t]*)[-*]\s+', _bullet, text, flags=re.MULTILINE)

    return text


def _split_mrkdwn_to_blocks(text: str) -> list[dict]:
    """長文 mrkdwn を Slack section block の上限内で分割する。

    改行優先で区切り、超過する単一行は文字数で強制切断する。
    """
    blocks: list[dict] = []
    buf = ""
    for line in text.split("\n"):
        # 単一行が上限を超える場合は強制分割
        while len(line) > _SLACK_SECTION_LIMIT:
            if buf:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
                buf = ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line[:_SLACK_SECTION_LIMIT]}})
            line = line[_SLACK_SECTION_LIMIT:]
        # 通常の改行単位で詰める
        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) > _SLACK_SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = line
        else:
            buf = candidate
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks

# --------------------------------------------------------------------------- #
# 投稿ファネル（docs/security-architecture.md §4.2）
#
# **Slack にはこれまで輸送層のファネルが存在せず、SDK 直叩きが 25 箇所あった。**
# ここに集約して、送信前の検査と egress ログを1箇所で行う。
#
# 移送の指針: `client.chat_postMessage(**kw)` → `slack_post.post_message(client, **kw)`
# のように機械的に置換できる形にしてある（kwargs はそのまま透過する）。
# --------------------------------------------------------------------------- #

_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"


class SlackEgressBlocked(RuntimeError):
    """送信前の検査で止めた（canary 検出・ゼロ幅文字）。"""


def _payload_text(kwargs: dict) -> str:
    """検査対象になるテキストを kwargs から集める（text と blocks の中身）。"""
    import json as _json

    parts = [str(kwargs.get("text") or "")]
    blocks = kwargs.get("blocks")
    if blocks:
        try:
            parts.append(_json.dumps(blocks, ensure_ascii=False))
        except Exception:
            parts.append(str(blocks))
    parts.append(str(kwargs.get("initial_comment") or ""))
    return "\n".join(p for p in parts if p)


def scan_text_for_egress(text: str, conn=None) -> list[str]:
    """送信・合成の前にテキストを検査し、問題の理由を返す（空なら通過）。

    **公開関数にしてあるのは、Slack 以外の出口からも呼ぶため** — Canvas / Box の
    ファネルと、TTS のようにテキストが別形式へ変換される経路（§4.2 の順序制約）。
    mp3 になった後では検査できないので、合成前が唯一の検査点になる。
    """
    reasons = []
    try:
        from db_utils import active_canary_tokens
        for tok in (active_canary_tokens(conn) if conn is not None else []):
            if tok and tok in text:
                reasons.append(f"canary トークンが含まれています: {tok}")
    except Exception:
        logger.exception("[SLACK-EGRESS] canary 台帳の読み取りに失敗（検査は続行）")
    if any(ch in text for ch in _ZERO_WIDTH):
        reasons.append("ゼロ幅文字が含まれています（不可視の埋め込みの可能性）")
    return reasons


def _guard(kwargs: dict, conn=None, *, method: str, source: str = "") -> None:
    """送信前の検査と記録。**拒否は例外**（戻り値だと呼び出し側が無視できる）。

    検査するのは canary とゼロ幅文字だけである。**自然な散文に符号化されたものは
    通る**（TrojanStego 型）ので、これで内容が安全になるわけではない（P10）。
    """
    text = _payload_text(kwargs)
    reasons = scan_text_for_egress(text, conn)

    outcome = "blocked" if reasons else "ok"
    if conn is not None:
        try:
            from db_utils import record_tool_call
            record_tool_call(
                conn, session_id=source or "slack", seq=0, plane="egress",
                tool_name=f"slack:{method}",
                args={"channel": str(kwargs.get("channel") or ""), "chars": len(text)},
                outcome=outcome, block_reason="; ".join(reasons) or None,
            )
        except Exception:
            logger.exception("[SLACK-EGRESS] tool_calls への記録に失敗（送信判断は継続）")

    if reasons:
        raise SlackEgressBlocked(f"[SLACK-EGRESS] {method}: {'; '.join(reasons)}")


def post_message(client, *, conn=None, source: str = "", **kwargs):
    """`chat_postMessage` のファネル。"""
    _guard(kwargs, conn, method="chat_postMessage", source=source)
    return client.chat_postMessage(**kwargs)


def post_ephemeral(client, *, conn=None, source: str = "", **kwargs):
    """`chat_postEphemeral` のファネル。"""
    _guard(kwargs, conn, method="chat_postEphemeral", source=source)
    return client.chat_postEphemeral(**kwargs)


def update_message(client, *, conn=None, source: str = "", **kwargs):
    """`chat_update` のファネル。"""
    _guard(kwargs, conn, method="chat_update", source=source)
    return client.chat_update(**kwargs)


def upload_file(client, *, conn=None, source: str = "", **kwargs):
    """`files_upload_v2` のファネル。

    **ファイル本体の中身は検査していない。** 検査できるのは `initial_comment` 等の
    テキストだけで、mp3/mp4/xlsx の中身には canary もゼロ幅文字も適用できない。
    テキスト以外の成果物は**生成元テキストの検査をもって代える**（§4.2 の原則）。
    """
    _guard(kwargs, conn, method="files_upload_v2", source=source)
    return client.files_upload_v2(**kwargs)

def guard_outbound_text(text: str, *, transport: str, dest: str = "",
                        conn=None, source: str = "") -> None:
    """Slack 以外の輸送（Canvas / Box）から呼ぶ共通ガード。

    検査して記録し、問題があれば例外を投げる。**Slack のファネルと同じ検査・同じ台帳**を
    使うのは、出口ごとに基準が違うと「どの出口なら通るか」を探せてしまうため。
    """
    reasons = scan_text_for_egress(text, conn)
    if conn is not None:
        try:
            from db_utils import record_tool_call
            record_tool_call(
                conn, session_id=source or transport, seq=0, plane="egress",
                tool_name=f"{transport}:post",
                args={"dest": dest, "chars": len(text)},
                outcome="blocked" if reasons else "ok",
                block_reason="; ".join(reasons) or None,
            )
        except Exception:
            logger.exception("[EGRESS] tool_calls への記録に失敗（送信判断は継続）")
    if reasons:
        raise SlackEgressBlocked(f"[EGRESS] {transport}: {'; '.join(reasons)}")
