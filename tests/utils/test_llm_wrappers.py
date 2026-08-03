"""Tests for LLM wrapper functions (requests.post mocked)."""
import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _skip_llm_secrets(monkeypatch):
    """load_llm_secrets() による ~/.secrets の source をスキップし、
    monkeypatch で設定した環境変数がファイルの実値で上書きされないようにする。"""
    monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "1")


# --------------------------------------------------------------------------- #
# SSE response helper
# --------------------------------------------------------------------------- #

def _make_sse_response(tokens: list[str], status_code: int = 200) -> MagicMock:
    """Build a mock streaming response that yields SSE chunks."""
    lines = []
    for t in tokens:
        chunk = {"choices": [{"delta": {"content": t}}]}
        lines.append(f"data: {json.dumps(chunk)}".encode())
    lines.append(b"data: [DONE]")

    mock = MagicMock()
    mock.status_code = status_code
    mock.iter_lines.return_value = iter(lines)
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_non_stream_response(content: str, status_code: int = 200) -> MagicMock:
    """Build a mock non-streaming response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"choices": [{"message": {"content": content}}]}
    mock.text = content
    return mock


# --------------------------------------------------------------------------- #
# _call_local_llm_inner — streaming mode
# --------------------------------------------------------------------------- #

class TestCallLocalLlmInnerStreaming:
    def _call(self, mock_post, **kwargs):
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", mock_post):
            return _call_local_llm_inner(
                "test prompt",
                model="test-model",
                base_url="http://localhost:8000/v1",
                api_key="dummy",
                **kwargs,
            )

    def test_basic_streaming_returns_content(self):
        mock = _make_sse_response(["hello", " world"])
        result = self._call(MagicMock(return_value=mock))
        assert result == "hello world"

    def test_streaming_strips_think_blocks(self):
        mock = _make_sse_response(["<think>thinking</think>", "final answer"])
        result = self._call(MagicMock(return_value=mock))
        assert "thinking" not in result
        assert "final answer" in result

    def test_streaming_empty_content_parts(self):
        mock = _make_sse_response([])
        result = self._call(MagicMock(return_value=mock))
        assert result == ""

    def test_non_streaming_returns_content(self):
        mock = _make_non_stream_response("answer text")
        result = self._call(MagicMock(return_value=mock), no_stream=True)
        assert result == "answer text"

    def test_4xx_raises(self):
        mock = MagicMock()
        mock.status_code = 500
        mock.text = "server error"
        mock.raise_for_status.side_effect = Exception("HTTP 500")
        with pytest.raises(Exception):
            self._call(MagicMock(return_value=mock))

    def test_url_constructed_correctly(self):
        mock = _make_sse_response(["ok"])
        captured = {}
        def fake_post(url, **kwargs):
            captured["url"] = url
            return mock
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", fake_post):
            _call_local_llm_inner(
                "p", model="m",
                base_url="http://host:8000/v1",
                api_key="k",
            )
        assert captured["url"] == "http://host:8000/v1/chat/completions"

    def test_bearer_token_in_header(self):
        mock = _make_sse_response(["ok"])
        captured = {}
        def fake_post(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return mock
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", fake_post):
            _call_local_llm_inner("p", model="m", base_url="http://h/v1", api_key="secret")
        assert captured["headers"].get("Authorization") == "Bearer secret"

    def test_return_reasoning_false_returns_str_default(self):
        """既定 (return_reasoning=False) は従来通り str のみを返す（回帰防止）。"""
        mock = _make_sse_response(["hello"])
        result = self._call(MagicMock(return_value=mock))
        assert isinstance(result, str)
        assert result == "hello"

    def test_streaming_return_reasoning_true_returns_tuple(self):
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'thinking...'}}]})}".encode(),
            f"data: {json.dumps({'choices': [{'delta': {'content': 'final answer'}}]})}".encode(),
            b"data: [DONE]",
        ]
        mock = MagicMock()
        mock.status_code = 200
        mock.iter_lines.return_value = iter(lines)
        result = self._call(MagicMock(return_value=mock), return_reasoning=True)
        assert result == ("final answer", "thinking...")

    def test_non_streaming_return_reasoning_true_returns_tuple(self):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {
            "choices": [{"message": {"content": "answer", "reasoning_content": "why"}}]
        }
        mock.text = "answer"
        result = self._call(MagicMock(return_value=mock), no_stream=True, return_reasoning=True)
        assert result == ("answer", "why")

    def test_reasoning_effort_included_in_payload_when_set(self):
        mock = _make_sse_response(["ok"])
        captured = {}
        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return mock
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", fake_post):
            _call_local_llm_inner(
                "p", model="m", base_url="http://h/v1", api_key="k",
                reasoning_effort="low",
            )
        assert captured["json"].get("reasoning_effort") == "low"

    def test_reasoning_effort_omitted_from_payload_when_none(self):
        """既定 (reasoning_effort=None) では payload に reasoning_effort を含めない（既存挙動維持）。"""
        mock = _make_sse_response(["ok"])
        captured = {}
        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return mock
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", fake_post):
            _call_local_llm_inner("p", model="m", base_url="http://h/v1", api_key="k")
        assert "reasoning_effort" not in captured["json"]

    def test_log_line_includes_reasoning_effort_when_set(self, capsys):
        mock = _make_sse_response(["ok"])
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", MagicMock(return_value=mock)):
            _call_local_llm_inner(
                "p", model="m", base_url="http://h/v1", api_key="k",
                reasoning_effort="low",
            )
        captured = capsys.readouterr()
        assert "reasoning_effort=low" in captured.err

    def test_log_line_omits_reasoning_effort_when_unset(self, capsys):
        mock = _make_sse_response(["ok"])
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", MagicMock(return_value=mock)):
            _call_local_llm_inner("p", model="m", base_url="http://h/v1", api_key="k")
        captured = capsys.readouterr()
        assert "reasoning_effort=" not in captured.err


# --------------------------------------------------------------------------- #
# _call_local_llm_inner — reasoning_content の <think> タグフォールバック / strip / ガード
# --------------------------------------------------------------------------- #

class TestCallLocalLlmInnerReasoningFallback:
    def _call(self, mock_post, **kwargs):
        from utils.llm import _call_local_llm_inner
        with patch("requests.post", mock_post):
            return _call_local_llm_inner(
                "test prompt", model="test-model",
                base_url="http://localhost:8000/v1", api_key="dummy",
                **kwargs,
            )

    def test_streaming_falls_back_to_think_tag_when_reasoning_content_empty(self):
        mock = _make_sse_response(["<think>fallback reasoning</think>", "final text"])
        result = self._call(MagicMock(return_value=mock), return_reasoning=True)
        assert result == ("final text", "fallback reasoning")

    def test_non_streaming_falls_back_to_think_tag_when_reasoning_content_empty(self):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {
            "choices": [{"message": {"content": "<think>fallback text</think>final answer"}}]
        }
        mock.text = "answer"
        result = self._call(MagicMock(return_value=mock), no_stream=True, return_reasoning=True)
        assert result == ("final answer", "fallback text")

    def test_truncated_think_tag_fallback_stays_empty(self):
        """閉じタグ欠落（truncation）時はフォールバック抽出も諦めて空のまま。"""
        mock = _make_sse_response(["<think>partial thinking with no closing tag"])
        result = self._call(MagicMock(return_value=mock), return_reasoning=True)
        assert result == ("", "")

    def test_reasoning_content_field_takes_priority_over_think_tag(self):
        """reasoning_content フィールドが取れていればフォールバックは使わない。"""
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': ' from field '}}]})}".encode(),
            f"data: {json.dumps({'choices': [{'delta': {'content': '<think>should not be used</think>final'}}]})}".encode(),
            b"data: [DONE]",
        ]
        mock = MagicMock()
        mock.status_code = 200
        mock.iter_lines.return_value = iter(lines)
        result = self._call(MagicMock(return_value=mock), return_reasoning=True)
        assert result == ("final", "from field")

    def test_reasoning_content_is_stripped(self):
        """call_rivault と整合させるため reasoning_content は strip() する。"""
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': '  padded  '}}]})}".encode(),
            f"data: {json.dumps({'choices': [{'delta': {'content': 'ok'}}]})}".encode(),
            b"data: [DONE]",
        ]
        mock = MagicMock()
        mock.status_code = 200
        mock.iter_lines.return_value = iter(lines)
        result = self._call(MagicMock(return_value=mock), return_reasoning=True)
        assert result == ("ok", "padded")

    def test_reasoning_content_ignored_when_return_reasoning_false(self):
        """return_reasoning=False（既定）では reasoning_content が SSE に含まれていても
        無視され、通常の str 契約のまま返る（不要な蓄積をしないガードの回帰防止）。"""
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'x' * 100}}]})}".encode(),
            f"data: {json.dumps({'choices': [{'delta': {'content': 'answer'}}]})}".encode(),
            b"data: [DONE]",
        ]
        mock = MagicMock()
        mock.status_code = 200
        mock.iter_lines.return_value = iter(lines)
        result = self._call(MagicMock(return_value=mock))
        assert result == "answer"
        assert isinstance(result, str)


# --------------------------------------------------------------------------- #
# ARGUS_REASONING_EFFORT — whitelist 検証
# --------------------------------------------------------------------------- #

class TestReasoningEffortValidation:
    def test_invalid_value_ignored_and_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("ARGUS_REASONING_EFFORT", "bogus")
        from utils.llm import _resolve_reasoning_effort_env
        result = _resolve_reasoning_effort_env()
        assert result is None
        captured = capsys.readouterr()
        assert "WARN" in captured.err
        assert "bogus" in captured.err

    def test_valid_values_pass_through(self, monkeypatch):
        from utils.llm import _resolve_reasoning_effort_env
        for v in ("low", "high", "max"):
            monkeypatch.setenv("ARGUS_REASONING_EFFORT", v)
            assert _resolve_reasoning_effort_env() == v

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("ARGUS_REASONING_EFFORT", raising=False)
        from utils.llm import _resolve_reasoning_effort_env
        assert _resolve_reasoning_effort_env() is None

    def test_invalid_effort_not_propagated_to_local_route(self, monkeypatch):
        """不正値が送られてサーバ400→静かに別ルートへフォールバックする罠を防ぐ。"""
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.setenv("ARGUS_REASONING_EFFORT", "invalid_value")
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: ["local"])
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")
        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        cli_utils.call_argus_llm("test")
        assert captured.get("reasoning_effort") is None


# --------------------------------------------------------------------------- #
# call_argus_llm — reasoning 空のサイレント no-op 対策（WARN ログ）
# --------------------------------------------------------------------------- #

class TestEmptyReasoningWarnings:
    def _patch_config(self, monkeypatch, priority: list[str]):
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: priority)

    def test_rivault_route_returns_empty_reasoning_and_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        self._patch_config(monkeypatch, ["rivault"])
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", lambda *a, **kw: "content only")

        result = cli_utils.call_argus_llm("test", return_reasoning=True)
        assert result == ("content only", "")
        captured = capsys.readouterr()
        assert "route=rivault" in captured.err

    def test_local_route_warns_when_reasoning_ends_up_empty(self, monkeypatch, capsys):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["local"])
        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")
        monkeypatch.setattr(cli_utils, "call_local_llm", lambda *a, **kw: ("content", ""))

        result = cli_utils.call_argus_llm("test", return_reasoning=True)
        assert result == ("content", "")
        captured = capsys.readouterr()
        assert "route=local" in captured.err
        assert "reasoning_content が空です" in captured.err

    def test_local_route_no_warn_when_reasoning_present(self, monkeypatch, capsys):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["local"])
        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")
        monkeypatch.setattr(
            cli_utils, "call_local_llm", lambda *a, **kw: ("content", "some reasoning"),
        )

        result = cli_utils.call_argus_llm("test", return_reasoning=True)
        assert result == ("content", "some reasoning")
        captured = capsys.readouterr()
        assert "reasoning_content が空です" not in captured.err


# --------------------------------------------------------------------------- #
# call_argus_llm — ARGUS_REASONING_EFFORT 伝播 (local ルート)
# --------------------------------------------------------------------------- #

class TestArgusReasoningEffortPropagation:
    def _patch_config(self, monkeypatch, priority: list[str]):
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: priority)

    def test_argus_reasoning_effort_env_propagates_to_local_route(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.setenv("ARGUS_REASONING_EFFORT", "max")
        self._patch_config(monkeypatch, ["local"])

        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)

        result = cli_utils.call_argus_llm("test")
        assert result == "local result"
        assert captured.get("reasoning_effort") == "max"

    def test_argus_reasoning_effort_unset_defaults_to_none(self, monkeypatch):
        """未設定時は None のまま渡され、既存挙動と同一（payload に送られない）。"""
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.delenv("ARGUS_REASONING_EFFORT", raising=False)
        self._patch_config(monkeypatch, ["local"])

        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)

        cli_utils.call_argus_llm("test")
        assert captured.get("reasoning_effort") is None


# --------------------------------------------------------------------------- #
# ARGUS_LLM_TEMPERATURE — float 変換検証 + call_argus_llm 伝播 (local ルート限定)
# --------------------------------------------------------------------------- #

class TestResolveLlmTemperatureEnv:
    def test_invalid_value_ignored_and_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("ARGUS_LLM_TEMPERATURE", "not-a-float")
        from utils.llm import _resolve_llm_temperature_env
        result = _resolve_llm_temperature_env()
        assert result is None
        captured = capsys.readouterr()
        assert "WARN" in captured.err
        assert "not-a-float" in captured.err

    def test_valid_value_converted_to_float(self, monkeypatch):
        from utils.llm import _resolve_llm_temperature_env
        monkeypatch.setenv("ARGUS_LLM_TEMPERATURE", "1.0")
        assert _resolve_llm_temperature_env() == 1.0

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("ARGUS_LLM_TEMPERATURE", raising=False)
        from utils.llm import _resolve_llm_temperature_env
        assert _resolve_llm_temperature_env() is None


class TestArgusLlmTemperaturePropagation:
    def _patch_config(self, monkeypatch, priority: list[str]):
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: priority)

    def test_argus_llm_temperature_env_propagates_to_local_route(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.setenv("ARGUS_LLM_TEMPERATURE", "1.0")
        self._patch_config(monkeypatch, ["local"])

        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)

        result = cli_utils.call_argus_llm("test")
        assert result == "local result"
        assert captured.get("temperature") == 1.0

    def test_argus_llm_temperature_unset_defaults_to_none(self, monkeypatch):
        """未設定時は None のまま渡され、既存挙動と同一（call_local_llm 側の既定 0.6/0.8 が使われる）。"""
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.delenv("ARGUS_LLM_TEMPERATURE", raising=False)
        self._patch_config(monkeypatch, ["local"])

        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)

        cli_utils.call_argus_llm("test")
        assert captured.get("temperature") is None

    def test_explicit_temperature_arg_overrides_env(self, monkeypatch):
        """call_argus_llm の temperature 引数が明示されている場合は env より優先される。"""
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        monkeypatch.setenv("ARGUS_LLM_TEMPERATURE", "1.0")
        self._patch_config(monkeypatch, ["local"])

        from utils import llm as cli_utils
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        captured = {}
        def fake_local(*a, **kw):
            captured.update(kw)
            return "local result"
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)

        cli_utils.call_argus_llm("test", temperature=0.3)
        assert captured.get("temperature") == 0.3


# --------------------------------------------------------------------------- #
# call_rivault
# --------------------------------------------------------------------------- #

class TestCallRivault:
    def test_raises_without_rivault_url(self, monkeypatch):
        monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "1")
        monkeypatch.setenv("RIVAULT_URL", "")
        from utils.llm import call_rivault
        with pytest.raises(RuntimeError, match="RIVAULT_URL"):
            call_rivault("test")

    def test_raises_without_rivault_model(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.delenv("RIVAULT_MODEL", raising=False)
        from utils.llm import call_rivault
        with pytest.raises(RuntimeError, match="RIVAULT_MODEL"):
            call_rivault("test")

    def test_returns_content(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_TOKEN", "tok")
        monkeypatch.setenv("RIVAULT_MODEL", "test-glm")
        mock = _make_sse_response(["RiVault ", "response"])
        with patch("requests.post", return_value=mock):
            from utils.llm import call_rivault
            result = call_rivault("test")
        assert result == "RiVault response"

    def test_4xx_raises(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_MODEL", "test-glm")
        mock = MagicMock()
        mock.status_code = 401
        mock.text = "Unauthorized"
        mock.raise_for_status.side_effect = Exception("HTTP 401")
        with patch("requests.post", return_value=mock):
            from utils.llm import call_rivault
            with pytest.raises(Exception):
                call_rivault("test")

    def test_reasoning_content_fallback(self, monkeypatch):
        """content が空で reasoning_content のみの場合は reasoning を返す。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_MODEL", "test-glm")
        # reasoning_content のみを含む SSE
        lines = [
            json.dumps({"choices": [{"delta": {"reasoning_content": "thinking"}}]}).encode(),
            json.dumps({"choices": [{"delta": {"content": ""}}]}).encode(),
            b"data: [DONE]",
        ]
        # data: prefix を付ける
        sse_lines = [f"data: {l.decode()}".encode() for l in lines[:2]] + [lines[2]]
        mock = MagicMock()
        mock.status_code = 200
        mock.iter_lines.return_value = iter(sse_lines)
        with patch("requests.post", return_value=mock):
            from utils.llm import call_rivault
            result = call_rivault("test")
        assert result == "thinking"


