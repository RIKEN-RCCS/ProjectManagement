"""_consensus_stage3 の空応答フォールバック回帰テスト（LLM/embedding/DB 不使用）。

glm-5.2 think=True が reasoning でトークンを使い切り、集約 LLM 呼び出しが
例外ではなく空文字を返すケースがある。既存の except 節は例外時にしか発動せず、
空応答は素通しして「（なし）」に化けていたバグの回帰テスト。
"""
from recording import generate_minutes_local as gml

# --------------------------------------------------------------------------- #
# 合成ドラフトデータ
#   - 決定事項・アクションアイテムとも 2 ドラフトに同種の内容を含める
#   - _greedy_cluster を monkeypatch し、全 index を 1 クラスタにまとめる
#     （min_vote=2 を満たす決定的な投票通過状態を作る）
# --------------------------------------------------------------------------- #

_DECISION_SHORT = "テストプロジェクトの体制を見直すことになった"
_DECISION_LONG = "テストプロジェクトの体制を見直すことになった（詳細な理由を含む）"

_DRAFT_1 = f"""## 決定事項

- {_DECISION_SHORT}

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| 鈴木花子 | 資料の作成を行う | |
"""

_DRAFT_2 = f"""## 決定事項

- {_DECISION_LONG}

## アクションアイテム

| 担当者 | タスク内容 | 期限 |
|---|---|---|
| 鈴木花子 | 資料の作成を行う | 2026-08-01 |
"""


def _fake_greedy_cluster(items, threshold, *, label):
    """全 index を 1 クラスタにまとめる決定的モック。"""
    return [list(range(len(items)))] if items else []


def _make_fake_llm(decisions_response: str, actions_response: str):
    """プロンプト種別（決定事項 / アクションアイテム）を判別して応答を返す。"""
    def fake(prompt, **kwargs):
        if "decision lists" in prompt:
            return decisions_response
        if "action item tables" in prompt:
            return actions_response
        raise AssertionError(f"想定外のプロンプト: {prompt[:80]!r}")
    return fake


def _run_stage3(monkeypatch, decisions_response: str, actions_response: str) -> str:
    monkeypatch.setattr(gml, "_greedy_cluster", _fake_greedy_cluster)
    monkeypatch.setattr(
        "recording.generate_minutes_local.call_argus_llm",
        _make_fake_llm(decisions_response, actions_response),
    )
    return gml._consensus_stage3(
        [_DRAFT_1, _DRAFT_2],
        min_vote=2,
        threshold=0.75,
        claude_md_context="",
        timeout=30,
        think=False,
        max_tokens=2048,
        no_chat_template_kwargs=False,
        temperature=0.7,
    )


# --------------------------------------------------------------------------- #
# 空応答 → 両方フォールバック（実バグの再現ケース）
# --------------------------------------------------------------------------- #

def test_both_empty_response_falls_back_to_cluster_representatives(monkeypatch):
    result = _run_stage3(monkeypatch, decisions_response="", actions_response="")

    # 決定事項側: クラスタ内最長のドラフトが代表として採用される
    assert _DECISION_LONG in result
    # アクションアイテム側: 期限ありの行が代表として採用される
    assert "2026-08-01" in result

    decisions_section = gml._extract_section(result, "決定事項")
    actions_section = gml._extract_section(result, "アクションアイテム")
    assert "（なし）" not in decisions_section
    assert "（なし）" not in actions_section


# --------------------------------------------------------------------------- #
# 正常なマークダウン応答 → そのまま採用される
# --------------------------------------------------------------------------- #

def test_valid_llm_response_is_used_as_is(monkeypatch):
    decisions_ok = "## 決定事項\n\n- 正規化されたテスト決定事項です"
    actions_ok = (
        "## アクションアイテム\n\n"
        "| 担当者 | タスク内容 | 期限 |\n"
        "|---|---|---|\n"
        "| 鈴木花子 | 正規化されたタスク内容です | 2026-09-01 |"
    )
    result = _run_stage3(monkeypatch, decisions_response=decisions_ok, actions_response=actions_ok)

    assert "正規化されたテスト決定事項です" in result
    assert "正規化されたタスク内容です" in result
    assert "2026-09-01" in result
    # フォールバック内容が混入していないこと
    assert _DECISION_LONG not in result
    assert "2026-08-01" not in result


# --------------------------------------------------------------------------- #
# 決定事項側のみ空応答 → 決定事項だけフォールバック、AI側はそのまま
# --------------------------------------------------------------------------- #

def test_decisions_only_empty_response_falls_back(monkeypatch):
    actions_ok = (
        "## アクションアイテム\n\n"
        "| 担当者 | タスク内容 | 期限 |\n"
        "|---|---|---|\n"
        "| 鈴木花子 | 検証済みタスク内容です | 2026-10-01 |"
    )
    result = _run_stage3(monkeypatch, decisions_response="", actions_response=actions_ok)

    assert _DECISION_LONG in result
    assert "検証済みタスク内容です" in result
    assert "2026-10-01" in result

    decisions_section = gml._extract_section(result, "決定事項")
    assert "（なし）" not in decisions_section


# --------------------------------------------------------------------------- #
# AI側のみ空応答 → AI側だけフォールバック、決定事項はそのまま
# --------------------------------------------------------------------------- #

def test_actions_only_empty_response_falls_back(monkeypatch):
    decisions_ok = "## 決定事項\n\n- 検証済み決定事項です"
    result = _run_stage3(monkeypatch, decisions_response=decisions_ok, actions_response="")

    assert "検証済み決定事項です" in result
    assert "2026-08-01" in result  # 期限ありの行がフォールバックで代表採用される

    actions_section = gml._extract_section(result, "アクションアイテム")
    assert "（なし）" not in actions_section
