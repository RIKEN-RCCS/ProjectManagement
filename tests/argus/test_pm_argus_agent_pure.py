"""Pure-function tests for pm_argus_agent parsers."""
from argus.agent_tools import TOOLS, _build_tool_descriptions
from argus.pm_argus_agent import parse_final_answer, parse_tool_calls


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
