"""net_guard.py — 外向き通信の宛先 allow-list（層1: ネットワーク層）

設計根拠: docs/security-architecture.md §4.7 層1（ネットワーク層）、P6・P9・P10。

`socket.getaddrinfo` / `socket.socket.connect` をプロセスグローバルにフックし、
この Python プロセスが到達しようとした宛先ホスト・IP・ポートを
allow-list（config/network_allowlist.yaml）と照合してログに記録する。

**名前解決（DNS）自体も allow-list 照合の対象である**（`getaddrinfo` フック）。
allow-list 外のホスト名は、数値 IP リテラルではなく `host=None` でもない限り、
`connect()` に進む前の解決段階で判定される。これは DNS クエリそのものが
任意長のデータを外部（権威 DNS サーバ）へ運べる流出経路になり得るため
（例: `secret-data-base64.attacker.example` を引くだけで情報が届く）、
「宛先が allow-list に載っているか」を接続前に確認する必要があるという判断による。

import すると自動的に install() が走る（プロセスにつき一度、冪等）。呼び出し側は

    from utils import net_guard  # noqa: F401 (import 時の install() 副作用のため)

のように import するだけでよい。個別に install() を呼ぶ必要はない。

モードは環境変数 ARGUS_NETGUARD で切り替える（既定は "warn"）。

    warn (既定) — 通す。ただし必ずログに記録する（allow/deny 両方、resolve/connect 両段）
    enforce     — allow-list 外の宛先を PermissionError で遮断する。
                  resolve 段（DNS 解決）・connect 段の両方で遮断する
    off         — フックを無効化する（テスト用）。照合もログも行わない

ログの `stage` フィールドで遮断・記録がどちらの段で起きたかを区別する
（`stage=resolve` は `getaddrinfo`、`stage=connect` は `socket.connect`）。

enforce モードでは以下も fail-closed にする（warn/off は従来どおり fail-open）。

    - フック内部の判定ロジックが例外を送出した場合 → 通さず PermissionError で遮断する。
      warn フェーズで宛先集合を確定させた後に enforce へ倒すので、その時点で内部エラーが
      出るなら通すより止める方が正しい判断だからである。
    - install() 時点で allow-list が読めない/空の場合 → 起動そのものを例外で止める。
      runtime に全通信が死ぬより起動時に落ちる方が事故として気づきやすい。

allow-list ファイルの場所は環境変数 ARGUS_NETGUARD_ALLOWLIST で上書きできる
（既定は <repo_root>/config/network_allowlist.yaml）。テスト用途を想定。

install() は上記フックの設置に加え、`verify_endpoints()` により allow-list の
`from_env` に指定された環境変数の実行時値をエントリのリテラル値と照合する
（§4.7 層1・P9）。これは「allow-list が固定でも実際の接続先が別ホストに
向いていれば誰も気づかない」問題への対策で、warn/off ではフック同様
fail-open（WARN ログのみ）、enforce では `EndpointMismatchError` で起動を
止める。off モードはこの照合自体を行わない。

運用支援 CLI（このファイルを直接実行した場合のみ有効。フックは設置しない）:

    python3 scripts/utils/net_guard.py --print-env-hosts
        既知の環境変数から YAML に貼り付けられる allow-list エントリを出力する。
        PM が config/network_allowlist.yaml のプレースホルダを実値に置換する
        手順の一部。

    python3 scripts/utils/net_guard.py --summarize-log <path|->
        ログから `[NETGUARD] verdict=deny` 行を集計し、allow-list に
        追加すべき候補を提示する。

    python3 scripts/utils/net_guard.py --plant-hostname-canary [--notes TEXT]
        hostname canary を発行して pm.db の canary_tokens に登録する（§4.3）。
        ホスト名は `.internal-check.invalid`（RFC 2606 予約 TLD）なので正引きは
        原理的に成功しない。発行時に「canary が allow-list に一致しないこと」を
        検証する（一致していたら検知面が消えるので例外で止める）。

    python3 scripts/utils/net_guard.py --list-canaries
    python3 scripts/utils/net_guard.py --revoke-canary <TOKEN>
        canary 台帳の一覧・失効。発火の検知は pm_selfcheck.py の
        canary_hit / netguard_deny チェックが行う（cron 06:30 平日）。

既知の限界（P10 — この対策が何を証明し、何を証明しないか）:

- 証明するのは「この Python プロセス内のどのコード経路もリスト外ホストに
  到達しなかった」であって、**「プロセスが到達できない」ではない**。
  ネットワーク層そのものを塞ぐには iptables / network namespace が必要。
- **`box` CLI / `git` などの subprocess 経由の通信は対象外**（別プロセスなので
  Python の socket フックは届かない）。OS 層でしか塞げない。
- 同一プロセス内のコードはこのフック自体を外せる（`socket.socket.connect` への
  再代入は誰でもできる）。**したがって悪意あるコード実行に対する境界にはならない。
  捕まえるのは事故と LLM 由来の逸脱である。**

hostname canary（docs/security-architecture.md §4.3）への影響: enforce モードで
DNS 解決自体を遮断すると、canary ホスト名は allow-list 外なので resolve 段で
`verdict=deny stage=resolve` としてここのログに現れ、クエリは外部の権威 DNS
サーバへは出ない。canary の検知点が「DNS クエリログ」から「本モジュールの
`verdict=deny` ログ」へ移ることを意味する（劣化ではなく、クエリを外に
出さずに検知できるという意味では改善）。
"""
from __future__ import annotations

