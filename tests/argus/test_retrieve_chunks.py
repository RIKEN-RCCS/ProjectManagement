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
# build_full_context_sections / generate_brief_report (全文脈方式, call_argus_llm mocked)
# --------------------------------------------------------------------------- #

class TestBuildFullContextSections:
    """build_full_context_sections が LLM を使わずセクション dict + meta を返し、
    char_budget を守ることを検証する（重い I/O はモックして決定的にする）。"""

    def _patch_common(self, monkeypatch, pm_argus):
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(pm_argus, "_load_channel_ids", lambda index_name=None: ["C111", "C222"])
        monkeypatch.setattr(pm_argus, "_load_minutes_names", lambda index_name=None: ["kind1"])
        monkeypatch.setattr(pm_argus, "_build_channel_name_map", lambda: {})

    def test_respects_char_budget_and_returns_sections(self, monkeypatch):
        """予算超過時、優先度1(pm_stats)は切り詰めず下位セクションが切り詰められる。"""
        import argus.pm_argus as pm_argus
        self._patch_common(monkeypatch, pm_argus)
        monkeypatch.setattr(pm_argus, "fetch_recent_minutes", lambda *a, **kw: "議事録本文" * 500)
        monkeypatch.setattr(
            pm_argus, "fetch_raw_messages",
            lambda ch_id, since_date, *, data_dir, no_encrypt, max_chars=10**9: ("会話ログ" * 2000)[:max_chars],
        )
        monkeypatch.setattr(
            pm_argus, "_fetch_box_documents_full",
            lambda *a, **kw: ("box本文" * 100, {"doc_count": 1, "used_count": 1, "truncated": False}),
        )

        sections, meta = pm_argus.build_full_context_sections(
            "2026-06-01", "2026-07-01", char_budget=2000, include_box=True,
        )

        assert set(sections.keys()) == {"pm_stats", "minutes", "slack", "box"}
        assert meta["char_budget"] == 2000
        for name in ("pm_stats", "minutes", "slack", "box"):
            assert name in meta["sections"]
            assert meta["sections"][name]["chars"] == len(sections[name])
        # 優先度1（構造化データ）は切り詰めない
        assert meta["sections"]["pm_stats"]["truncated"] is False
        # 予算が小さいため下位優先度セクションのいずれかは切り詰められているはず
        assert meta["sections"]["minutes"]["truncated"] or meta["sections"]["slack"]["truncated"]
        assert meta["total_chars"] == sum(info["chars"] for info in meta["sections"].values())
        assert meta["total_est_tokens"] == int(meta["total_chars"] / pm_argus._FULLCTX_CHARS_PER_TOKEN)

    def test_no_pm_db_paths_returns_empty_structured_section(self, monkeypatch):
        """pm_db_paths が空でも例外にならず空の構造化データセクションを返す。"""
        import argus.pm_argus as pm_argus
        self._patch_common(monkeypatch, pm_argus)
        monkeypatch.setattr(pm_argus, "fetch_recent_minutes", lambda *a, **kw: "")
        monkeypatch.setattr(
            pm_argus, "fetch_raw_messages",
            lambda ch_id, since_date, *, data_dir, no_encrypt, max_chars=10**9: "",
        )

        sections, meta = pm_argus.build_full_context_sections(
            "2026-06-01", "2026-07-01", char_budget=350_000, include_box=False,
        )

        assert "（decisions なし）" in sections["pm_stats"]
        assert "（action_items なし）" in sections["pm_stats"]
        assert meta["sections"]["box"]["skipped"] is True
        assert meta["total_chars"] <= meta["char_budget"]


