"""Pure-function tests for pm_argus_agent parsers."""
import logging

from argus import pm_argus_agent
from argus.agent_tools import TOOLS, _build_tool_descriptions
from argus.pm_argus_agent import (
    parse_final_answer,
    parse_tool_calls,
    run_document_qa,
)

# --------------------------------------------------------------------------- #
# parse_tool_calls
# --------------------------------------------------------------------------- #


def test_parse_tool_call_single():
    resp = '<TOOL>{"name": "search_text", "args": {"query": "x"}}</TOOL>'
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0]["name"] == "search_text"
    assert calls[0]["args"] == {"query": "x"}


def test_parse_tool_call_multiple():
    resp = (
        '<TOOL>{"name": "search_text", "args": {"q": "a"}}</TOOL>'
        ' some text '
        '<TOOL>{"name": "get_milestone_progress", "args": {}}</TOOL>'
    )
    calls = parse_tool_calls(resp)
    assert len(calls) == 2
    assert {c["name"] for c in calls} == {"search_text", "get_milestone_progress"}


def test_parse_tool_call_invalid_json_returns_error():
    # regex matches <tool_call>...</tool_call> (lowercase)
    # invalid JSON inside → JSONDecodeError → error dict appended
    resp = '<tool_call>{not valid json}</tool_call>'
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert "error" in calls[0]


def test_parse_tool_call_missing_name_skipped():
    resp = '<TOOL>{"args": {"q": "x"}}</TOOL>'
    calls = parse_tool_calls(resp)
    # name empty → not appended
    assert calls == []


def test_parse_tool_call_empty_args_dict():
    resp = '<TOOL>{"name": "noop", "args": {}}</TOOL>'
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0]["args"] == {}


def test_parse_tool_calls_answer_tag_fallback():
    # <answer>...{json}...</answer> format should be parsed
    resp = '<answer>prefix {"name": "search_decisions", "args": {"q": "r"}} suffix</answer>'
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0]["name"] == "search_decisions"


def test_parse_tool_calls_raw_json_fallback():
    resp = '{"name": "search_text", "args": {"q": "y"}}'
    calls = parse_tool_calls(resp)
    assert len(calls) == 1
    assert calls[0]["name"] == "search_text"


def test_parse_tool_calls_empty_response():
    assert parse_tool_calls("") == []
    assert parse_tool_calls("just a plain answer") == []


# --------------------------------------------------------------------------- #
# parse_final_answer
# --------------------------------------------------------------------------- #


def test_parse_final_answer_tag():
    resp = 'pre <final_answer>結論です</final_answer> post'
    assert parse_final_answer(resp) == "結論です"


def test_parse_final_answer_none_when_absent():
    assert parse_final_answer("no tags here") is None


def test_parse_final_answer_answer_tag_non_json():
    resp = '<answer>これは最終回答です</answer>'
    assert parse_final_answer(resp) == "これは最終回答です"


def test_parse_final_answer_answer_tag_with_json_tool_call():
    resp = '<answer>{"name": "search_text", "args": {}}</answer>'
    # JSON tool-call-like content → not treated as final answer
    assert parse_final_answer(resp) is None


# --------------------------------------------------------------------------- #
# _build_tool_descriptions
# --------------------------------------------------------------------------- #


def test_build_tool_descriptions_non_empty():
    desc = _build_tool_descriptions()
    assert desc
    assert isinstance(desc, str)


def test_build_tool_descriptions_includes_all_tools():
    desc = _build_tool_descriptions()
    for t in TOOLS:
        assert t.name in desc


def test_build_tool_descriptions_format():
    desc = _build_tool_descriptions()
    # Each entry should start with "N. **name** — description"
    lines = desc.split("\n")
    first = lines[0]
    assert first.startswith("1. **")
    assert " — " in first
    # Parameters line follows
    assert "引数:" in lines[1]


def test_build_tool_descriptions_empty_tools(monkeypatch):
    """Edge case: empty TOOLS list."""
    monkeypatch.setattr("argus.agent_tools.TOOLS", [])
    desc = _build_tool_descriptions()
    assert desc == ""


# --------------------------------------------------------------------------- #
# run_document_qa — 疑わしい却下（偽の「関連情報なし」）対策
# --------------------------------------------------------------------------- #


def _patch_single_window_doc(monkeypatch, content: str, name: str = "報告書.pdf"):
    """1ファイル・1窓の doc_content を返すよう _fetch_doc_qa_sources を差し替える。"""
    docs = [{"record_id": "rid1", "name": name, "content": content}]
    monkeypatch.setattr(
        pm_argus_agent, "_fetch_doc_qa_sources", lambda ctx: (docs, []),
    )