import argparse
import io
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

_THIS_FILE = str(Path(__file__).resolve())
_REPO_ROOT = Path(__file__).resolve().parents[2]

_MODE_ENV = "ARGUS_NETGUARD"
_ALLOWLIST_ENV = "ARGUS_NETGUARD_ALLOWLIST"
_DEFAULT_ALLOWLIST_PATH = _REPO_ROOT / "config" / "network_allowlist.yaml"

_state_lock = threading.Lock()
_install_lock = threading.Lock()
_installed = False

# (ip, port) の集合。getaddrinfo が allow-list 済みホスト名を解決した結果のみを記録する。
# connect() がホスト名を経由せず解決済み IP だけを受け取るケース（socket.create_connection
# の通常経路）で、その IP が正当な解決結果かどうかを判定するために使う。
_allowed_ip_ports: set[tuple[str, int]] = set()

# allow-list の読み込みキャッシュ。(path, mtime, entries) を1件だけ保持する。
_allowlist_cache: tuple[Path, float | None, list[dict]] | None = None

# フック前のオリジナル関数（import 時点で一度だけ捕捉する）
_original_getaddrinfo = socket.getaddrinfo
_original_connect = socket.socket.connect


# --------------------------------------------------------------------------- #
# allow-list 読み込み
# --------------------------------------------------------------------------- #

def _resolve_allowlist_path() -> Path:
    override = os.environ.get(_ALLOWLIST_ENV)
    return Path(override) if override else _DEFAULT_ALLOWLIST_PATH


def _load_allowlist_file(path: Path) -> list[dict]:
    """network_allowlist.yaml をパースし、フラットなエントリ一覧に変換する。"""
    if not path.is_file():
        logger.warning("[NETGUARD] allowlist ファイルが見つかりません: %s（空リストとして扱う）", path)
        return []
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("[NETGUARD] allowlist の読み込みに失敗しました: %s", path)
        return []

    entries: list[dict] = []
    if not isinstance(data, dict):
        return entries
    for plane_name, plane_entries in data.items():
        if not isinstance(plane_entries, list):
            continue
        for e in plane_entries:
            if not isinstance(e, dict) or "host" not in e:
                continue
            entries.append({
                "plane": plane_name,
                "host": str(e["host"]),
                "port": e.get("port"),
                "note": e.get("note"),
                "from_env": e.get("from_env"),
            })
    return entries


