"""Patrol 完了検出の深層防御テスト（AI #3056 誤クローズ再発防止）。

AI #3056 の証拠は box_document であり、FTS 側は従来から box 免除
（`_build_date_filter(exempt_box=True)`）で日付フィルタを素通りしていた。
つまり「vector 経路の since_date 欠落」だけでは 3056 型の誤クローズは
防げない（box が主因）。主防御は次の2つ:
1. `_get_activity_evidence` が `retrieve_chunks_hybrid(..., exempt_box=False)`
   を渡す（box_document も発生日フィルタの対象にする）
2. `_get_activity_evidence` の post-filter（retrieval の内部実装に依存しない
   確定的な絞り込み。exempt_box=False で通常は no-op になる backstop）

retrieve_chunks_vector への since_date 伝搬（_build_date_filter 導入・
exempt_box 化）は tests/argus/test_retrieve_chunks.py 側で検証する。
本ファイルは patrol 側の exempt_box 指定 / post-filter / カットオフ /
LLM プロンプトを検証する（LLM 非接続、call_argus_llm は monkeypatch）。
"""
from unittest.mock import Mock

from argus.patrol import detect


def _make_ctx(config, today="2026-07-27", data_dir=None):
    ctx = Mock()
    ctx.today = today
    ctx.config = config
    ctx.dry_run = False
    ctx.data_dir = data_dir
    ctx.state = Mock()
    ctx.state.already_notified.return_value = False
    return ctx


# --------------------------------------------------------------------------- #
# _get_activity_evidence: extracted_at より古い・空日付の証拠を除外
# --------------------------------------------------------------------------- #

