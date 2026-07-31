"""視覚モード（--slide-images、opt-in）のテスト。

call_vision_llm / call_argus_llm を monkeypatch し、実際の LLM 呼び出しは行わない。
既定（MINUTES_VISION_LLM_URL/_MODEL 未設定 or --slide-images 未指定）では
call_vision_llm が一切呼ばれないことを不変性として保証する。
"""
from pathlib import Path

import pytest
from recording import generate_minutes_local as gml

# --------------------------------------------------------------------------- #
# プロンプト種別を判別するマーカー（テンプレート本文の一部）
# --------------------------------------------------------------------------- #
_STAGE1_MARK = "Write a thorough Japanese prose summary"
_STAGE2_MARK = "You are writing the 議事内容"
_STAGE3_MARK = "You are extracting decisions and action items"

_TRANSCRIPT_CONTENT = """\
#### [00:00:00 - 00:00:30] SPEAKER_00
最初の発言です。今日はプロジェクトの進捗について話します。

#### [00:01:10 - 00:01:40] SPEAKER_01
2番目の発言です。前回からの変更点を共有します。

#### [00:02:20 - 00:02:50] SPEAKER_00
3番目の発言です。次のステップについて合意しました。
"""


@pytest.fixture(autouse=True)
def _isolate_vision_env(monkeypatch):
    """MINUTES_VISION_* をテスト間で確実に未設定状態からスタートさせる。"""
    for var in (
        "MINUTES_VISION_LLM_URL",
        "MINUTES_VISION_LLM_MODEL",
        "MINUTES_VISION_LLM_TOKEN",
        "MINUTES_VISION_LLM_TEMPERATURE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gml, "load_claude_md_context", lambda: "")


@pytest.fixture
def transcript_file(tmp_path: Path) -> Path:
    p = tmp_path / "transcript.md"
    p.write_text(_TRANSCRIPT_CONTENT, encoding="utf-8")
    return p


def _make_png_dir(tmp_path: Path, n: int, name: str = "frames") -> Path:
    d = tmp_path / name
    d.mkdir()
    for i in range(n):
        (d / f"slide_{i:04d}.png").write_bytes(b"\x89PNG\r\n")
    return d


def _fake_llm_response(prompt: str) -> str:
    """プロンプト種別に応じて最小限だが有効な形式の応答を返す。"""
    if _STAGE1_MARK in prompt:
        return "これはチャンク要約のテキストです。十分な長さを確保します。" * 3
    if _STAGE3_MARK in prompt:
        return "## 決定事項\n\n（なし）\n\n## アクションアイテム\n\n（なし）"
    return "## 議事内容\n\n### 概要\n\nこれはテスト用の議事内容本文です。十分な長さの文章にします。\n"


def _run_generate(tmp_path, transcript_file, **kwargs) -> str:
    return gml.generate_minutes(
        str(transcript_file), str(tmp_path / "out"), 30,
        multi_stage=True, chunk_minutes=1, enable_triage=False,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. 既定不変性: env 未設定・--slide-images 未指定では call_vision_llm 不使用
# --------------------------------------------------------------------------- #
def test_vision_disabled_by_default(monkeypatch, tmp_path, transcript_file):
    calls = {"argus": 0, "vision": 0}

    def fake_argus(prompt, **kwargs):
        calls["argus"] += 1
        return _fake_llm_response(prompt)

    def fake_vision(prompt, image_paths, **kwargs):
        calls["vision"] += 1
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)
    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)

    out = _run_generate(tmp_path, transcript_file)

    assert calls["vision"] == 0
    assert calls["argus"] > 0
    assert Path(out).exists()


# --------------------------------------------------------------------------- #
# 2. 有効時: Stage 2/3 のみ vision 経由、Stage 1（チャンク抽出）はテキストのまま
# --------------------------------------------------------------------------- #
def test_vision_enabled_stage2_stage3_only(monkeypatch, tmp_path, transcript_file):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    argus_prompts: list[str] = []
    vision_calls: list[tuple[str, list[str]]] = []

    def fake_argus(prompt, **kwargs):
        argus_prompts.append(prompt)
        return _fake_llm_response(prompt)

    def fake_vision(prompt, image_paths, **kwargs):
        vision_calls.append((prompt, image_paths))
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)
    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)

    images_dir = _make_png_dir(tmp_path, 5)
    out = _run_generate(tmp_path, transcript_file, slide_images_dir=str(images_dir))

    stage1_argus_calls = [p for p in argus_prompts if _STAGE1_MARK in p]
    assert len(stage1_argus_calls) == 3  # 3 チャンク分すべてテキスト経由

    assert len(vision_calls) == 2  # Stage2 (議事内容) + Stage3 (決定事項)
    assert sum(1 for p, _ in vision_calls if _STAGE2_MARK in p) == 1
    assert sum(1 for p, _ in vision_calls if _STAGE3_MARK in p) == 1
    for _, image_paths in vision_calls:
        assert len(image_paths) == 5

    # Stage2/3 はフォールバックが発火していない（vision で完結）
    assert not any(_STAGE2_MARK in p for p in argus_prompts)
    assert not any(_STAGE3_MARK in p for p in argus_prompts)
    assert Path(out).exists()


