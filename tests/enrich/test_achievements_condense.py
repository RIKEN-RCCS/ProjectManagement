"""condense_confirmed_titles の件数保証（バックフィル）ロジックのテスト（LLM は monkeypatch）。

0e53e6c で「性能評価最優先」の選定優先順位を追加した副作用で、LLM が優先順位を
「性能評価系以外は削る」と解釈し、max_items の枠を使い切らない回帰
（入力8件→出力2件）が実データで発生した。本テストはコードガード
（_backfill_condensed）でその回帰を固定する。実在人名は使わずダミーアプリ名を使う。
"""
from enrich.achievements_extract import condense_confirmed_titles


def _fake_call_returning(titles):
    def fake_call(prompt, **kwargs):
        joined = ", ".join(f'"{t}"' for t in titles)
        return f'{{"condensed": [{joined}]}}'
    return fake_call


def test_llm_underfills_result_is_backfilled_to_max_items(monkeypatch):
    titles = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
        "OpenACC版をGitHub公開 (2025-05)",
        "性能測定2回目実施 (2025-06)",
        "EEA登録完了 (2025-07)",
        "実機評価完了 (2025-08)",
    ]
    # LLM は性能評価系2件しか返さない（回帰時の症状を再現）
    monkeypatch.setattr(
        "enrich.achievements_extract.call_argus_llm",
        _fake_call_returning(["性能測定1回目実施 (2025-03)", "性能測定2回目実施 (2025-06)"]),
    )

    result = condense_confirmed_titles("ダミーアプリ", titles, max_items=5)

    assert len(result) == 5
    assert "性能測定1回目実施 (2025-03)" in result
    assert "性能測定2回目実施 (2025-06)" in result
    # 残り3件は新しい順（末尾側）の未カバー項目で補充される
    assert "OpenACC版をGitHub公開 (2025-05)" in result
    assert "EEA登録完了 (2025-07)" in result
    assert "実機評価完了 (2025-08)" in result
    # 補充されないはずの古い項目
    assert "要件定義完了 (2025-01)" not in result
    assert "GPU移植着手 (2025-02)" not in result
    # 全体は時系列順（元リストの出現順）に整列される
    expected_order = [
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
        "OpenACC版をGitHub公開 (2025-05)",
        "性能測定2回目実施 (2025-06)",
        "EEA登録完了 (2025-07)",
        "実機評価完了 (2025-08)",
    ]
    # 契約条件合意はカバーされていないので候補に入るが、needed=3なので
    # 新しい順(末尾側)3件が優先され「契約条件合意」は選ばれない
    assert "契約条件合意 (2025-04)" not in result
    filtered_expected_order = [t for t in expected_order if t in result]
    assert result == filtered_expected_order


def test_llm_returns_exactly_max_items_no_backfill(monkeypatch):
    titles = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
        "OpenACC版をGitHub公開 (2025-05)",
        "性能測定2回目実施 (2025-06)",
    ]
    llm_result = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
        "性能測定2回目実施 (2025-06)",
    ]
    monkeypatch.setattr(
        "enrich.achievements_extract.call_argus_llm",
        _fake_call_returning(llm_result),
    )

    result = condense_confirmed_titles("ダミーアプリ", titles, max_items=5)

    assert result == llm_result


def test_date_range_paraphrase_is_treated_as_covered(monkeypatch):
    # 正規化後の短い方が6文字未満だと不一致扱いになる（_titles_overlap の仕様）ため、
    # 「契約条件合意」（正規化後6文字）を使う。
    titles = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "契約条件合意 (2025-09)",
        "契約条件合意フォローアップ (2025-12)",
        "性能測定実施 (2025-10)",
        "OpenACC版をGitHub公開 (2025-11)",
    ]
    # LLM が「契約条件合意」を範囲表記に言い換えて1件に統合して返す
    monkeypatch.setattr(
        "enrich.achievements_extract.call_argus_llm",
        _fake_call_returning(["契約条件合意 (2025-09〜2025-12)"]),
    )

    result = condense_confirmed_titles("ダミーアプリ", titles, max_items=5)

    assert len(result) == 5
    # 元の「契約条件合意」系2件が二重に補充されていないこと
    assert "契約条件合意 (2025-09)" not in result
    assert "契約条件合意フォローアップ (2025-12)" not in result
    assert "契約条件合意 (2025-09〜2025-12)" in result


def test_titles_at_or_below_max_items_skips_llm(monkeypatch):
    titles = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
    ]
    called = {"count": 0}

    def fake_call(prompt, **kwargs):
        called["count"] += 1
        return '{"condensed": []}'
    monkeypatch.setattr("enrich.achievements_extract.call_argus_llm", fake_call)

    result = condense_confirmed_titles("ダミーアプリ", titles, max_items=5)

    assert called["count"] == 0
    assert result == titles


def test_llm_failure_falls_back_to_last_max_items(monkeypatch):
    titles = [
        "要件定義完了 (2025-01)",
        "GPU移植着手 (2025-02)",
        "性能測定1回目実施 (2025-03)",
        "契約条件合意 (2025-04)",
        "OpenACC版をGitHub公開 (2025-05)",
        "性能測定2回目実施 (2025-06)",
        "EEA登録完了 (2025-07)",
    ]

    def fake_call(prompt, **kwargs):
        raise RuntimeError("LLM down")
    monkeypatch.setattr("enrich.achievements_extract.call_argus_llm", fake_call)

    result = condense_confirmed_titles("ダミーアプリ", titles, max_items=5)

    assert result == titles[-5:]