# --------------------------------------------------------------------------- #
# load_llm_secrets — _LLM_ENV_PREFIXES
# --------------------------------------------------------------------------- #

class TestLoadLlmSecretsPrefixes:
    def test_local_ocr_prefix_included(self):
        from utils.llm import _LLM_ENV_PREFIXES
        assert "LOCAL_OCR_" in _LLM_ENV_PREFIXES

    def test_load_llm_secrets_propagates_local_ocr_vars(self, monkeypatch):
        """web デーモン経由のジョブで LOCAL_OCR_MODEL が復旧されない問題の回帰防止。
        source した env に LOCAL_OCR_ プレフィックスの変数があれば os.environ に反映される。"""
        import os
        import subprocess

        from utils import llm as llm_mod

        monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "0")
        monkeypatch.setattr(llm_mod, "_llm_secrets_mtime_cache", None)
        monkeypatch.delenv("LOCAL_OCR_MODEL", raising=False)

        class _FakeResult:
            stdout = b"LOCAL_OCR_MODEL=test-ocr-model\x00IRRELEVANT_VAR=ignored\x00"

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult())

        try:
            llm_mod.load_llm_secrets()
            assert os.environ.get("LOCAL_OCR_MODEL") == "test-ocr-model"
            assert "IRRELEVANT_VAR" not in os.environ
        finally:
            os.environ.pop("LOCAL_OCR_MODEL", None)


