"""infer_date_from_filename() の日付優先順位の回帰テスト。

背景: generate_minutes_local.py の出力ファイル名は
"<生成時刻>-<basename>-minutes.md" の形式で、<basename> 自体が別の日付を
含むことがある（実例: meeting_id
"2026-06-10-132343-2026-09-10_ApplicationDiscussion-minutes" で held_at が
末尾の 2026-09-10（誤り）に設定された。実開催日は先頭の 2026-06-10 で
parsed_at と一致）。この関数自体は当該ファイル名に対して元々正しく先頭の
2026-06-10 を返していた（誤設定の混入源は pm_from_recording.sh 側の
--held-at 導出ロジックで、別途修正済み）。

GMT 形式（Zoom 録画タイムスタンプ由来、最も信頼できる）が1件でも見つかれば、
出現位置に関わらず**絶対優先**で採用する。「複数候補（GMT・汎用形式問わず）を
まとめて最先頭を選ぶ」実装は、GMT がファイル名の先頭に無い実ファイル
（"<生成時刻>-GMT<...>_Recording..." 形式、data/minutes/*.db に32件実在）で
収録日でなく処理日を誤って返す回帰を招くため採用しない。GMT が無い場合のみ、
汎用形式（ハイフン/アンダースコア区切り）のうち最も先頭のものを採用する。
"""
from datetime import datetime
from pathlib import Path

from minutes.pm_minutes_import import infer_date_from_filename


def test_double_date_prefers_leading_date():
    """実例: 先頭の生成時刻日付を優先し、末尾の（誤った）録画ファイル名日付は無視する。"""
    path = Path("2026-06-10-132343-2026-09-10_ApplicationDiscussion-minutes.md")
    assert infer_date_from_filename(path) == "2026-06-10"


def test_double_date_same_value_unaffected():
    """先頭・末尾が同一日付の場合も先頭優先ロジックで従来通り正しく解決する。"""
    path = Path("2026-05-20-173108-2026-05-20_ApplicationDiscussion-minutes.md")
    assert infer_date_from_filename(path) == "2026-05-20"


def test_single_date_underscore_separator_unchanged():
    """単一日付ケース（既存の挙動）が変わらないことを確認する。"""
    path = Path("2026-07-01_Leader_Meeting.md")
    assert infer_date_from_filename(path) == "2026-07-01"


def test_gmt_style_filename_unchanged():
    """Zoom の GMT タイムスタンプ形式（単一日付）が変わらないことを確認する。"""
    path = Path("GMT20260302-032528_Recording.md")
    assert infer_date_from_filename(path) == "2026-03-02"


def test_gmt_wins_even_when_not_leftmost():
    """GMT形式が末尾の別日付より優先される（先頭にある場合、先頭優先の一般化）。"""
    path = Path("GMT20260701-015600_Recording_2026-09-10-minutes.md")
    assert infer_date_from_filename(path) == "2026-07-01"


def test_gmt_wins_absolutely_when_generation_prefix_comes_first():
    """GMT が生成時刻プレフィクスより後ろにある実形式でも GMT を絶対優先する
    （data/minutes/*.db に実在するパターン。「全候補中の最先頭」実装だと
    生成時刻 2026-07-22 を誤って返す回帰があった）。
    """
    path = Path("2026-07-22-095843-GMT20260701-232525_Recording_1920x1200-minutes.md")
    assert infer_date_from_filename(path) == "2026-07-01"


def test_no_date_falls_back_to_today():
    """日付が全く含まれない場合は本日日付にフォールバックする（既存の挙動）。"""
    path = Path("aaa.md")
    assert infer_date_from_filename(path) == datetime.now().strftime("%Y-%m-%d")
