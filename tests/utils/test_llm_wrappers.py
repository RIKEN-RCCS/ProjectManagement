"""Tests for LLM wrapper functions (requests.post mocked)."""
import json
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


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


# --------------------------------------------------------------------------- #
# call_rivault
# --------------------------------------------------------------------------- #

class TestCallRivault:
    def test_raises_without_rivault_url(self, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "")
        from utils.llm import call_rivault
        with pytest.raises(RuntimeError, match="RIVAULT_URL"):
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
# call_argus_llm — routing logic
# --------------------------------------------------------------------------- #

class TestCallArgusLlm:
    def test_uses_local_by_default(self, monkeypatch):
        """RIVAULT_URL 未設定 → ローカル LLM を使う。"""
        monkeypatch.setenv("RIVAULT_URL", "")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")

        called = {}
        def fake_local(*a, **kw):
            called["local"] = True
            return "local result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        # health check をスキップ
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test")
        assert called.get("local")
        assert result == "local result"

    def test_uses_rivault_when_url_set(self, monkeypatch):
        """RIVAULT_URL が設定されている → RiVault を優先。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")
        monkeypatch.setenv("RIVAULT_TOKEN", "tok")

        called = {}
        def fake_rivault(*a, **kw):
            called["rivault"] = True
            return "rivault result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)

        result = cli_utils.call_argus_llm("test")
        assert called.get("rivault")
        assert result == "rivault result"

    def test_fallback_to_local_on_rivault_failure(self, monkeypatch):
        """RiVault 失敗時は fallback=True なら local にフォールバック。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")

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

    def test_no_fallback_raises_on_failure(self, monkeypatch):
        """fallback=False では失敗時に例外を再送出する。"""
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")

        def fake_rivault(*a, **kw):
            raise RuntimeError("RiVault down")

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)

        with pytest.raises(RuntimeError, match="RiVault down"):
            cli_utils.call_argus_llm("test", fallback=False)