# --------------------------------------------------------------------------- #
# 3. vision 例外 → call_argus_llm に同一プロンプトで 1 回フォールバック
# --------------------------------------------------------------------------- #
def test_vision_exception_falls_back_to_argus(monkeypatch, tmp_path, transcript_file):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    vision_prompts: list[str] = []
    argus_prompts: list[str] = []

    def fake_vision(prompt, image_paths, **kwargs):
        vision_prompts.append(prompt)
        raise RuntimeError("vision boom")

    def fake_argus(prompt, **kwargs):
        argus_prompts.append(prompt)
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)
    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)

    images_dir = _make_png_dir(tmp_path, 3)
    out = _run_generate(tmp_path, transcript_file, slide_images_dir=str(images_dir))

    stage2_vision = [p for p in vision_prompts if _STAGE2_MARK in p]
    stage3_vision = [p for p in vision_prompts if _STAGE3_MARK in p]
    stage2_fallback = [p for p in argus_prompts if _STAGE2_MARK in p]
    stage3_fallback = [p for p in argus_prompts if _STAGE3_MARK in p]

    assert len(stage2_vision) == 1 and stage2_fallback == stage2_vision
    assert len(stage3_vision) == 1 and stage3_fallback == stage3_vision
    assert Path(out).exists()


# --------------------------------------------------------------------------- #
# 4. --slide-images-max の間引き（時系列均等）
# --------------------------------------------------------------------------- #
def test_load_slide_images_thins_evenly(tmp_path):
    d = _make_png_dir(tmp_path, 10)
    all_frames = sorted(d.glob("*.png"))

    result = gml._load_slide_images(str(d), 4)

    assert len(result) == 4
    step = 10 / 4
    expected_idx = {int(i * step) for i in range(4)}
    expected_names = sorted(all_frames[i].name for i in expected_idx)
    assert sorted(Path(p).name for p in result) == expected_names


def test_load_slide_images_no_thinning_when_under_max(tmp_path):
    d = _make_png_dir(tmp_path, 3)
    result = gml._load_slide_images(str(d), 40)
    assert len(result) == 3


def test_slide_image_labels_preserve_original_numbering_after_thinning(tmp_path):
    """間引き後も元の通し番号（/元の総枚数）を保持したラベルになる。"""
    d = _make_png_dir(tmp_path, 10)
    thinned = gml._load_slide_images(str(d), 4)
    labels = gml._slide_image_labels(str(d), thinned)

    all_frames = sorted(Path(d).glob("*.png"))
    expected = [f"Slide {all_frames.index(Path(p)) + 1}/10" for p in thinned]
    assert labels == expected


def test_slide_image_labels_no_thinning(tmp_path):
    d = _make_png_dir(tmp_path, 3)
    result = gml._load_slide_images(str(d), 40)
    labels = gml._slide_image_labels(str(d), result)
    assert labels == ["Slide 1/3", "Slide 2/3", "Slide 3/3"]


# --------------------------------------------------------------------------- #
# 5. URL のみ / MODEL のみ設定時は WARN + 無効化
# --------------------------------------------------------------------------- #
def test_resolve_vision_config_url_only_disables(monkeypatch, capsys):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    result = gml._resolve_vision_config()
    assert result is None
    assert "WARN" in capsys.readouterr().err


def test_resolve_vision_config_model_only_disables(monkeypatch, capsys):
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")
    result = gml._resolve_vision_config()
    assert result is None
    assert "WARN" in capsys.readouterr().err


def test_resolve_vision_config_both_set_enables(monkeypatch):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")
    result = gml._resolve_vision_config()
    assert result == {
        "url": "http://vision.example/v1",
        "model": "k3-vision",
        "token": "dummy",
        "temperature": 1.0,
    }


def test_resolve_vision_config_calls_load_llm_secrets(monkeypatch):
    """--from-combined 経路では Stage 1 が丸ごとスキップされ load_llm_secrets() を
    呼ぶ機会が他に無いため、_resolve_vision_config() 自身が呼ぶ契約を確認する。"""
    calls = {"n": 0}
    monkeypatch.setattr(gml, "load_llm_secrets", lambda: calls.__setitem__("n", calls["n"] + 1))
    gml._resolve_vision_config()
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 6. 画像のみ（OCR テキストなし）時の slide_context_block 定型文
# --------------------------------------------------------------------------- #
def test_slide_context_block_images_only(monkeypatch, tmp_path, transcript_file):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    captured_prompts: list[str] = []

    def fake_vision(prompt, image_paths, **kwargs):
        captured_prompts.append(prompt)
        return _fake_llm_response(prompt)

    def fake_argus(prompt, **kwargs):
        captured_prompts.append(prompt)
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)
    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)

    images_dir = _make_png_dir(tmp_path, 2)
    _run_generate(tmp_path, transcript_file, slide_images_dir=str(images_dir))

    stage2_prompts = [p for p in captured_prompts if _STAGE2_MARK in p]
    assert stage2_prompts
    assert "attached as images" in stage2_prompts[0]
    assert "ground truth" in stage2_prompts[0]