# --------------------------------------------------------------------------- #
# call_vision_llm — SSE response helper
# --------------------------------------------------------------------------- #

def _make_vision_sse_response(tokens: list[str], usage: dict | None = None,
                               status_code: int = 200) -> MagicMock:
    """call_vision_llm 用の SSE モック。argus_ab.py:905-973 の流儀に合わせ、
    usage は choices が空の最終チャンクで届く形にする。"""
    lines = []
    for t in tokens:
        chunk = {"choices": [{"delta": {"content": t}}]}
        lines.append(f"data: {json.dumps(chunk)}".encode())
    if usage is not None:
        lines.append(f"data: {json.dumps({'choices': [], 'usage': usage})}".encode())
    lines.append(b"data: [DONE]")

    mock = MagicMock()
    mock.status_code = status_code
    mock.iter_lines.return_value = iter(lines)
    return mock


class TestCallVisionLlmContentStructure:
    def test_content_order_with_labels(self, tmp_path):
        img1 = tmp_path / "a.png"
        img1.write_bytes(b"fakedata1")
        img2 = tmp_path / "b.png"
        img2.write_bytes(b"fakedata2")
        mock = _make_vision_sse_response(["answer"])
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return mock

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            call_vision_llm(
                "説明して", [img1, img2],
                model="k3", base_url="http://host/v1",
                image_labels=["スライド1", "スライド2"],
            )
        content = captured["json"]["messages"][-1]["content"]
        assert content[0] == {"type": "text", "text": "スライド1"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[2] == {"type": "text", "text": "スライド2"}
        assert content[3]["type"] == "image_url"
        assert content[4] == {"type": "text", "text": "説明して"}
        assert captured["json"]["stream"] is True
        assert captured["json"]["stream_options"] == {"include_usage": True}

    def test_content_order_without_labels(self, tmp_path):
        img1 = tmp_path / "a.png"
        img1.write_bytes(b"fakedata")
        mock = _make_vision_sse_response(["ok"])
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return mock

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            call_vision_llm("prompt", [img1], model="k3", base_url="http://host/v1")
        content = captured["json"]["messages"][-1]["content"]
        assert content[0]["type"] == "image_url"
        assert content[1] == {"type": "text", "text": "prompt"}

    def test_mismatched_image_labels_length_raises(self, tmp_path):
        img1 = tmp_path / "a.png"
        img1.write_bytes(b"fakedata")
        from utils.llm import call_vision_llm
        with pytest.raises(ValueError):
            call_vision_llm(
                "prompt", [img1], model="k3", base_url="http://host/v1",
                image_labels=["l1", "l2"],
            )


class TestCallVisionLlmUsage:
    def test_usage_recovery_and_log_line(self, tmp_path, capsys):
        img = tmp_path / "a.png"
        img.write_bytes(b"fakedata")
        usage = {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_tokens_details": {"image_tokens": 80, "cached_tokens": 10},
        }
        mock = _make_vision_sse_response(["hello", " world"], usage=usage)
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            text, usage_out = call_vision_llm(
                "prompt", [img], model="k3", base_url="http://host/v1",
                return_usage=True,
            )
        assert text == "hello world"
        assert usage_out == {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "image_tokens": 80, "cached_tokens": 10,
        }
        captured = capsys.readouterr()
        assert "vision usage: prompt=100 image=80 completion=20 cached=10" in captured.err
        assert "images=1" in captured.err

    def test_return_usage_false_returns_str_only(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"fakedata")
        mock = _make_vision_sse_response(["ok"])
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            result = call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        assert result == "ok"
        assert isinstance(result, str)

    def test_missing_usage_fields_logged_as_zero_not_none(self, tmp_path, capsys):
        """usage が全く届かない（None混入）場合でも、ログの usage 値は常に整数で出す契約。"""
        img = tmp_path / "a.png"
        img.write_bytes(b"fakedata")
        mock = _make_vision_sse_response(["ok"])  # usage=None → 全フィールド欠測
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        err = capsys.readouterr().err
        assert "None" not in err
        assert "vision usage: prompt=0 image=0 completion=0 cached=0" in err

    def test_empty_content_raises_runtimeerror(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"fakedata")
        mock = _make_vision_sse_response([])  # トークンなし → content 空
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            with pytest.raises(RuntimeError, match="空の応答"):
                call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")

    def test_whitespace_only_content_raises_runtimeerror(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"fakedata")
        mock = _make_vision_sse_response(["   ", "\n"])
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            with pytest.raises(RuntimeError, match="空の応答"):
                call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")


class TestCallVisionLlmCtxOverflowRetry:
    def test_retries_with_halved_images_then_succeeds(self, tmp_path):
        imgs = []
        for i in range(4):
            p = tmp_path / f"{i}.png"
            p.write_bytes(b"x")
            imgs.append(p)
        err_resp = MagicMock()
        err_resp.status_code = 400
        err_resp.text = ("This model's maximum context length is 100 tokens. "
                          "However you requested at least 500 input tokens")
        ok_resp = _make_vision_sse_response(
            ["ok"],
            usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55,
                   "image_tokens": 40, "cached_tokens": 0},
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["json"])
            return err_resp if len(calls) == 1 else ok_resp

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            text, usage_out = call_vision_llm(
                "prompt", imgs, model="k3", base_url="http://host/v1", return_usage=True,
            )
        assert text == "ok"
        assert len(calls) == 2
        image_entries = [c for c in calls[1]["messages"][-1]["content"] if c["type"] == "image_url"]
        assert len(image_entries) == 2  # ceil(4/2) に間引かれている
        assert usage_out["image_tokens"] == 40

    def test_all_retries_fail_raises_runtimeerror_with_token_count(self, tmp_path):
        imgs = []
        for i in range(4):
            p = tmp_path / f"{i}.png"
            p.write_bytes(b"x")
            imgs.append(p)
        err_resp = MagicMock()
        err_resp.status_code = 400
        err_resp.text = ("maximum context length is 100 tokens. "
                          "However you requested at least 999 input tokens")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["json"])
            return err_resp

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            with pytest.raises(RuntimeError, match="999"):
                call_vision_llm("prompt", imgs, model="k3", base_url="http://host/v1")
        assert len(calls) == 3  # n, ceil(n/2), ceil(n/4) の3段


