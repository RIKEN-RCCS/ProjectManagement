"""model_pin（供給網の固定、docs/security-architecture.md §4.6）のテスト。"""
from __future__ import annotations

import pytest
from utils import model_pin
from utils.model_pin import ModelPinError, assert_model_allowed


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(model_pin, "_pin_cache", None)
    monkeypatch.setattr(model_pin, "_warned", set())
    yield


@pytest.fixture
def pin(tmp_path, monkeypatch):
    p = tmp_path / "model_pin.yaml"
    p.write_text(
        """
models:
  glm-5.2:
    served_model_name: glm-5.2
    production: true
    verified_at: "2026-08-01"
    declared_revision: null
  未検証モデル:
    served_model_name: unverified-x
    production: true
    verified_at: null
  評価専用:
    served_model_name: judge-y
    production: false
    verified_at: "2026-08-01"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_MODEL_PIN_PATH", str(p))
    return p


class TestEnforce:
    def test_verified_production_model_passes(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        assert_model_allowed("glm-5.2")

    def test_served_name_also_matches(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        assert_model_allowed("judge-y", production=False)

    def test_unknown_model_is_rejected(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        with pytest.raises(ModelPinError, match="宣言されていません"):
            assert_model_allowed("どこかの新しいモデル")

    def test_eval_only_model_rejected_in_production(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        with pytest.raises(ModelPinError, match="評価専用"):
            assert_model_allowed("judge-y")

    def test_unverified_model_rejected(self, pin, monkeypatch):
        """verified_at が null＝id 照合が済んでいないモデルは本番で使わせない。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        with pytest.raises(ModelPinError, match="id 照合が未実施"):
            assert_model_allowed("unverified-x")


class TestWarnAndOff:
    def test_warn_allows_unknown_model(self, pin, monkeypatch, caplog):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "warn")
        with caplog.at_level("WARNING"):
            assert_model_allowed("未知のモデル")
        assert "MODELPIN" in caplog.text

    def test_warn_logs_once_per_model(self, pin, monkeypatch, caplog):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "warn")
        with caplog.at_level("WARNING"):
            assert_model_allowed("未知のモデル")
            assert_model_allowed("未知のモデル")
        assert caplog.text.count("未知のモデル") == 1

    def test_off_skips_entirely(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "off")
        assert_model_allowed("何でも通る")

    def test_empty_model_id_is_noop(self, pin, monkeypatch):
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        assert_model_allowed("")


class TestRealPinFile:
    def test_repository_pin_declares_production_models(self):
        """リポジトリの実ファイルに本番3モデルが宣言されていること。"""
        m = model_pin.models()
        assert {"glm-5.2", "Kimi-K2-Thinking", "bge-m3"} <= set(m)

    def test_k3_is_not_production_and_engine_unconfirmed(self):
        """K3 は本番不可のまま、engine/trust_remote_code は未確認であること（§4.6）。"""
        k3 = model_pin.models()["kimi-k3"]
        assert k3["production"] is False
        assert k3["declared_trust_remote_code"] is None
        assert k3["declared_engine"] is None

    def test_declared_fields_are_not_used_for_decisions(self, pin, monkeypatch):
        """declared_* は判定に使わない（検証できないものを根拠にしない・P10）。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        assert_model_allowed("glm-5.2")  # declared_revision が null でも通る
