"""Pure-function tests for pm_argus_agent parsers."""
import logging
import re
import time

import pytest
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
# _effective_doc_qa_window_size — ARGUS_DOC_QA_WINDOW 環境変数オーバーライド
# --------------------------------------------------------------------------- #


def test_effective_doc_qa_window_size_default(monkeypatch):
    monkeypatch.delenv("ARGUS_DOC_QA_WINDOW", raising=False)
    assert pm_argus_agent._effective_doc_qa_window_size() == pm_argus_agent._DOC_QA_WINDOW_SIZE


def test_effective_doc_qa_window_size_overridden(monkeypatch):
    monkeypatch.setenv("ARGUS_DOC_QA_WINDOW", "150000")
    assert pm_argus_agent._effective_doc_qa_window_size() == 150000


def test_effective_doc_qa_window_size_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ARGUS_DOC_QA_WINDOW", "not-a-number")
    assert pm_argus_agent._effective_doc_qa_window_size() == pm_argus_agent._DOC_QA_WINDOW_SIZE


def test_effective_doc_qa_window_size_zero_falls_back(monkeypatch):
    monkeypatch.setenv("ARGUS_DOC_QA_WINDOW", "0")
    assert pm_argus_agent._effective_doc_qa_window_size() == pm_argus_agent._DOC_QA_WINDOW_SIZE


def test_split_document_windows_uses_effective_window_size(monkeypatch):
    """run_document_qa は _split_document_windows へ _effective_doc_qa_window_size() の
    結果を渡す。ここではその実効値をヘルパ経由で直接検証する（統合実行は不要）。"""
    content = "あ" * (pm_argus_agent._DOC_QA_WINDOW_SIZE + 1000)
    monkeypatch.delenv("ARGUS_DOC_QA_WINDOW", raising=False)
    default_windows = pm_argus_agent._split_document_windows(
        content, window_size=pm_argus_agent._effective_doc_qa_window_size(),
    )
    assert len(default_windows) == 2  # 既定 24000 では収まらず2窓に分割される

    monkeypatch.setenv("ARGUS_DOC_QA_WINDOW", str(pm_argus_agent._DOC_QA_WINDOW_SIZE + 2000))
    expanded_windows = pm_argus_agent._split_document_windows(
        content, window_size=pm_argus_agent._effective_doc_qa_window_size(),
    )
    assert len(expanded_windows) == 1  # 拡大後は1窓に収まる


def test_run_document_qa_passes_effective_window_size_to_split(monkeypatch, agent_context):
    """run_document_qa が _split_document_windows へ ARGUS_DOC_QA_WINDOW の実効値を
    window_size= として実際に渡す配線そのものを検証する（_split_document_windows
    自体をモックし、フル統合実行は行わない）。"""
    content = "本文サンプル"
    _patch_single_window_doc(monkeypatch, content)
    agent_context.record_ids = ["rid1"]
    agent_context.scoped_file_names = ["報告書.pdf"]
    monkeypatch.setenv("ARGUS_DOC_QA_WINDOW", "150000")

    captured = {}

    def fake_split(text, window_size=pm_argus_agent._DOC_QA_WINDOW_SIZE,
                   overlap=pm_argus_agent._DOC_QA_WINDOW_OVERLAP):
        captured["window_size"] = window_size
        return [text]
    monkeypatch.setattr(pm_argus_agent, "_split_document_windows", fake_split)

    fake = _FakeLLM(["抽出結果本文", "reduceによるまとめ"])
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake)

    run_document_qa("質問", None, agent_context)

    assert captured["window_size"] == 150000


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


# --------------------------------------------------------------------------- #
# run_agent — ARGUS_PRESERVE_REASONING (preserved thinking mode の簡易近似 / Option B)
# --------------------------------------------------------------------------- #


