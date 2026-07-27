"""ingest/slack.py のトリアージ方式（two_stage / integrated）のテスト。

LLM 実接続なし（call_argus_llm / triage_items / retrieve_knowledge_for_extraction を
monkeypatch）。フィクスチャの人名はダミー（富岳太郎等）のみを使う。
"""
import pytest
from ingest import slack

_ROW = {
    "thread_text": "テストスレッド本文",
    "timestamp": "2026-01-01T00:00",
    "user_name": "富岳太郎",
}

_VALID_EXTRACTION_JSON = """```json
{
  "decisions": [
    {
      "content": "富岳太郎の提案によりXXX方式を採用する方針に決定した。",
      "decided_at": null,
      "rationale": null,
      "trade_off": null,
      "reversal_condition": null
    }
  ],
  "action_items": []
}
```"""

_VALID_TRIAGE_JSON = """```json
{
  "action_items": [],
  "decisions": [
    {
      "content": "富岳太郎の提案によりXXX方式を採用する方針に決定した。",
      "decided_at": null,
      "verdict": "KEEP",
      "reason": ""
    }
  ]
}
```"""


def _patch_knowledge(monkeypatch):
    monkeypatch.setattr(
        slack, "retrieve_knowledge_for_extraction",
        lambda *a, **kw: "（該当する過去議論なし）",
    )


# --------------------------------------------------------------------------- #
# プロンプト合成
# --------------------------------------------------------------------------- #


def test_extract_prompt_anchor_and_integrated_gates():
    assert slack.EXTRACT_PROMPT.count("## その他の指示") == 1
    assert slack.EXTRACT_PROMPT_INTEGRATED.count("ゲート1: マイルストーン関連性") == 1
    assert "ゲート1: マイルストーン関連性" not in slack.EXTRACT_PROMPT


def test_extract_prompt_integrated_formats_without_keyerror():
    out = slack.EXTRACT_PROMPT_INTEGRATED.format(
        context="ctx", knowledge_context="kc", timestamp="2026-01-01",
        user_name="富岳太郎", thread_text="thread", milestones="M1",
    )
    assert "ゲート1" in out


# --------------------------------------------------------------------------- #
# extract_from_thread: triage_mode の分岐
# --------------------------------------------------------------------------- #


def test_integrated_mode_single_llm_call_no_triage_items(monkeypatch):
    _patch_knowledge(monkeypatch)
    prompts = []

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return _VALID_EXTRACTION_JSON

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)
    monkeypatch.setattr(
        slack, "triage_items",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("triage_items は integrated モードで呼ばれてはならない")
        ),
    )

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=1, enable_triage=True, triage_mode="integrated",
    )

    assert len(prompts) == 1
    assert "ゲート1" in prompts[0]
    assert len(result["decisions"]) == 1


def test_two_stage_mode_two_llm_calls(monkeypatch):
    _patch_knowledge(monkeypatch)
    prompts = []
    responses = [_VALID_EXTRACTION_JSON, _VALID_TRIAGE_JSON]

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=1, enable_triage=True, triage_mode="two_stage",
    )

    assert len(prompts) == 2
    assert "ゲート1" not in prompts[0]
    assert len(result["decisions"]) == 1


def test_enable_triage_false_ignores_triage_mode(monkeypatch):
    """enable_triage=False の場合は triage_mode を無視し、現行挙動（素のEXTRACT_PROMPT・
    triage_items 不呼び出し）を維持する。"""
    _patch_knowledge(monkeypatch)
    prompts = []

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return _VALID_EXTRACTION_JSON

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)
    monkeypatch.setattr(
        slack, "triage_items",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("triage_items は enable_triage=False では呼ばれてはならない")
        ),
    )

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=1, enable_triage=False, triage_mode="integrated",
    )

    assert len(prompts) == 1
    assert "ゲート1" not in prompts[0]
    assert len(result["decisions"]) == 1


def test_bogus_triage_mode_raises_value_error():
    with pytest.raises(ValueError):
        slack.extract_from_thread(
            _ROW, "context", [], None,
            consensus_n=1, enable_triage=True, triage_mode="bogus",
        )


def test_consensus_integrated_skips_two_stage_triage(monkeypatch):
    """consensus_n=3 かつ triage_mode=integrated の場合、集約後に triage_items が
    呼ばれないこと。embedding 依存を避けるため、3サンプル中2件をJSONパース失敗
    させ drafts を1件に絞る（集約ロジック自体は通らない単一ドラフト経路）。"""
    _patch_knowledge(monkeypatch)
    prompts = []
    responses = ["not-json-1", "not-json-2", _VALID_EXTRACTION_JSON]

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        idx = len(prompts) - 1
        return responses[idx] if idx < len(responses) else responses[-1]

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)
    monkeypatch.setattr(
        slack, "triage_items",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("triage_items は集約後に呼ばれてはならない（integrated）")
        ),
    )

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=3, enable_triage=True, triage_mode="integrated",
    )

    assert len(prompts) == 3
    assert all("ゲート1" in p for p in prompts)
    assert len(result["decisions"]) == 1


# --------------------------------------------------------------------------- #
# consensus 集約経路（ドラフト2件以上、embedding非依存）
# --------------------------------------------------------------------------- #


def test_consensus_aggregation_two_stage_calls_triage_once(monkeypatch):
    """consensus_n=2 で2件のドラフトが得られ、集約（_consensus_decisions/
    _consensus_action_items）を通る経路で、two_stage では集約後に triage_items が
    1回だけ呼ばれること。embedding 依存を避けるため集約関数自体を monkeypatch する。"""
    _patch_knowledge(monkeypatch)
    prompts = []

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return _VALID_EXTRACTION_JSON

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)
    monkeypatch.setattr(
        slack, "_consensus_decisions",
        lambda drafts, min_vote, threshold: [{"content": "集約後の決定", "decided_at": None}],
    )
    monkeypatch.setattr(
        slack, "_consensus_action_items",
        lambda drafts, min_vote, threshold: [],
    )

    triage_calls = {"n": 0}

    def fake_triage(extracted, milestones):
        triage_calls["n"] += 1
        return extracted

    monkeypatch.setattr(slack, "triage_items", fake_triage)

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=2, enable_triage=True, triage_mode="two_stage",
    )

    assert len(prompts) == 2  # _sample_extractions の2回のみ（triage_items はmock）
    assert triage_calls["n"] == 1
    assert result["decisions"] == [{"content": "集約後の決定", "decided_at": None}]


def test_consensus_aggregation_integrated_skips_triage(monkeypatch):
    """同条件（consensus_n=2、集約経路）で triage_mode=integrated の場合、
    集約後に triage_items が呼ばれないこと。"""
    _patch_knowledge(monkeypatch)
    prompts = []

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return _VALID_EXTRACTION_JSON

    monkeypatch.setattr(slack, "call_argus_llm", fake_call)
    monkeypatch.setattr(
        slack, "_consensus_decisions",
        lambda drafts, min_vote, threshold: [{"content": "集約後の決定", "decided_at": None}],
    )
    monkeypatch.setattr(
        slack, "_consensus_action_items",
        lambda drafts, min_vote, threshold: [],
    )
    monkeypatch.setattr(
        slack, "triage_items",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("triage_items は集約後に呼ばれてはならない（integrated）")
        ),
    )

    result = slack.extract_from_thread(
        _ROW, "context", [], None,
        consensus_n=2, enable_triage=True, triage_mode="integrated",
    )

    assert len(prompts) == 2
    assert all("ゲート1" in p for p in prompts)
    assert result["decisions"] == [{"content": "集約後の決定", "decided_at": None}]