def _get_allowlist() -> list[dict]:
    """allow-list を mtime キャッシュ付きで返す。パス・mtime が変われば再読み込みする。"""
    global _allowlist_cache
    path = _resolve_allowlist_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _allowlist_cache is not None and _allowlist_cache[0] == path and _allowlist_cache[1] == mtime:
        return _allowlist_cache[2]
    entries = _load_allowlist_file(path)
    _allowlist_cache = (path, mtime, entries)
    return entries


def _host_allowed(host: str, port: int, entries: list[dict] | None = None) -> bool:
    """host:port が allow-list に一致するか判定する。

    エントリの host が `*.` で始まる場合は接尾辞一致（ワイルドカード）とする
    （`*.example.com` は `example.com` 自身と `foo.example.com` の両方に一致）。
    それ以外は完全一致（大文字小文字を無視、末尾の `.` を無視）。
    entry の port が None の場合はポートを問わず一致する。
    """
    if entries is None:
        entries = _get_allowlist()
    host_l = host.lower().rstrip(".")
    for entry in entries:
        entry_port = entry.get("port")
        if entry_port is not None and entry_port != port:
            continue
        pattern = str(entry["host"]).lower()
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            bare = pattern[2:]   # "example.com"
            if host_l == bare or host_l.endswith(suffix):
                return True
        elif host_l == pattern:
            return True
    return False


# --------------------------------------------------------------------------- #
# モード
# --------------------------------------------------------------------------- #

def _current_mode() -> str:
    mode = os.environ.get(_MODE_ENV, "warn").strip().lower()
    return mode if mode in ("warn", "enforce", "off") else "warn"


# --------------------------------------------------------------------------- #
# 呼び出し元の特定
# --------------------------------------------------------------------------- #

def _find_caller() -> str:
    """net_guard.py と標準ライブラリ・site-packages を除いた最初のフレームを返す。

    戻り値は `<repo相対パス>:<行番号>`（repo外なら絶対パス）。特定できなければ "unknown"。
    """
    try:
        stack = traceback.extract_stack()
    except Exception:
        return "unknown"
    for frame in reversed(stack):
        filename = frame.filename
        try:
            resolved = str(Path(filename).resolve())
        except Exception:
            resolved = filename
        if resolved == _THIS_FILE:
            continue
        if "site-packages" in resolved or f"{os.sep}lib{os.sep}python" in resolved:
            continue
        try:
            rel = str(Path(resolved).relative_to(_REPO_ROOT))
        except ValueError:
            rel = resolved
        return f"{rel}:{frame.lineno}"
    return "unknown"


# --------------------------------------------------------------------------- #
# ログ
# --------------------------------------------------------------------------- #

def _log(verdict: str, host: str | None, ip: str | None, port: int, caller: str, stage: str = "connect") -> None:
    msg = (
        f"[NETGUARD] verdict={verdict} host={host or '-'} ip={ip or '-'} "
        f"port={port} caller={caller} stage={stage}"
    )
    if verdict == "deny":
        logger.error(msg)
    else:
        logger.info(msg)


# --------------------------------------------------------------------------- #
# フック本体
# --------------------------------------------------------------------------- #

def _record_resolution(host: str, result: list) -> None:
    """allow-list 済みホスト名の解決結果 IP を _allowed_ip_ports に記録する。

    connect() がホスト名を経由せず解決済み IP だけを受け取る通常経路
    （socket.create_connection 相当）で、その IP が正当な解決結果だと
    判定できるようにするための下準備。
    """
    for item in result:
        try:
            sockaddr = item[4]
            ip = sockaddr[0]
            resolved_port = sockaddr[1]
        except (IndexError, TypeError):
            continue
        if not _host_allowed(host, resolved_port):
            continue
        with _state_lock:
            _allowed_ip_ports.add((ip, resolved_port))