def test_run_agent_preserve_reasoning_injects_previous_step_block(monkeypatch, agent_context):
    """ARGUS_PRESERVE_REASONING=1 のとき、STEP2 のプロンプトに STEP1 の
    reasoning_content が <previous_step_reasoning> ブロックとして埋め込まれること。
    STEP1 自身のプロンプトには（前ステップが存在しないため）埋め込まれない。"""
    monkeypatch.setenv("ARGUS_PRESERVE_REASONING", "1")
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", lambda q: None)

    prompts = []
    call_count = {"n": 0}

    def fake_llm(prompt, **kwargs):
        prompts.append(prompt)
        call_count["n"] += 1
        assert kwargs.get("return_reasoning") is True
        if call_count["n"] == 1:
            return (
                '<tool_call>{"name": "__no_such_tool__", "args": {}}</tool_call>',
                "STEP1の思考メモ",
            )
        return ("<final_answer>調査完了</final_answer>", "STEP2の思考メモ")

    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    result = pm_argus_agent.run_agent(
        "テスト質問", "", None, agent_context, max_steps=3, timeout=30,
    )

    assert "調査完了" in result
    assert call_count["n"] == 2
    assert "<previous_step_reasoning>" not in prompts[0]
    assert "<previous_step_reasoning>" in prompts[1]
    assert "STEP1の思考メモ" in prompts[1]
    assert "前ステップの思考メモ" in prompts[1]


def test_run_agent_preserve_reasoning_disabled_by_default(monkeypatch, agent_context):
    """ARGUS_PRESERVE_REASONING 未設定時は reasoning 取得経路も呼ばれず（return_reasoning
    キーワード自体が渡らない）、previous_step_reasoning ブロックも挿入されない
    （既存挙動と完全同一・オーバーヘッドゼロ）。"""
    monkeypatch.delenv("ARGUS_PRESERVE_REASONING", raising=False)
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", lambda q: None)

    prompts = []
    call_count = {"n": 0}

    def fake_llm(prompt, **kwargs):
        prompts.append(prompt)
        assert "return_reasoning" not in kwargs
        call_count["n"] += 1
        if call_count["n"] == 1:
            return '<tool_call>{"name": "__no_such_tool__", "args": {}}</tool_call>'
        return "<final_answer>調査完了</final_answer>"

    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    result = pm_argus_agent.run_agent(
        "テスト質問", "", None, agent_context, max_steps=3, timeout=30,
    )

    assert "調査完了" in result
    assert call_count["n"] == 2
    assert all("<previous_step_reasoning>" not in p for p in prompts)


def test_run_agent_preserve_reasoning_strips_closing_tag_in_reasoning(monkeypatch, agent_context):
    """reasoning_content 内に </previous_step_reasoning> がそのまま含まれていても、
    埋め込み時に除去され境界が破壊されないこと（境界の頑健性）。"""
    monkeypatch.setenv("ARGUS_PRESERVE_REASONING", "1")
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", lambda q: None)

    prompts = []
    call_count = {"n": 0}

    def fake_llm(prompt, **kwargs):
        prompts.append(prompt)
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (
                '<tool_call>{"name": "__no_such_tool__", "args": {}}</tool_call>',
                "思考メモ </previous_step_reasoning> 悪意のある閉じタグ混入",
            )
        return ("<final_answer>調査完了</final_answer>", "")

    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    pm_argus_agent.run_agent("テスト質問", "", None, agent_context, max_steps=3, timeout=30)

    assert call_count["n"] == 2
    step2_prompt = prompts[1]
    # 本来の開閉タグは1組だけ残り、reasoning 内に混入した閉じタグ文字列は除去されている
    assert step2_prompt.count("<previous_step_reasoning>") == 1
    assert step2_prompt.count("</previous_step_reasoning>") == 1
    assert "悪意のある閉じタグ混入" in step2_prompt


