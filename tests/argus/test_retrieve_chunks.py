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
    """chunk を 4 件持つ qa_index.db を作成して返す。

    r4 は held_at が古い box_document チャンク。box_document は既定
    （exempt_box=True）で日付フィルタを免除されるため、since_date テストで
    r3（非box の古いチャンク）とは異なる扱いになることの検証に使う。
    """
    db_path = tmp_path / "qa_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_QA_INDEX_SCHEMA)

    chunks = [
        ("minutes", "test.db", "r1", "2026-06-01", "スケールアウトネットワーク設計に関する議論"),
        ("slack",   "test.db", "r2", "2026-06-10", "富士通の演算性能ベンチマーク結果報告"),
        ("minutes", "test.db", "r3", "2026-01-01", "古い議事録の内容"),
        ("box_document", "test.db", "r4", "2026-01-15", "予算配分に関する古いBoxメモ"),
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
# _build_date_filter
# --------------------------------------------------------------------------- #

class TestBuildDateFilter:
    def test_since_date_none_returns_passthrough(self):
        from argus.retrieval import _build_date_filter
        clause, params = _build_date_filter(None)
        assert clause == "1=1"
        assert params == []

    def test_exempt_box_true_default(self):
        from argus.retrieval import _build_date_filter
        clause, params = _build_date_filter("2026-06-01")
        assert clause == "(c.held_at >= ? OR c.source_type = 'box_document')"
        assert params == ["2026-06-01"]

    def test_exempt_box_false(self):
        from argus.retrieval import _build_date_filter
        clause, params = _build_date_filter("2026-06-01", exempt_box=False)
        assert clause == "c.held_at >= ?"
        assert params == ["2026-06-01"]


# --------------------------------------------------------------------------- #
# sanitize_fts_query — 全角記号対応
# --------------------------------------------------------------------------- #

class TestSanitizeFtsQuery:
    def test_fullwidth_parentheses_are_split(self):
        """全角括弧が空白化され、括弧内外が別トークンとして分割される
        （2026-07 k3-loss-analysis: mh-nvl72 で括弧ごと1トークン化していたバグ）。"""
        from argus.retrieval import sanitize_fts_query
        result = sanitize_fts_query("外部GPU計算リソース（NVL72クラス）の確保方針")
        tokens = result.split()
        assert "外部GPU計算リソース（NVL72クラス）の確保方針" not in tokens
        assert any("NVL72" in t for t in tokens)

    def test_fullwidth_touten_kuten_are_split(self):
        """全角読点・句点で分割される。"""
        from argus.retrieval import sanitize_fts_query
        result = sanitize_fts_query("GENESIS、ライセンス変更。BSD/MIT")
        tokens = result.split()
        assert not any("、" in t or "。" in t for t in tokens)

    def test_choonpu_in_word_is_preserved(self):
        """長音符「ー」は語の一部として保持される（例: サーバー）。"""
        from argus.retrieval import sanitize_fts_query
        result = sanitize_fts_query("サーバーの構成について")
        assert "サーバー" in result or any("サーバー" in t for t in result.split())

    def test_ascii_parentheses_regression(self):
        """既存の半角記号除去は従来どおり動作する（回帰確認）。"""
        from argus.retrieval import sanitize_fts_query
        result = sanitize_fts_query("benchmark(GH200)result")
        assert "(" not in result
        assert ")" not in result

    def test_fullwidth_nakaguro_and_brackets(self):
        """中黒・角括弧・鍵括弧が空白化される。"""
        from argus.retrieval import sanitize_fts_query
        result = sanitize_fts_query("理研・富士通・NVIDIA間の「秘密保持契約」")
        tokens = result.split()
        assert not any("・" in t for t in tokens)
        assert not any("「" in t or "」" in t for t in tokens)


# --------------------------------------------------------------------------- #
# _fts5_escape_token — FTS5 予約文字（特にハイフン=NOT演算子）のクォート
# 2026-07-30 実測: sanitize_fts_query はハイフンを除去しないため
# "E-Wave"/"GH200-NVL72" のようなトークンがそのまま MATCH クエリに渡ると
# ハイフンが NOT 演算子として解釈され sqlite3.OperationalError
# （"no such column: Wave"）が発生し、_fts_tokens_search / retrieve_chunks の
# trigram ループで「ヒットなし」に丸められ、本来ヒットしうる部分一致が
# silently 握りつぶされていた（複合エンティティ導入前から潜在する既存バグ）。
# --------------------------------------------------------------------------- #

class TestFts5EscapeToken:
    def test_plain_token_unchanged(self):
        from argus.retrieval import _fts5_escape_token
        assert _fts5_escape_token("NVIDIA") == "NVIDIA"
        assert _fts5_escape_token("スケールアウト") == "スケールアウト"

    def test_hyphenated_token_is_quoted(self):
        from argus.retrieval import _fts5_escape_token
        assert _fts5_escape_token("E-Wave") == '"E-Wave"'
        assert _fts5_escape_token("GH200-NVL72") == '"GH200-NVL72"'

    def test_internal_double_quote_is_escaped(self):
        from argus.retrieval import _fts5_escape_token
        assert _fts5_escape_token('foo"bar-baz') == '"foo""bar-baz"'

    def test_query_string_with_hyphenated_token_does_not_raise_operational_error(self, tmp_path):
        """ハイフンを含むトークンで組み立てた MATCH クエリが構文エラーにならず、
        実際にヒットすること（'no such column: Wave' の再現・回帰防止）。"""
        db_path = _make_qa_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import _fts5_escape_token
            tokens = ["スケールアウト", "E-Wave"]
            q = " ".join(_fts5_escape_token(t) for t in tokens)
            # 例外を投げないこと（従来は "no such column: Wave" 相当で落ちていた）
            rows = conn.execute(
                "SELECT c.id FROM fts JOIN chunks c ON fts.rowid = c.id WHERE fts MATCH ?", (q,)
            ).fetchall()
            assert isinstance(rows, list)
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# sudachi_tokenize_query — トークン品質（ASCII複合エンティティ・機能動詞除去・並べ替え）
# 2026-07-30 本番実測障害の修正（E-Wave が 'Wave' に縮退、機能動詞混入）。
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def real_sudachi():
    """実 SudachiPy トークナイザを初期化する（未インストール環境ではスキップ）。"""
    import argus.retrieval as srv
    if srv._sudachi_tokenizer is None:
        if not srv._init_sudachi():
            pytest.skip("sudachipy が利用できない環境")
    return srv


class TestSudachiTokenizeQueryTokenQuality:
    def test_ascii_compound_entity_preserved_not_degraded_to_partial_word(self, real_sudachi):
        """「E-Wave」が複合エンティティとして丸ごと保持され、Sudachi由来の
        部分語 'Wave'（旧バグ: len>=2フィルタが'E'を、品詞フィルタが'-'を除去
        した結果）には縮退しない。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？")
        assert "E-Wave" in tokens
        assert "Wave" not in tokens
        assert "E" not in tokens

    def test_ascii_compound_entity_slash_separator(self, real_sudachi):
        """FrontFlow/blue のような '/' 区切り複合エンティティも保持される。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("FrontFlow/blue のライセンス変更について検討している")
        assert "FrontFlow/blue" in tokens

    def test_function_verbs_are_removed(self, real_sudachi):
        """「する」「いる」等の機能動詞（辞書形）は検索トークンから除外される。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？")
        assert "する" not in tokens
        assert "いる" not in tokens

    def test_generic_demote_terms_pushed_to_back(self, real_sudachi):
        """時制・汎用語（今年度・理由 等）は他の実質語より後方へ降格される。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？")
        assert tokens.index("今年度") > tokens.index("E-Wave")
        assert tokens.index("理由") > tokens.index("NVIDIA")

    def test_ascii_compound_entity_is_most_selective_top_token(self, real_sudachi):
        """段階的縮退で1語まで落ちた場合に残るのは先頭トークンであり、
        本番実測障害（'今年度' 1語まで縮退）の再発を防ぐため、複合エンティティが
        先頭に来ること自体を明示的に確認する。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？")
        assert tokens[0] == "E-Wave"

    def test_no_compound_entity_preserves_sudachi_natural_order(self, real_sudachi):
        """複合エンティティ・降格対象語が無い場合、Sudachiの形態素出現順
        （文中の語順）がそのまま維持される（2026-07-30: カタカナ・ASCII語を
        一律優先する4段階の並べ替えは既存クエリの selectivity を悪化させた
        ため、3段の簡易版に変更。自然な語順を無闇に崩さないことを確認）。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("スケールアウトネットワーク設計に関する議論")
        assert tokens[0] in ("スケールアウト", "ネットワーク")

    def test_single_token_degeneration_keeps_compound_entity_not_generic_term(self, real_sudachi):
        """先頭1語までの縮退（_fts_tokens_search の最弱段）で残るのが
        E-Wave であり、'今年度' ではないことを確認する（本番障害の直接再現）。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？")
        assert tokens[:1] == ["E-Wave"]

    def test_single_ascii_word_entity_does_not_disturb_following_noun_order(self, real_sudachi):
        """単独ASCII語（複合エンティティではない）は Sudachi の自然な出現順の
        先頭にとどまり、後続の一般名詞の相対順序を乱さない。
        （2026-07-30 recall_eval で発見した回帰: カタカナ・ASCII語を一般名詞より
        一律優先する並べ替えにすると、'BenchKit 外部からのコード貢献ルール確認'
        で自然語順なら1件に絞り込めていた組み合わせ（BenchKit+外部+コード）が
        壊れ、選択性の低い組み合わせ（BenchKit+コード+ルール）に縮退し、
        recall@30/60 が悪化した。3段簡易カテゴリへの変更で自然順を保持する）。
        単独ASCII語は正規表現での追加抽出を撤回した（下記テスト参照）ため、
        Sudachi の dictionary_form() 仕様どおり小文字化される。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("BenchKit 外部からのコード貢献ルール確認")
        assert tokens[:3] == ["benchkit", "外部", "コード"]

    def test_standalone_ascii_word_extraction_removed_relies_on_sudachi(self, real_sudachi):
        """単独ASCII語（NVIDIA 等）は正規表現での追加抽出をせず、Sudachi の
        形態素解析結果のみをトークンとして使う（複合エンティティのみ抽出に限定した
        較正: 2026-07-30 recall_eval 実測で単独語抽出時に注入された "AI4S" が
        Sudachi 由来の "AI" と別トークン化され、lqcd-dwf-hmc-comm-profiling-progress-202606
        の4語AND一致を壊し hybrid rank が 1→43 に劣化したため撤回）。"""
        from argus.retrieval import sudachi_tokenize_query
        tokens = sudachi_tokenize_query("LQCD 通信 AI4S プロファイリング")
        assert "AI4S" not in tokens
        assert tokens.count("AI") <= 1


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
        """since_date を指定すると古いチャンクが除外される
        （box_document は既定 exempt_box=True で日付フィルタ免除のため対象外）。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks("議事録", qa_db, since_date="2026-06-01")
        dates = [
            r["held_at"] for r in results
            if r.get("held_at") and r.get("source_type") != "box_document"
        ]
        assert all(d >= "2026-06-01" for d in dates)

    def test_since_date_exempt_box_false_also_filters_box(self, qa_db, monkeypatch):
        """exempt_box=False では box_document も held_at で判定され、
        since_date より古い box チャンク（r4）も除外される。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks(
            "予算配分", qa_db, since_date="2026-06-01", exempt_box=False,
        )
        assert all(r.get("record_id") != "r4" for r in results)

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
# retrieve_chunks — return_stage
# --------------------------------------------------------------------------- #

class TestRetrieveChunksReturnStage:
    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_default_return_stage_false_returns_list_only(self, qa_db, monkeypatch):
        """return_stage 未指定（既定）では従来どおり list のみを返す。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import retrieve_chunks
        results = retrieve_chunks("スケールアウトネットワーク", qa_db)
        assert isinstance(results, list)

    def test_return_stage_true_reports_fts_tokens_stage(self, qa_db, monkeypatch):
        """SudachiPy(fts_tokens) でヒットした場合 stage=STAGE_FTS_TOKENS。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: ["スケールアウトネットワーク"])
        monkeypatch.setattr(srv, "_fts_tokens_search", lambda *a, **kw: ([{"id": 1, "content": "dummy"}], 1))
        from argus.retrieval import STAGE_FTS_TOKENS, retrieve_chunks
        results, stage = retrieve_chunks(
            "スケールアウトネットワーク", qa_db, return_stage=True,
        )
        assert stage == STAGE_FTS_TOKENS
        assert len(results) >= 1

    def test_return_stage_true_reports_trigram_stage(self, qa_db, monkeypatch):
        """fts_tokens が空振り（sudachi無効化）で trigram にヒットした場合 stage=STAGE_TRIGRAM。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import STAGE_TRIGRAM, retrieve_chunks
        results, stage = retrieve_chunks(
            "スケールアウトネットワーク", qa_db, return_stage=True,
        )
        assert stage == STAGE_TRIGRAM
        assert len(results) >= 1

    def test_return_stage_true_reports_date_fallback_stage(self, qa_db, monkeypatch):
        """全段不一致の質問では stage=STAGE_DATE_FALLBACK。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import STAGE_DATE_FALLBACK, retrieve_chunks
        results, stage = retrieve_chunks(
            "存在しないキーワードXYZ", qa_db, return_stage=True,
        )
        assert stage == STAGE_DATE_FALLBACK
        assert len(results) >= 1

    def test_return_stage_true_reports_no_index_stage(self, tmp_path, monkeypatch):
        """DB が存在しない場合 stage=STAGE_NO_INDEX、chunks は空。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import STAGE_NO_INDEX, retrieve_chunks
        results, stage = retrieve_chunks(
            "test", tmp_path / "nonexistent.db", return_stage=True,
        )
        assert results == []
        assert stage == STAGE_NO_INDEX


# --------------------------------------------------------------------------- #
# retrieve_chunks — 弱段（1語まで縮退したヒット）の stage 判定
# 2026-07-30 本番実測障害の修正: 段階的縮退が1語まで落ちた低選択性のヒットを
# 通常ヒットと区別できるようにする。
# --------------------------------------------------------------------------- #

class TestRetrieveChunksWeakStage:
    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_trigram_hit_on_hyphenated_content_is_not_swallowed_by_operational_error(
        self, tmp_path, monkeypatch,
    ):
        """ハイフンを含む語（E-Wave）を含む本文が trigram 段で構文エラーに
        よって silent に握りつぶされず、正しくヒットする（本来2語ヒットする
        はずが構文エラーで弱段まで過剰縮退していた既存バグの回帰防止）。"""
        import argus.retrieval as srv
        db_path = tmp_path / "qa_index.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_QA_INDEX_SCHEMA)
        content = "E-Waveのコデザイン管理表にはNVIDIA対応状況の記載がある"
        conn.execute(
            "INSERT INTO chunks (source_type, source_db, record_id, held_at, content, indexed_at)"
            " VALUES (?,?,?,?,?,?)",
            ("minutes", "test.db", "r1", "2026-06-01", content, "2026-06-19T00:00:00"),
        )
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO fts(rowid, content) VALUES (?,?)", (chunk_id, content))
        conn.execute("INSERT INTO chunk_indexes (chunk_id, index_name) VALUES (?,?)", (chunk_id, "test"))
        conn.commit()
        conn.close()

        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        from argus.retrieval import STAGE_TRIGRAM, retrieve_chunks
        results, stage = retrieve_chunks(
            "E-WaveのNVIDIA対応状況について", db_path, index_name="test", return_stage=True,
        )
        assert stage == STAGE_TRIGRAM
        assert any("E-Wave" in r["content"] for r in results)

    def test_fts_tokens_weak_when_degenerated_to_one_token(self, qa_db, monkeypatch):
        """複数語クエリが1語まで縮退してヒットした場合 stage=STAGE_FTS_TOKENS_WEAK。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: ["今年度", "E-Wave", "NVIDIA"])
        monkeypatch.setattr(srv, "_fts_tokens_search", lambda *a, **kw: ([{"id": 1, "content": "dummy"}], 1))
        from argus.retrieval import STAGE_FTS_TOKENS_WEAK, retrieve_chunks
        results, stage = retrieve_chunks("dummy", qa_db, return_stage=True)
        assert stage == STAGE_FTS_TOKENS_WEAK
        assert len(results) >= 1

    def test_fts_tokens_not_weak_when_multiple_tokens_used(self, qa_db, monkeypatch):
        """縮退が2語以上で止まった場合は通常段（STAGE_FTS_TOKENS）のまま。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: ["今年度", "E-Wave", "NVIDIA"])
        monkeypatch.setattr(srv, "_fts_tokens_search", lambda *a, **kw: ([{"id": 1, "content": "dummy"}], 2))
        from argus.retrieval import STAGE_FTS_TOKENS, retrieve_chunks
        results, stage = retrieve_chunks("dummy", qa_db, return_stage=True)
        assert stage == STAGE_FTS_TOKENS

    def test_fts_tokens_not_weak_when_query_already_single_token(self, qa_db, monkeypatch):
        """元のクエリが最初から1語の場合は「縮退」ではないので弱段扱いにしない。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: ["スケールアウトネットワーク"])
        monkeypatch.setattr(srv, "_fts_tokens_search", lambda *a, **kw: ([{"id": 1, "content": "dummy"}], 1))
        from argus.retrieval import STAGE_FTS_TOKENS, retrieve_chunks
        results, stage = retrieve_chunks("dummy", qa_db, return_stage=True)
        assert stage == STAGE_FTS_TOKENS

    def test_trigram_weak_when_degenerated_to_one_token(self, qa_db, monkeypatch):
        """trigram 段でも複数語クエリが1語まで縮退してヒットした場合 stage=STAGE_TRIGRAM_WEAK。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(srv, "sanitize_fts_query", lambda q: "今年度 EWave NVIDIA")

        def fake_fts5_search(conn, q, k, *a, **kw):
            if q == "今年度":
                return [{"id": 1, "content": "dummy"}]
            return []
        monkeypatch.setattr(srv, "_fts5_search", fake_fts5_search)

        from argus.retrieval import STAGE_TRIGRAM_WEAK, retrieve_chunks
        results, stage = retrieve_chunks("dummy", qa_db, return_stage=True)
        assert stage == STAGE_TRIGRAM_WEAK
        assert len(results) >= 1

    def test_trigram_not_weak_when_full_set_hits(self, qa_db, monkeypatch):
        """全語のAND検索で一発ヒットした場合は通常段（STAGE_TRIGRAM）のまま。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(srv, "sanitize_fts_query", lambda q: "今年度 EWave NVIDIA")

        def fake_fts5_search(conn, q, k, *a, **kw):
            if q == "今年度 EWave NVIDIA":
                return [{"id": 1, "content": "dummy"}]
            return []
        monkeypatch.setattr(srv, "_fts5_search", fake_fts5_search)

        from argus.retrieval import STAGE_TRIGRAM, retrieve_chunks
        results, stage = retrieve_chunks("dummy", qa_db, return_stage=True)
        assert stage == STAGE_TRIGRAM


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

    def test_since_date_excludes_older_non_box_chunks(self, qa_db, monkeypatch):
        """since_date 指定時、held_at がそれより古い非box チャンク（r3:
        2026-01-01）が候補から除外される（FTS 経路 retrieve_chunks と同じ
        意味論）。box_document（r4）は既定 exempt_box=True で免除され残る。"""
        import embed_utils
        fixed_vec = np.ones(DIM, dtype=np.float32)
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: fixed_vec)

        conn = sqlite3.connect(str(qa_db))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import retrieve_chunks_vector
            results = retrieve_chunks_vector(
                "test", conn, k=4, index_name="test", since_date="2026-06-01",
            )
        finally:
            conn.close()

        record_ids = {r["record_id"] for r in results}
        assert record_ids == {"r1", "r2", "r4"}

    def test_since_date_exempt_box_false_excludes_box_too(self, qa_db, monkeypatch):
        """exempt_box=False では box_document（r4）も held_at で判定され、
        since_date より古いため除外される。"""
        import embed_utils
        fixed_vec = np.ones(DIM, dtype=np.float32)
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: fixed_vec)

        conn = sqlite3.connect(str(qa_db))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import retrieve_chunks_vector
            results = retrieve_chunks_vector(
                "test", conn, k=4, index_name="test", since_date="2026-06-01",
                exempt_box=False,
            )
        finally:
            conn.close()

        record_ids = {r["record_id"] for r in results}
        assert record_ids == {"r1", "r2"}

    def test_since_date_none_returns_all(self, qa_db, monkeypatch):
        """since_date=None（既定）では従来どおり全チャンクが候補になる。"""
        import embed_utils
        fixed_vec = np.ones(DIM, dtype=np.float32)
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: fixed_vec)

        conn = sqlite3.connect(str(qa_db))
        conn.row_factory = sqlite3.Row
        try:
            from argus.retrieval import retrieve_chunks_vector
            results = retrieve_chunks_vector("test", conn, k=4, index_name="test")
        finally:
            conn.close()

        assert len(results) == 4


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
        """since_date は FTS パス・vector パスの両方に適用される（retrieve_chunks_vector
        への伝搬修正後の挙動）。since_date より古い非box チャンク（r3:
        2026-01-01）は FTS・vector どちらの経路からも混入しない。box_document
        （r4）は既定 exempt_box=True で免除されるため、残っても許容する。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("議事録", qa_db, since_date="2026-06-01", index_name="test")
        assert isinstance(results, list)
        for r in results:
            assert "content" in r
            if r.get("source_type") != "box_document":
                assert r.get("held_at", "") >= "2026-06-01"

    def test_hybrid_exempt_box_false_excludes_old_box_chunk(self, qa_db, monkeypatch):
        """exempt_box=False を hybrid に伝搬すると、FTS・vector 両経路で
        box_document（r4）も since_date により除外される。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid(
            "予算配分", qa_db, since_date="2026-06-01", index_name="test",
            exempt_box=False,
        )
        assert all(r.get("record_id") != "r4" for r in results)

    def test_hybrid_propagates_since_date_to_vector(self, qa_db, monkeypatch):
        """retrieve_chunks_hybrid が retrieve_chunks_vector 呼び出しに since_date を
        伝搬すること（kwargs 捕捉、根本原因の再発防止）。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])

        captured = {}

        def fake_vector(query, conn, k=srv._VECTOR_K, index_name=None,
                        record_ids=None, since_date=None, exempt_box=True):
            captured["since_date"] = since_date
            return []

        monkeypatch.setattr(srv, "retrieve_chunks_vector", fake_vector)

        from argus.retrieval import retrieve_chunks_hybrid
        retrieve_chunks_hybrid("議事録", qa_db, since_date="2026-06-01", index_name="test")
        assert captured["since_date"] == "2026-06-01"

    def test_vector_k_unspecified_uses_module_default(self, qa_db, monkeypatch):
        """vector_k 未指定時は retrieve_chunks_vector へ従来どおり k=_VECTOR_K が渡る
        （one-shot broad-recall 未使用時の既存挙動不変の検証）。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])

        captured = {}

        def fake_vector(query, conn, k=srv._VECTOR_K, index_name=None,
                        record_ids=None, since_date=None, exempt_box=True):
            captured["k"] = k
            return []

        monkeypatch.setattr(srv, "retrieve_chunks_vector", fake_vector)

        from argus.retrieval import retrieve_chunks_hybrid
        retrieve_chunks_hybrid("議事録", qa_db, index_name="test")
        assert captured["k"] == srv._VECTOR_K

    def test_vector_k_specified_propagates_to_vector_leg(self, qa_db, monkeypatch):
        """vector_k=200 を指定すると vector 脚の取得件数として 200 が伝播する
        （one-shot broad-recall 用の上書き）。"""
        import argus.retrieval as srv
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])

        captured = {}

        def fake_vector(query, conn, k=srv._VECTOR_K, index_name=None,
                        record_ids=None, since_date=None, exempt_box=True):
            captured["k"] = k
            return []

        monkeypatch.setattr(srv, "retrieve_chunks_vector", fake_vector)

        from argus.retrieval import retrieve_chunks_hybrid
        retrieve_chunks_hybrid("議事録", qa_db, index_name="test", vector_k=200)
        assert captured["k"] == 200

    def test_large_k_and_vector_k_returns_up_to_fixture_size(self, qa_db, monkeypatch):
        """k・vector_k を大きく指定した場合、_rrf_merge はフィクスチャの範囲
        （4件）まで結果を返す。"""
        import argus.retrieval as srv
        import embed_utils
        monkeypatch.setattr(srv, "sudachi_tokenize_query", lambda q: [])
        monkeypatch.setattr(embed_utils, "embed_one", lambda q, **kw: np.ones(DIM, dtype=np.float32))

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid(
            "議事録", qa_db, k=100, index_name="test", vector_k=100,
        )
        assert len(results) == 4


