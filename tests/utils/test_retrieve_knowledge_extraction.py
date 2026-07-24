"""retrieve_knowledge_for_extraction の keyword_mode 結線テスト。

LLM / SudachiPy / retrieve_chunks_hyde は monkeypatch し、実 LLM・実 DB 検索には触れない。
"""
import pytest


@pytest.fixture
def qa_db_path(tmp_path):
    p = tmp_path / "qa_index.db"
    p.touch()
    return p


def _patch_pipeline(monkeypatch, *, llm_keywords, sudachi_keywords=None, capture=None):
    import argus.retrieval as retrieval
    import enrich.knowledge_context as kc

    def fake_llm(text, **kw):
        return llm_keywords
    monkeypatch.setattr(kc, "extract_topic_keywords_llm", fake_llm)

    def fake_sudachi(text):
        return sudachi_keywords or ["sudachi_kw"]
    monkeypatch.setattr(kc, "extract_topic_keywords", fake_sudachi)

    def fake_hyde(search_query, qa_db, k=20, since_date=None, index_name=None,
                  skip_keyword_extract=False):
        if capture is not None:
            capture["search_query"] = search_query
            capture["skip_keyword_extract"] = skip_keyword_extract
        return []
    monkeypatch.setattr(retrieval, "retrieve_chunks_hyde", fake_hyde)
    monkeypatch.setattr(retrieval, "rerank_chunks", lambda query, chunks, **kw: chunks)


# --------------------------------------------------------------------------- #
# LLM 成功 → skip_keyword_extract=True
# --------------------------------------------------------------------------- #