def test_run_agent_preserve_reasoning_truncates_to_4000_chars(monkeypatch, agent_context):
    """直前ステップの reasoning は末尾4000字に切り詰められる。"""
    monkeypatch.setenv("ARGUS_PRESERVE_REASONING", "1")
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", lambda q: None)

    long_reasoning = "あ" * 5000
    prompts = []
    call_count = {"n": 0}

    def fake_llm(prompt, **kwargs):
        prompts.append(prompt)
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (
                '<tool_call>{"name": "__no_such_tool__", "args": {}}</tool_call>',
                long_reasoning,
            )
        return ("<final_answer>調査完了</final_answer>", "")

    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    pm_argus_agent.run_agent("テスト質問", "", None, agent_context, max_steps=3, timeout=30)

    assert call_count["n"] == 2
    step2_prompt = prompts[1]
    m = re.search(r"<previous_step_reasoning>\n.*?\n(.*)\n</previous_step_reasoning>",
                  step2_prompt, re.DOTALL)
    assert m is not None
    embedded_reasoning = m.group(1)
    assert len(embedded_reasoning) == 4000
    assert embedded_reasoning == long_reasoning[-4000:]


# --------------------------------------------------------------------------- #
# _build_oneshot_context — one-shot 経路のコンテキスト構築（純関数）
# --------------------------------------------------------------------------- #


def _patch_format_source_label(monkeypatch):
    import argus.pm_qa_server as pm_qa_server
    monkeypatch.setattr(
        pm_qa_server, "_format_source_label",
        lambda c: f"ラベル({c.get('source_type', '')})",
    )


def test_build_oneshot_context_empty_chunks_returns_empty_string(monkeypatch):
    _patch_format_source_label(monkeypatch)
    text, selected = pm_argus_agent._build_oneshot_context([], 100_000)
    assert text == ""
    assert selected == []


def test_build_oneshot_context_char_budget_drops_rrf_lower_ranked(monkeypatch):
    """RRF順（関連度降順）で渡した chunks のうち、char_budget を超える下位分が落ちる。

    各エントリの概算サイズは約150字（content100 + label/held_at/見出し概算50）。
    budget=400 では 1件目・2件目の合計約300字は収まるが3件目を足すと超過するため、
    3件目（最下位ランク）のみが落ちる。budget=150（旧値）だと1件目の概算サイズと
    ちょうど一致してしまい、「先頭は無条件採用」ガードの検証にしかならないため、
    2件目以降にも実際の予算判定が効くことを検証できる値に変更した。"""
    _patch_format_source_label(monkeypatch)
    chunks = [
        {"content": "あ" * 100, "held_at": "2026-06-01", "source_type": "minutes_content"},
        {"content": "い" * 100, "held_at": "2026-06-02", "source_type": "minutes_content"},
        {"content": "う" * 100, "held_at": "2026-06-03", "source_type": "minutes_content"},
    ]
    text, selected = pm_argus_agent._build_oneshot_context(chunks, char_budget=400)
    assert len(selected) == 2
    assert selected[0]["content"] == chunks[0]["content"]
    assert selected[1]["content"] == chunks[1]["content"]
    assert "う" * 100 not in text


def test_build_oneshot_context_sorts_selected_by_held_at_ascending(monkeypatch):
    """採用分は held_at 昇順に安定ソートされる（RRF入力順とは別）。"""
    _patch_format_source_label(monkeypatch)
    chunks = [
        {"content": "最新の内容", "held_at": "2026-06-03", "source_type": "minutes_content"},
        {"content": "最古の内容", "held_at": "2026-06-01", "source_type": "minutes_content"},
        {"content": "中間の内容", "held_at": "2026-06-02", "source_type": "minutes_content"},
    ]
    text, selected = pm_argus_agent._build_oneshot_context(chunks, char_budget=1_000_000)
    assert [c["held_at"] for c in selected] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert text.index("最古の内容") < text.index("中間の内容") < text.index("最新の内容")


def test_build_oneshot_context_numbers_and_includes_full_content(monkeypatch):
    """`[n] 出典: ラベル（held_at）` 形式のヘッダと全文（切り詰めなし）を含む。"""
    _patch_format_source_label(monkeypatch)
    long_content = "本文" * 5000  # 1200字抜粋を超える長さ
    chunks = [{"content": long_content, "held_at": "2026-06-01", "source_type": "minutes_content"}]
    text, selected = pm_argus_agent._build_oneshot_context(chunks, char_budget=1_000_000)
    assert "[1] 出典: ラベル(minutes_content)（2026-06-01）" in text
    assert long_content in text  # 全文（切り詰めなし）


