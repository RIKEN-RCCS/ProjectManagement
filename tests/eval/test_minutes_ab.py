"""scripts/eval/minutes_ab.py のテスト。

LLM 実接続・subprocess 実行・ffmpeg/OCR 実行なし。scripts/eval は pytest の
pythonpath 対象外のため、import 前に sys.path へ追加する（minutes_ab.py 自身が
行っているブートストラップと同じパス）。
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "eval"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import minutes_ab  # noqa: E402 — 上記パス追加後にインポート

# --------------------------------------------------------------------------- #
# stem 照合（解像度サフィックス・重複DLサフィックスの剥がし）
# --------------------------------------------------------------------------- #

class TestStemVariants:
    def test_plain_stem_has_single_variant(self):
        assert minutes_ab._stem_variants("2026-01-05_Leader_Meeting") == [
            "2026-01-05_Leader_Meeting",
        ]

    def test_resolution_suffix_stripped(self):
        variants = minutes_ab._stem_variants("GMT20260713-064325_Recording_1920x1150")
        assert "GMT20260713-064325_Recording_1920x1150" in variants
        assert "GMT20260713-064325_Recording" in variants

    def test_duplicate_download_suffix_stripped(self):
        variants = minutes_ab._stem_variants("Recording (1)")
        assert "Recording (1)" in variants
        assert "Recording" in variants

    def test_both_resolution_and_duplicate_suffix_stripped(self):
        variants = minutes_ab._stem_variants("Recording_1920x1150 (1)")
        assert "Recording_1920x1150 (1)" in variants
        assert "Recording" in variants

    def test_no_duplicate_variants(self):
        variants = minutes_ab._stem_variants("plain")
        assert len(variants) == len(set(variants))


class TestCombinedBasename:
    def test_matches_timestamped_combined_filename(self):
        path = Path("2026-07-13-214037-GMT20260713-064325_Recording_1920x1150-combined.txt")
        assert minutes_ab._combined_basename(path) == "GMT20260713-064325_Recording_1920x1150"

    def test_non_combined_filename_returns_none(self):
        assert minutes_ab._combined_basename(Path("2026-01-05_Leader_Meeting.mp4")) is None


class TestFindCombinedForStem:
    def test_exact_match_found(self, tmp_path):
        (tmp_path / "2026-07-02-093142-2026-01-05_Leader_Meeting-combined.txt").write_text("x")
        result = minutes_ab.find_combined_for_stem("2026-01-05_Leader_Meeting", tmp_path)
        assert result is not None
        assert result.name == "2026-07-02-093142-2026-01-05_Leader_Meeting-combined.txt"

    def test_resolution_suffix_mismatch_still_matches(self, tmp_path):
        # combined.txt 側の basename に解像度サフィックスが含まれ、mp4 側の stem と
        # バリアント（サフィックス剥がし後）で一致するケース
        (tmp_path / "2026-07-13-214037-GMT20260713-064325_Recording_1920x1150-combined.txt").write_text("x")
        result = minutes_ab.find_combined_for_stem(
            "GMT20260713-064325_Recording_1920x1150", tmp_path,
        )
        assert result is not None

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "2026-07-02-093142-other_meeting-combined.txt").write_text("x")
        assert minutes_ab.find_combined_for_stem("2026-01-05_Leader_Meeting", tmp_path) is None

    def test_multiple_candidates_picks_latest_by_filename(self, tmp_path):
        older = tmp_path / "2026-07-13-214037-GMT20260713-064325_Recording_1920x1150-combined.txt"
        newer = tmp_path / "2026-07-13-224100-GMT20260713-064325_Recording_1920x1150-combined.txt"
        older.write_text("old")
        newer.write_text("new")
        result = minutes_ab.find_combined_for_stem(
            "GMT20260713-064325_Recording_1920x1150", tmp_path,
        )
        assert result == newer


class TestFindVttForStem:
    def test_direct_vtt_match(self, tmp_path):
        (tmp_path / "2026-01-05_Leader_Meeting.vtt").write_text("x")
        result = minutes_ab._find_vtt_for_stem("2026-01-05_Leader_Meeting", tmp_path)
        assert result is not None
        assert result.name == "2026-01-05_Leader_Meeting.vtt"

    def test_resolution_suffix_variant_matches_transcript_vtt(self, tmp_path):
        # mp4 側の stem に解像度サフィックスが付いていても、剥がした
        # バリアント（"Recording"）で "Recording.transcript.vtt" にマッチする
        (tmp_path / "Recording.transcript.vtt").write_text("x")
        result = minutes_ab._find_vtt_for_stem("Recording_1920x1150", tmp_path)
        assert result is not None
        assert result.name == "Recording.transcript.vtt"

    def test_no_vtt_returns_none(self, tmp_path):
        assert minutes_ab._find_vtt_for_stem("nope", tmp_path) is None


# --------------------------------------------------------------------------- #
# アーム env 構築（${VAR} 展開・打ち消し・ホワイトリスト記録にトークンが含まれない）
# --------------------------------------------------------------------------- #

class TestBuildArmEnv:
    def test_arm_a_has_no_overrides(self):
        assert minutes_ab.build_arm_env("A") == {}

    def test_arm_b_expands_rikyu_refs(self, monkeypatch):
        monkeypatch.setenv("RIKYU_URL", "http://rikyu.example")
        monkeypatch.setenv("RIKYU_TOKEN", "rikyu-secret")
        monkeypatch.setenv("EMBED_API_BASE", "http://embed.example")
        monkeypatch.setenv("EMBED_API_KEY", "embed-secret")
        monkeypatch.setenv("EMBED_MODEL", "bge-m3:567m")
        env = minutes_ab.build_arm_env("B")
        assert env["LOCAL_LLM_URL"] == "http://rikyu.example"
        assert env["LOCAL_LLM_TOKEN"] == "rikyu-secret"
        assert env["LOCAL_LLM_MODEL"] == "kimi-k3"
        assert env["RIVAULT_URL"] == ""
        assert env["RIVAULT_TOKEN"] == ""

    def test_arm_c_and_d_expand_vision_refs(self, monkeypatch):
        monkeypatch.setenv("RIKYU_URL", "http://rikyu.example")
        monkeypatch.setenv("RIKYU_TOKEN", "rikyu-secret")
        for name in ("C", "D"):
            env = minutes_ab.build_arm_env(name)
            assert env["MINUTES_VISION_LLM_URL"] == "http://rikyu.example"
            assert env["MINUTES_VISION_LLM_TOKEN"] == "rikyu-secret"
            assert env["MINUTES_VISION_LLM_MODEL"] == "kimi-k3"

    def test_unset_ref_raises(self, monkeypatch):
        monkeypatch.delenv("RIKYU_URL", raising=False)
        with pytest.raises(ValueError, match="RIKYU_URL"):
            minutes_ab.build_arm_env("B")

    def test_arm_definitions_flags(self):
        assert minutes_ab.ARM_PRESETS["A"]["slide_context"] is True
        assert minutes_ab.ARM_PRESETS["A"]["slide_images"] is False
        assert minutes_ab.ARM_PRESETS["B"]["slide_context"] is True
        assert minutes_ab.ARM_PRESETS["B"]["slide_images"] is False
        assert minutes_ab.ARM_PRESETS["C"]["slide_context"] is False
        assert minutes_ab.ARM_PRESETS["C"]["slide_images"] is True
        assert minutes_ab.ARM_PRESETS["D"]["slide_context"] is True
        assert minutes_ab.ARM_PRESETS["D"]["slide_images"] is True


class TestBuildSubprocessEnv:
    def test_controlled_keys_stripped_from_parent(self, monkeypatch):
        for key in minutes_ab._ARM_CONTROLLED_ENV_KEYS:
            monkeypatch.setenv(key, "polluted")
        env = minutes_ab._build_subprocess_env({})
        for key in minutes_ab._ARM_CONTROLLED_ENV_KEYS:
            assert key not in env

    def test_arm_env_overlay_applied_after_stripping(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_MODEL", "polluted")
        env = minutes_ab._build_subprocess_env({"LOCAL_LLM_MODEL": "kimi-k3"})
        assert env["LOCAL_LLM_MODEL"] == "kimi-k3"

    def test_unrelated_env_vars_preserved(self, monkeypatch):
        monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
        env = minutes_ab._build_subprocess_env({})
        assert env["SOME_UNRELATED_VAR"] == "keep-me"


class TestWhitelistedArmConfig:
    def _set_b_arm_refs(self, monkeypatch):
        monkeypatch.setenv("RIKYU_URL", "http://rikyu.example")
        monkeypatch.setenv("RIKYU_TOKEN", "super-secret-token")
        monkeypatch.setenv("EMBED_API_BASE", "http://embed.example")
        monkeypatch.setenv("EMBED_API_KEY", "embed-secret-key")
        monkeypatch.setenv("EMBED_MODEL", "bge-m3:567m")

    def test_only_whitelisted_keys_kept(self, monkeypatch):
        self._set_b_arm_refs(monkeypatch)
        env = minutes_ab.build_arm_env("B")
        config = minutes_ab._whitelisted_arm_config(env)
        assert config == {"LOCAL_LLM_MODEL": "kimi-k3", "ARGUS_LLM_TEMPERATURE": "1.0"}

    def test_no_token_values_leak(self, monkeypatch):
        self._set_b_arm_refs(monkeypatch)
        env = minutes_ab.build_arm_env("B")
        config = minutes_ab._whitelisted_arm_config(env)
        assert "super-secret-token" not in str(config)
        assert "http://rikyu.example" not in str(config)
        assert "embed-secret-key" not in str(config)

    def test_arm_a_config_is_empty(self):
        assert minutes_ab._whitelisted_arm_config(minutes_ab.build_arm_env("A")) == {}


# --------------------------------------------------------------------------- #
# build_run_cmd
# --------------------------------------------------------------------------- #

class TestBuildRunCmd:
    def test_arm_a_has_slide_context_not_images(self, tmp_path):
        cmd = minutes_ab.build_run_cmd(
            stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
            arm_name="A", combined_path=tmp_path / "c.txt", vtt_path=tmp_path / "v.vtt",
        )
        assert "--slide-context" in cmd
        assert "--slide-images" not in cmd
        assert "--from-combined" in cmd

    def test_arm_c_has_slide_images_not_context(self, tmp_path):
        cmd = minutes_ab.build_run_cmd(
            stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
            arm_name="C", combined_path=tmp_path / "c.txt", vtt_path=tmp_path / "v.vtt",
        )
        assert "--slide-images" in cmd
        assert "--slide-context" not in cmd

    def test_arm_d_has_both(self, tmp_path):
        cmd = minutes_ab.build_run_cmd(
            stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
            arm_name="D", combined_path=tmp_path / "c.txt", vtt_path=tmp_path / "v.vtt",
        )
        assert "--slide-images" in cmd
        assert "--slide-context" in cmd

    def test_full_mode_uses_multi_stage_not_from_combined(self, tmp_path):
        raw = tmp_path / "raw.md"
        cmd = minutes_ab.build_run_cmd(
            stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
            arm_name="A", vtt_path=tmp_path / "v.vtt",
            full=True, raw_transcript_path=raw,
        )
        assert "--multi-stage" in cmd
        assert "--from-combined" not in cmd
        assert str(raw) in cmd

    def test_full_mode_without_raw_transcript_raises(self, tmp_path):
        with pytest.raises(ValueError, match="raw_transcript_path"):
            minutes_ab.build_run_cmd(
                stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
                arm_name="A", vtt_path=tmp_path / "v.vtt", full=True,
            )

    def test_non_full_mode_without_combined_raises(self, tmp_path):
        with pytest.raises(ValueError, match="combined_path"):
            minutes_ab.build_run_cmd(
                stem="m1", workspace=tmp_path, output_dir=tmp_path / "out",
                arm_name="A", vtt_path=tmp_path / "v.vtt", full=False,
            )


# --------------------------------------------------------------------------- #
# vision usage 行のパース
# --------------------------------------------------------------------------- #

class TestParseVisionUsage:
    def test_single_line_parsed(self):
        stderr = (
            "[INFO] vision usage: prompt=1200 image=900 completion=300 "
            "cached=100 latency_ms=4500 images=12"
        )
        usage = minutes_ab.parse_vision_usage(stderr)
        assert usage == {
            "prompt_tokens": 1200, "image_tokens": 900, "completion_tokens": 300,
            "cached_tokens": 100, "latency_ms": 4500, "images": 12, "calls": 1,
        }

    def test_multiple_lines_summed(self):
        stderr = "\n".join([
            "[INFO] vision usage: prompt=1000 image=800 completion=200 "
            "cached=0 latency_ms=3000 images=10",
            "[INFO] vision usage: prompt=500 image=400 completion=100 "
            "cached=50 latency_ms=1500 images=10",
        ])
        usage = minutes_ab.parse_vision_usage(stderr)
        assert usage["prompt_tokens"] == 1500
        assert usage["image_tokens"] == 1200
        assert usage["completion_tokens"] == 300
        assert usage["cached_tokens"] == 50
        assert usage["latency_ms"] == 4500
        assert usage["images"] == 20
        assert usage["calls"] == 2

    def test_absent_returns_none(self):
        assert minutes_ab.parse_vision_usage("no relevant lines here") is None

    def test_empty_stderr_returns_none(self):
        assert minutes_ab.parse_vision_usage("") is None

    def test_none_values_tolerated_and_treated_as_zero(self):
        """utils/llm.py は usage を常に整数でログ出力する契約だが、万一 None が
        混入しても行ごと欠測せず 0 扱いで集計できることを多層防御として確認する。"""
        stderr = (
            "[INFO] vision usage: prompt=None image=None completion=50 "
            "cached=None latency_ms=1200 images=8"
        )
        usage = minutes_ab.parse_vision_usage(stderr)
        assert usage == {
            "prompt_tokens": 0, "image_tokens": 0, "completion_tokens": 50,
            "cached_tokens": 0, "latency_ms": 1200, "images": 8, "calls": 1,
        }


# --------------------------------------------------------------------------- #
# 実効モデルのパース（S5）
# --------------------------------------------------------------------------- #

class TestParseEffectiveModel:
    def test_vision_model_only(self):
        stderr = "[INFO] vision config: model=kimi-k3\n[INFO] some other line"
        result = minutes_ab.parse_effective_model(stderr)
        assert result == {"vision_model": "kimi-k3"}

    def test_route_order_only(self):
        stderr = "[INFO] call_argus_llm: route_order=rivault>local think=False fallback=True"
        result = minutes_ab.parse_effective_model(stderr)
        assert result == {"route_order": ["rivault>local"]}

    def test_both_present(self):
        stderr = (
            "[INFO] vision config: model=kimi-k3\n"
            "[INFO] call_argus_llm: route_order=local think=False fallback=True\n"
        )
        result = minutes_ab.parse_effective_model(stderr)
        assert result == {"vision_model": "kimi-k3", "route_order": ["local"]}

    def test_neither_present_returns_none(self):
        assert minutes_ab.parse_effective_model("no relevant lines") is None

    def test_empty_stderr_returns_none(self):
        assert minutes_ab.parse_effective_model("") is None

    def test_duplicate_route_orders_deduplicated(self):
        stderr = "\n".join([
            "[INFO] call_argus_llm: route_order=local think=False fallback=True",
            "[INFO] call_argus_llm: route_order=local think=True fallback=True",
        ])
        result = minutes_ab.parse_effective_model(stderr)
        assert result == {"route_order": ["local"]}


# --------------------------------------------------------------------------- #
# err_tail ノイズ除去（nit）
# --------------------------------------------------------------------------- #

class TestFilteredStderrTail:
    def test_noisy_lines_excluded(self):
        stderr = "\n".join([
            "[INFO] LLM call: backend=local model=gemma4 url=http://x think=False",
            "[INFO] vision config: model=kimi-k3",
            "[ERROR] vLLM 500: internal error",
        ])
        result = minutes_ab._filtered_stderr_tail(stderr)
        assert "LLM call:" not in result
        assert "vision config:" not in result
        assert "internal error" in result

    def test_tail_limited_to_n_lines(self):
        lines = [f"line {i}" for i in range(30)]
        result = minutes_ab._filtered_stderr_tail("\n".join(lines), n=5)
        assert result.splitlines() == lines[-5:]

    def test_empty_stderr_returns_empty_string(self):
        assert minutes_ab._filtered_stderr_tail("") == ""


# --------------------------------------------------------------------------- #
# 自動メトリクス（フィクスチャ議事録での件数・埋まり率・形式適合）
# --------------------------------------------------------------------------- #

_WELL_FORMED_MINUTES = """\
## 決定事項