def test_llm_success_skips_keyword_extract(monkeypatch, qa_db_path):
    capture = {}
    _patch_pipeline(monkeypatch, llm_keywords=["MONAKA-Y", "NVL144"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("スレッド本文", qa_db_path=qa_db_path)

    assert capture["skip_keyword_extract"] is True
    assert capture["search_query"] == "MONAKA-Y NVL144"


# --------------------------------------------------------------------------- #
# LLM None → SudachiPy フォールバック、skip_keyword_extract=False
# --------------------------------------------------------------------------- #


def test_llm_none_falls_back_to_sudachi(monkeypatch, qa_db_path):
    capture = {}
    _patch_pipeline(
        monkeypatch, llm_keywords=None, sudachi_keywords=["sudachi_kw1"], capture=capture,
    )

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("スレッド本文", qa_db_path=qa_db_path)

    assert capture["skip_keyword_extract"] is False
    assert capture["search_query"] == "sudachi_kw1"


# --------------------------------------------------------------------------- #
# ARGUS_DISABLE_LLM_KEYWORDS=1 (auto既定) → LLM 不呼
# --------------------------------------------------------------------------- #


def test_env_disable_skips_llm_in_auto_mode(monkeypatch, qa_db_path):
    monkeypatch.setenv("ARGUS_DISABLE_LLM_KEYWORDS", "1")
    called = {"llm": False}
    capture = {}

    import argus.retrieval as retrieval
    import enrich.knowledge_context as kc

    def fake_llm(text, **kw):
        called["llm"] = True
        return ["should_not_happen"]
    monkeypatch.setattr(kc, "extract_topic_keywords_llm", fake_llm)
    monkeypatch.setattr(kc, "extract_topic_keywords", lambda t: ["sudachi_kw"])

    def fake_hyde(search_query, qa_db, k=20, since_date=None, index_name=None,
                  skip_keyword_extract=False):
        capture["search_query"] = search_query
        capture["skip_keyword_extract"] = skip_keyword_extract
        return []
    monkeypatch.setattr(retrieval, "retrieve_chunks_hyde", fake_hyde)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("スレッド本文", qa_db_path=qa_db_path)

    assert called["llm"] is False
    assert capture["skip_keyword_extract"] is False
    assert capture["search_query"] == "sudachi_kw"


# --------------------------------------------------------------------------- #
# keyword_mode 明示指定は env より優先
# --------------------------------------------------------------------------- #


def test_keyword_mode_llm_overrides_env_disable(monkeypatch, qa_db_path):
    monkeypatch.setenv("ARGUS_DISABLE_LLM_KEYWORDS", "1")
    capture = {}
    _patch_pipeline(monkeypatch, llm_keywords=["MONAKA-Y"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction(
        "スレッド本文", qa_db_path=qa_db_path, keyword_mode="llm",
    )

    assert capture["skip_keyword_extract"] is True
    assert capture["search_query"] == "MONAKA-Y"


def test_keyword_mode_sudachi_skips_llm_even_without_env(monkeypatch, qa_db_path):
    called = {"llm": False}
    capture = {}

    import argus.retrieval as retrieval
    import enrich.knowledge_context as kc

    def fake_llm(text, **kw):
        called["llm"] = True
        return ["should_not_be_used"]
    monkeypatch.setattr(kc, "extract_topic_keywords_llm", fake_llm)
    monkeypatch.setattr(kc, "extract_topic_keywords", lambda t: ["sudachi_kw"])

    def fake_hyde(search_query, qa_db, k=20, since_date=None, index_name=None,
                  skip_keyword_extract=False):
        capture["search_query"] = search_query
        capture["skip_keyword_extract"] = skip_keyword_extract
        return []
    monkeypatch.setattr(retrieval, "retrieve_chunks_hyde", fake_hyde)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction(
        "スレッド本文", qa_db_path=qa_db_path, keyword_mode="sudachi",
    )

    assert called["llm"] is False
    assert capture["search_query"] == "sudachi_kw"
    assert capture["skip_keyword_extract"] is False


# --------------------------------------------------------------------------- #
# qa_db 不存在 → 空文字（回帰）
# --------------------------------------------------------------------------- #


def test_qa_db_missing_returns_empty(tmp_path):
    from cli_utils import retrieve_knowledge_for_extraction
    result = retrieve_knowledge_for_extraction("text", qa_db_path=tmp_path / "nonexistent.db")
    assert result == ""


# --------------------------------------------------------------------------- #
# llm_rerank 結線（rerank_chunks への use_llm 伝播）
# --------------------------------------------------------------------------- #


def _patch_pipeline_with_chunks(monkeypatch, *, llm_keywords, capture=None):
    """retrieve_chunks_hyde が非空チャンクを返すようにし、rerank_chunks に
    渡される kwargs を capture に記録する。"""
    import argus.retrieval as retrieval
    import enrich.knowledge_context as kc

    monkeypatch.setattr(kc, "extract_topic_keywords_llm", lambda text, **kw: llm_keywords)
    monkeypatch.setattr(kc, "extract_topic_keywords", lambda t: ["sudachi_kw"])
    monkeypatch.setattr(retrieval, "retrieve_chunks_hyde",
                        lambda *a, **kw: [{"content": "chunk1"}])

    def fake_rerank(query, chunks, **kw):
        if capture is not None:
            capture.update(kw)
        return chunks
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank)


def test_llm_rerank_true_passes_use_llm_true(monkeypatch, qa_db_path):
    capture = {}
    _patch_pipeline_with_chunks(monkeypatch, llm_keywords=["kw"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("text", qa_db_path=qa_db_path, llm_rerank=True)

    assert capture.get("use_llm") is True


def test_llm_rerank_none_without_env_is_enabled_by_default(monkeypatch, qa_db_path):
    """既定（opt-out）: env 未設定なら LLM re-rank は有効。"""
    capture = {}
    _patch_pipeline_with_chunks(monkeypatch, llm_keywords=["kw"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("text", qa_db_path=qa_db_path)

    assert capture.get("use_llm") is True


def test_llm_rerank_none_with_disable_env_is_disabled(monkeypatch, qa_db_path):
    monkeypatch.setenv("ARGUS_DISABLE_LLM_RERANK", "1")
    capture = {}
    _patch_pipeline_with_chunks(monkeypatch, llm_keywords=["kw"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("text", qa_db_path=qa_db_path)

    assert capture.get("use_llm") is False


def test_llm_rerank_false_overrides_default_enabled(monkeypatch, qa_db_path):
    """bool 明示指定は env より優先（keyword_mode と同じ流儀）。
    env 未設定（既定有効）でも llm_rerank=False を明示すれば無効になる。"""
    capture = {}
    _patch_pipeline_with_chunks(monkeypatch, llm_keywords=["kw"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("text", qa_db_path=qa_db_path, llm_rerank=False)

    assert capture.get("use_llm") is False


def test_llm_rerank_true_overrides_disable_env(monkeypatch, qa_db_path):
    """bool 明示指定は env より優先: ARGUS_DISABLE_LLM_RERANK=1 でも
    llm_rerank=True を明示すれば有効になる。"""
    monkeypatch.setenv("ARGUS_DISABLE_LLM_RERANK", "1")
    capture = {}
    _patch_pipeline_with_chunks(monkeypatch, llm_keywords=["kw"], capture=capture)

    from cli_utils import retrieve_knowledge_for_extraction
    retrieve_knowledge_for_extraction("text", qa_db_path=qa_db_path, llm_rerank=True)

    assert capture.get("use_llm") is True