class TestGetActivityEvidencePostFilter:
    def test_filters_old_and_empty_dates_keeps_new(self, tmp_path, monkeypatch):
        (tmp_path / "qa_index.db").touch()
        ctx = _make_ctx(
            {"patrol": {"completion_detection": {"evidence_from_index": True}}},
            data_dir=tmp_path,
        )
        ai_row = {"content": "資料を提出する", "extracted_at": "2026-06-10T09:00:00"}

        monkeypatch.setattr(
            "enrich.knowledge_context.extract_topic_keywords", lambda content: [],
        )

        def fake_hybrid(q, qa_index_path, k=6, since_date=None, index_name=None,
                        exempt_box=True):
            return [
                {"source_type": "meeting", "held_at": "2026-05-01",
                 "source_ref": "old", "content": "古い証拠（発生日より前）"},
                {"source_type": "meeting", "held_at": "",
                 "source_ref": "empty", "content": "日付不明の証拠"},
                {"source_type": "meeting", "held_at": "2026-06-15",
                 "source_ref": "new", "content": "発生日より後の証拠"},
            ]

        monkeypatch.setattr("argus.retrieval.retrieve_chunks_hybrid", fake_hybrid)

        result = detect._get_activity_evidence(ctx, ai_row)

        assert len(result) == 1
        assert result[0]["content"] == "発生日より後の証拠"
        assert result[0]["held_at"] == "2026-06-15"

    def test_ai_3056_regression_box_document_older_than_extracted_at_excluded(
        self, tmp_path, monkeypatch,
    ):
        """AI #3056 の再現: extracted_at より古い box_document の証拠は、
        exempt_box=False（検索段）+ post-filter（backstop）の両方で除外される。"""
        (tmp_path / "qa_index.db").touch()
        ctx = _make_ctx(
            {"patrol": {"completion_detection": {"evidence_from_index": True}}},
            data_dir=tmp_path,
        )
        # AI #3056: extracted_at=2026-06-09、根拠に使われた box 文書は 2026-05-18
        ai_row = {"content": "資料を提出する", "extracted_at": "2026-06-09T00:00:00"}

        monkeypatch.setattr(
            "enrich.knowledge_context.extract_topic_keywords", lambda content: [],
        )

        captured_exempt_box = {}

        def fake_hybrid(q, qa_index_path, k=6, since_date=None, index_name=None,
                        exempt_box=True):
            captured_exempt_box["value"] = exempt_box
            return [
                {"source_type": "box_document", "held_at": "2026-05-18",
                 "source_ref": "box:old", "content": "発生日より前の box 文書"},
                {"source_type": "box_document", "held_at": "2026-06-20",
                 "source_ref": "box:new", "content": "発生日より後の box 文書"},
            ]

        monkeypatch.setattr("argus.retrieval.retrieve_chunks_hybrid", fake_hybrid)

        result = detect._get_activity_evidence(ctx, ai_row)

        assert captured_exempt_box["value"] is False
        assert len(result) == 1
        assert result[0]["content"] == "発生日より後の box 文書"

    def test_evidence_since_extracted_false_keeps_all(self, tmp_path, monkeypatch):
        """evidence_since_extracted=False の場合は post-filter を適用しない
        （既存挙動を変えない）。"""
        (tmp_path / "qa_index.db").touch()
        ctx = _make_ctx(
            {"patrol": {"completion_detection": {
                "evidence_from_index": True, "evidence_since_extracted": False,
            }}},
            data_dir=tmp_path,
        )
        ai_row = {"content": "資料を提出する", "extracted_at": "2026-06-10T09:00:00"}

        monkeypatch.setattr(
            "enrich.knowledge_context.extract_topic_keywords", lambda content: [],
        )

        def fake_hybrid(q, qa_index_path, k=6, since_date=None, index_name=None,
                        exempt_box=True):
            return [
                {"source_type": "meeting", "held_at": "2026-05-01",
                 "source_ref": "old", "content": "古い証拠"},
                {"source_type": "meeting", "held_at": "",
                 "source_ref": "empty", "content": "日付不明の証拠"},
            ]

        monkeypatch.setattr("argus.retrieval.retrieve_chunks_hybrid", fake_hybrid)

        result = detect._get_activity_evidence(ctx, ai_row)

        assert len(result) == 2

    def test_obsolete_detection_cfg_shares_same_date_filter(self, tmp_path, monkeypatch):
        """detect_obsolete_items が使う evidence_cfg（evidence_since_extracted
        は _get_activity_evidence 側の既定 True がそのまま効く）でも、同じ
        post-filter が機能すること。"""
        (tmp_path / "qa_index.db").touch()
        ctx = _make_ctx(
            {"patrol": {"obsolete_detection": {"evidence_from_index": True}}},
            data_dir=tmp_path,
        )
        ai_row = {"content": "統合を進める", "extracted_at": "2026-06-10T09:00:00"}
        evidence_cfg = {"evidence_from_index": True}

        monkeypatch.setattr(
            "enrich.knowledge_context.extract_topic_keywords", lambda content: [],
        )

        def fake_hybrid(q, qa_index_path, k=6, since_date=None, index_name=None,
                        exempt_box=True):
            return [
                {"source_type": "meeting", "held_at": "2026-05-01",
                 "source_ref": "old", "content": "古い方針（発生日より前）"},
                {"source_type": "meeting", "held_at": "2026-06-20",
                 "source_ref": "new", "content": "新しい方針転換の証拠"},
            ]

        monkeypatch.setattr("argus.retrieval.retrieve_chunks_hybrid", fake_hybrid)

        result = detect._get_activity_evidence(ctx, ai_row, evidence_cfg)

        assert len(result) == 1
        assert result[0]["content"] == "新しい方針転換の証拠"


# --------------------------------------------------------------------------- #
# detect_completion_signals: スレッド返信の実カットオフ
# --------------------------------------------------------------------------- #

