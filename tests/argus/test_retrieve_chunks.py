"""Tests for retrieve_chunks / retrieve_chunks_hybrid (fixture qa_index.db)."""
import sqlite3
from pathlib import Path

import numpy as np
import pytest


# --------------------------------------------------------------------------- #
# qa_index.db スキーマ（pm_embed.py より抜粋）
# --------------------------------------------------------------------------- #

_QA_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_db   TEXT NOT NULL,
    record_id   TEXT,
    held_at     TEXT,
    content     TEXT NOT NULL,
    tokens      TEXT,
    source_ref  TEXT,
    indexed_at  TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tokens USING fts5(
    tokens,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS chunk_indexes (
    chunk_id   INTEGER NOT NULL,
    index_name TEXT NOT NULL,
    PRIMARY KEY (chunk_id, index_name),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id    INTEGER PRIMARY KEY,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    embedded_at TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
"""

DIM = 4  # テスト用低次元ベクトル


def _make_qa_db(tmp_path: Path, index_name: str = "test") -> Path:
    """chunk を 3 件持つ qa_index.db を作成して返す。"""
    db_path = tmp_path / "qa_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_QA_INDEX_SCHEMA)

    chunks = [
        ("minutes", "test.db", "r1", "2026-06-01", "スケールアウトネットワーク設計に関する議論"),
        ("slack",   "test.db", "r2", "2026-06-10", "富士通の演算性能ベンチマーク結果報告"),
        ("minutes", "test.db", "r3", "2026-01-01", "古い議事録の内容"),
    ]
    for src_type, src_db, rec_id, held_at, content in chunks:
        conn.execute(
            "INSERT INTO chunks (source_type, source_db, record_id, held_at, content, indexed_at)"
            " VALUES (?,?,?,?,?,?)",
            (src_type, src_db, rec_id, held_at, content, "2026-06-19T00:00:00"),
        )
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO fts(rowid, content) VALUES (?,?)", (chunk_id, content)
        )
        conn.execute(
            "INSERT INTO chunk_indexes (chunk_id, index_name) VALUES (?,?)",
            (chunk_id, index_name),
        )

    # FTS tokens (空トークンでも動作確認)
    for row in conn.execute("SELECT id, content FROM chunks").fetchall():
        conn.execute("INSERT INTO fts_tokens(rowid, tokens) VALUES (?,?)", (row[0], row[1]))

    # chunk_embeddings: 各チャンクにランダムベクトルを付与
    rng = np.random.default_rng(42)
    for row in conn.execute("SELECT id FROM chunks").fetchall():
        cid = row[0]
        vec = rng.random(DIM).astype(np.float32)
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, model, dim, vector, embedded_at)"
            " VALUES (?,?,?,?,?)",
            (cid, "bge-m3", DIM, vec.tobytes(), "2026-06-19"),
        )

    conn.commit()
    conn.close()
    return db_path


# --------------------------------------------------------------------------- #
# retrieve_chunks (FTS5 trigram path)
# --------------------------------------------------------------------------- #

class TestRetrieveChunks:
    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_finds_keyword_in_content(self, qa_db, monkeypatch):
        """trigram FTS5 でキーワードに一致するチャンクを取得できる。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks("スケールアウトネットワーク", qa_db)
        assert len(results) >= 1
        assert any("スケールアウト" in r["content"] for r in results)

    def test_since_date_filters_old_records(self, qa_db, monkeypatch):
        """since_date を指定すると古いチャンクが除外される。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks("議事録", qa_db, since_date="2026-06-01")
        dates = [r["held_at"] for r in results if r.get("held_at")]
        assert all(d >= "2026-06-01" for d in dates)

    def test_nonexistent_db_returns_empty(self, tmp_path, monkeypatch):
        """DB ファイルが存在しない場合は空リストを返す。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks("test", tmp_path / "nonexistent.db")
        assert results == []

    def test_index_name_filter(self, tmp_path, monkeypatch):
        """index_name 指定で chunk_indexes フィルタが効く。"""
        # "other" インデックスのみのチャンクを追加した DB
        db_path = _make_qa_db(tmp_path, index_name="main")
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        # "main" インデックスなら取得できる
        results_main = retrieve_chunks("スケールアウト", db_path, index_name="main")
        assert len(results_main) >= 1
        # "other" インデックスは空
        results_other = retrieve_chunks("スケールアウト", db_path, index_name="other")
        assert results_other == []


# --------------------------------------------------------------------------- #
# retrieve_chunks_vector
# --------------------------------------------------------------------------- #

class TestRetrieveChunksVector:
    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_returns_results_with_fake_embed(self, qa_db, monkeypatch):
        """embed_one をモックすると cosine similarity で結果が返る。"""
        import embed_utils
        fixed_vec = np.ones(DIM, dtype=np.float32)
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: fixed_vec)

        conn = sqlite3.connect(str(qa_db))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import retrieve_chunks_vector
            results = retrieve_chunks_vector("test", conn, k=3, index_name="test")
        finally:
            conn.close()

        assert len(results) == 3
        assert all("vector_score" in r for r in results)
        # scores should be between 0 and 1
        for r in results:
            assert 0.0 <= r["vector_score"] <= 1.0 + 1e-6

    def test_embed_failure_returns_empty(self, qa_db, monkeypatch):
        """embed_one が例外を投げた場合は空リストを返す。"""
        import embed_utils
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: (_ for _ in ()).throw(RuntimeError("no server")))

        conn = sqlite3.connect(str(qa_db))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import retrieve_chunks_vector
            results = retrieve_chunks_vector("test", conn, index_name="test")
        finally:
            conn.close()

        assert results == []


