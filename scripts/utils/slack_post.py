"""Slack の整形ユーティリティと**投稿ファネル**。

GitHub Flavored Markdown を Slack mrkdwn に変換し、Slack section block の
文字数制限内に分割する。あわせて **Slack 投稿の唯一の通り道**（`post_message` /
`post_ephemeral` / `update_message` / `upload_file`）を提供する。

**このファイルは以前は整形ヘルパだけで、輸送層ではなかった**（設計文書 §4.2 が
「`slack_post.py` は輸送層ではない」と実測で指摘した箇所）。SDK 直叩き 25 箇所を
ここへ移送して、送信前の検査と egress ログを1箇所に集約する。
"""
import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Slack section block の text は 3000 文字上限。超過するとブロック全体が無音で破棄される。
_SLACK_SECTION_LIMIT = 2900  # 安全マージン


def _to_slack_mrkdwn(text: str) -> str:
    """GitHub Flavored Markdown を Slack mrkdwn に変換。

    - `## heading` / `### heading` → `*heading*`
    - `**bold**` → `*bold*`
    - 入れ子箇条書き (`- ` / `  - ` / `    - `) は section block では
      先頭スペースが消えてフラット表示になるため、Unicode のブレット文字と
      NBSP (　) インデントに置換して階層感を保つ:
        `- item`     → `• item`
        `  - item`   → `　　◦ item`
        `    - item` → `　　　　▪ item`
    """
    # ヘッダー (## ... / ### ...) を太字に変換
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*([^*\n]+?)\*\*', r'*\1*', text)

    # 箇条書きを Unicode ブレット + NBSP インデントに変換
    def _bullet(m: re.Match) -> str:
        leading = m.group(1)
        spaces = leading.replace("\t", "    ")
        depth = len(spaces) // 2
        if depth >= 2:
            marker = "▪"
        elif depth == 1:
            marker = "◦"
        else:
            marker = "•"
        indent = "　　" * depth
        return f"{indent}{marker} "
    text = re.sub(r'^([ \t]*)[-*]\s+', _bullet, text, flags=re.MULTILINE)

    return text


def _split_mrkdwn_to_blocks(text: str) -> list[dict]:
    """長文 mrkdwn を Slack section block の上限内で分割する。

    改行優先で区切り、超過する単一行は文字数で強制切断する。
    """
    blocks: list[dict] = []
    buf = ""
    for line in text.split("\n"):
        # 単一行が上限を超える場合は強制分割
        while len(line) > _SLACK_SECTION_LIMIT:
            if buf:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
                buf = ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line[:_SLACK_SECTION_LIMIT]}})
            line = line[_SLACK_SECTION_LIMIT:]
        # 通常の改行単位で詰める
        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) > _SLACK_SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = line
        else:
            buf = candidate
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks

# --------------------------------------------------------------------------- #
# 投稿ファネル（docs/security-architecture.md §4.2）
#
# **Slack にはこれまで輸送層のファネルが存在せず、SDK 直叩きが 25 箇所あった。**
# ここに集約して、送信前の検査と egress ログを1箇所で行う。
#
# 移送の指針: `client.chat_postMessage(**kw)` → `slack_post.post_message(client, **kw)`
# のように機械的に置換できる形にしてある（kwargs はそのまま透過する）。
# --------------------------------------------------------------------------- #

_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"


class SlackEgressBlocked(RuntimeError):
    """送信前の検査で止めた（canary 検出・ゼロ幅文字・宛先照合）。"""


# --------------------------------------------------------------------------- #
# 層3（宛先粒度）の照合（docs/security-architecture.md §4.7）
#
# 層1（ホスト）は net_guard、層2（ツール）は agent_tools.py の registry で
# 実装済み。`slack.com` は層1で丸ごと許可されているため、許可済みホストの内側で
# どのチャンネル・どの相手に出すかは誰も見ていなかった。
#
# **まだ enforce にはしない。** 正当な宛先の集合（Argus 自身が知らない、
# コマンド実行チャンネルへの ephemeral 応答等）を今の時点で確定できないため、
# 既定は warn（観測のみ）。`ARGUS_EGRESS_TARGETS=enforce` で拒否に切り替えられる。
# --------------------------------------------------------------------------- #

_EGRESS_TARGETS_MODE_ENV = "ARGUS_EGRESS_TARGETS"