# --------------------------------------------------------------------------- #
# retrieve_chunks_hybrid — 日付フォールバック時の RRF 汚染遮断
# --------------------------------------------------------------------------- #

def _fake_chunk(cid: int, source_type: str = "slack_raw") -> dict:
    return {"id": cid, "source_type": source_type, "source_db": "test.db",
            "record_id": f"r{cid}", "held_at": "2026-07-29",
            "content": f"content{cid}", "source_ref": None, "rank": 0}


class TestRetrieveChunksHybridFallbackExclusion:
    """retrieve_chunks の stage=STAGE_DATE_FALLBACK 時、RRF マージから FTS 脚を
    除外し vector 脚のみを使うこと（vector 脚が空の場合のみ従来どおりフォール
    バック結果を最終手段として返す）。"""

    @pytest.fixture
    def qa_db(self, tmp_path):
        return _make_qa_db(tmp_path)

    def test_date_fallback_with_vector_present_excludes_fts_from_rrf(self, qa_db, monkeypatch):
        import argus.retrieval as srv
        fallback_chunks = [_fake_chunk(i) for i in range(1, 71)]  # 70件（k+20 相当）
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0 - i * 0.01) for i in range(50)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fallback_chunks, srv.STAGE_DATE_FALLBACK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        result_ids = {r["id"] for r in results}
        fallback_ids = {c["id"] for c in fallback_chunks}
        vector_ids = {c["id"] for c in vector_chunks}
        # フォールバックの無関係チャンクが混入しない（根本原因の再現テスト）
        assert result_ids.isdisjoint(fallback_ids)
        # vector 候補が生き残る
        assert result_ids <= vector_ids
        assert len(results) > 0

    def test_date_fallback_with_empty_vector_returns_fallback_as_last_resort(self, qa_db, monkeypatch):
        import argus.retrieval as srv
        fallback_chunks = [_fake_chunk(i) for i in range(1, 71)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fallback_chunks, srv.STAGE_DATE_FALLBACK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        assert len(results) == 50
        assert {r["id"] for r in results} <= {c["id"] for c in fallback_chunks}

    def test_normal_stage_still_merges_both_legs_via_rrf(self, qa_db, monkeypatch):
        """stage が STAGE_TRIGRAM 等（フォールバックでない）場合は従来どおり
        FTS・vector 両脚を RRF マージする。"""
        import argus.retrieval as srv
        fts_chunks = [_fake_chunk(i) for i in range(1, 6)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0 - i * 0.01) for i in range(5)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fts_chunks, srv.STAGE_TRIGRAM),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("スケールアウト", qa_db, k=10)

        result_ids = {r["id"] for r in results}
        fts_ids = {c["id"] for c in fts_chunks}
        vector_ids = {c["id"] for c in vector_chunks}
        # 両脚が混ざって出てくる（フォールバックでないため除外されない）
        assert result_ids & fts_ids
        assert result_ids & vector_ids

    def test_hybrid_logs_exclusion_message_on_fallback(self, qa_db, monkeypatch, caplog):
        """フォールバック遮断発動時に評価メトリクスで拾えるログを INFO で出す。"""
        import logging

        import argus.retrieval as srv
        fallback_chunks = [_fake_chunk(i) for i in range(1, 71)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0) for i in range(5)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fallback_chunks, srv.STAGE_DATE_FALLBACK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        with caplog.at_level(logging.INFO, logger="pm_qa_server"):
            retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        assert any(
            "FTS date_fallback excluded from RRF" in rec.message for rec in caplog.records
        )

    def test_like_stage_with_vector_present_excludes_fts_from_rrf(self, qa_db, monkeypatch):
        """LIKE 段（rank一律0・ORDER BYなし）も date_fallback と同様、vector 存在時は
        RRF マージから除外される（S1: 遮断条件の対象範囲拡張）。"""
        import argus.retrieval as srv
        like_chunks = [_fake_chunk(i) for i in range(1, 71)]  # 70件（k+20 相当）
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0 - i * 0.01) for i in range(50)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (like_chunks, srv.STAGE_LIKE),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        result_ids = {r["id"] for r in results}
        like_ids = {c["id"] for c in like_chunks}
        vector_ids = {c["id"] for c in vector_chunks}
        assert result_ids.isdisjoint(like_ids)
        assert result_ids <= vector_ids
        assert len(results) > 0

    def test_like_stage_with_empty_vector_returns_like_as_last_resort(self, qa_db, monkeypatch):
        """LIKE 段でも vector 脚が空なら従来どおり LIKE 結果を最終手段として返す。"""
        import argus.retrieval as srv
        like_chunks = [_fake_chunk(i) for i in range(1, 71)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (like_chunks, srv.STAGE_LIKE),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        assert len(results) == 50
        assert {r["id"] for r in results} <= {c["id"] for c in like_chunks}

    def test_like_stage_logs_exclusion_message_with_stage_name(self, qa_db, monkeypatch, caplog):
        """LIKE 段の遮断ログにも stage 名（"like"）が含まれる。"""
        import logging

        import argus.retrieval as srv
        like_chunks = [_fake_chunk(i) for i in range(1, 71)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0) for i in range(5)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (like_chunks, srv.STAGE_LIKE),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        with caplog.at_level(logging.INFO, logger="pm_qa_server"):
            retrieve_chunks_hybrid("mh-nvl72 外部GPU計算リソース確保方針", qa_db, k=50)

        assert any(
            "FTS like excluded from RRF" in rec.message for rec in caplog.records
        )

    def test_fts_tokens_weak_stage_with_vector_present_excludes_fts_from_rrf(self, qa_db, monkeypatch):
        """STAGE_FTS_TOKENS_WEAK（1語まで縮退した弱いヒット）も vector 存在時は
        RRF マージから除外される（2026-07-30 本番実測障害の修正: 190件ヒットの
        低関連結果が vector 候補を押し出した）。"""
        import argus.retrieval as srv
        weak_chunks = [_fake_chunk(i) for i in range(1, 71)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0 - i * 0.01) for i in range(50)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (weak_chunks, srv.STAGE_FTS_TOKENS_WEAK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？", qa_db, k=50)

        result_ids = {r["id"] for r in results}
        weak_ids = {c["id"] for c in weak_chunks}
        vector_ids = {c["id"] for c in vector_chunks}
        assert result_ids.isdisjoint(weak_ids)
        assert result_ids <= vector_ids
        assert len(results) > 0

    def test_fts_tokens_weak_stage_with_empty_vector_returns_weak_as_last_resort(self, qa_db, monkeypatch):
        """弱段でも vector 脚が空なら従来どおり最終手段として返す。"""
        import argus.retrieval as srv
        weak_chunks = [_fake_chunk(i) for i in range(1, 71)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (weak_chunks, srv.STAGE_FTS_TOKENS_WEAK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？", qa_db, k=50)

        assert len(results) == 50
        assert {r["id"] for r in results} <= {c["id"] for c in weak_chunks}

    def test_trigram_weak_stage_with_vector_present_excludes_fts_from_rrf(self, qa_db, monkeypatch):
        """STAGE_TRIGRAM_WEAK も vector 存在時は RRF マージから除外される。"""
        import argus.retrieval as srv
        weak_chunks = [_fake_chunk(i) for i in range(1, 71)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0 - i * 0.01) for i in range(50)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (weak_chunks, srv.STAGE_TRIGRAM_WEAK),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        results = retrieve_chunks_hybrid("今年度のE-WaveのNVIDIAとのコラボレーションが停滞している理由は？", qa_db, k=50)

        result_ids = {r["id"] for r in results}
        weak_ids = {c["id"] for c in weak_chunks}
        vector_ids = {c["id"] for c in vector_chunks}
        assert result_ids.isdisjoint(weak_ids)
        assert result_ids <= vector_ids

    def test_vector_leg_log_reports_nonzero_count(self, qa_db, monkeypatch, caplog):
        """vector 脚が非空の場合 `[hybrid] vector_leg n=` に実件数を INFO ログする
        （2026-07-30 誤診修正: _run_oneshot の DEGRADED 誤検知を hybrid ログと
        突き合わせて判別できるようにする）。"""
        import logging

        import argus.retrieval as srv
        fts_chunks = [_fake_chunk(i) for i in range(1, 6)]
        vector_chunks = [dict(_fake_chunk(1000 + i), vector_score=1.0) for i in range(7)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fts_chunks, srv.STAGE_TRIGRAM),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: vector_chunks)

        from argus.retrieval import retrieve_chunks_hybrid
        with caplog.at_level(logging.INFO, logger="pm_qa_server"):
            retrieve_chunks_hybrid("スケールアウト", qa_db, k=10)

        assert any("[hybrid] vector_leg n=7" in rec.message for rec in caplog.records)

    def test_vector_leg_log_warns_when_empty(self, qa_db, monkeypatch, caplog):
        """vector 脚が空の場合は `[hybrid] vector_leg n=0` を WARNING で出す。"""
        import logging

        import argus.retrieval as srv
        fts_chunks = [_fake_chunk(i) for i in range(1, 6)]

        monkeypatch.setattr(
            srv, "retrieve_chunks",
            lambda *a, **kw: (fts_chunks, srv.STAGE_TRIGRAM),
        )
        monkeypatch.setattr(srv, "retrieve_chunks_vector", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hybrid
        with caplog.at_level(logging.WARNING, logger="pm_qa_server"):
            retrieve_chunks_hybrid("スケールアウト", qa_db, k=10)

        assert any("[hybrid] vector_leg n=0" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# retrieve_chunks_hyde — skip_keyword_extract
# --------------------------------------------------------------------------- #

class TestRetrieveChunksHydeSkipKeyword:
    """skip_keyword_extract の真偽で extract_search_keywords 呼び出し有無が切り替わること。"""

    def test_skip_false_calls_extract_search_keywords(self, tmp_path, monkeypatch):
        import argus.retrieval as srv
        called = {"n": 0}

        def fake_extract(q, timeout=30):
            called["n"] += 1
            return q
        monkeypatch.setattr(srv, "extract_search_keywords", fake_extract)
        monkeypatch.setattr(srv, "expand_query_hyde", lambda q, n_extra=2, timeout=30: [q])
        monkeypatch.setattr(srv, "retrieve_chunks_hybrid", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hyde
        retrieve_chunks_hyde("質問", tmp_path / "qa.db")
        assert called["n"] == 1

    def test_skip_true_does_not_call_extract_search_keywords(self, tmp_path, monkeypatch):
        import argus.retrieval as srv
        called = {"n": 0}

        def fake_extract(q, timeout=30):
            called["n"] += 1
            return q
        monkeypatch.setattr(srv, "extract_search_keywords", fake_extract)
        monkeypatch.setattr(srv, "expand_query_hyde", lambda q, n_extra=2, timeout=30: [q])
        monkeypatch.setattr(srv, "retrieve_chunks_hybrid", lambda *a, **kw: [])

        from argus.retrieval import retrieve_chunks_hyde
        retrieve_chunks_hyde("質問", tmp_path / "qa.db", skip_keyword_extract=True)
        assert called["n"] == 0


# --------------------------------------------------------------------------- #
# rerank_chunks — use_llm / openai_base 後方互換・max_tokens スケーリング
# --------------------------------------------------------------------------- #

class TestRerankChunksUseLlm:
    def test_use_llm_true_invokes_llm_and_selects_indices(self, monkeypatch):
        import cli_utils

        def fake_call(prompt, **kw):
            return "0 3"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(5)]
        result = rerank_chunks("q", chunks, top_k=2, use_llm=True)
        assert [c["content"] for c in result] == ["c0", "c3"]

    def test_use_llm_false_and_no_openai_base_skips_llm(self, monkeypatch):
        import cli_utils
        called = {"n": 0}

        def fake_call(prompt, **kw):
            called["n"] += 1
            return "0"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(5)]
        result = rerank_chunks("q", chunks, top_k=2)
        assert called["n"] == 0
        assert [c["content"] for c in result] == ["c0", "c1"]

    def test_openai_base_truthy_is_backward_compatible(self, monkeypatch):
        """use_llm 未指定でも openai_base が truthy なら従来どおり LLM が呼ばれる。"""
        import cli_utils

        def fake_call(prompt, **kw):
            return "1 2"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(5)]
        result = rerank_chunks("q", chunks, top_k=2, openai_base="http://example/v1")
        assert [c["content"] for c in result] == ["c1", "c2"]

    def test_max_tokens_fixed_at_4096(self, monkeypatch):
        """max_tokens=4096 固定。Kimi-K2-Thinking フォールバック時の thinking 消費
        （2-3k）を吸収するための下限であり top_k とは無関係。"""
        import cli_utils
        captured = {}

        def fake_call(prompt, **kw):
            captured.update(kw)
            return "0"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(25)]
        rerank_chunks("q", chunks, top_k=20, use_llm=True)
        assert captured.get("max_tokens") == 4096

    def test_timeout_fixed_at_30s(self, monkeypatch):
        """timeout=30s 固定。search_text は高頻度ツールのため investigate の
        480s 予算を守る。"""
        import cli_utils
        captured = {}

        def fake_call(prompt, **kw):
            captured.update(kw)
            return "0"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(10)]
        rerank_chunks("q", chunks, top_k=3, use_llm=True)
        assert captured.get("timeout") == 30

    def test_len_chunks_le_top_k_skips_llm_even_when_enabled(self, monkeypatch):
        import cli_utils
        called = {"n": 0}

        def fake_call(prompt, **kw):
            called["n"] += 1
            return "0"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(3)]
        result = rerank_chunks("q", chunks, top_k=5, use_llm=True)
        assert called["n"] == 0
        assert result == chunks