# --------------------------------------------------------------------------- #
# _oneshot_enabled — 空文字 export の誤有効化バグ回避
# --------------------------------------------------------------------------- #


def test_oneshot_enabled_unset_is_false(monkeypatch):
    monkeypatch.delenv("ARGUS_ONESHOT", raising=False)
    assert pm_argus_agent._oneshot_enabled() is False


def test_oneshot_enabled_zero_is_false(monkeypatch):
    monkeypatch.setenv("ARGUS_ONESHOT", "0")
    assert pm_argus_agent._oneshot_enabled() is False


def test_oneshot_enabled_empty_string_is_false(monkeypatch):
    """空文字 export（例: `export ARGUS_ONESHOT=`）は誤って有効判定にならない。"""
    monkeypatch.setenv("ARGUS_ONESHOT", "")
    assert pm_argus_agent._oneshot_enabled() is False


def test_oneshot_enabled_one_is_true(monkeypatch):
    monkeypatch.setenv("ARGUS_ONESHOT", "1")
    assert pm_argus_agent._oneshot_enabled() is True


# --------------------------------------------------------------------------- #
# run_agent — ARGUS_ONESHOT 早期分岐（_rewrite_query バイパス保証）
# --------------------------------------------------------------------------- #


def test_run_agent_oneshot_env_bypasses_rewrite_and_delegates(monkeypatch, agent_context):
    """ARGUS_ONESHOT=1 のとき、run_agent は _rewrite_query を呼ばず _run_oneshot に委譲する。"""
    monkeypatch.setenv("ARGUS_ONESHOT", "1")

    rewrite_called = {"n": 0}

    def fake_rewrite(question):
        rewrite_called["n"] += 1
        return None
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", fake_rewrite)

    captured = {}

    def fake_run_oneshot(question, seed_data, ctx, *, timeout, include_intent_header, context):
        captured.update(
            question=question, seed_data=seed_data, ctx=ctx,
            timeout=timeout, include_intent_header=include_intent_header, context=context,
        )
        return "one-shot回答"
    monkeypatch.setattr(pm_argus_agent, "_run_oneshot", fake_run_oneshot)

    result = pm_argus_agent.run_agent(
        "テスト質問", "シード", None, agent_context, max_steps=3, timeout=30,
    )

    assert result == "one-shot回答"
    assert rewrite_called["n"] == 0
    assert captured["question"] == "テスト質問"
    assert captured["seed_data"] == "シード"
    assert captured["timeout"] == 30


def test_run_agent_oneshot_env_unset_does_not_delegate(monkeypatch, agent_context):
    """既定（ARGUS_ONESHOT 未設定）では one-shot 分岐に入らず、従来の rewrite→ループ経路を通る。"""
    monkeypatch.delenv("ARGUS_ONESHOT", raising=False)
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", lambda q: None)

    def fail_if_called(*a, **kw):
        raise AssertionError("ARGUS_ONESHOT 未設定時は _run_oneshot を呼ばないはず")
    monkeypatch.setattr(pm_argus_agent, "_run_oneshot", fail_if_called)

    def fake_llm(prompt, **kwargs):
        return "<final_answer>通常経路の回答</final_answer>"
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    result = pm_argus_agent.run_agent(
        "テスト質問", "", None, agent_context, max_steps=3, timeout=30,
    )
    assert "通常経路の回答" in result


# --------------------------------------------------------------------------- #
# one-shot env knob — ARGUS_ONESHOT_TOP_K / _CHAR_BUDGET / _MAX_TOKENS 既定値
# --------------------------------------------------------------------------- #


