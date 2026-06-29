"""Tests for LLM wrapper functions (requests.post mocked)."""
import json
import sys
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
    # -- 後方互換モード（config なし = env-var ベース） --

    def _patch_no_config(self, monkeypatch):
        """_load_llm_routing_priority を None 返しに差し替え + ANTHROPIC_BASE_URL を無効化。"""
        from utils import llm as _llm
        monkeypatch.setattr(_llm, "_load_llm_routing_priority", lambda: None)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "")

    def test_uses_local_by_default(self, monkeypatch):
        """RIVAULT_URL 未設定 → ローカル LLM を使う。"""
        self._patch_no_config(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")

        called = {}
        def fake_local(*a, **kw):
            called["local"] = True
            return "local result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test")
        assert called.get("local")
        assert result == "local result"

    def test_uses_rivault_when_url_set(self, monkeypatch):
        """RIVAULT_URL が設定されている → RiVault を優先。"""
        self._patch_no_config(monkeypatch)
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
        self._patch_no_config(monkeypatch)
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
        self._patch_no_config(monkeypatch)
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example/v1")

        def fake_rivault(*a, **kw):
            raise RuntimeError("RiVault down")

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_rivault", fake_rivault)

        with pytest.raises(RuntimeError, match="RiVault down"):
            cli_utils.call_argus_llm("test", fallback=False)

    # -- Config-driven モード --

    def _patch_config(self, monkeypatch, priority: list[str]):
        """_load_llm_routing_priority を monkeypatch で差し替え。"""
        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "_load_llm_routing_priority", lambda: priority)

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

    def test_config_priority_skips_unconfigured(self, monkeypatch):
        """claude_code が優先度にあっても ANTHROPIC_BASE_URL 未設定ならスキップ。"""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "")  # 未設定
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        self._patch_config(monkeypatch, ["claude_code", "local"])

        called = {}
        def fake_local(*a, **kw):
            called["local"] = True
            return "local result"

        from utils import llm as cli_utils
        monkeypatch.setattr(cli_utils, "call_local_llm", fake_local)
        monkeypatch.setattr("requests.get", MagicMock(return_value=MagicMock(status_code=200)))
        monkeypatch.setattr(cli_utils, "detect_vllm_model", lambda *a, **kw: "test-model")

        result = cli_utils.call_argus_llm("test")
        assert called.get("local")
        assert result == "local result"

    def test_config_priority_all_skipped_raises(self, monkeypatch):
        """全ルートスキップ → RuntimeError。"""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
        monkeypatch.setenv("RIVAULT_URL", "")
        self._patch_config(monkeypatch, ["claude_code", "rivault"])

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

    def test_config_priority_prefer_rivault_override(self, monkeypatch):
        """config priority [local, rivault] + prefer_rivault() → rivault が先頭。"""
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

        with cli_utils.prefer_rivault():
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

    def test_load_local_llm_endpoint_default(self, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_TOKEN", raising=False)
        from recording.generate_minutes_local import load_local_llm_endpoint
        url, token = load_local_llm_endpoint()
        assert url == "http://localhost:8000/v1" and token == "dummy"

    def test_load_local_llm_endpoint_env(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://my-server:8080/v1")
        monkeypatch.setenv("LOCAL_LLM_TOKEN", "my-token")
        from recording.generate_minutes_local import load_local_llm_endpoint
        url, token = load_local_llm_endpoint()
        assert url == "http://my-server:8080/v1" and token == "my-token"

    def test_generate_minutes_basic(self, monkeypatch, tmp_path):
        def fake_llm(prompt, **kw):
            return "### テスト\n\n本文"
        from utils import llm
        monkeypatch.setattr(llm, "call_argus_llm", fake_llm)
        monkeypatch.setenv("RIVAULT_URL", "")
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:8000/v1")
        from recording.generate_minutes_local import generate_minutes
        out = generate_minutes(self._make_transcript_md(tmp_path), str(tmp_path), 30,
                               multi_stage=False, consensus_n=1,
                               slide_context="")
        assert (tmp_path / out).exists()