def _check_resolve_allowed(host: object, port: object) -> None:
    """resolve 段（getaddrinfo）で allow-list 照合とログ記録を行う。

    host が None・数値 IP リテラルの場合は判定対象外（connect 段でのみ判定する）。
    enforce モードで allow-list 外と判定した場合は PermissionError を送出する。
    """
    mode = _current_mode()
    if mode == "off" or host is None:
        return
    host_s = str(host)
    try:
        ipaddress.ip_address(host_s)
        return  # 数値IPリテラルは resolve 段の判定対象外（connect 段で判定される）
    except ValueError:
        pass

    allowed = _host_allowed(host_s, port)
    verdict = "allow" if allowed else "deny"
    caller = _find_caller()
    _log(verdict, host_s, None, port, caller, stage="resolve")

    if verdict == "deny" and mode == "enforce":
        raise PermissionError(
            f"NETGUARD: {host_s} (port={port}) は network_allowlist.yaml に無い宛先です。"
            "enforce モードのため DNS 解決を遮断しました。"
        )


def _wrapped_getaddrinfo(*args, **kwargs):
    host = args[0] if args else kwargs.get("host")
    port = args[1] if len(args) > 1 else kwargs.get("port")

    try:
        _check_resolve_allowed(host, port)
    except PermissionError:
        raise
    except Exception:
        if _current_mode() == "enforce":
            logger.exception("[NETGUARD] getaddrinfo フック内部エラー（enforce のため fail-closed で遮断）")
            raise PermissionError(
                "NETGUARD: getaddrinfo フック内部エラーのため enforce モードで遮断しました。"
            ) from None
        logger.exception("[NETGUARD] getaddrinfo フック内部エラー（fail-open で通過）")

    result = _original_getaddrinfo(*args, **kwargs)
    try:
        if host:
            _record_resolution(str(host), result)
    except Exception:
        logger.exception("[NETGUARD] getaddrinfo 解決結果の記録に失敗しました（fail-open）")
    return result


def _classify(host_or_ip: str, port: int) -> tuple[str, str | None, str | None]:
    """(verdict, host, ip) を返す。host_or_ip が IP リテラルか否かで判定方法を変える。"""
    try:
        ipaddress.ip_address(host_or_ip)
        is_ip = True
    except ValueError:
        is_ip = False

    if is_ip:
        with _state_lock:
            resolved_ok = (host_or_ip, port) in _allowed_ip_ports
        # 直接 IP が allow-list に literal で載っている場合（127.0.0.1 の
        # VOICEVOX/fish-speech 等）も許可する。ホスト名を経由しない直接 IP
        # 接続はここでしか捕捉できない。
        allowed = resolved_ok or _host_allowed(host_or_ip, port)
        return ("allow" if allowed else "deny"), None, host_or_ip
    allowed = _host_allowed(host_or_ip, port)
    return ("allow" if allowed else "deny"), host_or_ip, None


def _wrapped_connect(self, address):
    try:
        if hasattr(socket, "AF_UNIX") and getattr(self, "family", None) == socket.AF_UNIX:
            return _original_connect(self, address)

        mode = _current_mode()
        if mode == "off":
            return _original_connect(self, address)

        if not isinstance(address, tuple) or len(address) < 2:
            return _original_connect(self, address)
        host_or_ip, port = address[0], address[1]
        if not isinstance(host_or_ip, str):
            return _original_connect(self, address)

        verdict, host_field, ip_field = _classify(host_or_ip, port)
        caller = _find_caller()
        _log(verdict, host_field, ip_field, port, caller, stage="connect")

        if verdict == "deny" and mode == "enforce":
            raise PermissionError(
                f"NETGUARD: {host_or_ip}:{port} は network_allowlist.yaml に無い宛先です。"
                "enforce モードのため接続を遮断しました。"
            )
    except PermissionError:
        raise
    except Exception:
        # enforce に倒すのは warn フェーズで宛先集合を確定させた後なので、その時点で
        # 内部エラーが出るなら通すより止める方が正しい（修正3の判断根拠）。
        if _current_mode() == "enforce":
            logger.exception("[NETGUARD] connect フック内部エラー（enforce のため fail-closed で遮断）")
            raise PermissionError(
                "NETGUARD: connect フック内部エラーのため enforce モードで遮断しました。"
            ) from None
        logger.exception("[NETGUARD] connect フック内部エラー（fail-open で接続を許可）")
    return _original_connect(self, address)