class TestThreadReplyCutoff:
    def test_reply_cutoff_uses_extracted_at_when_more_recent(self, tmp_path, monkeypatch):
        """max_reply_age_days（60日）より extracted_at が新しい場合、
        _get_recent_replies に渡るカットオフは extracted_at になる
        （アイテム発生前の返信を完了証拠にしないため）。"""
        row = {
            "id": 1, "content": "対応する", "assignee": "someone", "due_date": None,
            "source_ref": "https://x.slack.com/archives/C0XXXXXXX/p1234567890123456",
            "source": "slack", "extracted_at": "2026-07-20T00:00:00", "note": None,
        }
        ctx = _make_ctx(
            {"patrol": {"completion_detection": {
                "max_reply_age_days": 60, "evidence_from_index": False,
            }}},
            today="2026-07-27",
            data_dir=tmp_path,
        )
        ctx.conn = Mock()
        ctx.conn.execute.return_value.fetchall.return_value = [row]

        captured = {}

        def fake_get_recent_replies(data_dir, channel_id, thread_ts, cutoff_date):
            captured["cutoff_date"] = cutoff_date
            return []

        monkeypatch.setattr(detect, "_get_recent_replies", fake_get_recent_replies)

        detect.detect_completion_signals(ctx)

        # base_cutoff = 2026-07-27 - 60日 = 2026-05-28 (extracted_at より古い)
        assert captured["cutoff_date"] == "2026-07-20"

    def test_reply_cutoff_uses_base_cutoff_when_extracted_at_older(self, tmp_path, monkeypatch):
        """extracted_at が base_cutoff より古い場合は従来どおり base_cutoff を使う。"""
        row = {
            "id": 2, "content": "対応する", "assignee": "someone", "due_date": None,
            "source_ref": "https://x.slack.com/archives/C0XXXXXXX/p1234567890123456",
            "source": "slack", "extracted_at": "2026-01-01T00:00:00", "note": None,
        }
        ctx = _make_ctx(
            {"patrol": {"completion_detection": {
                "max_reply_age_days": 60, "evidence_from_index": False,
            }}},
            today="2026-07-27",
            data_dir=tmp_path,
        )
        ctx.conn = Mock()
        ctx.conn.execute.return_value.fetchall.return_value = [row]

        captured = {}

        def fake_get_recent_replies(data_dir, channel_id, thread_ts, cutoff_date):
            captured["cutoff_date"] = cutoff_date
            return []

        monkeypatch.setattr(detect, "_get_recent_replies", fake_get_recent_replies)

        detect.detect_completion_signals(ctx)

        assert captured["cutoff_date"] == "2026-05-28"


# --------------------------------------------------------------------------- #
# _llm_judge_completion: プロンプトに発生日と禁止文言が含まれる
# --------------------------------------------------------------------------- #

class TestLlmJudgeCompletionPrompt:
    def test_prompt_includes_extracted_at_and_guard_text(self, monkeypatch):
        captured = {}

        def fake_call(prompt, **kw):
            captured["prompt"] = prompt
            return "NO"

        monkeypatch.setattr("cli_utils.call_argus_llm", fake_call)

        evidence = [{"source_type": "meeting", "held_at": "2026-06-15",
                     "source_ref": "ref", "content": "何らかの証拠"}]
        detect._llm_judge_completion("資料を提出する", evidence, "2026-06-10T09:00:00")

        assert "発生日: 2026-06-10" in captured["prompt"]
        assert "発生日（2026-06-10）より前の日付の情報は完了の証拠にならない" in captured["prompt"]
        assert "アイテム化時点で既知の情報" in captured["prompt"]

    def test_prompt_omits_guard_text_when_extracted_at_missing(self, monkeypatch):
        """extracted_at 不明時は発生日行・ガード文の両方を省略する
        （「発生日: ?」のような退化した記述を出さない）。"""
        captured = {}

        def fake_call(prompt, **kw):
            captured["prompt"] = prompt
            return "NO"

        monkeypatch.setattr("cli_utils.call_argus_llm", fake_call)

        evidence = [{"source_type": "meeting", "held_at": "2026-06-15",
                     "source_ref": "ref", "content": "何らかの証拠"}]
        detect._llm_judge_completion("資料を提出する", evidence)

        assert "発生日" not in captured["prompt"]
        assert "?" not in captured["prompt"]
