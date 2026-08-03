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

    def test_k3_is_production_as_reader_only_but_engine_unconfirmed(self):
        """K3 は 2026-08-03 に読み手（recall）専用として production: true になったが、
        engine/trust_remote_code は依然未確認であること（嘘を書かない・§4.6）。"""
        k3 = model_pin.models()["kimi-k3"]
        assert k3["production"] is True
        assert k3["declared_trust_remote_code"] is None
        assert k3["declared_engine"] is None
        assert k3["risk_accepted"]["date"] == "2026-08-03"
        assert k3["risk_accepted"]["accepted_by"] == "PM"

    def test_declared_fields_are_not_used_for_decisions(self, pin, monkeypatch):
        """declared_* は判定に使わない（検証できないものを根拠にしない・P10）。"""
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        assert_model_allowed("glm-5.2")  # declared_revision が null でも通る

    def test_all_production_models_pass_enforce(self, monkeypatch):
        """本番経路の全モデルが enforce で通ること（2026-08-03 pin 実態合わせ）。

        リポジトリの実 config/model_pin.yaml を対象に、production: true の
        エントリすべてが enforce モードで例外を投げないことを確認する。
        ネットワークアクセスは行わない（assert_model_allowed はここに出ない）。
        """
        monkeypatch.setenv("ARGUS_MODEL_PIN", "enforce")
        for key, entry in model_pin.models().items():
            if not entry.get("production"):
                continue
            served = entry.get("served_model_name") or key
            assert_model_allowed(served)

    def test_deepseek_v4_flash_is_production_and_kimi_k2_is_retired(self):
        """PM 回答（2026-08-03）に基づく訂正: DeepSeek-V4-Flash が本番、
        Kimi-K2-Thinking は退役（production: false）。"""
        m = model_pin.models()
        assert m["DeepSeek-V4-Flash"]["production"] is True
        assert m["Kimi-K2-Thinking"]["production"] is False


# --------------------------------------------------------------------------- #
# check_endpoints（実ネットワークアクセスなし。fetch_served_models をモンキーパッチ）
# --------------------------------------------------------------------------- #
@pytest.fixture
def endpoints_pin(tmp_path, monkeypatch):
    p = tmp_path / "model_pin.yaml"
    p.write_text(
        """
models:
  glm-5.2:
    served_model_name: glm-5.2
    endpoint_env: [FAKE_RIKYU_URL]
    token_env: [FAKE_RIKYU_TOKEN]
    production: true
    verified_at: "2026-08-01"
    verified_max_input_tokens: 1000000
    verified_max_output_tokens: 1048576
  Kimi-K2-Thinking:
    served_model_name: Kimi-K2-Thinking
    endpoint_env: [FAKE_RIVAULT_URL]
    token_env: [FAKE_RIVAULT_TOKEN]
    production: true
    verified_at: "2026-08-01"
    verified_max_input_tokens: null
    verified_max_output_tokens: null
  no-endpoint:
    served_model_name: no-endpoint
    endpoint_env: [FAKE_UNSET_URL]
    production: true
    verified_at: "2026-08-01"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_MODEL_PIN_PATH", str(p))
    monkeypatch.setenv("FAKE_RIKYU_URL", "http://fake-rikyu.invalid/v1")
    monkeypatch.setenv("FAKE_RIVAULT_URL", "http://fake-rivault.invalid/v1")
    return p


class TestCheckEndpoints:
    def test_ok_when_id_and_max_tokens_match(self, endpoints_pin, monkeypatch):
        def fake_fetch(base_url, api_key=None, timeout=10):
            if "rikyu" in base_url:
                return [{"id": "glm-5.2", "max_input_tokens": 1000000, "max_output_tokens": 1048576}]
            return [{"id": "Kimi-K2-Thinking", "max_input_tokens": None, "max_output_tokens": None}]

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["glm-5.2"]["status"] == "ok"
        assert rows["Kimi-K2-Thinking"]["status"] == "ok"

    def test_max_input_tokens_mismatch_is_a_violation(self, endpoints_pin, monkeypatch):
        def fake_fetch(base_url, api_key=None, timeout=10):
            if "rikyu" in base_url:
                # max_input_tokens が宣言と異なる（context 長が変わっている）
                return [{"id": "glm-5.2", "max_input_tokens": 500000, "max_output_tokens": 1048576}]
            return [{"id": "Kimi-K2-Thinking", "max_input_tokens": None, "max_output_tokens": None}]

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["glm-5.2"]["status"] == "mismatch"
        assert "max_input_tokens" in rows["glm-5.2"]["detail"]

    def test_id_mismatch_is_a_violation(self, endpoints_pin, monkeypatch):
        def fake_fetch(base_url, api_key=None, timeout=10):
            return [{"id": "some-other-model", "max_input_tokens": 1, "max_output_tokens": 1}]

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["glm-5.2"]["status"] == "mismatch"

    def test_api_not_returning_max_tokens_skips_that_comparison(self, endpoints_pin, monkeypatch):
        """API が max_input_tokens/max_output_tokens を返さないエントリは、
        verified_* が null なので照合をスキップし ok を維持、かつその旨を detail に残す。"""
        def fake_fetch(base_url, api_key=None, timeout=10):
            if "rikyu" in base_url:
                return [{"id": "glm-5.2", "max_input_tokens": 1000000, "max_output_tokens": 1048576}]
            return [{"id": "Kimi-K2-Thinking", "max_input_tokens": None, "max_output_tokens": None}]

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["Kimi-K2-Thinking"]["status"] == "ok"
        assert "スキップ" in rows["Kimi-K2-Thinking"]["detail"]

    def test_unreachable_endpoint_is_error_not_raised(self, endpoints_pin, monkeypatch):
        def fake_fetch(base_url, api_key=None, timeout=10):
            raise TimeoutError("接続できません")

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["glm-5.2"]["status"] == "error"
        assert rows["Kimi-K2-Thinking"]["status"] == "error"

    def test_missing_endpoint_env_is_skip(self, endpoints_pin, monkeypatch):
        def fake_fetch(base_url, api_key=None, timeout=10):
            return [{"id": "glm-5.2", "max_input_tokens": 1000000, "max_output_tokens": 1048576}]

        monkeypatch.setattr(model_pin, "fetch_served_models", fake_fetch)
        rows = {r["model"]: r for r in model_pin.check_endpoints()}
        assert rows["no-endpoint"]["status"] == "skip"
