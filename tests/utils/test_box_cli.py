"""box_cli.box_get_or_create_shared_link のアクセス範囲テスト（subprocess 未実行、box_json をモック）。"""
import subprocess
from unittest.mock import patch

import pytest
from utils.box_cli import (
    BOX_SHARED_LINK_ACCESS,
    _box_share,
    box_get_or_create_shared_link,
)


def _noop_log(_msg: str) -> None:
    pass


def test_creates_link_with_collaborators_access_when_absent():
    """共有リンクが存在しない場合、--access collaborators で新規作成される。"""
    responses = [
        {"shared_link": None},
        {"shared_link": {"url": "https://box.example.com/s/new", "access": BOX_SHARED_LINK_ACCESS}},
    ]
    calls = []

    def _fake_box_json(cmd, timeout=60):
        calls.append(cmd)
        return responses.pop(0)

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        url = box_get_or_create_shared_link("123", _noop_log)

    assert url == "https://box.example.com/s/new"
    # 1回目: files:get, 2回目: files:share
    assert calls[0][:2] == ["box", "files:get"]
    share_cmd = calls[1]
    assert share_cmd[:2] == ["box", "files:share"]
    assert "--access" in share_cmd
    idx = share_cmd.index("--access")
    assert share_cmd[idx + 1] == "collaborators"


def test_existing_collaborators_link_is_returned_without_reshare():
    """既存リンクが collaborators の場合、貼り直さずそのまま返す。"""
    get_response = {
        "shared_link": {"url": "https://box.example.com/s/existing", "access": "collaborators"},
    }
    calls = []

    def _fake_box_json(cmd, timeout=60):
        calls.append(cmd)
        return get_response

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        url = box_get_or_create_shared_link("123", _noop_log)

    assert url == "https://box.example.com/s/existing"
    # files:get のみが呼ばれ、files:share は呼ばれない
    assert len(calls) == 1
    assert calls[0][:2] == ["box", "files:get"]


def test_existing_open_link_is_reshared_to_collaborators():
    """既存リンクが open の場合、collaborators に貼り直してから返す。"""
    responses = [
        {"shared_link": {"url": "https://box.example.com/s/old", "access": "open"}},
        {"shared_link": {"url": "https://box.example.com/s/old", "access": "collaborators"}},
    ]
    calls = []

    def _fake_box_json(cmd, timeout=60):
        calls.append(cmd)
        return responses.pop(0)

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        url = box_get_or_create_shared_link("123", _noop_log)

    assert url == "https://box.example.com/s/old"
    assert len(calls) == 2
    share_cmd = calls[1]
    assert share_cmd[:2] == ["box", "files:share"]
    idx = share_cmd.index("--access")
    assert share_cmd[idx + 1] == "collaborators"


def test_existing_company_link_is_reshared_to_collaborators():
    """既存リンクが company の場合、collaborators に貼り直してから返す。"""
    responses = [
        {"shared_link": {"url": "https://box.example.com/s/old2", "access": "company"}},
        {"shared_link": {"url": "https://box.example.com/s/old2", "access": "collaborators"}},
    ]
    calls = []

    def _fake_box_json(cmd, timeout=60):
        calls.append(cmd)
        return responses.pop(0)

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        url = box_get_or_create_shared_link("123", _noop_log)

    assert url == "https://box.example.com/s/old2"
    assert len(calls) == 2
    share_cmd = calls[1]
    idx = share_cmd.index("--access")
    assert share_cmd[idx + 1] == "collaborators"


def _run_and_collect_calls(responses: list[dict]) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_box_json(cmd, timeout=60):
        calls.append(cmd)
        return responses.pop(0)

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        box_get_or_create_shared_link("123", _noop_log)
    return calls


def test_no_open_access_argument_is_ever_generated():
    """`--access open` という引数列が生成されないことを保証する。"""
    scenarios = [
        [{"shared_link": None},
         {"shared_link": {"url": "https://box.example.com/s/a", "access": "collaborators"}}],
        [{"shared_link": {"url": "https://box.example.com/s/b", "access": "open"}},
         {"shared_link": {"url": "https://box.example.com/s/b", "access": "collaborators"}}],
        [{"shared_link": {"url": "https://box.example.com/s/c", "access": "company"}},
         {"shared_link": {"url": "https://box.example.com/s/c", "access": "collaborators"}}],
    ]

    for responses in scenarios:
        calls = _run_and_collect_calls(responses)
        for cmd in calls:
            if "--access" in cmd:
                idx = cmd.index("--access")
                assert cmd[idx + 1] not in ("open", "company")


# --------------------------------------------------------------------------- #
# box_json が subprocess.CalledProcessError / TimeoutExpired を投げるケース
# （修正1: box_cli.py の呼び出し元 3 箇所は RuntimeError しか捕捉していないため、
#  _box_share は CalledProcessError/TimeoutExpired を RuntimeError に包んで re-raise する）
# --------------------------------------------------------------------------- #


def test_box_share_wraps_called_process_error_as_runtime_error():
    def _raise(cmd, timeout=60):
        raise subprocess.CalledProcessError(1, cmd)

    with patch("utils.box_cli.box_json", side_effect=_raise):
        with pytest.raises(RuntimeError):
            _box_share("123", _noop_log)


def test_box_share_wraps_timeout_expired_as_runtime_error():
    def _raise(cmd, timeout=60):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with patch("utils.box_cli.box_json", side_effect=_raise):
        with pytest.raises(RuntimeError):
            _box_share("123", _noop_log)


def test_box_get_or_create_shared_link_wraps_called_process_error_on_create():
    """既存リンクが無く新規作成が失敗するケース: RuntimeError が送出される。"""
    def _fake_box_json(cmd, timeout=60):
        if "files:share" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return {"shared_link": None}

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        with pytest.raises(RuntimeError):
            box_get_or_create_shared_link("123", _noop_log)


def test_box_get_or_create_shared_link_wraps_called_process_error_on_reshare():
    """既存リンクが open で貼り直しが失敗するケース: RuntimeError が送出される。"""
    def _fake_box_json(cmd, timeout=60):
        if "files:share" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return {"shared_link": {"url": "https://box.example.com/s/old", "access": "open"}}

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        with pytest.raises(RuntimeError):
            box_get_or_create_shared_link("123", _noop_log)


def test_box_share_warns_when_effective_access_downgraded():
    """access が BOX_SHARED_LINK_ACCESS と異なる場合、例外ではなく WARN ログのみ。"""
    logged = []

    def _fake_box_json(cmd, timeout=60):
        return {"shared_link": {"url": "https://box.example.com/s/x", "access": "company",
                                 "effective_access": "company"}}

    with patch("utils.box_cli.box_json", side_effect=_fake_box_json):
        url = _box_share("123", logged.append)

    assert url == "https://box.example.com/s/x"
    assert any("WARN" in msg and "company" in msg for msg in logged)
