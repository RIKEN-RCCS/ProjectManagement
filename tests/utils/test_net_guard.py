"""net_guard（scripts/utils/net_guard.py）のフック挙動テスト。

tests/conftest.py の autouse fixture は ARGUS_NETGUARD=off を設定してテスト全体で
フックを無効化するが、本ファイルはフックの挙動そのものを検証するため、各テスト内で
monkeypatch.setenv("ARGUS_NETGUARD", ...) により明示的に上書きする。

実際に外部へ接続するテストは書かない。127.0.0.1 のローカルリスナー、または
AF_UNIX ソケットのみを使う。
"""
import logging
import socket
import threading

import pytest
from utils import net_guard


@pytest.fixture(autouse=True)
def _reset_net_guard_state(monkeypatch):
    """_allowed_ip_ports / allowlist キャッシュをテストごとにクリアする。"""
    monkeypatch.setattr(net_guard, "_allowed_ip_ports", set())
    monkeypatch.setattr(net_guard, "_allowlist_cache", None)
    yield


@pytest.fixture
def local_listener():
    """127.0.0.1 の空きポートで accept するだけのローカル TCP リスナー。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    stop = threading.Event()

    def _accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except TimeoutError:
                continue
            except OSError:
                break

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    try:
        yield port
    finally:
        stop.set()
        srv.close()
        t.join(timeout=1)


# --------------------------------------------------------------------------- #
# allow-list に載ったホスト
# --------------------------------------------------------------------------- #

def test_allowed_connection_passes_and_logs_allow(monkeypatch, tmp_path, local_listener, caplog):
    port = local_listener
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(f"write_plane:\n  - host: \"127.0.0.1\"\n    port: {port}\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "warn")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass

    assert any("verdict=allow" in r.message for r in caplog.records)
    assert not any("verdict=deny" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# allow-list に載っていないホスト: warn / enforce
# --------------------------------------------------------------------------- #

def test_denied_host_warn_passes_but_logs_deny(monkeypatch, tmp_path, local_listener, caplog):
    port = local_listener
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "warn")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    # warn モードは通す
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass

    assert any("verdict=deny" in r.message for r in caplog.records)


def test_denied_host_enforce_raises_permission_error(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(PermissionError):
            sock.connect(("127.0.0.1", 65000))
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# ワイルドカード一致
# --------------------------------------------------------------------------- #

def test_wildcard_host_matching():
    entries = [{"host": "*.slack.com", "port": 443}]
    assert net_guard._host_allowed("wss-abc123.slack.com", 443, entries) is True
    assert net_guard._host_allowed("slack.com", 443, entries) is True
    assert net_guard._host_allowed("notslack.com", 443, entries) is False
    # ポート不一致
    assert net_guard._host_allowed("wss-abc123.slack.com", 8080, entries) is False


# --------------------------------------------------------------------------- #
# AF_UNIX は対象外
# --------------------------------------------------------------------------- #

def test_af_unix_socket_is_unaffected(tmp_path, monkeypatch, caplog):
    # 最も厳しい設定（enforce + 空の allow-list）でも AF_UNIX には一切影響しないこと
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    sock_path = str(tmp_path / "test.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(sock_path)
    finally:
        client.close()
        srv.close()

    assert not any("NETGUARD" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# install() の冪等性
# --------------------------------------------------------------------------- #

def test_install_is_idempotent(monkeypatch):
    # プロセス起動時（他モジュールの import 経由）に既にインストール済みのはず
    assert net_guard._installed is True
    sentinel = object()
    monkeypatch.setattr(socket, "getaddrinfo", sentinel)
    net_guard.install()  # 既にインストール済みなので no-op のはず
    assert socket.getaddrinfo is sentinel


# --------------------------------------------------------------------------- #
# 呼び出し元の特定
# --------------------------------------------------------------------------- #

def test_find_caller_identifies_this_test_file():
    caller = net_guard._find_caller()
    assert caller.startswith("tests/utils/test_net_guard.py:")


# --------------------------------------------------------------------------- #
# 名前解決（DNS）段での allow-list 遮断
# --------------------------------------------------------------------------- #

def test_resolve_denied_hostname_warn_passes_but_logs_deny(monkeypatch, tmp_path, caplog):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "warn")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    # 実際の DNS クエリは出さない（オリジナル関数をスタブに差し替える）
    fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 80))]
    monkeypatch.setattr(net_guard, "_original_getaddrinfo", lambda *a, **k: fake_result)

    result = socket.getaddrinfo("denied.example.invalid", 80)

    assert result == fake_result
    assert any(
        "verdict=deny" in r.message and "stage=resolve" in r.message for r in caplog.records
    )


def test_resolve_denied_hostname_enforce_raises_permission_error(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))

    with pytest.raises(PermissionError):
        socket.getaddrinfo("denied.example.invalid", 80)


def test_resolve_allowed_hostname_passes_and_logs_allow(monkeypatch, tmp_path, caplog):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane:\n  - host: \"localhost\"\n    port: 80\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    result = socket.getaddrinfo("localhost", 80)

    assert result
    assert any(
        "verdict=allow" in r.message and "stage=resolve" in r.message for r in caplog.records
    )


def test_resolve_numeric_ip_literal_is_not_blocked(monkeypatch, tmp_path):
    # enforce + 空 allow-list という最も厳しい設定でも、数値 IP リテラルの解決は
    # resolve 段の判定対象外（connect 段でのみ判定される）ため遮断されない。
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))

    result = socket.getaddrinfo("127.0.0.1", 80)
    assert result


def test_resolve_host_none_is_not_blocked(monkeypatch, tmp_path):
    # bind 用途などで host=None が渡されるケース（AI_PASSIVE）。遮断対象外。
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))

    result = socket.getaddrinfo(None, 80, socket.AF_INET, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
    assert result


def test_resolve_internal_error_fails_closed_in_enforce(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane:\n  - host: \"localhost\"\n    port: 80\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(net_guard, "_host_allowed", _boom)

    with pytest.raises(PermissionError):
        socket.getaddrinfo("localhost", 80)


def test_resolve_internal_error_fails_open_in_warn(monkeypatch, tmp_path, caplog):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane:\n  - host: \"localhost\"\n    port: 80\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "warn")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    caplog.set_level(logging.ERROR, logger="utils.net_guard")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(net_guard, "_host_allowed", _boom)

    result = socket.getaddrinfo("localhost", 80)
    assert result
    assert any("fail-open" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# install() 時点の allow-list 検証（enforce のみ）
# --------------------------------------------------------------------------- #

def test_install_enforce_raises_when_allowlist_empty(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    monkeypatch.setattr(net_guard, "_installed", False)

    with pytest.raises(RuntimeError):
        net_guard.install()


def test_install_enforce_raises_when_allowlist_file_missing(monkeypatch, tmp_path):
    allowlist = tmp_path / "does_not_exist.yaml"
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    monkeypatch.setattr(net_guard, "_installed", False)

    with pytest.raises(RuntimeError):
        net_guard.install()


def test_install_warn_does_not_raise_when_allowlist_empty(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text("write_plane: []\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_NETGUARD", "warn")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    monkeypatch.setattr(net_guard, "_installed", False)

    net_guard.install()  # 例外を出さないこと


# --------------------------------------------------------------------------- #
# verify_endpoints: from_env の起動時照合
# --------------------------------------------------------------------------- #

def test_verify_endpoints_matching_value_logs_ok(monkeypatch, caplog):
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_URL"}]
    monkeypatch.setenv("MY_URL", "https://expected.example.riken.jp/v1")
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="enforce")  # 一致するので例外なし

    assert any("起動時照合 OK" in r.message for r in caplog.records)
    assert not any("起動時照合 NG" in r.message for r in caplog.records)


def test_verify_endpoints_mismatch_warn_logs_warning_but_does_not_raise(monkeypatch, caplog):
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_URL"}]
    monkeypatch.setenv("MY_URL", "https://wrong.example.com/v1")
    caplog.set_level(logging.WARNING, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="warn")  # 例外を出さない

    assert any("起動時照合 NG" in r.message for r in caplog.records)


def test_verify_endpoints_mismatch_enforce_raises(monkeypatch):
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_URL"}]
    monkeypatch.setenv("MY_URL", "https://wrong.example.com/v1")

    with pytest.raises(net_guard.EndpointMismatchError):
        net_guard.verify_endpoints(entries, mode="enforce")


def test_verify_endpoints_unset_env_is_skipped(monkeypatch, caplog):
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_UNSET_URL"}]
    monkeypatch.delenv("MY_UNSET_URL", raising=False)
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="enforce")  # 未設定なので例外を出さない

    assert any("未設定のため起動時照合をスキップ" in r.message for r in caplog.records)


def test_verify_endpoints_off_mode_skips_entirely(monkeypatch, caplog):
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_URL"}]
    monkeypatch.setenv("MY_URL", "https://wrong.example.com/v1")
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="off")  # 照合自体を行わない

    assert not any("NETGUARD" in r.message for r in caplog.records)


def test_verify_endpoints_value_without_scheme(monkeypatch, caplog):
    """`host:port` 形式（スキーム無し）の値も urlparse せず照合できること。"""
    entries = [{"host": "localhost", "port": 8001, "from_env": "MY_EMBED_URL"}]
    monkeypatch.setenv("MY_EMBED_URL", "localhost:8001")
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="enforce")

    assert any("起動時照合 OK" in r.message for r in caplog.records)


def test_verify_endpoints_from_env_list_all_must_match(monkeypatch, caplog):
    entries = [
        {"host": "expected.example.riken.jp", "port": 443, "from_env": ["MY_URL_A", "MY_URL_B"]}
    ]
    monkeypatch.setenv("MY_URL_A", "https://expected.example.riken.jp/v1")
    monkeypatch.setenv("MY_URL_B", "https://expected.example.riken.jp/v1")
    caplog.set_level(logging.INFO, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="enforce")  # 両方一致するので例外なし

    assert not any("起動時照合 NG" in r.message for r in caplog.records)


def test_verify_endpoints_from_env_list_one_mismatch_raises(monkeypatch):
    entries = [
        {"host": "expected.example.riken.jp", "port": 443, "from_env": ["MY_URL_A", "MY_URL_B"]}
    ]
    monkeypatch.setenv("MY_URL_A", "https://expected.example.riken.jp/v1")
    monkeypatch.setenv("MY_URL_B", "https://other.example.com/v1")

    with pytest.raises(net_guard.EndpointMismatchError):
        net_guard.verify_endpoints(entries, mode="enforce")


def test_verify_endpoints_does_not_log_credentials(monkeypatch, caplog):
    """ログにトークン・userinfo・URL全体が出ないこと（host:port のみ出す）。"""
    entries = [{"host": "expected.example.riken.jp", "port": 443, "from_env": "MY_URL"}]
    monkeypatch.setenv(
        "MY_URL", "https://alice:s3cr3t-token@wrong.example.com/v1?api_key=topsecret"
    )
    caplog.set_level(logging.WARNING, logger="utils.net_guard")

    net_guard.verify_endpoints(entries, mode="warn")

    joined = "\n".join(r.message for r in caplog.records)
    assert "s3cr3t-token" not in joined
    assert "alice" not in joined
    assert "topsecret" not in joined
    assert "wrong.example.com:443" in joined


def test_install_calls_verify_endpoints_and_raises_on_enforce_mismatch(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(
        'read_plane:\n  - host: "expected.example.riken.jp"\n    port: 443\n'
        "    from_env: MY_URL\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_NETGUARD", "enforce")
    monkeypatch.setenv("ARGUS_NETGUARD_ALLOWLIST", str(allowlist))
    monkeypatch.setenv("MY_URL", "https://wrong.example.com/v1")
    monkeypatch.setattr(net_guard, "_installed", False)

    with pytest.raises(net_guard.EndpointMismatchError):
        net_guard.install()


# --------------------------------------------------------------------------- #
# --print-env-hosts
# --------------------------------------------------------------------------- #

@pytest.fixture
def _clear_env_host_vars(monkeypatch):
    """print-env-hosts が参照する既知の環境変数を全てクリアする。

    tests/conftest.py の _isolate_env が LOCAL_LLM_URL 等に固定値を設定するため、
    このテストファイル内では明示的にクリアしてから必要な値だけ設定する。
    """
    for var in net_guard._ENV_HOST_VARS:
        monkeypatch.delenv(var, raising=False)


def test_print_env_hosts_groups_same_host_port(_clear_env_host_vars, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_URL", "http://api.rikyu.r-ccs.riken.jp/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "http://api.rikyu.r-ccs.riken.jp/v1")

    output = net_guard.print_env_hosts()

    assert 'host: "api.rikyu.r-ccs.riken.jp"' in output
    assert "port: 80" in output
    assert "from_env: [LOCAL_LLM_URL, OPENAI_API_BASE]" in output


def test_print_env_hosts_unset_vars_shown_as_comment(_clear_env_host_vars, monkeypatch):
    monkeypatch.setenv("RIVAULT_URL", "http://llm.ai.r-ccs.riken.jp:11434")

    output = net_guard.print_env_hosts()

    assert "from_env: RIVAULT_URL" in output
    for var in net_guard._ENV_HOST_VARS:
        if var != "RIVAULT_URL":
            assert f"# {var}: 未設定" in output


def test_print_env_hosts_does_not_leak_userinfo(_clear_env_host_vars, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_URL", "https://alice:s3cr3t-token@api.rikyu.r-ccs.riken.jp/v1")

    output = net_guard.print_env_hosts()

    assert "s3cr3t-token" not in output
    assert "alice" not in output
    assert 'host: "api.rikyu.r-ccs.riken.jp"' in output


# --------------------------------------------------------------------------- #
# --summarize-log
# --------------------------------------------------------------------------- #

_SAMPLE_LOG_LINES = [
    "2026-07-31 12:00:00 ERROR utils.net_guard: [NETGUARD] verdict=deny host=evil.example.com "
    "ip=- port=443 caller=scripts/foo.py:10 stage=connect\n",
    "2026-07-31 12:00:01 ERROR utils.net_guard: [NETGUARD] verdict=deny host=evil.example.com "
    "ip=- port=443 caller=scripts/foo.py:10 stage=connect\n",
    "2026-07-31 12:00:02 ERROR utils.net_guard: [NETGUARD] verdict=deny host=evil.example.com "
    "ip=- port=443 caller=scripts/bar.py:22 stage=connect\n",
    "2026-07-31 12:00:03 INFO utils.net_guard: [NETGUARD] verdict=allow host=api.box.com "
    "ip=- port=443 caller=scripts/baz.py:5 stage=connect\n",
    "2026-07-31 12:00:04 ERROR utils.net_guard: [NETGUARD] verdict=deny host=- ip=203.0.113.5 "
    "port=80 caller=scripts/qux.py:7 stage=resolve\n",
    "some unrelated log line without netguard marker\n",
    "[NETGUARD] this line is garbage and unparsable\n",
]


def test_summarize_log_orders_by_count_descending():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)
    lines = output.splitlines()

    idx_evil = next(i for i, line in enumerate(lines) if "host=evil.example.com" in line)
    idx_ip = next(i for i, line in enumerate(lines) if "ip=203.0.113.5" in line)
    assert idx_evil < idx_ip
    assert lines[idx_evil].startswith("3件")
    assert lines[idx_ip].startswith("1件")


def test_summarize_log_extracts_caller_examples():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)

    assert "scripts/foo.py:10" in output
    assert "scripts/bar.py:22" in output


def test_summarize_log_groups_by_ip_when_host_is_dash():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)

    assert "ip=203.0.113.5 port=80" in output


def test_summarize_log_ignores_allow_verdict():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)

    assert "api.box.com" not in output


def test_summarize_log_counts_unparseable_lines():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)

    assert "解析できなかった行: 1件" in output


def test_summarize_log_emits_yaml_candidate_template():
    output = net_guard.summarize_log(_SAMPLE_LOG_LINES)

    assert "allow-list に追加する候補" in output
    assert 'host: "evil.example.com"' in output
    assert 'host: "203.0.113.5"' in output


def test_summarize_log_empty_input_reports_no_candidates():
    output = net_guard.summarize_log([])

    assert "解析できなかった行: 0件" in output
    assert "該当する deny 行なし" in output
