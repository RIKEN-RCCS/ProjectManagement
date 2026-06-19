"""Pure-function tests for pm_qa_server (no DB / no LLM)."""
from datetime import date

import pytest

from argus.retrieval import (
    _combined_score,
    _recency_score,
    _rrf_merge,
    sanitize_fts_query,
)


# --------------------------------------------------------------------------- #
# sanitize_fts_query
# --------------------------------------------------------------------------- #


def test_sanitize_empty_returns_empty():
    assert sanitize_fts_query("") == ""


def test_sanitize_punctuation_only_returns_stripped():
    # only punctuation → both branches reduce to whitespace, final strip returns ""
    assert sanitize_fts_query("？？？") == ""


def test_sanitize_long_token_preserved():
    out = sanitize_fts_query("スケールアウトネットワークの構成")
    # ひらがな "の" で分割 → "スケールアウトネットワーク" + "構成"
    # "構成" は 2 文字 < 3 → 除外。"スケールアウトネットワーク" はカタカナ連続 → 1 トークン
    assert "スケールアウトネットワーク" in out


def test_sanitize_short_tokens_dropped():
    # ひらがな分割後、3文字未満の部分はトークンに含まれない
    out = sanitize_fts_query("abc あいう xyz")
    assert "abc" in out
    assert "xyz" in out


# --------------------------------------------------------------------------- #
# _recency_score
# --------------------------------------------------------------------------- #


def test_recency_none_returns_half():
    assert _recency_score(None) == 0.5
    assert _recency_score("") == 0.5


def test_recency_invalid_date_returns_half():
    assert _recency_score("not-a-date") == 0.5


def test_recency_today_is_one():
    today = date(2026, 6, 19)
    assert _recency_score("2026-06-19", today) == pytest.approx(1.0)


def test_recency_half_life_is_half():
    """After _RECENCY_HALF_LIFE_DAYS (180), score is 0.5."""
    today = date(2026, 6, 19)
    held = date(2025, 12, 21)  # ~180 days earlier
    score = _recency_score(held.isoformat(), today)
    assert score == pytest.approx(0.5, abs=0.02)


def test_recency_double_half_life_is_quarter():
    today = date(2026, 6, 19)
    held = today.replace(year=today.year - 1)  # 365 days earlier
    score = _recency_score(held.isoformat(), today)
    # exp(-365/180 * ln2) ≈ 0.245
    assert score == pytest.approx(0.25, abs=0.02)


# --------------------------------------------------------------------------- #
# _combined_score
# --------------------------------------------------------------------------- #


def test_combined_no_rank_uses_default_bm25():
    today = date(2026, 6, 19)
    chunk = {"rank": None, "held_at": "2026-06-19"}
    score = _combined_score(chunk, today)
    # bm25_norm = 0.5, rec = 1.0
    expected = (1 - 0.4) * 0.5 + 0.4 * 1.0
    assert score == pytest.approx(expected)


def test_combined_rank_zero_max_bm25():
    today = date(2026, 6, 19)
    chunk = {"rank": 0.0, "held_at": "2026-06-19"}
    # r = -0 = 0, bm25_norm = 1/(1+0) = 1.0
    score = _combined_score(chunk, today)
    expected = (1 - 0.4) * 1.0 + 0.4 * 1.0
    assert score == pytest.approx(expected)


def test_combined_invalid_rank_uses_half_bm25():
    today = date(2026, 6, 19)
    chunk = {"rank": "invalid", "held_at": "2026-06-19"}
    score = _combined_score(chunk, today)
    expected = (1 - 0.4) * 0.5 + 0.4 * 1.0
    assert score == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# _rrf_merge
# --------------------------------------------------------------------------- #


def test_rrf_merge_fts_only():
    fts = [
        {"id": 1, "content": "a"},
        {"id": 2, "content": "b"},
    ]
    vec = []
    merged = _rrf_merge(fts, vec, k=5)
    assert [m["id"] for m in merged] == [1, 2]
    # rrf_score for rank 0 = 1/(60+0) = 1/60
    assert merged[0]["rrf_score"] == pytest.approx(1.0 / 60)


def test_rrf_merge_vec_only():
    fts = []
    vec = [{"id": 5, "content": "x"}]
    merged = _rrf_merge(fts, vec, k=5)
    assert [m["id"] for m in merged] == [5]
    # weight 0.4 / (60+0) = 0.4/60
    assert merged[0]["rrf_score"] == pytest.approx(0.4 / 60)


def test_rrf_merge_both_dedup():
    # id=1 is in both fts and vec → scores add up
    fts = [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]
    vec = [{"id": 1, "content": "a"}, {"id": 3, "content": "c"}]
    merged = _rrf_merge(fts, vec, k=5)
    ids = [m["id"] for m in merged]
    assert 1 in ids
    # id=1 should have higher rrf_score than id=2 or id=3 (which only get 1 rank each)
    score_1 = next(m["rrf_score"] for m in merged if m["id"] == 1)
    score_2 = next(m["rrf_score"] for m in merged if m["id"] == 2)
    assert score_1 > score_2


def test_rrf_merge_k_limit():
    fts = [{"id": i, "content": str(i)} for i in range(10)]
    merged = _rrf_merge(fts, [], k=3)
    assert len(merged) == 3
    assert [m["id"] for m in merged] == [0, 1, 2]


def test_rrf_merge_empty_returns_empty():
    assert _rrf_merge([], [], k=5) == []