class TestCallVisionLlm5xxAndErrors:
    def test_5xx_raises_without_retry(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "internal error"
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(1)
            return resp

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        assert len(calls) == 1

    def test_connection_error_propagates_without_retry(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(1)
            raise ConnectionError("boom")

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            with pytest.raises(ConnectionError):
                call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        assert len(calls) == 1

    def test_strip_think_blocks_applied(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        mock = _make_vision_sse_response(["<think>reasoning</think>", "final answer"])
        from utils.llm import call_vision_llm
        with patch("requests.post", MagicMock(return_value=mock)):
            text = call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        assert text == "final answer"

    def test_nonexistent_image_path_raises_valueerror(self, tmp_path):
        from utils.llm import call_vision_llm
        with pytest.raises(ValueError):
            call_vision_llm(
                "prompt", [tmp_path / "missing.png"],
                model="k3", base_url="http://host/v1",
            )


class TestCallVisionLlmTemperature:
    def test_temperature_omitted_when_none(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        mock = _make_vision_sse_response(["ok"])
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs["json"]
            return mock

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            call_vision_llm("prompt", [img], model="k3", base_url="http://host/v1")
        assert "temperature" not in captured["json"]

    def test_temperature_included_when_set(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        mock = _make_vision_sse_response(["ok"])
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs["json"]
            return mock

        from utils.llm import call_vision_llm
        with patch("requests.post", fake_post):
            call_vision_llm(
                "prompt", [img], model="k3", base_url="http://host/v1", temperature=1.0,
            )
        assert captured["json"]["temperature"] == 1.0


class TestCallVisionLlmEnvPrefix:
    def test_minutes_vision_prefix_included(self):
        from utils.llm import _LLM_ENV_PREFIXES
        assert "MINUTES_VISION_" in _LLM_ENV_PREFIXES


# --------------------------------------------------------------------------- #
# _token_for_base — base_url に応じたトークン選択（RiVault/RIKYU 混同バグの回帰防止）
# --------------------------------------------------------------------------- #

class TestTokenForBase:
    def _clear(self, monkeypatch):
        for var in ("RIVAULT_URL", "RIVAULT_TOKEN", "LOCAL_LLM_TOKEN", "RIKYU_TOKEN"):
            monkeypatch.delenv(var, raising=False)

    def test_rivault_host_uses_rivault_token(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_TOKEN", "dummy-rivault")
        monkeypatch.setenv("LOCAL_LLM_TOKEN", "dummy-local")
        from utils.llm import _token_for_base
        assert _token_for_base("http://rivault.example/v1") == "dummy-rivault"

    def test_rivault_host_match_ignores_trailing_slash_and_v1(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1/")
        monkeypatch.setenv("RIVAULT_TOKEN", "dummy-rivault")
        from utils.llm import _token_for_base
        assert _token_for_base("http://rivault.example") == "dummy-rivault"
        assert _token_for_base("http://rivault.example/") == "dummy-rivault"
        assert _token_for_base("http://rivault.example/v1") == "dummy-rivault"

    def test_different_host_uses_local_llm_token(self, monkeypatch):
        """RIKYU 相当の別ホストには RIVAULT_TOKEN を使わない（本バグの本体）。"""
        self._clear(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_TOKEN", "dummy-rivault")
        monkeypatch.setenv("LOCAL_LLM_TOKEN", "dummy-rikyu")
        from utils.llm import _token_for_base
        assert _token_for_base("http://api.rikyu.example/v1") == "dummy-rikyu"

    def test_local_llm_token_unset_falls_back_to_rikyu_token(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_TOKEN", "dummy-rivault")
        monkeypatch.setenv("RIKYU_TOKEN", "dummy-rikyu-direct")
        from utils.llm import _token_for_base
        assert _token_for_base("http://api.rikyu.example/v1") == "dummy-rikyu-direct"

    def test_nothing_set_falls_back_to_dummy(self, monkeypatch):
        self._clear(monkeypatch)
        from utils.llm import _token_for_base
        assert _token_for_base("http://api.rikyu.example/v1") == "dummy"


# --------------------------------------------------------------------------- #
# call_local_llm — RiVault → local フォールバックの LOCAL_LLM_MODEL / pin 遵守
# --------------------------------------------------------------------------- #

class TestCallLocalLlmFallbackModelPin:
    def test_fallback_prefers_local_llm_model_over_detect(self, monkeypatch):
        """LOCAL_LLM_MODEL が設定されていれば detect_vllm_model を呼ばない。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://api.rikyu.example/v1")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "glm-5.2")

        from utils import llm as llm_mod

        def fail_if_called(*a, **kw):
            raise AssertionError("LOCAL_LLM_MODEL 設定時は detect_vllm_model を呼ばないはず")
        monkeypatch.setattr(llm_mod, "detect_vllm_model", fail_if_called)

        captured = {}

        def fake_inner(prompt, model, base_url, api_key, **kw):
            captured["model"] = model
            captured["base_url"] = base_url
            if base_url == "http://rivault.example/v1":
                raise RuntimeError("RiVault down")
            return "local result"
        monkeypatch.setattr(llm_mod, "_call_local_llm_inner", fake_inner)
        monkeypatch.setattr(llm_mod, "_assert_model_pin", MagicMock())

        result = llm_mod.call_local_llm(
            "prompt", model="rivault-model", base_url="http://rivault.example/v1",
            api_key="tok",
        )
        assert result == "local result"
        assert captured["model"] == "glm-5.2"

    def test_fallback_calls_assert_model_pin(self, monkeypatch):
        """フォールバック経路で解決したモデルに対して _assert_model_pin が呼ばれる。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://api.rikyu.example/v1")
        monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)

        from utils import llm as llm_mod
        monkeypatch.setattr(llm_mod, "detect_vllm_model", lambda *a, **kw: "qwen3.6-35b")

        def fake_inner(prompt, model, base_url, api_key, **kw):
            if base_url == "http://rivault.example/v1":
                raise RuntimeError("RiVault down")
            return "local result"
        monkeypatch.setattr(llm_mod, "_call_local_llm_inner", fake_inner)

        pin_mock = MagicMock()
        monkeypatch.setattr(llm_mod, "_assert_model_pin", pin_mock)

        result = llm_mod.call_local_llm(
            "prompt", model="rivault-model", base_url="http://rivault.example/v1",
            api_key="tok",
        )
        assert result == "local result"
        pin_mock.assert_called_once_with("qwen3.6-35b")


# --------------------------------------------------------------------------- #
# pin 照合のチョークポイント化（_call_local_llm_inner / call_rivault / call_vision_llm
# の HTTP 直前で _assert_model_pin を呼ぶ。2026-08-03）
# --------------------------------------------------------------------------- #

class TestChokepointModelPin:
    def test_call_local_llm_enforce_rejects_undeclared_model(self, monkeypatch):
        """model_pin.yaml に無いモデルを直接指定した call_local_llm は enforce で拒否される。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        from utils.llm import call_local_llm
        from utils.model_pin import ModelPinError
        with patch("requests.post") as mock_post:
            with pytest.raises(ModelPinError):
                call_local_llm(
                    "prompt", model="未宣言モデル",
                    base_url="http://localhost:8000/v1", api_key="dummy",
                    _fallback_to_local=False,
                )
            mock_post.assert_not_called()

    def test_call_local_llm_enforce_allows_declared_production_model(self, monkeypatch):
        """宣言済み・production: true のモデルは enforce でも通る（実 config/model_pin.yaml）。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        from utils.llm import call_local_llm
        mock = _make_sse_response(["ok"])
        with patch("requests.post", return_value=mock):
            result = call_local_llm(
                "prompt", model="glm-5.2",
                base_url="http://localhost:8000/v1", api_key="dummy",
                _fallback_to_local=False,
            )
        assert result == "ok"

    def test_call_vision_llm_enforce_rejects_undeclared_model(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        from utils.llm import call_vision_llm
        from utils.model_pin import ModelPinError
        with patch("requests.post") as mock_post:
            with pytest.raises(ModelPinError):
                call_vision_llm("prompt", [img], model="未宣言モデル", base_url="http://host/v1")
            mock_post.assert_not_called()

    def test_call_vision_llm_enforce_allows_declared_production_model(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        mock = _make_vision_sse_response(["ok"])
        from utils.llm import call_vision_llm
        with patch("requests.post", return_value=mock):
            result = call_vision_llm("prompt", [img], model="qwen3.6-35b", base_url="http://host/v1")
        assert result == "ok"

    def test_call_rivault_enforce_rejects_explicit_undeclared_model(self, monkeypatch):
        """call_rivault は model 明示指定でも HTTP 直前で pin 照合される（748行の分岐を通らない経路）。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        from utils.llm import call_rivault
        from utils.model_pin import ModelPinError
        with patch("requests.post") as mock_post:
            with pytest.raises(ModelPinError):
                call_rivault("prompt", model="未宣言モデル")
            mock_post.assert_not_called()

    def test_call_rivault_enforce_allows_declared_production_model(self, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        mock = _make_sse_response(["ok"])
        from utils.llm import call_rivault
        with patch("requests.post", return_value=mock):
            result = call_rivault("prompt", model="DeepSeek-V4-Flash")
        assert result == "ok"

    def test_kimi_k3_allowed_in_enforce_as_reader_only(self, monkeypatch):
        """kimi-k3 は 2026-08-03 に読み手（recall）専用として production: true に
        なったため enforce でも通る。役割の限定は pin では表現できず、role の記述と
        運用規律に依存する（config/model_pin.yaml 参照）。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        from utils.llm import call_local_llm
        mock = _make_sse_response(["ok"])
        with patch("requests.post", return_value=mock):
            result = call_local_llm(
                "prompt", model="kimi-k3",
                base_url="http://localhost:8000/v1", api_key="dummy",
                _fallback_to_local=False,
            )
        assert result == "ok"

    def test_off_mode_skips_pin_check_entirely(self, monkeypatch):
        """ARGUS_MODEL_PIN=off では未宣言モデルでも照合されず HTTP まで到達する。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "off")
        mock = _make_sse_response(["ok"])
        from utils.llm import call_local_llm
        with patch("requests.post", return_value=mock):
            result = call_local_llm(
                "prompt", model="未宣言モデル",
                base_url="http://localhost:8000/v1", api_key="dummy",
                _fallback_to_local=False,
            )
        assert result == "ok"

    def test_warn_mode_logs_but_still_calls_http(self, monkeypatch, caplog):
        """既定 (warn) では未宣言モデルでも警告のみで HTTP 呼び出しは実行される。"""
        monkeypatch.delenv("ARGUS_MODEL_PIN", raising=False)
        mock = _make_sse_response(["ok"])
        from utils.llm import call_local_llm
        with caplog.at_level("WARNING"):
            with patch("requests.post", return_value=mock):
                result = call_local_llm(
                    "prompt", model="未宣言モデル",
                    base_url="http://localhost:8000/v1", api_key="dummy",
                    _fallback_to_local=False,
                )
        assert result == "ok"
        assert "MODELPIN" in caplog.text


# --------------------------------------------------------------------------- #
# call_argus_llm — routing logic
# --------------------------------------------------------------------------- #

class TestCallArgusLlm:
    # -- Config-driven モード（常に routing_priority が必要） --

    def _patch_config(self, monkeypatch, priority: list[str]):
        """_load_llm_routing_priority を monkeypatch で差し替え。"""
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: priority)

    def test_think_true_on_rivault_route_is_logged_as_debug(self, monkeypatch, caplog):
        """think は local ルートのみ有効。rivault ルートに think=True を渡しても
        call_rivault には think 引数が渡らず、debug ログで無視される旨のみ記録される。"""
        import logging as _logging

        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        self._patch_config(monkeypatch, ["rivault"])

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", lambda *a, **kw: "rivault result")

        with caplog.at_level(_logging.DEBUG, logger="utils.llm"):
            result = cli_utils.call_argus_llm("test", think=True)

        assert result == "rivault result"
        assert any("think=True" in r.message for r in caplog.records)

    def test_config_priority_respected(self, monkeypatch):
        """config priority が [local, rivault] → local が先に呼ばれる。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["local", "rivault"])

        call_order = []
        def fake_local(*a, **kw):
            call_order.append("local")
            return "local result"
        def fake_rivault(*a, **kw):
            call_order.append("rivault")
            return "rivault result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test")
        assert call_order == ["local"]
        assert result == "local result"

    def test_config_priority_all_skipped_raises(self, monkeypatch):
        """全ルートスキップ → RuntimeError。"""
        monkeypatch.setenv("RIVAULT_URL", "")
        self._patch_config(monkeypatch, ["rivault"])

        from utils import llm as cli_utils
        with pytest.raises(RuntimeError, match="No LLM routes available"):
            cli_utils.call_argus_llm("test")

    def test_config_priority_fallback_chain(self, monkeypatch):
        """rivault 失敗 → local にフォールバック。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["rivault", "local"])

        def fake_rivault(*a, **kw):
            raise RuntimeError("RiVault down")
        def fake_local(*a, **kw):
            return "local fallback"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test", fallback=True)
        assert result == "local fallback"

    def test_config_priority_no_fallback_raises(self, monkeypatch):
        """fallback=False → rivault 失敗時に例外再送出。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        self._patch_config(monkeypatch, ["rivault", "local"])

        def fake_rivault(*a, **kw):
            raise RuntimeError("RiVault down")

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)

        with pytest.raises(RuntimeError, match="RiVault down"):
            cli_utils.call_argus_llm("test", fallback=False)

    def test_config_priority_first_available_used(self, monkeypatch):
        """config priority [rivault, local] で rivault が先に呼ばれる。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["rivault", "local"])

        call_order = []
        def fake_rivault(*a, **kw):
            call_order.append("rivault")
            return "rivault result"
        def fake_local(*a, **kw):
            call_order.append("local")
            return "local result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test")
        assert call_order == ["rivault"]
        assert result == "rivault result"


# --------------------------------------------------------------------------- #
# generate_minutes_local.py — smoke tests
# --------------------------------------------------------------------------- #

class TestGenerateMinutesLocal:
    """最小限の smoke test: main() のパースと変数参照が正常に動作すること。"""

    def test_main_rejects_nonexistent_file(self):
        """存在しないファイルパス → exit 1。"""
        from recording.generate_minutes_local import main
        with patch.object(sys, "argv", ["prog", "/nonexistent/file.md"]):
            rc = main()
        assert rc == 1

    def test_main_parses_args_without_nameerror(self, monkeypatch):
        """引数パース + 初期処理で NameError／AttributeError が起きない。"""
        import tempfile
        monkeypatch.setenv("RIVAULT_URL", "")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("dummy")
            tmp = f.name
        try:
            from recording.generate_minutes_local import main
            with patch.object(sys, "argv", ["prog", tmp, "--multi-stage", "--consensus", "1"]):
                rc = main()
            assert rc == 1  # parse_transcript 失敗で exit 1。NameError でないこと
        finally:
            import os as _os
            _os.unlink(tmp)

    def test_argparse_defaults_consensus_1_chunk_minutes_90(self, monkeypatch, tmp_path):
        """既定値の回帰: --consensus=1・--chunk-minutes=90（2026-07-26 A/B・R1/R2 反映）。"""
        import recording.generate_minutes_local as gml

        captured = {}

        def fake_generate_minutes(*args, **kwargs):
            captured.update(kwargs)
            return "dummy_output.md"

        monkeypatch.setattr(gml, "generate_minutes", fake_generate_minutes)
        transcript = tmp_path / "t.md"
        transcript.write_text("dummy", encoding="utf-8")
        with patch.object(sys, "argv", ["prog", str(transcript)]):
            rc = gml.main()
        assert rc == 0
        assert captured["consensus_n"] == 1
        assert captured["chunk_minutes"] == 90


class TestGenerateMinutesCore:

    def _make_transcript_md(self, tmp_path, segments=None):
        if segments is None:
            segments = [
                "#### [00:01:00 - 00:02:00] SPEAKER_00\nテスト発言1です",
                "#### [00:02:00 - 00:03:00] SPEAKER_01\nテスト発言2です",
            ]
        path = tmp_path / "test_transcript.md"
        path.write_text("\n\n".join(segments), encoding="utf-8")
        return str(path)

    def test_parse_transcript_basic(self, tmp_path):
        from recording.generate_minutes_local import parse_transcript
        segs = parse_transcript(self._make_transcript_md(tmp_path))
        assert len(segs) == 2

    def test_parse_transcript_skips_ellipsis(self, tmp_path):
        from recording.generate_minutes_local import parse_transcript
        segs = parse_transcript(self._make_transcript_md(tmp_path, [
            "#### [00:01:00 - 00:02:00] SPEAKER_00\n...",
            "#### [00:02:00 - 00:03:00] SPEAKER_01\n通常発言",
        ]))
        assert len(segs) == 1

    def test_chunk_transcript(self, tmp_path):
        from recording.generate_minutes_local import chunk_transcript, parse_transcript
        chunks = chunk_transcript(parse_transcript(self._make_transcript_md(tmp_path)), 3600)
        assert len(chunks) == 1

    def test_extract_from_chunk_routes_via_call_argus_llm(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        called = {}
        def fake_llm(prompt, **kw):
            called["called"] = True
            return "抽出結果"
        monkeypatch.setattr("recording.generate_minutes_local.call_argus_llm", fake_llm)
        monkeypatch.setenv("RIVAULT_URL", "")
        from recording.generate_minutes_local import extract_from_chunk
        result = extract_from_chunk("テキスト", 1, 2, "00:01:00〜00:02:00", "", 300)
        assert called.get("called") and result == "抽出結果"

    def test_load_local_llm_endpoint_unset_raises(self, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_TOKEN", raising=False)
        from recording.generate_minutes_local import load_local_llm_endpoint
        with pytest.raises(RuntimeError, match="LOCAL_LLM_URL"):
            load_local_llm_endpoint()

    def test_load_local_llm_endpoint_env(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://my-server:8080/v1")
        monkeypatch.setenv("LOCAL_LLM_TOKEN", "my-token")
        from recording.generate_minutes_local import load_local_llm_endpoint
        url, token = load_local_llm_endpoint()
        assert url == "http://my-server:8080/v1" and token == "my-token"

    def test_generate_minutes_basic(self, monkeypatch, tmp_path):
        def fake_llm(prompt, **kw):
            return "### テスト\n\n本文"
        # generate_minutes_local は from cli_utils import call_argus_llm で取り込んでいる。
        # 先に cli_utils の名前空間を差し替え、generate_minutes_local をリロードする
        from cli_utils import call_argus_llm as _orig
        monkeypatch.setattr("cli_utils.call_argus_llm", fake_llm)
        import importlib

        import recording.generate_minutes_local
        importlib.reload(recording.generate_minutes_local)
        from recording.generate_minutes_local import generate_minutes
        out = generate_minutes(self._make_transcript_md(tmp_path), str(tmp_path), 30,
                               multi_stage=False, consensus_n=1,
                               slide_context="")
        assert (tmp_path / out).exists()

    def test_generate_minutes_multistage_single_chunk_skips_stage1(self, monkeypatch, tmp_path):
        """1チャンクに収まる場合は Stage 1（extract_from_chunk）を呼ばず全文を投入する。"""
        def fake_llm(prompt, **kw):
            return "### テスト\n\n本文"
        monkeypatch.setattr("cli_utils.call_argus_llm", fake_llm)
        import importlib

        import recording.generate_minutes_local
        importlib.reload(recording.generate_minutes_local)

        def fail_if_called(*a, **kw):
            raise AssertionError("1チャンクに収まる場合は extract_from_chunk を呼ばないはず")
        monkeypatch.setattr(recording.generate_minutes_local, "extract_from_chunk", fail_if_called)

        # 全滅ガード（実効長200字未満で中断）に引っかからないよう十分な長さの発言にする
        long_segments = [
            "#### [00:01:00 - 00:02:00] SPEAKER_00\n" + "テスト発言1です。" * 15,
            "#### [00:02:00 - 00:03:00] SPEAKER_01\n" + "テスト発言2です。" * 15,
        ]
        out = recording.generate_minutes_local.generate_minutes(
            self._make_transcript_md(tmp_path, long_segments), str(tmp_path), 30,
            multi_stage=True, chunk_minutes=90, consensus_n=1,
            slide_context="", enable_triage=False,
        )
        assert (tmp_path / out).exists()
        combined_files = list(tmp_path.glob("*-combined.txt"))
        assert len(combined_files) == 1
        combined_text = combined_files[0].read_text(encoding="utf-8")
        assert "テスト発言1です" in combined_text and "テスト発言2です" in combined_text

    def test_generate_minutes_multistage_multi_chunk_scales_target_chars_and_timeout(
        self, monkeypatch, tmp_path,
    ):
        """複数チャンク時は Stage 1 の target_chars・timeout をチャンク長に比例させる
        （90分チャンク・timeout=30 → target_chars=5400, timeout=90）。"""
        def fake_llm(prompt, **kw):
            return "### テスト\n\n本文"
        monkeypatch.setattr("cli_utils.call_argus_llm", fake_llm)
        import importlib

        import recording.generate_minutes_local
        importlib.reload(recording.generate_minutes_local)

        fake_chunks = [
            [{"start": 0, "end": 60, "speaker": "SPEAKER_00", "text": "発言A"}],
            [{"start": 60, "end": 120, "speaker": "SPEAKER_01", "text": "発言B"}],
        ]
        monkeypatch.setattr(
            recording.generate_minutes_local, "chunk_transcript",
            lambda segments, chunk_duration_sec: fake_chunks,
        )

        calls = []

        def fake_extract(chunk_text, chunk_idx, total_chunks, time_range, claude_md_context,
                         timeout, **kw):
            calls.append({"timeout": timeout, "target_chars": kw.get("target_chars")})
            # 全滅ガード（実効長200字未満で中断）に引っかからないよう十分な長さにする
            return "抽出結果テキストです。" * 15
        monkeypatch.setattr(recording.generate_minutes_local, "extract_from_chunk", fake_extract)

        out = recording.generate_minutes_local.generate_minutes(
            self._make_transcript_md(tmp_path), str(tmp_path), 30,
            multi_stage=True, chunk_minutes=90, consensus_n=1,
            slide_context="", enable_triage=False,
        )
        assert (tmp_path / out).exists()
        assert len(calls) == 2  # 2チャンク分（各1回、リトライなし）
        for c in calls:
            assert c["timeout"] == 30 * max(1, 90 // 30)  # == 90
            assert c["target_chars"] == min(6000, max(800, 90 * 60))  # == 5400

    def test_generate_minutes_multistage_all_empty_extraction_raises(self, monkeypatch, tmp_path):
        """全チャンク抽出が空（全滅）の場合、空議事録を防ぐため非ゼロ終了（例外）になる。"""
        def fake_llm(prompt, **kw):
            return "### テスト\n\n本文"
        monkeypatch.setattr("cli_utils.call_argus_llm", fake_llm)
        import importlib

        import recording.generate_minutes_local
        importlib.reload(recording.generate_minutes_local)

        fake_chunks = [
            [{"start": 0, "end": 60, "speaker": "SPEAKER_00", "text": "発言A"}],
            [{"start": 60, "end": 120, "speaker": "SPEAKER_01", "text": "発言B"}],
        ]
        monkeypatch.setattr(
            recording.generate_minutes_local, "chunk_transcript",
            lambda segments, chunk_duration_sec: fake_chunks,
        )
        monkeypatch.setattr(recording.generate_minutes_local, "extract_from_chunk",
                            lambda *a, **kw: "")

        with pytest.raises(RuntimeError, match="空議事録を防ぐため中断"):
            recording.generate_minutes_local.generate_minutes(
                self._make_transcript_md(tmp_path), str(tmp_path), 30,
                multi_stage=True, chunk_minutes=90, consensus_n=1,
                slide_context="", enable_triage=False,
            )
