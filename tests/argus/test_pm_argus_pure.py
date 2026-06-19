"""Pure-function tests for pm_argus Slack block helpers."""
from utils.slack_post import (
    _SLACK_SECTION_LIMIT,
    _split_mrkdwn_to_blocks,
    _to_slack_mrkdwn,
)


# --------------------------------------------------------------------------- #
# _to_slack_mrkdwn
# --------------------------------------------------------------------------- #


def test_mrkdwn_heading_to_bold():
    assert _to_slack_mrkdwn("## 見出し") == "*見出し*"


def test_mrkdwn_h3_heading():
    assert _to_slack_mrkdwn("### 詳細") == "*詳細*"


def test_mrkdwn_bold_conversion():
    assert _to_slack_mrkdwn("これは **重要** です") == "これは *重要* です"


def test_mrkdwn_bullet_depth0():
    out = _to_slack_mrkdwn("- 項目1")
    assert "•" in out
    assert "項目1" in out


def test_mrkdwn_bullet_depth1_two_spaces():
    out = _to_slack_mrkdwn("  - 子項目")
    assert "◦" in out


def test_mrkdwn_bullet_depth1_four_spaces_treated_as_depth2():
    # 4 spaces → depth = 4//2 = 2 → marker "▪"
    out = _to_slack_mrkdwn("    - 孫項目")
    assert "▪" in out


def test_mrkdwn_plain_text_unchanged():
    assert _to_slack_mrkdwn("hello world") == "hello world"


# --------------------------------------------------------------------------- #
# _split_mrkdwn_to_blocks
# --------------------------------------------------------------------------- #


def test_split_empty_returns_empty():
    assert _split_mrkdwn_to_blocks("") == []


def test_split_short_text_single_block():
    out = _split_mrkdwn_to_blocks("短いテキスト")
    assert len(out) == 1
    assert out[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "短いテキスト"}}


def test_split_long_single_line_forced_cut():
    long_line = "a" * (_SLACK_SECTION_LIMIT + 100)
    out = _split_mrkdwn_to_blocks(long_line)
    # First block holds the first _SLACK_SECTION_LIMIT chars, then buf
    # then remaining 100 chars triggers another split
    assert len(out) >= 2
    assert all(len(b["text"]["text"]) <= _SLACK_SECTION_LIMIT for b in out)
    # Content should reconstruct to original
    assert "".join(b["text"]["text"] for b in out) == long_line


def test_split_multi_line_breaks_on_limit():
    # Two lines, where combined > limit but each < limit
    line1 = "b" * (_SLACK_SECTION_LIMIT - 10)
    line2 = "c" * (_SLACK_SECTION_LIMIT - 10)
    out = _split_mrkdwn_to_blocks(line1 + "\n" + line2)
    # Should split into 2 blocks because combined length > _SLACK_SECTION_LIMIT
    assert len(out) == 2


def test_split_preserves_newlines_within_limit():
    text = "行1\n行2\n行3"
    out = _split_mrkdwn_to_blocks(text)
    assert len(out) == 1
    assert out[0]["text"]["text"] == "行1\n行2\n行3"