def test_oneshot_env_knob_defaults(monkeypatch):
    monkeypatch.delenv("ARGUS_ONESHOT_TOP_K", raising=False)
    monkeypatch.delenv("ARGUS_ONESHOT_CHAR_BUDGET", raising=False)
    monkeypatch.delenv("ARGUS_ONESHOT_MAX_TOKENS", raising=False)
    assert pm_argus_agent._effective_oneshot_top_k() == 200
    assert pm_argus_agent._effective_oneshot_char_budget() == 400_000
    assert pm_argus_agent._effective_oneshot_max_tokens() == 16_384


# --------------------------------------------------------------------------- #
# _run_oneshot — LLM 呼び出し1回・補助 LLM 呼び出し全種バイパスの回帰テスト
# --------------------------------------------------------------------------- #


def test_run_oneshot_calls_llm_exactly_once_and_bypasses_auxiliary_llm_calls(monkeypatch, agent_context):
    """_run_oneshot は retrieve_chunks_hybrid の広域検索1回のみを行い、
    query rewrite / keyword抽出 / HyDE / re-rank の補助 LLM 呼び出しを一切行わない。

    retrieve_chunks_hybrid は _run_oneshot 内で関数内 import されるため、
    argus.retrieval モジュール側の属性を patch する。
    """
    import argus.retrieval as retrieval_module

    _patch_format_source_label(monkeypatch)

    def fake_retrieve(*a, **kw):
        return [{"content": "本文", "held_at": "2026-06-01", "source_type": "minutes_content"}]
    monkeypatch.setattr(retrieval_module, "retrieve_chunks_hybrid", fake_retrieve)

    def _fail(name):
        def _f(*a, **kw):
            raise AssertionError(f"{name} が呼ばれてはいけません（one-shot は補助LLM呼び出しをバイパスするはず）")
        return _f

    monkeypatch.setattr(retrieval_module, "rerank_chunks", _fail("rerank_chunks"))
    monkeypatch.setattr(retrieval_module, "retrieve_chunks_hyde", _fail("retrieve_chunks_hyde"))
    monkeypatch.setattr(retrieval_module, "extract_search_keywords", _fail("extract_search_keywords"))
    monkeypatch.setattr(retrieval_module, "expand_query_hyde", _fail("expand_query_hyde"))
    monkeypatch.setattr(pm_argus_agent, "_rewrite_query", _fail("_rewrite_query"))

    llm_calls = {"n": 0}

    def fake_llm(prompt, **kwargs):
        llm_calls["n"] += 1
        return "<final_answer>調査結果\n\n## 出典\n- [1] foo</final_answer>"
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_llm)

    result = pm_argus_agent._run_oneshot(
        "質問", "", agent_context, timeout=30, include_intent_header=False, context="",
    )

    assert llm_calls["n"] == 1
    assert "調査結果" in result


def test_run_oneshot_returns_early_and_skips_llm_when_no_chunks_found(monkeypatch, agent_context, caplog):
    """retrieve_chunks_hybrid が空を返す場合、LLM を呼ばず定型応答で早期リターンする。
    [oneshot] retrieved=0 のログは維持される。"""
    import argus.retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "retrieve_chunks_hybrid", lambda *a, **kw: [])

    def fail_if_called(*a, **kw):
        raise AssertionError("chunks 0件時は LLM を呼んではいけません")
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fail_if_called)

    with caplog.at_level(logging.INFO, logger="pm_argus_agent"):
        result = pm_argus_agent._run_oneshot(
            "質問", "", agent_context, timeout=30, include_intent_header=False, context="",
        )

    assert "見つかりませんでした" in result
    assert any("[oneshot] retrieved=0" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# _call_oneshot_llm — one-shot LLM override（K3 配線第1弾、ARGUS_ONESHOT_LLM_URL）
# --------------------------------------------------------------------------- #


def test_call_oneshot_llm_override_unset_uses_call_argus_llm(monkeypatch):
    """override 未設定（既定）では call_argus_llm が呼ばれ、call_local_llm は呼ばれない。"""
    monkeypatch.delenv("ARGUS_ONESHOT_LLM_URL", raising=False)

    def fake_argus(prompt, **kwargs):
        return "通常経路の回答"
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_argus)

    def fail_if_called(*a, **kw):
        raise AssertionError("override 未設定時は call_local_llm を呼んではいけません")
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fail_if_called)

    result = pm_argus_agent._call_oneshot_llm(
        "プロンプト", system="システム", max_tokens=100, deadline=time.monotonic() + 30,
    )
    assert result == "通常経路の回答"


