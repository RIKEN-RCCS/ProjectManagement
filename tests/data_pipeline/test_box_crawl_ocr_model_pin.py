"""pm_box_crawl.py の OCR 経路がモデル pin（docs/security-architecture.md §4.6）を
通ることのテスト。`_ocr_image` は llm.py を経由しない唯一の本番 LLM 呼び出しであり、
`assert_model_allowed` を明示的に呼ぶ必要がある。

実ネットワークアクセスは行わない（requests.post をモンキーパッチする）。
"""
from __future__ import annotations

from pathlib import Path

import pm_box_crawl as box_crawl
import pytest
import requests
from utils import model_pin


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "slide.png"
    # 最小の有効な PNG である必要はない（_ocr_image は base64 化するだけ）。
    p.write_bytes(b"fake-png-bytes")
    return p


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestOcrImagePassesThroughModelPin:
    def test_assert_model_allowed_is_called_with_resolved_model(
        self, png_path, monkeypatch,
    ):
        monkeypatch.setenv("LOCAL_OCR_MODEL", "qwen3.6-35b")
        calls: list[str] = []
        monkeypatch.setattr(
            model_pin, "assert_model_allowed", lambda m, **k: calls.append(m),
        )
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: _FakeResponse("[図なし]"),
        )

        result = box_crawl._ocr_image(png_path, "http://127.0.0.1:1/v1")

        assert calls == ["qwen3.6-35b"]
        assert result == "[図なし]"

    def test_pin_violation_is_not_swallowed_as_generic_ocr_failure(
        self, png_path, monkeypatch, caplog,
    ):
        """pin 違反時は None を返す（既存の失敗時 None 設計を維持）が、
        ERROR ログで区別されること（WARNING の「マルチモーダルOCR失敗」に紛れない）。"""
        monkeypatch.setenv("LOCAL_OCR_MODEL", "pin-未宣言モデル")

        def _deny(model_id, **kwargs):
            raise model_pin.ModelPinError(f"[MODELPIN] モデル {model_id!r} は宣言されていません")

        monkeypatch.setattr(model_pin, "assert_model_allowed", _deny)

        posted = {"n": 0}

        def _fail_if_called(*a, **k):
            posted["n"] += 1
            raise AssertionError("pin 違反時はネットワーク呼び出しに進んではいけない")

        monkeypatch.setattr(requests, "post", _fail_if_called)

        with caplog.at_level("ERROR"):
            result = box_crawl._ocr_image(png_path, "http://127.0.0.1:1/v1")

        assert result is None
        assert posted["n"] == 0
        assert "MODELPIN" in caplog.text
        assert any(r.levelname == "ERROR" for r in caplog.records)
