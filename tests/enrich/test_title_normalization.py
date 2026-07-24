"""_strip_app_name_prefix の純関数テスト（LLM/DB 不使用）。

実測で achievements title の一部がアプリ名で始まる冗長表記（例:「XxxAppの〜」）に
なっていたため、抽出時のサニタイズで先頭のアプリ名重複を除去する。
実在アプリ名は使わず架空名（TestApp 等）で検証する。
"""
from enrich.achievements_extract import _sanitize_achievement, _strip_app_name_prefix

# --------------------------------------------------------------------------- #
# アプリ名先頭一致で除去
# --------------------------------------------------------------------------- #

def test_strips_app_name_with_no_separator():
    assert _strip_app_name_prefix("TestApp性能評価", "TestApp") == "性能評価"


def test_strips_app_name_with_no_prefix_returns_unchanged():
    assert _strip_app_name_prefix("関係ないタイトルです", "TestApp") == "関係ないタイトルです"


# --------------------------------------------------------------------------- #
# 大文字小文字無視
# --------------------------------------------------------------------------- #

def test_strip_is_case_insensitive():
    assert _strip_app_name_prefix("testapp の性能評価", "TestApp") == "性能評価"
    assert _strip_app_name_prefix("TESTAPP:高速化対応", "testapp") == "高速化対応"


# --------------------------------------------------------------------------- #
# 区切り文字パターン各種（の, :, ：, /, ・, 空白, -, を）
# --------------------------------------------------------------------------- #

def test_separator_variants():
    cases = [
        ("TestAppの性能評価", "性能評価"),
        ("TestApp:性能評価", "性能評価"),
        ("TestApp：性能評価", "性能評価"),
        ("TestApp/性能評価", "性能評価"),
        ("TestApp・性能評価", "性能評価"),
        ("TestApp 性能評価", "性能評価"),
        ("TestApp-性能評価", "性能評価"),
        ("TestAppを検証した", "検証した"),
    ]
    for title, expected in cases:
        assert _strip_app_name_prefix(title, "TestApp") == expected, title


def test_separator_up_to_two_chars_consumed():
    # ":" + " " の2文字区切りがまとめて除去される
    assert _strip_app_name_prefix("TestApp: 性能評価", "TestApp") == "性能評価"


# --------------------------------------------------------------------------- #
# 除去後の残りが4文字未満なら元の title を維持
# --------------------------------------------------------------------------- #

def test_keeps_original_when_remainder_too_short():
    # "TestApp" 単体（区切りなし、残り0文字）
    assert _strip_app_name_prefix("TestApp", "TestApp") == "TestApp"
    # 除去後が3文字以下
    assert _strip_app_name_prefix("TestAppの完了", "TestApp") == "TestAppの完了"


# --------------------------------------------------------------------------- #
# app 名なし呼び出しはスキップ
# --------------------------------------------------------------------------- #

def test_skips_normalization_without_app_name():
    assert _strip_app_name_prefix("TestAppの性能評価", "") == "TestAppの性能評価"
    assert _strip_app_name_prefix("TestAppの性能評価", None) == "TestAppの性能評価"


# --------------------------------------------------------------------------- #
# 40字制限の再適用（_sanitize_achievement 経由）
# --------------------------------------------------------------------------- #

def test_sanitize_achievement_reapplies_40_char_limit_after_strip():
    long_body = "性能評価" * 15  # 60字
    raw = {"title": f"TestAppの{long_body}", "confidence": "high"}
    result = _sanitize_achievement("TestApp", raw)
    assert result is not None
    assert len(result["title"]) == 40
    assert result["title"].endswith("…")
    assert not result["title"].startswith("TestApp")


def test_sanitize_achievement_without_app_name_prefix_untouched():
    raw = {"title": "架空プロジェクトの完了実績", "confidence": "low"}
    result = _sanitize_achievement("TestApp", raw)
    assert result is not None
    assert result["title"] == "架空プロジェクトの完了実績"