def test_call_oneshot_llm_override_enabled_calls_call_local_llm(monkeypatch):
    """URL+MODEL 設定時、call_local_llm が正しい引数（base_url/model/temperature/streaming）で呼ばれる。"""
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_URL", "http://k3-endpoint/v1")
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_MODEL", "kimi-k3")
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_TOKEN", "secret-token")
    monkeypatch.delenv("ARGUS_ONESHOT_LLM_TEMPERATURE", raising=False)

    def fail_if_called(*a, **kw):
        raise AssertionError("override 有効時は call_argus_llm を呼んではいけません")
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fail_if_called)

    captured = {}

    def fake_local(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return "override回答"
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fake_local)

    result = pm_argus_agent._call_oneshot_llm(
        "プロンプト", system="システム", max_tokens=100, deadline=time.monotonic() + 30,
    )

    assert result == "override回答"
    assert captured["base_url"] == "http://k3-endpoint/v1"
    assert captured["model"] == "kimi-k3"
    assert captured["api_key"] == "secret-token"
    assert captured["temperature"] == 1.0
    assert captured["think"] is False
    assert "no_stream" not in captured  # 既定のストリーミング受信を明示的に無効化しない


def test_call_oneshot_llm_missing_model_warns_and_falls_back(monkeypatch, caplog):
    """URL のみ（MODEL 欠落）の場合は WARN を出し、従来経路（call_argus_llm）を使う。"""
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_URL", "http://k3-endpoint/v1")
    monkeypatch.delenv("ARGUS_ONESHOT_LLM_MODEL", raising=False)

    def fake_argus(prompt, **kwargs):
        return "通常経路の回答"
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_argus)

    def fail_if_called(*a, **kw):
        raise AssertionError("MODEL 欠落時は call_local_llm を呼んではいけません")
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fail_if_called)

    with caplog.at_level(logging.WARNING, logger="pm_argus_agent"):
        result = pm_argus_agent._call_oneshot_llm(
            "プロンプト", system="システム", max_tokens=100, deadline=time.monotonic() + 30,
        )

    assert result == "通常経路の回答"
    assert any("ARGUS_ONESHOT_LLM_MODEL" in r.message for r in caplog.records)


def test_call_oneshot_llm_override_failure_falls_back_to_call_argus_llm(monkeypatch, caplog):
    """override 呼び出しが例外を送出した場合、call_argus_llm へ1回だけフォールバックする
    （残り時間が十分にある場合）。"""
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_URL", "http://k3-endpoint/v1")
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_MODEL", "kimi-k3")

    def fake_local(prompt, **kwargs):
        raise TimeoutError("override timeout")
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fake_local)

    def fake_argus(prompt, **kwargs):
        return "フォールバック回答"
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_argus)

    with caplog.at_level(logging.WARNING, logger="pm_argus_agent"):
        result = pm_argus_agent._call_oneshot_llm(
            "プロンプト", system="システム", max_tokens=100, deadline=time.monotonic() + 60,
        )

    assert result == "フォールバック回答"
    assert any("FALLBACK" in r.message for r in caplog.records)


def test_call_oneshot_llm_both_routes_fail_raises(monkeypatch):
    """override・従来経路の両方が失敗した場合は例外がそのまま送出される（残り時間十分な場合）。"""
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_URL", "http://k3-endpoint/v1")
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_MODEL", "kimi-k3")

    def fake_local(prompt, **kwargs):
        raise TimeoutError("override timeout")
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fake_local)

    def fake_argus(prompt, **kwargs):
        raise RuntimeError("fallback also failed")
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fake_argus)

    with pytest.raises(RuntimeError, match="fallback also failed"):
        pm_argus_agent._call_oneshot_llm(
            "プロンプト", system="システム", max_tokens=100, deadline=time.monotonic() + 60,
        )