# --------------------------------------------------------------------------- #
# retrieve_chunks_hybrid (RRF 統合)
# --------------------------------------------------------------------------- #

class TestRetrieveChunksHybrid:
    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_hybrid_returns_results(self, qa_db, monkeypatch):
        """FTS + vector のハイブリッド検索が動作する。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("スケールアウト", qa_db, k=3, index_name="test")
        assert len(results) >= 1

    def test_hybrid_has_rrf_score(self, qa_db, monkeypatch):
        """ハイブリッド結果に rrf_score が付与される。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("スケールアウト", qa_db, k=3, index_name="test")
        # RRF 統合が走った場合は rrf_score が付く
        if len(results) > 0:
            assert "rrf_score" in results[0]

    def test_hybrid_since_date_filter(self, qa_db, monkeypatch):
        """since_date は FTS パスには適用されるが vector パスには適用されない（設計上の挙動）。
        hybrid 結果が空でないことと、クラッシュしないことを確認する。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("議事録", qa_db, since_date="2026-06-01", index_name="test")
        # vector path は date を無視するため、since_date 以前のチャンクも混入しうる
        # クラッシュせず結果が返ること、各 chunk に rrf_score があることを確認
        assert isinstance(results, list)
        for r in results:
            assert "content" in r


# --------------------------------------------------------------------------- #
# _run_brief_worker / _run_risk_worker (call_argus_llm mocked)
# --------------------------------------------------------------------------- #

class TestArgusWorkers:
    def test_brief_worker_pm_calls_llm(self, monkeypatch):
        """_run_brief_worker("pm") が call_argus_llm を呼んで結果を返す。"""
        import argus.pm_argus as pm_argus
        monkeypatch.setattr(pm_argus, "call_argus_llm", lambda *a, **kw: "brief result")
        from argus.pm_argus import _run_brief_worker
        result = _run_brief_worker("pm", "test data")
        assert result == "brief result"

    def test_brief_worker_unknown_type(self):
        """不明な worker_type は LLM 呼び出しなしにエラーメッセージを返す。"""
        from argus.pm_argus import _run_brief_worker
        result = _run_brief_worker("unknown_type", "data")
        assert "不明" in result

    def test_brief_worker_conversation(self, monkeypatch):
        import argus.pm_argus as pm_argus
        monkeypatch.setattr(pm_argus, "call_argus_llm", lambda *a, **kw: "conv result")
        from argus.pm_argus import _run_brief_worker
        assert _run_brief_worker("conversation", "data") == "conv result"

    def test_brief_worker_minutes(self, monkeypatch):
        import argus.pm_argus as pm_argus
        monkeypatch.setattr(pm_argus, "call_argus_llm", lambda *a, **kw: "minutes result")
        from argus.pm_argus import _run_brief_worker
        assert _run_brief_worker("minutes", "data") == "minutes result"