# ephemeral はコマンドが実行されたチャンネルへ返るため「設定に無い」が正常な
# 場合が多い。enforce でここまで遮断すると本番が壊れるので、常に warn 扱いに
# 固定する（記録・ログは他の method と同じく行い、分布は観測し続ける）。
_ALWAYS_WARN_METHODS = {"chat_postEphemeral"}

_dest_cache: tuple[Path, float | None, frozenset] | None = None


def _egress_targets_mode() -> str:
    m = os.environ.get(_EGRESS_TARGETS_MODE_ENV, "warn").strip().lower()
    return m if m in ("warn", "enforce", "off") else "warn"


def _argus_config_path() -> Path:
    """`data/argus_config.yaml` のパス。テストはこの関数を monkeypatch して差し替える。"""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "argus_config.yaml"


def configured_slack_destinations(conn=None) -> set[str]:
    """Argus が設定として持っている宛先（チャンネルID / ユーザーID / Canvas ID /
    Box folder_id）の集合を返す。

    `data/argus_config.yaml` から以下を集める（キー構造は `pm-argus-config-schema`
    Skill 参照。実値は機密のためこの関数の本文は Claude から読まない運用にすること）:

      - `patrol.leader_channel` / `patrol.dm_redirect_user`
      - `indices.*.channels` / `channel_map` のキー / `mention_allowed_channels`
      - `channel_names` のキー / `user_names` のキー
      - `report.canvas_id` / `report.box_folder_id`
      - `argus_daily.brief_canvas_id` / `argus_daily.risk_canvas_id`
      - `meetings.*.box_folder_id` / `meetings.*.catalog_canvas_id`

    mtime キャッシュ付き（`model_pin.load_pin` と同じ書き方）。**この関数は値を
    ログに出さない**。読み込み結果の件数だけを INFO で出す。

    `conn` は現状未使用（呼び出し元のガード関数と同じシグネチャに揃えるため
    受け取っている。将来 DB 由来の宛先集合を足す場合の拡張点）。
    """
    global _dest_cache
    path = _argus_config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _dest_cache is not None and _dest_cache[0] == path and _dest_cache[1] == mtime:
        return set(_dest_cache[2])

    cfg: dict = {}
    if path.is_file():
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("[EGRESS-L3] argus_config.yaml の読み込みに失敗しました")
            cfg = {}
    else:
        logger.warning("[EGRESS-L3] argus_config.yaml が見つかりません: %s", path)

    dests: set[str] = set()

    def _add(value) -> None:
        if value is None or value == "":
            return
        dests.add(str(value))

    patrol = cfg.get("patrol") or {}
    _add(patrol.get("leader_channel"))
    _add(patrol.get("dm_redirect_user"))

    for entry in (cfg.get("indices") or {}).values():
        if isinstance(entry, dict):
            for ch in entry.get("channels") or []:
                _add(ch)

    for ch in cfg.get("channel_map") or {}:
        _add(ch)

    for ch in cfg.get("mention_allowed_channels") or []:
        _add(ch)

    for ch in cfg.get("channel_names") or {}:
        _add(ch)

    for uid in cfg.get("user_names") or {}:
        _add(uid)

    report = cfg.get("report") or {}
    _add(report.get("canvas_id"))
    _add(report.get("box_folder_id"))

    argus_daily = cfg.get("argus_daily") or {}
    _add(argus_daily.get("brief_canvas_id"))
    _add(argus_daily.get("risk_canvas_id"))

    for entry in (cfg.get("meetings") or {}).values():
        if isinstance(entry, dict):
            _add(entry.get("box_folder_id"))
            _add(entry.get("catalog_canvas_id"))

    _dest_cache = (path, mtime, frozenset(dests))
    logger.info("[EGRESS-L3] 設定済み宛先を読み込みました（%d 件）", len(dests))
    return set(dests)


def _check_destination(dest: str, method: str, conn) -> tuple[bool | None, str | None]:
    """宛先照合（層3）。`(dest_known, enforce時のブロック理由 or None)` を返す。

    mode=off なら照合自体を行わず `(None, None)`（呼び出し側はこれを「dest_known を
    記録しない」の意味で扱う）。
    """
    mode = _egress_targets_mode()
    if mode == "off":
        return None, None

    dest_known = dest in configured_slack_destinations(conn)
    if dest_known:
        return True, None

    detail = f"method={method} dest={dest!r}"
    effective_mode = "warn" if method in _ALWAYS_WARN_METHODS else mode
    if effective_mode == "enforce":
        return False, f"設定に無い宛先です（{detail}）"
    logger.warning("[EGRESS-L3] 設定に無い宛先です: %s", detail)
    return False, None