- ベンチマークWGの次回開催日を8月1日とする
- 予算配分をA案で確定する

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| 田中 | ベンチマーク結果をまとめる | 2026-08-01 |
| 佐藤 | 予算資料を作成する | （未定） |

## 議事内容

### 進捗確認

FrontFlow の性能評価が完了した。MONAKA-X との連携を検討する。
"""


class TestComputeAutoMetrics:
    def test_counts_decisions_and_actions(self):
        metrics = minutes_ab.compute_auto_metrics(_WELL_FORMED_MINUTES)
        assert metrics["n_decisions"] == 2
        assert metrics["n_actions"] == 2

    def test_assignee_and_due_fill_rate(self):
        metrics = minutes_ab.compute_auto_metrics(_WELL_FORMED_MINUTES)
        assert metrics["assignee_fill_rate"] == pytest.approx(1.0)
        assert metrics["due_fill_rate"] == pytest.approx(0.5)

    def test_well_formed_minutes_pass_format_check(self):
        metrics = minutes_ab.compute_auto_metrics(_WELL_FORMED_MINUTES)
        assert metrics["format_ok"] is True
        assert metrics["table_parseable"] is True
        assert metrics["speaker_leak"] is False

    def test_char_count(self):
        metrics = minutes_ab.compute_auto_metrics(_WELL_FORMED_MINUTES)
        assert metrics["char_count"] == len(_WELL_FORMED_MINUTES)

    def test_terminology_hit_rate(self):
        metrics = minutes_ab.compute_auto_metrics(
            _WELL_FORMED_MINUTES, terminology=["FrontFlow", "MONAKA-X", "存在しない用語XYZ"],
        )
        assert metrics["terminology_hits"] == 2
        assert metrics["terminology_total"] == 3
        assert metrics["terminology_hit_rate"] == pytest.approx(2 / 3)

    def test_no_terminology_gives_none_rate(self):
        metrics = minutes_ab.compute_auto_metrics(_WELL_FORMED_MINUTES)
        assert metrics["terminology_hit_rate"] is None

    def test_missing_section_fails_format_check(self):
        text = "## 決定事項\n\n- 何かを決めた\n\n## アクションアイテム\n\n（なし）"
        metrics = minutes_ab.compute_auto_metrics(text)
        assert metrics["sections_present"]["議事内容"] is False
        assert metrics["format_ok"] is False

    def test_speaker_leak_detected(self):
        text = _WELL_FORMED_MINUTES + "\nSPEAKER_00 が発言した。"
        metrics = minutes_ab.compute_auto_metrics(text)
        assert metrics["speaker_leak"] is True
        assert metrics["format_ok"] is False

    def test_no_actions_gives_none_fill_rates(self):
        text = "## 決定事項\n\n（なし）\n\n## アクションアイテム\n\n（なし）\n\n## 議事内容\n\n本文"
        metrics = minutes_ab.compute_auto_metrics(text)
        assert metrics["n_actions"] == 0
        assert metrics["assignee_fill_rate"] is None
        assert metrics["due_fill_rate"] is None
        assert metrics["table_parseable"] is True

    def test_freeform_text_in_action_section_breaks_table_parseable(self):
        text = (
            "## 決定事項\n\n（なし）\n\n"
            "## アクションアイテム\n\n田中さんが対応する予定です。\n\n"
            "## 議事内容\n\n本文"
        )
        metrics = minutes_ab.compute_auto_metrics(text)
        assert metrics["table_parseable"] is False
        assert metrics["format_ok"] is False

    def test_empty_text_gives_zero_counts(self):
        metrics = minutes_ab.compute_auto_metrics("")
        assert metrics["n_decisions"] == 0
        assert metrics["n_actions"] == 0
        assert metrics["format_ok"] is False


# --------------------------------------------------------------------------- #
# auto-tie 判定
# --------------------------------------------------------------------------- #

class TestIsAutoTie:
    def test_identical_output_is_tie(self):
        is_tie, reason = minutes_ab.is_auto_tie("同じ本文です", "同じ本文です")
        assert is_tie is True
        assert reason == "identical_output"

    def test_both_empty_is_tie(self):
        is_tie, reason = minutes_ab.is_auto_tie("（なし）", "")
        assert is_tie is True
        assert reason == "both_empty"

    def test_different_non_empty_is_not_tie(self):
        is_tie, reason = minutes_ab.is_auto_tie("本文A", "本文B")
        assert is_tie is False
        assert reason == ""

    def test_one_empty_one_non_empty_is_not_tie(self):
        is_tie, _reason = minutes_ab.is_auto_tie("", "本文B")
        assert is_tie is False


# --------------------------------------------------------------------------- #
# _select_by_length_distribution
# --------------------------------------------------------------------------- #

class TestSelectByLengthDistribution:
    def _meetings(self, sizes):
        return [{"stem": f"m{i}", "combined_chars": s} for i, s in enumerate(sizes)]

    def test_returns_all_when_n_exceeds_population(self):
        meetings = self._meetings([100, 200, 300])
        selected = minutes_ab._select_by_length_distribution(meetings, 10)
        assert len(selected) == 3

    def test_selects_requested_count(self):
        meetings = self._meetings([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        selected = minutes_ab._select_by_length_distribution(meetings, 5)
        assert len(selected) == 5

    def test_single_selection_picks_median(self):
        meetings = self._meetings([10, 20, 30])
        selected = minutes_ab._select_by_length_distribution(meetings, 1)
        assert selected == [meetings[1]]

    def test_spans_short_and_long(self):
        meetings = self._meetings([10, 20, 30, 40, 50])
        selected = minutes_ab._select_by_length_distribution(meetings, 3)
        sizes = sorted(m["combined_chars"] for m in selected)
        assert sizes[0] == 10
        assert sizes[-1] == 50


# --------------------------------------------------------------------------- #
# cmd_judge — RIVAULT プリフライトチェック（S8）
# --------------------------------------------------------------------------- #

class TestCmdJudgeRivaultPreflight:
    def test_missing_rivault_url_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RIVAULT_URL", raising=False)
        monkeypatch.setenv("RIVAULT_TOKEN", "secret")
        args = argparse.Namespace(
            workspace=str(tmp_path), jsonl=str(tmp_path / "r.jsonl"),
            judges_jsonl=str(tmp_path / "j.jsonl"),
            pairs="B:A", judge_model="DeepSeek-V4-Flash",
            judge_max_tokens=4096, judge_timeout=300, seed=7,
        )
        rc = minutes_ab.cmd_judge(args)
        assert rc == 2

    def test_missing_rivault_token_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example")
        monkeypatch.delenv("RIVAULT_TOKEN", raising=False)
        args = argparse.Namespace(
            workspace=str(tmp_path), jsonl=str(tmp_path / "r.jsonl"),
            judges_jsonl=str(tmp_path / "j.jsonl"),
            pairs="B:A", judge_model="DeepSeek-V4-Flash",
            judge_max_tokens=4096, judge_timeout=300, seed=7,
        )
        rc = minutes_ab.cmd_judge(args)
        assert rc == 2

    def test_both_set_passes_preflight_and_reaches_manifest_check(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("RIVAULT_URL", "http://rivault.example")
        monkeypatch.setenv("RIVAULT_TOKEN", "secret")
        args = argparse.Namespace(
            workspace=str(tmp_path), jsonl=str(tmp_path / "r.jsonl"),
            judges_jsonl=str(tmp_path / "j.jsonl"),
            pairs="B:A", judge_model="DeepSeek-V4-Flash",
            judge_max_tokens=4096, judge_timeout=300, seed=7,
        )
        rc = minutes_ab.cmd_judge(args)
        # プリフライトは通過し、manifest/results 未生成のエラーに到達する
        assert rc == 2
        assert "manifest/results" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# cmd_prep — 空OCR会議の除外（S4）
# --------------------------------------------------------------------------- #

class TestCmdPrepEmptyOcrExclusion:
    def _make_meeting(self, processing_dir: Path, stem: str) -> None:
        (processing_dir / f"{stem}.mp4").write_bytes(b"x")
        (processing_dir / f"{stem}.vtt").write_text("x")
        (processing_dir / f"2026-07-02-093142-{stem}-combined.txt").write_text(
            "combined body " * 20,
        )

    def _fake_extract_slide_frames(self, mp4_path, out_dir, scene_threshold, max_frames):
        out_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(minutes_ab._MIN_FRAMES):
            p = out_dir / f"slide_{i:04d}.png"
            p.write_bytes(b"x")
            frames.append(p)
        return frames

    def _fake_ocr_slides(self, frame_paths):
        if not frame_paths:
            return []
        stem = frame_paths[0].parent.name
        if stem == "empty_ocr_meeting":
            return ["" for _ in frame_paths]
        return ["dummy slide text" for _ in frame_paths]

    def _run_prep(self, tmp_path, monkeypatch):
        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        workspace = tmp_path / "workspace"
        self._make_meeting(processing_dir, "good_meeting")
        self._make_meeting(processing_dir, "empty_ocr_meeting")

        monkeypatch.setattr(minutes_ab, "load_llm_secrets", lambda: None)
        monkeypatch.setattr(minutes_ab, "extract_slide_frames", self._fake_extract_slide_frames)
        monkeypatch.setattr(minutes_ab, "ocr_slides", self._fake_ocr_slides)
        monkeypatch.setattr(
            minutes_ab, "extract_terminology",
            lambda slide_mds, use_llm_filter=True: [],
        )
        monkeypatch.setattr(minutes_ab, "_probe_image_tokens", lambda frames_dir, selected: None)

        args = argparse.Namespace(
            processing_dir=str(processing_dir), workspace=str(workspace),
            n=2, meetings=None,
        )
        rc = minutes_ab.cmd_prep(args)
        return rc, workspace

    def test_empty_ocr_meeting_excluded_from_manifest(self, tmp_path, monkeypatch, capsys):
        rc, workspace = self._run_prep(tmp_path, monkeypatch)
        assert rc == 0
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        stems = {m["stem"] for m in manifest["selected"]}
        assert stems == {"good_meeting"}
        assert "empty_ocr_meeting" not in stems
        assert "OCR結果が空のため除外" in capsys.readouterr().err

    def test_load_llm_secrets_called(self, tmp_path, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            minutes_ab, "load_llm_secrets",
            lambda: calls.__setitem__("n", calls["n"] + 1),
        )
        monkeypatch.setattr(minutes_ab, "extract_slide_frames", self._fake_extract_slide_frames)
        monkeypatch.setattr(minutes_ab, "ocr_slides", self._fake_ocr_slides)
        monkeypatch.setattr(
            minutes_ab, "extract_terminology",
            lambda slide_mds, use_llm_filter=True: [],
        )
        monkeypatch.setattr(minutes_ab, "_probe_image_tokens", lambda frames_dir, selected: None)

        processing_dir = tmp_path / "processing"
        processing_dir.mkdir()
        workspace = tmp_path / "workspace"
        self._make_meeting(processing_dir, "good_meeting")
        args = argparse.Namespace(
            processing_dir=str(processing_dir), workspace=str(workspace),
            n=1, meetings=None,
        )
        minutes_ab.cmd_prep(args)
        assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# _discover_meetings（stem照合の結合テスト）
# --------------------------------------------------------------------------- #

class TestDiscoverMeetings:
    def test_meeting_requires_vtt_and_combined(self, tmp_path):
        (tmp_path / "2026-01-05_Leader_Meeting.mp4").write_bytes(b"x")
        (tmp_path / "2026-01-05_Leader_Meeting.vtt").write_text("x")
        (tmp_path / "2026-07-02-093142-2026-01-05_Leader_Meeting-combined.txt").write_text("combined body")
        meetings = minutes_ab._discover_meetings(tmp_path)
        assert len(meetings) == 1
        assert meetings[0]["stem"] == "2026-01-05_Leader_Meeting"
        assert meetings[0]["combined_chars"] == len("combined body")

    def test_meeting_without_combined_excluded(self, tmp_path):
        (tmp_path / "2026-01-05_Leader_Meeting.mp4").write_bytes(b"x")
        (tmp_path / "2026-01-05_Leader_Meeting.vtt").write_text("x")
        meetings = minutes_ab._discover_meetings(tmp_path)
        assert meetings == []

    def test_meeting_without_vtt_excluded(self, tmp_path):
        (tmp_path / "2026-01-05_Leader_Meeting.mp4").write_bytes(b"x")
        (tmp_path / "2026-07-02-093142-2026-01-05_Leader_Meeting-combined.txt").write_text("x")
        meetings = minutes_ab._discover_meetings(tmp_path)
        assert meetings == []