class _FakeLLM:
    """呼び出し順に応答を返し、プロンプト全文を記録するスタブ。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("call_argus_llm が想定回数を超えて呼ばれた")
        return self.responses.pop(0)


def test_run_document_qa_retries_suspicious_no_info_and_recovers(monkeypatch, agent_context):
    """(a) 中身のある窓で1回目「関連情報なし」→リトライ発火→2回目成功で抽出反映。"""
    content = "第3章 LATTICE QCD 計算結果: 実行時間は 2847.77 秒であった。"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目の map（疑わしい却下）
        "LATTICE QCD の実行時間は 2847.77 秒。",  # リトライで成功
        "GENESIS/LATTICE QCD ともに 2847.77 秒。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    answer = run_document_qa("LATTICE QCDの実行時間は？", None, agent_context)

    assert len(fake.calls) == 3
    assert "再試行" in fake.calls[1]
    assert "2847.77" in answer
    assert "抽出に失敗" not in answer


def test_run_document_qa_records_failure_after_retry(monkeypatch, agent_context):
    """(b) 2回とも「なし」→制限事項に記録。"""
    content = "第3章 LATTICE QCD の章はあるが数値記述が乏しい断片。"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目
        "関連情報なし",  # リトライも失敗
        "抽出結果には該当情報がありません。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    answer = run_document_qa("LATTICE QCDの実行時間は？", None, agent_context)

    assert len(fake.calls) == 3
    assert "## 制限事項" in answer
    assert "LATTICE" in answer
    assert "抽出に失敗（2回試行）" in answer


def test_run_document_qa_no_retry_when_entity_absent(monkeypatch, agent_context):
    """(c) エンティティを含まない窓の「関連情報なし」はリトライしない（無駄呼び出しなし）。"""
    content = "第1章 プロジェクト概要（本件と無関係な章）。"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目のみ、リトライは発生しないはず
        "抽出結果には該当情報がありません。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    run_document_qa("LATTICE QCDの実行時間は？", None, agent_context)

    assert len(fake.calls) == 2
    assert "再試行" not in fake.calls[0]


def test_run_document_qa_reduce_input_has_fragment_header_with_entities(monkeypatch, agent_context):
    """(d) reduce 入力に断片ヘッダ（含まれるエンティティ）が付く。"""
    content = "第3章 LATTICE QCD 計算結果: 実行時間は 2847.77 秒であった。"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        # 50字以上にして「極端に短い」判定（疑わしい却下）に該当させない
        "LATTICE QCD の実行時間は 2847.77 秒であり、他の主要アプリと比べても妥当な水準の数値である。",
        "LATTICE QCD は 2847.77 秒。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    run_document_qa("LATTICE QCDの実行時間は？", None, agent_context)

    reduce_prompt = fake.calls[-1]
    assert "含まれるエンティティ" in reduce_prompt
    assert "LATTICE" in reduce_prompt


# --------------------------------------------------------------------------- #
# run_document_qa — フォールバックガード（エンティティ非依存）
# --------------------------------------------------------------------------- #


def _pad_to_length(text: str, min_len: int) -> str:
    filler = "本節は評価結果に関する背景説明を記述する。"
    while len(text) < min_len:
        text += filler
    return text


def test_run_document_qa_fallback_retries_long_window_without_entities(monkeypatch, agent_context):
    """(e) エンティティ空・5,000字以上の窓の「関連情報なし」→フォールバックリトライ発火。"""
    content = _pad_to_length("本報告書は評価結果の詳細を記述する。", pm_argus_agent._DOC_QA_FALLBACK_MIN_CHARS + 200)
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目（エンティティなし・長文窓）
        "性能評価の結論として、全アプリでGPU化により大幅な高速化を達成したと記述されている。",  # フォールバックリトライで成功
        "reduceによるまとめ",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    answer = run_document_qa("この報告書の性能評価の結論をまとめて", None, agent_context)

    assert len(fake.calls) == 3
    assert "再試行(フォールバック)" in fake.calls[1]
    assert "抽出に失敗" not in answer
    reduce_prompt = fake.calls[-1]
    assert "性能評価の結論として" in reduce_prompt


def test_run_document_qa_no_fallback_retry_for_short_window(monkeypatch, agent_context):
    """(f) 5,000字未満（表紙相当）の「なし」→リトライなし。"""
    content = "表紙: 「富岳NEXT」アプリ協調設計及びアプリ評価報告書"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目のみ、フォールバックリトライは発生しないはず
        "抽出結果には該当情報がありません。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    run_document_qa("この報告書の性能評価の結論をまとめて", None, agent_context)

    assert len(fake.calls) == 2
    assert "フォールバック" not in fake.calls[0]


def test_run_document_qa_fallback_failure_not_recorded_in_limitations(monkeypatch, agent_context, caplog):
    """(g) フォールバックリトライ失敗時は制限事項へは書かれずログのみ。"""
    content = _pad_to_length("本報告書は評価結果の詳細を記述する。", pm_argus_agent._DOC_QA_FALLBACK_MIN_CHARS + 200)
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]

    fake = _FakeLLM([
        "関連情報なし",  # 1回目
        "関連情報なし",  # フォールバックリトライも失敗
        "抽出結果には該当情報がありません。",  # reduce
    ])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    caplog.set_level(logging.INFO, logger="pm_argus_agent")
    answer = run_document_qa("この報告書の性能評価の結論をまとめて", None, agent_context)

    assert len(fake.calls) == 3
    assert "## 制限事項" not in answer
    assert "抽出に失敗" not in answer
    assert any("フォールバックリトライ後も関連情報なし" in r.message for r in caplog.records)