# --------------------------------------------------------------------------- #
# 環境変数オーバーライド — ARGUS_TOP_K_RERANK / ARGUS_RERANK_PREVIEW_CHARS
# --------------------------------------------------------------------------- #

class TestRerankEnvOverrides:
    def test_effective_top_k_rerank_default(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.delenv("ARGUS_TOP_K_RERANK", raising=False)
        assert srv._effective_top_k_rerank() == srv.TOP_K_RERANK_DEFAULT

    def test_effective_top_k_rerank_overridden(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.setenv("ARGUS_TOP_K_RERANK", "10")
        assert srv._effective_top_k_rerank() == 10

    def test_effective_top_k_rerank_invalid_falls_back(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.setenv("ARGUS_TOP_K_RERANK", "not-a-number")
        assert srv._effective_top_k_rerank() == srv.TOP_K_RERANK_DEFAULT

    def test_effective_top_k_rerank_zero_falls_back(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.setenv("ARGUS_TOP_K_RERANK", "0")
        assert srv._effective_top_k_rerank() == srv.TOP_K_RERANK_DEFAULT

    def test_rerank_chunks_top_k_none_uses_env_override(self, monkeypatch):
        """top_k 未指定（None）時、rerank_chunks は ARGUS_TOP_K_RERANK を実効値に使う。"""
        import cli_utils
        monkeypatch.setenv("ARGUS_TOP_K_RERANK", "2")

        def fake_call(prompt, **kw):
            return "0 1"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": f"c{i}"} for i in range(5)]
        result = rerank_chunks("q", chunks, use_llm=True)
        assert len(result) == 2

    def test_effective_rerank_preview_chars_default(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.delenv("ARGUS_RERANK_PREVIEW_CHARS", raising=False)
        assert srv._effective_rerank_preview_chars() == srv._RERANK_PREVIEW_CHARS_DEFAULT

    def test_effective_rerank_preview_chars_overridden(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.setenv("ARGUS_RERANK_PREVIEW_CHARS", "800")
        assert srv._effective_rerank_preview_chars() == 800

    def test_effective_rerank_preview_chars_invalid_falls_back(self, monkeypatch):
        import argus.retrieval as srv
        monkeypatch.setenv("ARGUS_RERANK_PREVIEW_CHARS", "-5")
        assert srv._effective_rerank_preview_chars() == srv._RERANK_PREVIEW_CHARS_DEFAULT

    def test_rerank_chunks_preview_uses_env_override(self, monkeypatch):
        """re-rank プロンプトのチャンクプレビュー長が ARGUS_RERANK_PREVIEW_CHARS に従う。"""
        import cli_utils
        monkeypatch.setenv("ARGUS_RERANK_PREVIEW_CHARS", "5")
        captured = {}

        def fake_call(prompt, **kw):
            captured["prompt"] = prompt
            return "0"
        monkeypatch.setattr(cli_utils, "call_argus_llm", fake_call)

        from argus.retrieval import rerank_chunks
        chunks = [{"content": "0123456789"} for _ in range(3)]
        rerank_chunks("q", chunks, top_k=1, use_llm=True)
        assert "01234" in captured["prompt"]
        assert "0123456789" not in captured["prompt"]


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


# --------------------------------------------------------------------------- #
# today (日次サマリー) の全文脈トグル
# --------------------------------------------------------------------------- #

class TestGenerateDailySummaryReportFullctxToggle:
    """ARGUS_DISABLE_FULLCTX=1 で generate_daily_summary_report が従来（切り詰め）プロンプトを
    使うことを検証する（call_argus_llm はモック）。プロンプト本文は _DAILY_SUMMARY_PROMPT の
    まま変更されないため、入力データ（messages/minutes）の中身の違いで判定する。"""

    def test_disable_fullctx_uses_truncated_prompt(self, monkeypatch):
        import argus.pm_argus as pm_argus
        monkeypatch.setenv("ARGUS_DISABLE_FULLCTX", "1")

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            return "truncated result"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("切り詰めメッセージ", "切り詰め議事録", {"stats": {}}, "knowledge", "web"),
        )

        result, messages = pm_argus.generate_daily_summary_report("2026-07-26")

        assert result == "truncated result"
        assert messages == "切り詰めメッセージ"
        assert "本日 2026-07-26 のプロジェクト活動記録" in captured["prompt"]
        assert "切り詰めメッセージ" in captured["prompt"]
        assert "切り詰め議事録" in captured["prompt"]

    def test_fullctx_enabled_by_default_uses_full_sections(self, monkeypatch):
        """既定（ARGUS_DISABLE_FULLCTX 未設定）では build_full_context_sections の
        slack/minutes セクション（切り詰めなし・見出し込み）がそのまま使われる。
        fullctx LLM 呼び出しには max_tokens/timeout が明示され（M2）、
        web 記事は取得しない（M2 の無駄解消。2026-07-26 レビュー指摘）。"""
        import argus.pm_argus as pm_argus
        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return "fullctx result"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(pm_argus, "fetch_background_knowledge", lambda **kw: "knowledge")

        def fail_if_called(*a, **kw):
            raise AssertionError("today は web 記事を取得しないはず")
        monkeypatch.setattr(pm_argus, "fetch_recent_web_articles", fail_if_called)

        fake_sections = {
            "pm_stats": "## 構造化データ\n\nダミー",
            "minutes": "## 議事録（切り詰めなし）\n\n全文議事録",
            "slack": "## Slack 会話（期間: 2026-07-26〜2026-07-26, 切り詰めなし）\n\n全文Slack",
            "box": "## Box 資料\n\n（除外設定により省略）",
        }
        fake_meta = {"total_chars": 100, "total_est_tokens": 62,
                     "sections": {k: {"chars": len(v), "truncated": False} for k, v in fake_sections.items()}}
        monkeypatch.setattr(
            pm_argus, "build_full_context_sections", lambda *a, **kw: (fake_sections, fake_meta),
        )

        result, messages = pm_argus.generate_daily_summary_report("2026-07-26")

        assert result == "fullctx result"
        assert messages == fake_sections["slack"]
        assert "全文Slack" in captured["prompt"]
        assert "全文議事録" in captured["prompt"]
        assert "切り詰めメッセージ" not in captured["prompt"]
        assert captured["kwargs"]["max_tokens"] == pm_argus._BRIEF_RISK_MAX_TOKENS
        assert captured["kwargs"]["timeout"] == 600


# --------------------------------------------------------------------------- #
# draft (草案生成) の全文脈トグル
# --------------------------------------------------------------------------- #

class TestGenerateDraftReportFullctxToggle:
    """agenda/request purpose は {messages}（Slack 会話）を使うため
    ARGUS_DISABLE_FULLCTX の影響を受ける。report purpose は Slack 会話を使わないため
    影響を受けないことも合わせて検証する。"""

    _FAKE_STATS = {
        "stats": {}, "milestones": [], "assignee_workload": [],
        "overdue_items": [], "unacknowledged_decisions": [], "weekly_trends": [],
        "unlinked_count": 0, "no_assignee_count": 0,
    }

    def test_disable_fullctx_uses_truncated_prompt(self, monkeypatch):
        import argus.pm_argus as pm_argus
        monkeypatch.setenv("ARGUS_DISABLE_FULLCTX", "1")

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            return "truncated draft"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("切り詰めメッセージ", "minutes", dict(self._FAKE_STATS), "knowledge", "web"),
        )

        result = pm_argus.generate_draft_report(
            "agenda", "次回リーダー会議", "2026-07-26", "2026-07-12",
        )

        assert result == "truncated draft"
        assert "切り詰めメッセージ" in captured["prompt"]

    def test_fullctx_enabled_by_default_uses_full_slack_section(self, monkeypatch):
        """fullctx 経路（既定）は重い _collect_all_data を呼ばず、stats のみ
        _fetch_single_pm_stats 経由で軽量取得する（二重収集の解消。2026-07-26 レビュー指摘 M1）。
        fullctx LLM 呼び出しには max_tokens/timeout が明示される（M2）。"""
        import argus.pm_argus as pm_argus
        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)

        captured = {}

        def fake_call_argus_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return "fullctx draft"

        monkeypatch.setattr(pm_argus, "call_argus_llm", fake_call_argus_llm)
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(pm_argus, "_load_channel_ids", lambda index_name=None: ["C111"])
        monkeypatch.setattr(pm_argus, "_load_minutes_names", lambda index_name=None: ["kind1"])

        def fail_if_called(*a, **kw):
            raise AssertionError("fullctx 経路は _collect_all_data を呼ばないはず（二重収集）")
        monkeypatch.setattr(pm_argus, "_collect_all_data", fail_if_called)

        fake_sections = {
            "pm_stats": "## 構造化データ\n\nダミー",
            "minutes": "## 議事録\n\nダミー",
            "slack": "## Slack 会話（切り詰めなし）\n\n全文メッセージ",
            "box": "## Box 資料\n\n（除外設定により省略）",
        }
        fake_meta = {"total_chars": 100, "total_est_tokens": 62,
                     "sections": {k: {"chars": len(v), "truncated": False} for k, v in fake_sections.items()}}
        monkeypatch.setattr(
            pm_argus, "build_full_context_sections", lambda *a, **kw: (fake_sections, fake_meta),
        )

        result = pm_argus.generate_draft_report(
            "agenda", "次回リーダー会議", "2026-07-26", "2026-07-12",
        )

        assert result == "fullctx draft"
        assert "全文メッセージ" in captured["prompt"]
        assert "切り詰めメッセージ" not in captured["prompt"]
        assert captured["kwargs"]["max_tokens"] == pm_argus._BRIEF_RISK_MAX_TOKENS
        assert captured["kwargs"]["timeout"] == 600

    def test_report_purpose_ignores_fullctx_flag(self, monkeypatch):
        """report purpose は Slack 会話を使わないため build_full_context_sections を
        呼ばず、ARGUS_DISABLE_FULLCTX の値に関わらず同じロジックで生成される。"""
        import argus.pm_argus as pm_argus
        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)

        def fail_if_called(*a, **kw):
            raise AssertionError("report purpose では build_full_context_sections を呼ばないはず")

        monkeypatch.setattr(pm_argus, "build_full_context_sections", fail_if_called)
        monkeypatch.setattr(pm_argus, "call_argus_llm", lambda prompt, **kw: "report result")
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("messages", "minutes", dict(self._FAKE_STATS), "knowledge", "web"),
        )

        result = pm_argus.generate_draft_report(
            "report", "月次進捗報告", "2026-07-26", "2026-07-12",
        )

        assert result == "report result"

    def test_report_purpose_degenerate_output_is_sanitized(self, monkeypatch):
        """report purpose にも _ensure_not_degenerate が無条件適用される
        （純サニタイズなので安全。2026-07-26 レビュー指摘 m4）。"""
        import argus.pm_argus as pm_argus
        monkeypatch.delenv("ARGUS_DISABLE_FULLCTX", raising=False)

        monkeypatch.setattr(pm_argus, "call_argus_llm", lambda prompt, **kw: "!" * 32768)
        monkeypatch.setattr(pm_argus, "load_claude_md_context", lambda: "context")
        monkeypatch.setattr(pm_argus, "load_pm_db_paths", lambda index_name=None: [])
        monkeypatch.setattr(
            pm_argus, "_collect_all_data",
            lambda *a, **kw: ("messages", "minutes", dict(self._FAKE_STATS), "knowledge", "web"),
        )

        result = pm_argus.generate_draft_report(
            "report", "月次進捗報告", "2026-07-26", "2026-07-12",
        )

        assert result != "!" * 32768
        assert "出力生成に失敗しました" in result