# --------------------------------------------------------------------------- #
# 起動時の from_env 照合（§4.7 層1、P6・P9）
# --------------------------------------------------------------------------- #

class EndpointMismatchError(RuntimeError):
    """from_env で指定された環境変数の実行時値が allow-list のリテラルと不一致（enforce時）。"""


def _parse_host_port(raw: str) -> tuple[str | None, int | None]:
    """URL 文字列または `host[:port]` 形式の文字列からホスト名とポートを取り出す。

    スキーム付き（`https://host:port/v1`）・スキーム無し（`host:port`）の
    どちらにも対応する。ポートが省略されておりスキームから既定値
    （https→443 / http→80）を補えない場合は None を返す。
    `urlparse` の `hostname`/`port` プロパティ経由でしか値を取り出さないため、
    userinfo（`user:pass@`）や path/query が結果に混入することはない。
    """
    candidate = raw if "://" in raw else f"//{raw}"
    parsed = urlparse(candidate)
    host = parsed.hostname
    port = parsed.port
    if port is None:
        if parsed.scheme == "https":
            port = 443
        elif parsed.scheme == "http":
            port = 80
    return host, port


def verify_endpoints(entries: list[dict] | None = None, mode: str | None = None) -> None:
    """allow-list の `from_env` に指定された環境変数の実行時の値を、
    エントリのリテラル値（host/port）と照合する（起動時アサーション）。

    - 変数が未設定なら検証対象外（INFO ログのみ）。`FISH_TTS_HOST` のように
      未設定が正常なケースがあるため。
    - `from_env` はリストも受け付ける。同一ホストを指す複数の環境変数
      （`RIKYU_URL` / `LOCAL_LLM_URL` / `OPENAI_API_BASE` 等）がある実運用を
      想定し、「設定されている全ての変数がこのエントリと一致する」ことを
      条件とする（設定されている変数の1つでも不一致なら NG）。
    - 不一致時: enforce は `EndpointMismatchError` を送出して起動を止める。
      warn/off は WARN ログのみで継続する（off は照合自体を行わない）。
    - ログには変数名・期待値（リテラル）・実際の host:port のみを出す。
      raw の環境変数値そのもの（トークンや userinfo を含みうる）は出力しない。
    """
    if mode is None:
        mode = _current_mode()
    if mode == "off":
        return
    if entries is None:
        entries = _get_allowlist()

    for entry in entries:
        env_spec = entry.get("from_env")
        if not env_spec:
            continue
        env_names = [env_spec] if isinstance(env_spec, str) else list(env_spec)
        expected_host = str(entry["host"])
        expected_port = entry.get("port")
        expected = f"{expected_host}:{expected_port if expected_port is not None else '-'}"

        for env_name in env_names:
            raw = os.environ.get(env_name)
            if not raw:
                logger.info(
                    "[NETGUARD] %s は未設定のため起動時照合をスキップします", env_name
                )
                continue
            host, port = _parse_host_port(raw)
            actual = f"{host or '-'}:{port if port is not None else '-'}"
            matches = (
                host is not None
                and host.lower().rstrip(".") == expected_host.lower().rstrip(".")
                and (expected_port is None or port == expected_port)
            )
            if matches:
                logger.info(
                    "[NETGUARD] 起動時照合 OK: %s expected=%s actual=%s",
                    env_name, expected, actual,
                )
                continue
            msg = (
                f"[NETGUARD] 起動時照合 NG: {env_name} expected={expected} actual={actual} "
                "(network_allowlist.yaml と不一致)"
            )
            if mode == "enforce":
                raise EndpointMismatchError(msg)
            logger.warning(msg)


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