def test_call_oneshot_llm_low_remaining_skips_fallback_and_raises_original(monkeypatch, caplog):
    """フォールバック直前の残り時間が _ONESHOT_FALLBACK_MIN_REMAINING_S 未満の場合、
    フォールバックせず override 呼び出しの元例外をそのまま送出する
    （wall-clock 二重消費を避けるため）。"""
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_URL", "http://k3-endpoint/v1")
    monkeypatch.setenv("ARGUS_ONESHOT_LLM_MODEL", "kimi-k3")

    def fake_local(prompt, **kwargs):
        raise TimeoutError("override timeout")
    monkeypatch.setattr(pm_argus_agent, "call_local_llm", fake_local)

    def fail_if_called(*a, **kw):
        raise AssertionError("残り時間不足時は call_argus_llm フォールバックを呼んではいけません")
    monkeypatch.setattr(pm_argus_agent, "call_argus_llm", fail_if_called)

    with caplog.at_level(logging.WARNING, logger="pm_argus_agent"):
        with pytest.raises(TimeoutError, match="override timeout"):
            pm_argus_agent._call_oneshot_llm(
                "プロンプト", system="システム", max_tokens=100,
                deadline=time.monotonic() + 5,
            )

    assert any("FALLBACK" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# _effective_investigate_timeout — ARGUS_INVESTIGATE_TIMEOUT の実効値解決
# --------------------------------------------------------------------------- #


def test_effective_investigate_timeout_default(monkeypatch):
    monkeypatch.delenv("ARGUS_INVESTIGATE_TIMEOUT", raising=False)
    assert pm_argus_agent._effective_investigate_timeout() == 480


def test_effective_investigate_timeout_overridden(monkeypatch):
    monkeypatch.setenv("ARGUS_INVESTIGATE_TIMEOUT", "600")
    assert pm_argus_agent._effective_investigate_timeout() == 600


def test_effective_investigate_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ARGUS_INVESTIGATE_TIMEOUT", "not-a-number")
    assert pm_argus_agent._effective_investigate_timeout() == 480


def test_run_agent_resolves_timeout_from_env_when_unspecified(monkeypatch, agent_context):
    """run_agent が timeout 未指定で呼ばれた場合、ARGUS_INVESTIGATE_TIMEOUT を解決する。"""
    monkeypatch.setenv("ARGUS_ONESHOT", "1")
    monkeypatch.setenv("ARGUS_INVESTIGATE_TIMEOUT", "600")

    captured = {}

    def fake_run_oneshot(question, seed_data, ctx, *, timeout, include_intent_header, context):
        captured["timeout"] = timeout
        return "one-shot回答"
    monkeypatch.setattr(pm_argus_agent, "_run_oneshot", fake_run_oneshot)

    result = pm_argus_agent.run_agent("質問", "シード", None, agent_context)

    assert result == "one-shot回答"
    assert captured["timeout"] == 600.0


def test_run_agent_explicit_timeout_bypasses_env(monkeypatch, agent_context):
    """run_agent に timeout が明示指定された場合、ARGUS_INVESTIGATE_TIMEOUT より優先される。"""
    monkeypatch.setenv("ARGUS_ONESHOT", "1")
    monkeypatch.setenv("ARGUS_INVESTIGATE_TIMEOUT", "600")

    captured = {}

    def fake_run_oneshot(question, seed_data, ctx, *, timeout, include_intent_header, context):
        captured["timeout"] = timeout
        return "one-shot回答"
    monkeypatch.setattr(pm_argus_agent, "_run_oneshot", fake_run_oneshot)

    pm_argus_agent.run_agent("質問", "シード", None, agent_context, timeout=30)

    assert captured["timeout"] == 30
