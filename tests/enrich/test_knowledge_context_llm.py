"""extract_topic_keywords_llm の純関数寄りテスト（LLM は monkeypatch）。

Slack抽出ナレッジ検索の第一段 LLM 化（query rewrite 移植）。実在人名・実在アプリ名は使わない。
"""
from enrich.knowledge_context import extract_topic_keywords_llm, sanitize_fts_query

# --------------------------------------------------------------------------- #
# 正常系
# --------------------------------------------------------------------------- #


def test_normal_parse_returns_keyword_list(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "MONAKA-Y NVL144 スケールインネットワーク"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    result = extract_topic_keywords_llm("何かのSlackスレッド本文")
    assert result == ["MONAKA-Y", "NVL144", "スケールインネットワーク"]


def test_bullet_prefix_is_stripped(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "- MONAKA-Y NVL144"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    result = extract_topic_keywords_llm("スレッド本文")
    assert result == ["MONAKA-Y", "NVL144"]


def test_multiple_lines_uses_first_line_only(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "MONAKA-Y NVL144\n余計な2行目\n余計な3行目"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    result = extract_topic_keywords_llm("スレッド本文")
    assert result == ["MONAKA-Y", "NVL144"]


def test_16_tokens_with_dup_are_capped_at_15(monkeypatch):
    tokens = [f"語彙{i:02d}" for i in range(16)]
    tokens.append("語彙00")  # 重複
    def fake_call(prompt, **kwargs):
        return " ".join(tokens)
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    result = extract_topic_keywords_llm("スレッド本文")
    assert len(result) == 15
    assert result == [f"語彙{i:02d}" for i in range(15)]


# --------------------------------------------------------------------------- #
# 劣化・失敗 → None
# --------------------------------------------------------------------------- #


def test_explanatory_sentence_with_period_returns_none(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "申し訳ありませんが該当するキーワードはありません。"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    assert extract_topic_keywords_llm("スレッド本文") is None


def test_empty_response_returns_none(monkeypatch):
    def fake_call(prompt, **kwargs):
        return ""
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    assert extract_topic_keywords_llm("スレッド本文") is None


def test_nashi_response_returns_none(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "なし"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    assert extract_topic_keywords_llm("スレッド本文") is None


def test_exception_returns_none(monkeypatch):
    def fake_call(prompt, **kwargs):
        raise RuntimeError("LLM down")
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    assert extract_topic_keywords_llm("スレッド本文") is None


def test_zero_valid_tokens_returns_none(monkeypatch):
    """全トークンが長さ制約(2〜30字)を外れる場合は None。"""
    def fake_call(prompt, **kwargs):
        return "A"  # 1文字のみ → 有効トークン0
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    assert extract_topic_keywords_llm("スレッド本文") is None


# --------------------------------------------------------------------------- #
# 切り詰め（head+tail）
# --------------------------------------------------------------------------- #


def test_head_tail_truncation_hides_middle_marker(monkeypatch):
    """max_input_chars 超過時、中間部の目印がプロンプトに含まれないこと。"""
    head = "先頭" * 1600  # 3200字超（3000字超過分は切られる）
    middle_marker = "MIDDLE_MARKER_SHOULD_BE_DROPPED"
    tail = "末尾" * 600  # 1200字（末尾1000字は残る）
    text = head + middle_marker + tail

    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return "キーワード"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    extract_topic_keywords_llm(text, max_input_chars=4000)
    assert middle_marker not in captured["prompt"]


def test_short_text_is_not_truncated(monkeypatch):
    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return "キーワード"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    text = "短いスレッド本文"
    extract_topic_keywords_llm(text, max_input_chars=4000)
    assert text in captured["prompt"]


# --------------------------------------------------------------------------- #
# think ブロック除去
# --------------------------------------------------------------------------- #


def test_think_block_is_stripped(monkeypatch):
    def fake_call(prompt, **kwargs):
        return "<think>推論過程</think>MONAKA-Y NVL144"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    result = extract_topic_keywords_llm("スレッド本文")
    assert result == ["MONAKA-Y", "NVL144"]


# --------------------------------------------------------------------------- #
# 呼び出しkwargs検査
# --------------------------------------------------------------------------- #


def test_call_kwargs_temperature_zero_and_max_tokens(monkeypatch):
    captured = {}

    def fake_call(prompt, **kwargs):
        captured.update(kwargs)
        return "MONAKA-Y"
    monkeypatch.setattr("utils.llm.call_argus_llm", fake_call)

    extract_topic_keywords_llm("スレッド本文", timeout=45)
    assert captured.get("temperature") == 0.0
    assert captured.get("max_tokens") == 4096
    assert captured.get("timeout") == 45


# --------------------------------------------------------------------------- #
# sanitize_fts_query — argus.retrieval への一本化（重複実装廃止）の回帰確認
# --------------------------------------------------------------------------- #


def test_sanitize_fts_query_splits_fullwidth_parentheses():
    """全角括弧が空白化され、括弧内外が別トークンとして分割される
    （argus.retrieval.sanitize_fts_query 一本化により knowledge_context 側にも反映）。"""
    result = sanitize_fts_query("外部GPU計算リソース（NVL72クラス）の確保方針")
    tokens = result.split()
    assert "外部GPU計算リソース（NVL72クラス）の確保方針" not in tokens
    assert any("NVL72" in t for t in tokens)


def test_sanitize_fts_query_choonpu_is_preserved():
    """長音符「ー」は語の一部として保持される（例: サーバー）。"""
    result = sanitize_fts_query("サーバーの構成について")
    assert "サーバー" in result or any("サーバー" in t for t in result.split())