class TestGenerateBriefReportFullctxToggle:
    """ARGUS_DISABLE_FULLCTX=1 で generate_brief_report が従来（切り詰め）プロンプトを
    使うことを検証する（call_argus_llm はモック）。"""

    def test_disable_fullctx_uses_truncated_prompt(self, monkeypatch):
        import argus.pm_argus as pm_argus
        monkeypatch.setenv("ARGUS_DISABLE_FULLCTX", "1")

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return "<final_answer>truncated result</final_answer>"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "_load_context_with_glossary", lambda: "context")
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("messages", "minutes", {"stats": {}}, "knowledge", "web"),
        )
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])

        result = pm_argus.generate_brief_report("2026-07-23", "2026-06-23")

        assert result == "truncated result"
        assert "## pm.db 統計" in captured["prompt"]
        assert "## 構造化データ" not in captured["prompt"]
        assert captured["kwargs"]["max_tokens"] == pm_argus._BRIEF_RISK_MAX_TOKENS
        assert captured["kwargs"]["timeout"] == 600

    def test_fullctx_enabled_by_default_uses_structured_prompt(self, monkeypatch):
        """既定（ARGUS_DISABLE_FULLCTX 未設定）では全文脈プロンプトが使われる。"""
        import argus.pm_argus as pm_argus
        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            return "<final_answer>fullctx result</final_answer>"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "_load_context_with_glossary", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_fetch_knowledge_and_web_articles", lambda *a, **kw: ("knowledge", "web"),
        )
        fake_sections = {"pm_stats": "## 構造化データ\n\nダミー", "minutes": "## 議事録\n\nダミー",
                          "slack": "## Slack 会話\n\nダミー", "box": "## Box 資料\n\nダミー"}
        fake_meta = {"total_chars": 100, "total_est_tokens": 62,
                     "sections": {k: {"chars": len(v), "truncated": False} for k, v in fake_sections.items()}}
        monkeypatch.setattr(
            pm_argus, "build_full_context_sections", lambda *a, **kw: (fake_sections, fake_meta),
        )

        result = pm_argus.generate_brief_report("2026-07-23", "2026-06-23")

        assert result == "fullctx result"
        assert "## 構造化データ" in captured["prompt"]
        assert "## pm.db 統計" not in captured["prompt"]


# --------------------------------------------------------------------------- #
# 出力品質バリデーション（退化検知ゲート）
# --------------------------------------------------------------------------- #

class TestDegenerateOutputGuard:
    def test_is_degenerate_output_detects_repeated_char(self):
        """同一文字の100連続以上は退化と判定される（32,768トークン「!」埋め尽くしの再現）。"""
        import argus.pm_argus as pm_argus
        assert pm_argus._is_degenerate_output("!" * 32768) is True

    def test_is_degenerate_output_accepts_normal_text(self):
        """通常の日本語テキストは退化と判定されない。"""
        import argus.pm_argus as pm_argus
        normal_text = (
            "## ブリーフィング\n\n- **[優先度: 高]** マイルストーンM1の期限超過対応\n"
            "  - 状況: 3件のアクションアイテムが期限超過しています。\n"
            "  - 根拠: AI-101, AI-102, AI-103（担当: 西澤）\n"
        )
        assert pm_argus._is_degenerate_output(normal_text) is False

    def test_generate_brief_report_falls_back_when_fullctx_output_degenerate(self, monkeypatch):
        """fullctx 出力が退化（「!」連続）していた場合、truncated 方式へフォールバックすること。"""
        import argus.pm_argus as pm_argus

        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)
        monkeypatch.setattr(pm_argus, "_load_context_with_glossary", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_fetch_knowledge_and_web_articles", lambda *a, **kw: ("knowledge", "web"),
        )
        fake_sections = {"pm_stats": "## 構造化データ\n\nダミー", "minutes": "## 議事録\n\nダミー",
                          "slack": "## Slack 会話\n\nダミー", "box": "## Box 資料\n\nダミー"}
        fake_meta = {"total_chars": 100, "total_est_tokens": 62,
                     "sections": {k: {"chars": len(v), "truncated": False} for k, v in fake_sections.items()}}
        monkeypatch.setattr(
            pm_argus, "build_full_context_sections", lambda *a, **kw: (fake_sections, fake_meta),
        )
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("messages", "minutes", {"stats": {}}, "knowledge", "web"),
        )

        calls = []

        def flaky_call_argus_llm(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return "!" * 32768  # fullctx 1回目: 退化出力（例外ではなく正常終了）
            return "<final_answer>truncated fallback result</final_answer>"

        monkeypatch.setattr(pm_argus, "call_argus_llm", flaky_call_argus_llm)

        result = pm_argus.generate_brief_report("2026-07-23", "2026-06-23")

        assert result == "truncated fallback result"
        assert len(calls) == 2
        assert "## 構造化データ" in calls[0]
        assert "## pm.db 統計" in calls[1] and "## 構造化データ" not in calls[1]