def install() -> None:
    """socket.getaddrinfo / socket.socket.connect にフックを設置する。

    プロセス内で何度呼ばれても一度しか適用しない（冪等）。

    enforce モードの場合は、allow-list が読めない・空であれば例外を送出して
    起動そのものを止める（修正4）。runtime に全通信が死ぬより起動時に落ちる方が
    事故として気づきやすいという判断による。warn / off ではこの検査は行わない。

    続けて `verify_endpoints()` により、allow-list の `from_env` に指定された
    環境変数の実行時値をリテラルと照合する（§4.7 層1・P9）。off モードでは
    この照合自体を行わない。
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        mode = _current_mode()
        entries = _get_allowlist()
        if mode == "enforce" and not entries:
            path = _resolve_allowlist_path()
            raise RuntimeError(
                f"NETGUARD: enforce モードですが allow-list ({path}) が空か読み込めません。"
                "全通信が遮断されるため起動を中止します。"
            )
        verify_endpoints(entries, mode)
        socket.getaddrinfo = _wrapped_getaddrinfo
        socket.socket.connect = _wrapped_connect
        _installed = True


# --------------------------------------------------------------------------- #
# 運用支援 CLI（--print-env-hosts / --summarize-log）
#
# どちらのモードも install() のフックを設置しない（単なる情報表示であり、
# CLI 実行そのものに副作用を持たせないため）。
# --------------------------------------------------------------------------- #

# --print-env-hosts が対象にする環境変数。追加しやすいよう定数として定義する。
_ENV_HOST_VARS: tuple[str, ...] = (
    "RIKYU_URL",
    "LOCAL_LLM_URL",
    "OPENAI_API_BASE",
    "ARGUS_ONESHOT_LLM_URL",
    "RIVAULT_URL",
    "EMBED_API_BASE",
    "DOCLING_SERVE_URL",
    "FISH_TTS_HOST",
)


def _group_env_hosts(
    env_vars: tuple[str, ...] = _ENV_HOST_VARS,
) -> tuple[list[tuple[tuple[str, int | None], list[str]]], list[str], list[str]]:
    """環境変数を host:port でグループ化する。

    戻り値は (グループ一覧, 未設定の変数名一覧, 解析できなかった変数名一覧)。
    グループ一覧の各要素は ((host, port), [var_name, ...])。
    """
    groups: dict[tuple[str, int | None], list[str]] = {}
    unset: list[str] = []
    unparsed: list[str] = []
    for var in env_vars:
        raw = os.environ.get(var)
        if not raw:
            unset.append(var)
            continue
        host, port = _parse_host_port(raw)
        if host is None:
            unparsed.append(var)
            continue
        key = (host, port)
        groups.setdefault(key, []).append(var)
    return list(groups.items()), unset, unparsed


def print_env_hosts() -> str:
    """`--print-env-hosts`: 環境変数から YAML に貼り付けられる allow-list エントリを組み立てる。

    同一 host:port を指す変数は1エントリにまとめ `from_env` をリストで出す
    （変数が1つだけの場合は単一値のまま出す）。未設定・解析不能の変数は
    コメント行で示す。host/port 以外（トークン・userinfo・path・query）は
    一切出力しない。
    """
    groups, unset, unparsed = _group_env_hosts()
    lines: list[str] = []
    for (host, port), var_names in groups:
        lines.append(f'  - host: "{host}"')
        if port is not None:
            lines.append(f"    port: {port}")
        if len(var_names) == 1:
            lines.append(f"    from_env: {var_names[0]}")
        else:
            lines.append(f"    from_env: [{', '.join(var_names)}]")
    for var in unset:
        lines.append(f"  # {var}: 未設定")
    for var in unparsed:
        lines.append(f"  # {var}: 値を解析できませんでした（host:port 形式を確認してください）")
    return "\n".join(lines)


_LOG_LINE_RE = re.compile(
    r"\[NETGUARD\]\s+verdict=(?P<verdict>\S+)\s+host=(?P<host>\S+)\s+ip=(?P<ip>\S+)"
    r"\s+port=(?P<port>\S+)\s+caller=(?P<caller>\S+)\s+stage=(?P<stage>\S+)"
)


def _parse_netguard_log_line(line: str) -> dict | None:
    """NETGUARD ログの1行から verdict/host/ip/port/caller/stage を抜き出す。"""
    match = _LOG_LINE_RE.search(line)
    if not match:
        return None
    d = match.groupdict()
    try:
        d["port"] = int(d["port"])
    except ValueError:
        d["port"] = None
    return d


def summarize_log(lines) -> str:
    """`--summarize-log`: `[NETGUARD] verdict=deny` 行を集計する。

    stage・host（host が `-` の場合は ip）・port でグループ化し、件数の多い順に
    出力する。各グループについて caller の代表例（最大3件）も添える。
    「解析できない行」は `[NETGUARD]` を含みながら期待するフォーマットに
    一致しなかった行のみを数える（NETGUARD と無関係な大量の一般ログ行を
    ノイズとしてカウントしないため）。
    """
    groups: dict[tuple[str, str, str, object], dict] = {}
    unparsed = 0
    for raw_line in lines:
        if "[NETGUARD]" not in raw_line:
            continue
        parsed = _parse_netguard_log_line(raw_line)
        if parsed is None:
            unparsed += 1
            continue
        if parsed["verdict"] != "deny":
            continue
        host = parsed["host"]
        ip = parsed["ip"]
        stage = parsed["stage"]
        port = parsed["port"]
        use_ip = host in (None, "-")
        label = ip if use_ip else host
        key = (stage, "ip" if use_ip else "host", label, port)
        group = groups.setdefault(key, {"count": 0, "callers": []})
        group["count"] += 1
        caller = parsed.get("caller") or "-"
        if caller not in group["callers"] and len(group["callers"]) < 3:
            group["callers"].append(caller)

    ordered = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)

    out: list[str] = []
    for (stage, kind, label, port), info in ordered:
        label_field = f"ip={label}" if kind == "ip" else f"host={label}"
        out.append(f"{info['count']}件 stage={stage} {label_field} port={port}")
        out.append(f"  callers: {', '.join(info['callers'])}")

    out.append("")
    out.append(f"解析できなかった行: {unparsed}件")
    out.append("")
    out.append("# allow-list に追加する候補")
    if not ordered:
        out.append("  (該当する deny 行なし)")
    for (_stage, _kind, label, port), _info in ordered:
        out.append(f'  - host: "{label}"')
        out.append(f"    port: {port}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# hostname canary（docs/security-architecture.md §4.3）
# --------------------------------------------------------------------------- #

def host_in_allowlist_any_port(host: str, entries: list[dict] | None = None) -> bool:
    """host が allow-list のどれか（ポートを問わず）に一致するか。

    canary ホスト名が allow-list に載っていないことの検証に使う。載っていると
    canary への到達が `verdict=allow` になり、検知面が丸ごと消えるため。
    """
    if entries is None:
        entries = _get_allowlist()
    return any(_host_allowed(host, entry.get("port"), [entry]) for entry in entries)


def plant_hostname_canary(db_path, *, no_encrypt: bool = False, notes: str | None = None) -> dict:
    """hostname canary を発行して pm.db の canary_tokens に登録する。

    ホスト名は `.internal-check.invalid`（RFC 2606 予約 TLD）なので、正引きが
    成功することは無い。したがって「このホスト名の解決が試みられた」= 到達を
    試みた主体が居た、という強いシグナルになる（enforce なら resolve 段で
    遮断され、warn でも `verdict=deny stage=resolve` として記録される）。

    埋め込み先（Box 文書・議事録等）への実際の記載は呼び出し側／運用の責務で、
    ここでは台帳登録までを行う（planted_in='registry_only'）。
    """
    from db_utils import (  # 遅延 import（循環回避）
        CANARY_HOSTNAME_SUFFIX,
        open_db,
        plant_canary,
    )

    conn = open_db(db_path, encrypt=not no_encrypt)
    try:
        row = plant_canary(
            conn,
            kind="hostname",
            planted_in="registry_only",
            notes=notes or f"hostname canary（{CANARY_HOSTNAME_SUFFIX} / RFC 2606 予約 TLD）",
        )
    finally:
        conn.close()

    if host_in_allowlist_any_port(row["token"]):
        raise RuntimeError(
            f"NETGUARD: 発行した canary ホスト名 {row['token']} が allow-list に一致します。"
            "allow-list を修正してください（canary は必ず allow-list 外である必要があります）。"
        )
    return row


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "net_guard 運用支援 CLI"
            "（--print-env-hosts / --summarize-log / canary の発行・一覧・失効）"
        )
    )
    parser.add_argument(
        "--print-env-hosts",
        action="store_true",
        help="環境変数から YAML に貼り付けられる allow-list エントリを出力する",
    )
    parser.add_argument(
        "--summarize-log",
        metavar="PATH",
        help="ログファイル（'-' で stdin）から [NETGUARD] verdict=deny 行を集計する",
    )
    parser.add_argument(
        "--plant-hostname-canary",
        action="store_true",
        help="hostname canary を発行して pm.db の canary_tokens に登録する（§4.3）",
    )
    parser.add_argument(
        "--list-canaries",
        action="store_true",
        help="canary_tokens の active な行を一覧する",
    )
    parser.add_argument(
        "--revoke-canary",
        metavar="TOKEN",
        help="指定した canary を失効させる（行は残す）",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="--plant-hostname-canary 時に台帳へ残すメモ（埋め込み先など）",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="pm.db のパス（既定: <repo>/data/pm.db）。canary 系サブコマンドで使う",
    )
    parser.add_argument(
        "--no-encrypt",
        action="store_true",
        help="pm.db を平文で開く（canary 系サブコマンド）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.print_env_hosts:
        print(print_env_hosts())
        return 0
    if args.summarize_log:
        # errors="replace" は必須。実運用のログには不正な UTF-8 バイト列が混ざる
        # （Box 文書名・LLM 出力・端末制御シーケンス等）。strict デコードだと
        # 集計そのものが UnicodeDecodeError で落ち、enforce へ倒す判断ができない。
        if args.summarize_log == "-":
            stream = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
            lines = stream.readlines()
        else:
            with open(args.summarize_log, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        print(summarize_log(lines))
        return 0

    if args.plant_hostname_canary or args.list_canaries or args.revoke_canary:
        from pathlib import Path as _Path

        db_path = _Path(args.db) if args.db else _REPO_ROOT / "data" / "pm.db"
        if args.plant_hostname_canary:
            row = plant_hostname_canary(
                db_path, no_encrypt=args.no_encrypt, notes=args.notes
            )
            print(f"植えた canary: {row['token']}")
            print(f"  kind={row['kind']} planted_in={row['planted_in']} planted_at={row['planted_at']}")
            print("  次にやること: このホスト名を「モデルが読む場所」に記載する")
            print("    （例: Box 文書・議事録本文。人間向けレポートに出る場所は避ける）")
            print("  記載したら --notes 相当を canary_tokens.row_ref に反映しておく")
            return 0
        if args.revoke_canary:
            from db_utils import open_db, revoke_canary

            conn = open_db(db_path, encrypt=not args.no_encrypt)
            try:
                ok = revoke_canary(conn, args.revoke_canary)
            finally:
                conn.close()
            print("失効させました" if ok else f"該当する canary がありません: {args.revoke_canary}")
            return 0 if ok else 1

        from db_utils import list_canaries, open_db

        conn = open_db(db_path, encrypt=not args.no_encrypt)
        try:
            rows = list_canaries(conn, active_only=True)
        finally:
            conn.close()
        if not rows:
            print("active な canary はありません")
            return 0
        for row in rows:
            in_allowlist = host_in_allowlist_any_port(row["token"]) if row["kind"] == "hostname" else False
            warn = "  ★allow-list に一致（検知面が消えています）" if in_allowlist else ""
            print(f"{row['token']}  kind={row['kind']} planted_in={row['planted_in']}"
                  f" row_ref={row['row_ref'] or '-'} planted_at={row['planted_at']}{warn}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
else:
    install()