# --------------------------------------------------------------------------- #
# 7. Stage 1（チャンク抽出）には画像添付を示唆する定型文を注入しない
#    （Stage 1 は extract_from_chunk = call_argus_llm 直呼びで画像を送らない）
# --------------------------------------------------------------------------- #
def test_stage1_no_vision_injection_images_only(monkeypatch, tmp_path, transcript_file):
    """画像のみモード（OCRテキストなし）では Stage 1 に slide_context_block を無注入にする。"""
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    argus_prompts: list[str] = []

    def fake_argus(prompt, **kwargs):
        argus_prompts.append(prompt)
        return _fake_llm_response(prompt)

    def fake_vision(prompt, image_paths, **kwargs):
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)
    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)

    images_dir = _make_png_dir(tmp_path, 5)
    _run_generate(tmp_path, transcript_file, slide_images_dir=str(images_dir))

    stage1_prompts = [p for p in argus_prompts if _STAGE1_MARK in p]
    assert stage1_prompts
    for p in stage1_prompts:
        assert "attached as images" not in p
        assert "extracted via OCR" not in p


def test_stage1_ocr_block_without_vision_suffix(monkeypatch, tmp_path, transcript_file):
    """OCR併用時、Stage 1 は OCR ブロックのみを注入し、
    Stage 2/3 用の「画像もこの呼び出しに添付されている」suffix は付けない。"""
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    argus_prompts: list[str] = []

    def fake_argus(prompt, **kwargs):
        argus_prompts.append(prompt)
        return _fake_llm_response(prompt)

    def fake_vision(prompt, image_paths, **kwargs):
        return _fake_llm_response(prompt)

    monkeypatch.setattr(gml, "call_argus_llm", fake_argus)
    monkeypatch.setattr(gml, "call_vision_llm", fake_vision)

    images_dir = _make_png_dir(tmp_path, 5)
    _run_generate(
        tmp_path, transcript_file,
        slide_images_dir=str(images_dir),
        slide_context="OCRで抽出したスライド本文です。",
    )

    stage1_prompts = [p for p in argus_prompts if _STAGE1_MARK in p]
    assert stage1_prompts
    for p in stage1_prompts:
        assert "extracted via OCR" in p
        assert "ALSO attached as images" not in p


# --------------------------------------------------------------------------- #
# 8. 実効モデルのログ（S5）・consensus 併用時の再送 WARN（S6）
# --------------------------------------------------------------------------- #
def _fake_greedy_cluster_single_cluster(items, threshold, *, label):
    """全 index を 1 クラスタにまとめる決定的モック（consensus 経路を高速化）。"""
    return [list(range(len(items)))] if items else []


def test_vision_config_logged_when_enabled(monkeypatch, tmp_path, transcript_file, capsys):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")

    monkeypatch.setattr(gml, "call_vision_llm", lambda prompt, image_paths, **kw: _fake_llm_response(prompt))
    monkeypatch.setattr(gml, "call_argus_llm", lambda prompt, **kw: _fake_llm_response(prompt))

    images_dir = _make_png_dir(tmp_path, 3)
    _run_generate(tmp_path, transcript_file, slide_images_dir=str(images_dir))

    err = capsys.readouterr().err
    assert "[INFO] vision config: model=k3-vision" in err
    assert "http://vision.example" not in err


def test_vision_consensus_resend_warn(monkeypatch, tmp_path, transcript_file, capsys):
    monkeypatch.setenv("MINUTES_VISION_LLM_URL", "http://vision.example/v1")
    monkeypatch.setenv("MINUTES_VISION_LLM_MODEL", "k3-vision")
    monkeypatch.setattr(gml, "_greedy_cluster", _fake_greedy_cluster_single_cluster)

    monkeypatch.setattr(gml, "call_vision_llm", lambda prompt, image_paths, **kw: _fake_llm_response(prompt))
    monkeypatch.setattr(gml, "call_argus_llm", lambda prompt, **kw: _fake_llm_response(prompt))

    images_dir = _make_png_dir(tmp_path, 3)
    _run_generate(
        tmp_path, transcript_file, slide_images_dir=str(images_dir), consensus_n=3,
    )

    err = capsys.readouterr().err
    assert "vision 有効 + consensus N=3" in err


def test_no_vision_consensus_warn_when_vision_disabled(monkeypatch, tmp_path, transcript_file, capsys):
    """視覚モード無効時は consensus N>=2 でも vision 再送 WARN を出さない。"""
    monkeypatch.setattr(gml, "_greedy_cluster", _fake_greedy_cluster_single_cluster)
    monkeypatch.setattr(gml, "call_argus_llm", lambda prompt, **kw: _fake_llm_response(prompt))

    _run_generate(tmp_path, transcript_file, consensus_n=3)

    err = capsys.readouterr().err
    assert "vision 有効" not in err