def _open_audit_conn():
    """canary 台帳・egress 記録用の pm.db 接続を自前で開く。

    呼び出し側（`post_message` 等の 25 箇所）が `conn` を渡さない場合の
    フォールバック。`canvas_utils.py` / `box_cli.py` のガードもこの関数を呼ぶ
    （差し替え点をここ1箇所に集約するため）。

    失敗したら None を返す。**黙って握りつぶさない** — 検査が効いていないのに
    効いているように見える状態を作らないため、必ず WARNING を出す（P6）。

    テスト中に本番 `data/pm.db` を開こうとした場合は fail-closed で `RuntimeError`
    を送出する（二重の防御。主対策は `tests/conftest.py` の autouse フィクスチャで
    この関数自体を差し替えること）。判定には `PYTEST_CURRENT_TEST` を使う —
    これは pytest が各テスト実行中にのみ自動設定する環境変数であり、本番実行では
    一切設定されないため、**この分岐は本番実行時には効かない**。
    """
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    db_path = repo_root / "data" / "pm.db"

    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        raise RuntimeError(
            "テストから本番 pm.db への監査書き込みを止めた。"
            "tests/conftest.py の autouse フィクスチャを確認すること"
        )

    try:
        from db_utils import open_db
        return open_db(db_path, encrypt=True)
    except Exception as exc:
        logger.warning(
            "[EGRESS] canary 台帳用の pm.db を開けませんでした。"
            "canary 検査とegress記録を行わずに送信します: %s", exc
        )
        return None


def _payload_text(kwargs: dict) -> str:
    """検査対象になるテキストを kwargs から集める（text と blocks の中身）。"""
    import json as _json

    parts = [str(kwargs.get("text") or "")]
    blocks = kwargs.get("blocks")
    if blocks:
        try:
            parts.append(_json.dumps(blocks, ensure_ascii=False))
        except Exception:
            parts.append(str(blocks))
    parts.append(str(kwargs.get("initial_comment") or ""))
    return "\n".join(p for p in parts if p)


def scan_text_for_egress(text: str, conn=None) -> list[str]:
    """送信・合成の前にテキストを検査し、問題の理由を返す（空なら通過）。

    **公開関数にしてあるのは、Slack 以外の出口からも呼ぶため** — Canvas / Box の
    ファネルと、TTS のようにテキストが別形式へ変換される経路（§4.2 の順序制約）。
    mp3 になった後では検査できないので、合成前が唯一の検査点になる。
    """
    reasons = []
    try:
        from db_utils import active_canary_tokens
        for tok in (active_canary_tokens(conn) if conn is not None else []):
            if tok and tok in text:
                reasons.append(f"canary トークンが含まれています: {tok}")
    except Exception:
        logger.exception("[SLACK-EGRESS] canary 台帳の読み取りに失敗（検査は続行）")
    if any(ch in text for ch in _ZERO_WIDTH):
        reasons.append("ゼロ幅文字が含まれています（不可視の埋め込みの可能性）")
    return reasons


