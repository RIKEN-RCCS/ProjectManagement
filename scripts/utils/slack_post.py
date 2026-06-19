"""Slack mrkdwn フォーマット・Block Kit 分割ユーティリティ。

GitHub Flavored Markdown を Slack mrkdwn に変換し、
Slack section block の文字数制限内に分割する。
"""
import re

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