def _guard(kwargs: dict, conn=None, *, method: str, source: str = "") -> None:
    """送信前の検査と記録。**拒否は例外**（戻り値だと呼び出し側が無視できる）。

    検査するのは canary・ゼロ幅文字・宛先照合（層3）である。**自然な散文に
    符号化されたものは通る**（TrojanStego 型）ので、これで内容が安全になる
    わけではない（P10）。宛先照合も同様に、既知の宛先集合に無いことだけを見る
    観測（warn 既定）であり、enforce にしても内容までは保証しない。

    `conn` が渡されなかった場合は `_open_audit_conn()` で自分で pm.db を開く。
    自分で開いた接続は必ず閉じる（呼び出し側から渡された接続は所有権を持たないため
    閉じない）。

    **fail-open**: `_open_audit_conn()` が接続を得られなかった場合（pm.db が
    無い・鍵が読めない等）、`conn` は None のままになり、canary 検査は行わず
    ゼロ幅文字だけを見て通す（`scan_text_for_egress` が `conn is None` のとき
    canary チェックをスキップするため）。ただし WARNING は必ず出る
    （`_open_audit_conn()` 側）ため、検査が効いていないことは分かる。
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _open_audit_conn()
    try:
        text = _payload_text(kwargs)
        reasons = scan_text_for_egress(text, conn)

        channel = str(kwargs.get("channel") or "")
        args: dict = {"channel": channel, "chars": len(text)}
        dest_known, dest_block_reason = _check_destination(channel, method, conn)
        if dest_known is not None:
            args["dest_known"] = dest_known
            if dest_block_reason:
                reasons.append(dest_block_reason)

        outcome = "blocked" if reasons else "ok"
        if conn is not None:
            try:
                from db_utils import record_tool_call
                record_tool_call(
                    conn, session_id=source or "slack", seq=0, plane="egress",
                    tool_name=f"slack:{method}",
                    args=args,
                    outcome=outcome, block_reason="; ".join(reasons) or None,
                )
            except Exception:
                logger.exception("[SLACK-EGRESS] tool_calls への記録に失敗（送信判断は継続）")

        if reasons:
            raise SlackEgressBlocked(f"[SLACK-EGRESS] {method}: {'; '.join(reasons)}")
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def post_message(client, *, conn=None, source: str = "", **kwargs):
    """`chat_postMessage` のファネル。"""
    _guard(kwargs, conn, method="chat_postMessage", source=source)
    return client.chat_postMessage(**kwargs)


def post_ephemeral(client, *, conn=None, source: str = "", **kwargs):
    """`chat_postEphemeral` のファネル。"""
    _guard(kwargs, conn, method="chat_postEphemeral", source=source)
    return client.chat_postEphemeral(**kwargs)


def update_message(client, *, conn=None, source: str = "", **kwargs):
    """`chat_update` のファネル。"""
    _guard(kwargs, conn, method="chat_update", source=source)
    return client.chat_update(**kwargs)


def upload_file(client, *, conn=None, source: str = "", **kwargs):
    """`files_upload_v2` のファネル。

    **ファイル本体の中身は検査していない。** 検査できるのは `initial_comment` 等の
    テキストだけで、mp3/mp4/xlsx の中身には canary もゼロ幅文字も適用できない。
    テキスト以外の成果物は**生成元テキストの検査をもって代える**（§4.2 の原則）。
    """
    _guard(kwargs, conn, method="files_upload_v2", source=source)
    return client.files_upload_v2(**kwargs)

def guard_outbound_text(text: str, *, transport: str, dest: str = "",
                        conn=None, source: str = "") -> None:
    """Slack 以外の輸送（Canvas / Box）から呼ぶ共通ガード。

    検査して記録し、問題があれば例外を投げる。**Slack のファネルと同じ検査・同じ台帳**を
    使うのは、出口ごとに基準が違うと「どの出口なら通るか」を探せてしまうため
    （canary・ゼロ幅文字・宛先照合（層3）のいずれも共通）。

    `conn` が渡されなかった場合は `_open_audit_conn()` で自分で pm.db を開く
    （`_guard()` と同じ）。自分で開いた接続は必ず閉じる。

    **fail-open**: `conn` が得られなかった場合、canary 検査は行わずゼロ幅文字
    だけを見て通す（`_guard()` と同じ挙動。詳細はそちらの docstring 参照）。
    ただし WARNING は必ず出る。
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _open_audit_conn()
    try:
        reasons = scan_text_for_egress(text, conn)

        args: dict = {"dest": dest, "chars": len(text)}
        dest_known, dest_block_reason = _check_destination(dest, transport, conn)
        if dest_known is not None:
            args["dest_known"] = dest_known
            if dest_block_reason:
                reasons.append(dest_block_reason)

        if conn is not None:
            try:
                from db_utils import record_tool_call
                record_tool_call(
                    conn, session_id=source or transport, seq=0, plane="egress",
                    tool_name=f"{transport}:post",
                    args=args,
                    outcome="blocked" if reasons else "ok",
                    block_reason="; ".join(reasons) or None,
                )
            except Exception:
                logger.exception("[EGRESS] tool_calls への記録に失敗（送信判断は継続）")
        if reasons:
            raise SlackEgressBlocked(f"[EGRESS] {transport}: {'; '.join(reasons)}")
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
