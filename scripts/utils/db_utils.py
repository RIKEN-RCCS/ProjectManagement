#!/usr/bin/env python3
"""
db_utils.py

SQLite / SQLCipher の接続を一元管理するユーティリティ。

暗号化モード（デフォルト）:
  - sqlcipher3 を使用して AES-256 でDBを暗号化する
  - 鍵の読み込み優先順位:
      1. 環境変数 PM_DB_KEY
      2. ~/.secrets/pm_db_key.txt

平文モード（--no-encrypt オプション等で切り替え）:
  - 標準の sqlite3 を使用する
  - 既存の平文DBをそのまま使いたい場合や、暗号化不要な場合に使用

CLI サブコマンド:
  --gen-key               暗号化鍵を生成して ~/.secrets/pm_db_key.txt に保存する
  --show-key-path         鍵ファイルのパスを表示する
  --migrate DB [DB ...]   平文DBを SQLCipher 暗号化DBに変換する
  --no-backup             --migrate 時にバックアップを作成しない
  --dry-run               --migrate 時に変換せず確認のみ行う
  --audit-log             audit_log（変更履歴）を表示する
  --db PATH               --audit-log 時の pm.db パス（デフォルト: data/pm.db）
  --limit N               --audit-log 時の表示件数（デフォルト: 30）
  --source SOURCE         --audit-log 時にソースで絞り込む（canvas_sync / relink）
  --id ID                 --audit-log 時にアクションアイテムIDで絞り込む
"""

import os
import secrets
import sqlite3 as _sqlite3
from pathlib import Path

# net_guard の import（import 時の install() 副作用のため）。
# db_utils.py は `python3 scripts/db_utils.py --gen-key` 等で直接実行されることが
# あり、その場合 sys.path[0] は scripts/utils（scripts/ ではない）になるため
# `from utils import net_guard` がそのままでは失敗する。フォールバックで
# scripts/ を sys.path に追加してから再 import する。
try:
    from utils import net_guard  # noqa: F401
except ImportError:
    import sys as _sys
    _scripts_dir = str(Path(__file__).resolve().parent.parent)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    from utils import net_guard  # noqa: F401

# sqlcipher3 が利用可能かチェック
try:
    from sqlcipher3 import dbapi2 as _sqlcipher3
    SQLCIPHER_AVAILABLE = True
except ImportError:
    SQLCIPHER_AVAILABLE = False

DEFAULT_KEY_FILE = Path.home() / ".secrets" / "pm_db_key.txt"


# --------------------------------------------------------------------------- #
# 鍵の読み込み
# --------------------------------------------------------------------------- #
def load_key() -> str:
    """
    暗号化鍵を取得する。
    優先順位: 環境変数 PM_DB_KEY > ~/.secrets/pm_db_key.txt
    """
    key = os.getenv("PM_DB_KEY")
    if key:
        return key.strip()

    if DEFAULT_KEY_FILE.exists():
        key = DEFAULT_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key

    raise RuntimeError(
        "暗号化鍵が見つかりません。\n"
        "  環境変数 PM_DB_KEY を設定するか、\n"
        f"  {DEFAULT_KEY_FILE} に鍵を保存してください。\n"
        "  鍵の生成: python3 scripts/db_utils.py --gen-key"
    )


def gen_key() -> str:
    """32バイト（64文字）の16進数ランダム鍵を生成して保存する"""
    key = secrets.token_hex(32)
    DEFAULT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_KEY_FILE.write_text(key + "\n", encoding="utf-8")
    DEFAULT_KEY_FILE.chmod(0o600)
    return key


# --------------------------------------------------------------------------- #
# DB 接続
# --------------------------------------------------------------------------- #
def open_db(
    db_path: Path | str,
    *,
    encrypt: bool = True,
    row_factory: bool = True,
    schema: str | None = None,
    migrations: list[str] | None = None,
) -> _sqlite3.Connection:
    """
    DB を開いて接続を返す。

    Parameters
    ----------
    db_path : Path | str
        DBファイルのパス。
    encrypt : bool
        True（デフォルト）なら sqlcipher3 で暗号化接続する。
        False なら標準 sqlite3 で平文接続する。
    row_factory : bool
        True なら conn.row_factory = sqlite3.Row を設定する。
    schema : str | None
        初期化SQLスクリプト（CREATE TABLE IF NOT EXISTS ...）。
        指定した場合は接続後に executescript で実行する。
    migrations : list[str] | None
        マイグレーション用SQLのリスト。順番に execute する。

    Returns
    -------
    sqlite3.Connection（または sqlcipher3 の Connection）
    """
    db_path = Path(db_path)

    if encrypt:
        if not SQLCIPHER_AVAILABLE:
            raise RuntimeError(
                "sqlcipher3 がインストールされていません。\n"
                "  uv pip install sqlcipher3\n"
                "または --no-encrypt オプションで平文モードを使用してください。"
            )
        key = load_key()
        conn = _sqlcipher3.connect(db_path)
        # パスフレーズをSQLエスケープして PRAGMA key に渡す
        escaped = key.replace("'", "''")
        conn.execute(f"PRAGMA key='{escaped}'")
        # 既存DBの場合: PRAGMA key 直後に SELECT を行い SQLCipher が既存の
        # salt を読み込んで HMAC コンテキストを確立させる。これをしないと
        # 後続の commit が新しい salt で page 1 を上書きし HMAC 破損が起きる。
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    else:
        conn = _sqlite3.connect(db_path)

    if row_factory:
        conn.row_factory = _sqlcipher3.Row if encrypt and SQLCIPHER_AVAILABLE else _sqlite3.Row

    if schema:
        # executescript() は暗黙 COMMIT を発行するため使用しない。
        # 個別 execute + 明示 commit を使う。
        for stmt in schema.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass
        conn.commit()

    if migrations:
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass  # 既に適用済みのマイグレーションはスキップ
        conn.commit()

    return conn


def open_db_plain(db_path: Path | str, *, row_factory: bool = True) -> _sqlite3.Connection:
    """平文（非暗号化）でDBを開く。読み取り専用操作や移行スクリプト用。"""
    return open_db(db_path, encrypt=False, row_factory=row_factory)


# --------------------------------------------------------------------------- #
# pm.db 初期化
# --------------------------------------------------------------------------- #
import re as _re  # noqa: E402 (ローカルimport)

_PM_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   TEXT PRIMARY KEY,
    held_at      TEXT,
    kind         TEXT,
    file_path    TEXT,
    summary      TEXT,
    parsed_at    TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    assignee     TEXT,
    due_date     TEXT,
    status       TEXT DEFAULT 'open',
    note         TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   TEXT,
    content      TEXT,
    decided_at   TEXT,
    source       TEXT DEFAULT 'meeting',
    source_ref   TEXT,
    extracted_at TEXT
);

CREATE TABLE IF NOT EXISTS terminology (
    term         TEXT PRIMARY KEY,
    category     TEXT,
    aliases      TEXT,
    source       TEXT,
    last_seen    TEXT,
    frequency    INTEGER DEFAULT 1,
    meeting_kinds TEXT
);

-- Argus 垂直軸: 前提・意思決定台帳（有向グラフ）。詳細は docs/FugakuNEXT_Argus_designsheet 参照。
-- 目標・制約: 意図された方向の3層（最上位/識別要件/前提条件）を表現する。
CREATE TABLE IF NOT EXISTS ledger_goals (
    goal_id       TEXT PRIMARY KEY,
    kind          TEXT,
    layer         TEXT,
    is_top_goal   INTEGER DEFAULT 0,
    name          TEXT,
    identification_test TEXT,
    weight        TEXT,
    weight_status TEXT,
    source        TEXT,
    source_status TEXT,
    state         TEXT DEFAULT 'active',
    created_at    TEXT,
    last_reviewed_at TEXT
);

-- 前提: 確信度・根拠・監視対象（機能1の取り込み口）を保持する。
CREATE TABLE IF NOT EXISTS ledger_assumptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT,
    confidence    TEXT,
    evidence      TEXT,
    monitor_target TEXT,
    source        TEXT,
    state         TEXT DEFAULT 'active',
    created_at    TEXT,
    last_reviewed_at TEXT
);

-- 論点: 未解決事項。責任者・期限を持ち、決定をブロックする。
CREATE TABLE IF NOT EXISTS ledger_issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id      TEXT UNIQUE,
    content       TEXT,
    owner         TEXT,
    due_date      TEXT,
    state         TEXT DEFAULT 'open',
    created_at    TEXT
);

-- 型付き辺: 有向グラフ本体。方向に関する情報は辺が保持する。
-- edge_type: contributes(貢献) / depends_on(依拠) / monitors(監視) / blocks(ブロック)
CREATE TABLE IF NOT EXISTS ledger_edges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_type     TEXT,
    from_kind     TEXT,
    from_id       TEXT,
    to_kind       TEXT,
    to_id         TEXT,
    weight        REAL,
    source        TEXT,
    rationale     TEXT,
    state         TEXT DEFAULT 'active',
    created_at    TEXT,
    UNIQUE(edge_type, from_kind, from_id, to_kind, to_id)
);

-- 実績台帳: アプリ別の完了実績（LLM抽出 + 重複排除で埋める）。
CREATE TABLE IF NOT EXISTS achievements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    app             TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT,
    achieved_on     TEXT,
    evidence_ref    TEXT,
    evidence_quote  TEXT,
    confidence      TEXT DEFAULT 'low',
    status          TEXT DEFAULT 'proposed',
    source          TEXT DEFAULT 'argus_auto',
    dedup_key       TEXT UNIQUE,
    created_at      TEXT,
    updated_at      TEXT,
    deleted         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_achievements_app ON achievements(app);

-- canary トークン台帳（docs/security-architecture.md §4.3）。
-- 「植えた canary が、本来出てこない場所に現れたか」を検知するための正本。
-- 実際の検知は pm_selfcheck.py の canary_hit / netguard_deny チェックが行う。
-- token は「スキャン対象の文字列そのもの」を入れる（kind='hostname' なら
-- ホスト名、kind='text' なら ARGUS-CANARY-xxxx 形式のトークン）。
CREATE TABLE IF NOT EXISTS canary_tokens (
    token       TEXT PRIMARY KEY,
    planted_in  TEXT NOT NULL,   -- 'action_items' | 'decisions' | 'minutes' | 'box_docs' | 'slack' | 'registry_only'
    row_ref     TEXT,            -- 埋めた行の参照（未植え付けなら NULL）
    planted_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    kind        TEXT NOT NULL,   -- 'text' | 'hostname'
    notes       TEXT
);

-- ツール呼び出し台帳（docs/security-architecture.md §4.4）。
-- audit_log は「DB の列がどう変わったか」の記録で、ツール名・引数は残らない。
-- egress ログとして不十分なため、LLM のツール呼び出しを別テーブルに追記専用で残す。
-- entry_hash = sha256(prev_hash || call_id || ts || tool_name || args_json || outcome)
-- のハッシュ連鎖により、過去エントリの改竄が検出できる。
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id          TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    ts               TEXT NOT NULL,
    plane            TEXT NOT NULL,   -- 'read' | 'mutate' | 'egress'
    tool_name        TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    args_max_entropy REAL,
    result_bytes     INTEGER,
    result_sha256    TEXT,
    model            TEXT NOT NULL DEFAULT '',
    model_revision   TEXT NOT NULL DEFAULT '',
    reasoning_sha256 TEXT,
    outcome          TEXT NOT NULL,   -- 'ok' | 'blocked' | 'error'
    block_reason     TEXT,
    prev_hash        TEXT NOT NULL,
    entry_hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, seq);

CREATE TRIGGER IF NOT EXISTS tool_calls_no_update BEFORE UPDATE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;

CREATE TRIGGER IF NOT EXISTS tool_calls_no_delete BEFORE DELETE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;

-- 思考トレース（docs/security-architecture.md §4.4）。
-- **モデルが見た機微データがそのまま入る機微データストア**なので、pm.db 内に置いて
-- SQLCipher の対象とし、保持期間を定める（既定90日、purge_reasoning_traces）。
-- レポート系クエリからは除外する。tool_calls 側は sha256 のみを持つ。
CREATE TABLE IF NOT EXISTS reasoning_traces (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    step           INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    model_revision TEXT NOT NULL DEFAULT '',
    trace_sha256   TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    trace          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reasoning_session ON reasoning_traces(session_id, step);
CREATE INDEX IF NOT EXISTS idx_reasoning_ts ON reasoning_traces(ts);

-- 第2系統（独立系統）による差分検査の記録（docs/security-architecture.md §4.9 対策3+5）。
-- フラグ語が立った項目にだけ非中国系モデルを当て、主系統との一致／不一致を残す。
-- **第2系統の判定で主系統を上書きはしない** — 小型モデルの能力差による誤りが混ざるため、
-- 自動で覆さずフラグを立てるに留め、人が見る。
CREATE TABLE IF NOT EXISTS triage_second_opinion (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- 'action_items' | 'decisions'
    content_sha256  TEXT NOT NULL,
    content_head    TEXT NOT NULL,   -- 先頭のみ（全文は pm.db 本体にある）
    primary_verdict TEXT NOT NULL,
    second_verdict  TEXT NOT NULL,   -- 'KEEP' | 'DROP' | 'UNKNOWN'
    agreed          INTEGER NOT NULL,
    flagged_terms   TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_second_opinion_agreed ON triage_second_opinion(agreed, ts);

-- 変更履歴（pm_relink.py / pm_sync_canvas.py / pm_xlsx_sync.py 等が共有）。
-- pm_xlsx_sync.py の鮮度ガードが SELECT するため、pm.db 生成時から必ず存在させる。
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,
    source     TEXT
);
"""


def init_pm_db(db_path: Path, no_encrypt: bool = False):
    """pm.db を初期化して接続を返す。スキーマ作成・マイグレーションを自動適用する。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(
        db_path,
        encrypt=not no_encrypt,
        schema=_PM_SCHEMA,
        migrations=[
            "ALTER TABLE action_items ADD COLUMN note TEXT",
            "ALTER TABLE action_items ADD COLUMN milestone_id TEXT",
            "ALTER TABLE decisions ADD COLUMN source_context TEXT",
            "ALTER TABLE action_items ADD COLUMN deleted INTEGER DEFAULT 0",
            "ALTER TABLE decisions ADD COLUMN deleted INTEGER DEFAULT 0",
            # Knowledge-Augmented Extraction: エンリッチメント用カラム
            "ALTER TABLE decisions ADD COLUMN decided_by TEXT",
            "ALTER TABLE decisions ADD COLUMN decided_by_confidence TEXT",
            "ALTER TABLE decisions ADD COLUMN rationale TEXT",
            "ALTER TABLE decisions ADD COLUMN related_ids TEXT",
            "ALTER TABLE action_items ADD COLUMN source_context TEXT",
            "ALTER TABLE action_items ADD COLUMN requested_by TEXT",
            "ALTER TABLE action_items ADD COLUMN requested_by_confidence TEXT",
            "ALTER TABLE action_items ADD COLUMN rationale TEXT",
            "ALTER TABLE action_items ADD COLUMN related_ids TEXT",
            # 2026-05-18: Slack チャンネル ID をフィルタ・集計のキーとして正規化
            "ALTER TABLE action_items ADD COLUMN channel_id TEXT",
            "ALTER TABLE decisions ADD COLUMN channel_id TEXT",
            # 2026-07-01: Argus 垂直軸 — 前提・意思決定台帳（有向グラフ）
            "ALTER TABLE decisions ADD COLUMN trade_off TEXT",
            "ALTER TABLE decisions ADD COLUMN reversal_condition TEXT",
            # 2026-07-05: 選別ゲート（設計書§4: 荷重を持つ決定だけを台帳へ取り込む）
            "ALTER TABLE decisions ADD COLUMN ledger_gate TEXT",
            "ALTER TABLE decisions ADD COLUMN ledger_gate_reason TEXT",
        ],
    )


# --------------------------------------------------------------------------- #
# canary トークン台帳（docs/security-architecture.md §4.3）
# --------------------------------------------------------------------------- #

# hostname canary のドメイン。`.invalid` は RFC 2606 の予約 TLD であり、
# 正引きは原理的に成功しない。実在ドメインを使うと「canary への到達」が
# 本物の外部 DNS クエリになってしまうため、必ず予約 TLD を使う。
CANARY_HOSTNAME_SUFFIX = ".internal-check.invalid"

_CANARY_DDL = """
CREATE TABLE IF NOT EXISTS canary_tokens (
    token       TEXT PRIMARY KEY,
    planted_in  TEXT NOT NULL,
    row_ref     TEXT,
    planted_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    kind        TEXT NOT NULL,
    notes       TEXT
);
"""


def table_exists(conn: "_sqlite3.Connection", name: str) -> bool:
    """テーブルの存在を sqlite_master で確認する。

    例外での判定にはしない。SQLCipher 使用時の `sqlcipher3.dbapi2.OperationalError`
    は標準 `sqlite3.OperationalError` の派生ではないため、`except sqlite3.OperationalError`
    では捕まらず、暗号化 DB でだけ落ちる（実際に踏んだ）。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def ensure_canary_table(conn: "_sqlite3.Connection") -> None:
    """canary_tokens テーブルを作成する（既存 pm.db への後付け用・冪等）。

    init_pm_db() を経由しない既存 DB でも動くようにするため、スキーマ定義と
    同じ DDL をここでも実行できるようにしてある。
    """
    conn.executescript(_CANARY_DDL)
    conn.commit()


def _new_canary_token(kind: str) -> str:
    suffix = secrets.token_hex(4)
    if kind == "hostname":
        return f"docs-{suffix}{CANARY_HOSTNAME_SUFFIX}"
    return f"ARGUS-CANARY-{suffix}"


def plant_canary(
    conn: "_sqlite3.Connection",
    *,
    kind: str,
    planted_in: str,
    row_ref: str | None = None,
    notes: str | None = None,
    token: str | None = None,
) -> dict:
    """canary を台帳に登録して、登録した行を dict で返す。

    kind='hostname' の場合、token はスキャン対象のホスト名そのものになる。
    planted_in='registry_only' は「台帳に登録したがまだどこにも埋めていない」
    状態を表す（発行だけ先に済ませて植え付けは別途行う運用のため）。

    実データ（pm.db の行・box_docs の文書等）への埋め込みは呼び出し側の責務。
    """
    from datetime import UTC, datetime

    if kind not in ("text", "hostname"):
        raise ValueError(f"kind は 'text' | 'hostname' のいずれか: {kind!r}")
    ensure_canary_table(conn)
    tok = token or _new_canary_token(kind)
    planted_at = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO canary_tokens (token, planted_in, row_ref, planted_at, active, kind, notes)"
        " VALUES (?, ?, ?, ?, 1, ?, ?)",
        (tok, planted_in, row_ref, planted_at, kind, notes),
    )
    conn.commit()
    return {
        "token": tok,
        "planted_in": planted_in,
        "row_ref": row_ref,
        "planted_at": planted_at,
        "active": 1,
        "kind": kind,
        "notes": notes,
    }


def list_canaries(conn: "_sqlite3.Connection", *, active_only: bool = True) -> list[dict]:
    """canary_tokens を新しい順に返す。テーブルが無い場合は空リスト。"""
    if not table_exists(conn, "canary_tokens"):
        return []
    sql = "SELECT token, planted_in, row_ref, planted_at, active, kind, notes FROM canary_tokens"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY planted_at DESC"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def revoke_canary(conn: "_sqlite3.Connection", token: str) -> bool:
    """canary を失効させる（行は残す）。対象が無ければ False。"""
    cur = conn.execute("UPDATE canary_tokens SET active = 0 WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount > 0


def active_canary_tokens(conn: "_sqlite3.Connection") -> list[str]:
    """active な canary の token 文字列（＝スキャン対象）を返す。"""
    return [row["token"] for row in list_canaries(conn, active_only=True)]


# --------------------------------------------------------------------------- #
# tool_calls 台帳（docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #

_TOOL_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id          TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    ts               TEXT NOT NULL,
    plane            TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    args_max_entropy REAL,
    result_bytes     INTEGER,
    result_sha256    TEXT,
    model            TEXT NOT NULL DEFAULT '',
    model_revision   TEXT NOT NULL DEFAULT '',
    reasoning_sha256 TEXT,
    outcome          TEXT NOT NULL,
    block_reason     TEXT,
    prev_hash        TEXT NOT NULL,
    entry_hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, seq);
CREATE TRIGGER IF NOT EXISTS tool_calls_no_update BEFORE UPDATE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tool_calls_no_delete BEFORE DELETE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;
"""

GENESIS_HASH = "0" * 64


def ensure_tool_calls_table(conn: "_sqlite3.Connection") -> None:
    """tool_calls テーブルとトリガを作成する（既存 pm.db への後付け用・冪等）。"""
    conn.executescript(_TOOL_CALLS_DDL)
    conn.commit()


def shannon_entropy(s: str) -> float:
    """文字列のシャノンエントロピー（bits/文字）。

    Base64 で符号化された機微データは自然文より高いエントロピーを持つため、
    引数の異常検知の手がかりに使う（§4.4 の `args_max_entropy`）。
    **これは検知の補助であって判定ではない** — 高エントロピー＝流出ではないし、
    低エントロピーなら安全でもない（自然な散文で運ぶ TrojanStego 型がある）。
    """
    if not s:
        return 0.0
    from collections import Counter
    from math import log2

    n = len(s)
    return -sum((c / n) * log2(c / n) for c in Counter(s).values())


def _args_max_entropy(args: dict) -> float:
    """引数のうち文字列値の最大エントロピー。数値・真偽値は対象外。"""
    vals = [v for v in args.values() if isinstance(v, str) and v]
    return max((shannon_entropy(v) for v in vals), default=0.0)


def record_tool_call(
    conn: "_sqlite3.Connection",
    *,
    session_id: str,
    seq: int,
    plane: str,
    tool_name: str,
    args: dict,
    outcome: str,
    result: str | None = None,
    block_reason: str | None = None,
    model: str = "",
    model_revision: str = "",
    reasoning_sha256: str | None = None,
) -> dict:
    """ツール呼び出しを1件追記し、書いた行を返す（§4.4）。

    ハッシュ連鎖の計算と INSERT は**呼び出し側で直列化すること**（並列ツール実行が
    あるため、prev_hash の読み取りと INSERT の間に他の追記が入ると連鎖が壊れる）。

    > 連鎖の頭が同じ信頼領域内にあると、意図的な改竄は検出できない（§4.4 の警告）。
    > 検出できるのは事故による破損まで。外部アンカー（日次のハッシュ投稿）は Phase 3。
    """
    import hashlib
    import json as _json
    import uuid
    from datetime import UTC, datetime

    if plane not in ("read", "mutate", "egress"):
        raise ValueError(f"plane は 'read' | 'mutate' | 'egress': {plane!r}")
    if outcome not in ("ok", "blocked", "error"):
        raise ValueError(f"outcome は 'ok' | 'blocked' | 'error': {outcome!r}")

    ensure_tool_calls_table(conn)
    row = conn.execute(
        "SELECT entry_hash FROM tool_calls ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    prev_hash = (row[0] if not hasattr(row, "keys") else row["entry_hash"]) if row else GENESIS_HASH

    call_id = uuid.uuid4().hex
    ts = datetime.now(UTC).isoformat()
    args_json = _json.dumps(args, ensure_ascii=False, sort_keys=True)
    payload = f"{prev_hash}{call_id}{ts}{tool_name}{args_json}{outcome}"
    entry_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    result_bytes = len(result.encode("utf-8")) if result is not None else None
    result_sha256 = (
        hashlib.sha256(result.encode("utf-8")).hexdigest() if result is not None else None
    )

    conn.execute(
        "INSERT INTO tool_calls (call_id, session_id, seq, ts, plane, tool_name, args_json,"
        " args_max_entropy, result_bytes, result_sha256, model, model_revision,"
        " reasoning_sha256, outcome, block_reason, prev_hash, entry_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, session_id, seq, ts, plane, tool_name, args_json,
         _args_max_entropy(args), result_bytes, result_sha256, model, model_revision,
         reasoning_sha256, outcome, block_reason, prev_hash, entry_hash),
    )
    conn.commit()
    return {
        "call_id": call_id, "session_id": session_id, "seq": seq, "ts": ts,
        "plane": plane, "tool_name": tool_name, "outcome": outcome,
        "prev_hash": prev_hash, "entry_hash": entry_hash,
    }


def verify_tool_call_chain(conn: "_sqlite3.Connection") -> list[dict]:
    """tool_calls のハッシュ連鎖を検証し、壊れている箇所を返す（空なら健全）。

    検出できるのは**事故による破損**であって意図的な改竄ではない（§4.4）。
    コード実行を取られたらエントリと連鎖の頭の両方を書き換えられる。
    """
    import hashlib

    if not table_exists(conn, "tool_calls"):
        return []
    rows = conn.execute(
        "SELECT call_id, ts, tool_name, args_json, outcome, prev_hash, entry_hash"
        " FROM tool_calls ORDER BY rowid"
    ).fetchall()
    broken = []
    expected_prev = GENESIS_HASH
    for r in rows:
        d = dict(r)
        payload = f"{d['prev_hash']}{d['call_id']}{d['ts']}{d['tool_name']}{d['args_json']}{d['outcome']}"
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if d["prev_hash"] != expected_prev:
            broken.append({"call_id": d["call_id"], "reason": "prev_hash が直前の entry_hash と一致しない"})
        elif recomputed != d["entry_hash"]:
            broken.append({"call_id": d["call_id"], "reason": "entry_hash が内容から再計算した値と一致しない"})
        expected_prev = d["entry_hash"]
    return broken


# --------------------------------------------------------------------------- #
# reasoning_traces（思考トレース。docs/security-architecture.md §4.4）
# --------------------------------------------------------------------------- #

# 既定の保持期間。canary 発火時は該当セッションを別途保全する（§4.4 のランブック）。
REASONING_RETENTION_DAYS = 90

_REASONING_DDL = """
CREATE TABLE IF NOT EXISTS reasoning_traces (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    step           INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    model_revision TEXT NOT NULL DEFAULT '',
    trace_sha256   TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    trace          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reasoning_session ON reasoning_traces(session_id, step);
CREATE INDEX IF NOT EXISTS idx_reasoning_ts ON reasoning_traces(ts);
"""


def ensure_reasoning_traces_table(conn: "_sqlite3.Connection") -> None:
    """reasoning_traces テーブルを作成する（冪等）。

    **このテーブルはモデルが見た機微データがそのまま入る新しい機微データストアである**
    （§4.4）。容量と保持期間だけの問題として扱ってはならない。pm.db 内に置くことで
    SQLCipher の適用対象とし、レポート系クエリからは除外する。
    """
    conn.executescript(_REASONING_DDL)
    conn.commit()


def record_reasoning_trace(
    conn: "_sqlite3.Connection",
    *,
    session_id: str,
    step: int,
    trace: str,
    model: str = "",
    model_revision: str = "",
) -> str | None:
    """思考トレースを1件保存し、その sha256 を返す（空文字なら None）。

    `tool_calls` 側はこの sha256 だけを持つ（本体は容量が大きく保持期間も異なるため）。
    """
    import hashlib
    from datetime import UTC, datetime

    if not trace:
        return None
    ensure_reasoning_traces_table(conn)
    sha = hashlib.sha256(trace.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO reasoning_traces (session_id, step, ts, model, model_revision,"
        " trace_sha256, char_count, trace) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, step, datetime.now(UTC).isoformat(), model, model_revision,
         sha, len(trace), trace),
    )
    conn.commit()
    return sha


def purge_reasoning_traces(
    conn: "_sqlite3.Connection", *, days: int = REASONING_RETENTION_DAYS,
    keep_sessions: "list[str] | None" = None,
) -> int:
    """保持期間を過ぎた思考トレースを削除し、削除件数を返す。

    `keep_sessions` に挙げたセッションは期間を過ぎても残す（canary 発火時の保全用）。
    **無期限に持つ理由はない** — canary 調査に要るのは直近だけである（§4.4）。
    """
    from datetime import UTC, datetime, timedelta

    if not table_exists(conn, "reasoning_traces"):
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    sql = "DELETE FROM reasoning_traces WHERE ts < ?"
    params: list = [cutoff]
    if keep_sessions:
        placeholders = ",".join("?" * len(keep_sessions))
        sql += f" AND session_id NOT IN ({placeholders})"
        params += list(keep_sessions)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def record_second_opinion(
    conn: "_sqlite3.Connection", *, kind: str, content: str,
    primary_verdict: str, second_verdict: str, flagged_terms: list[str],
    model: str = "", raw: str | None = None,
) -> None:
    """第2系統の判定結果を記録する（§4.9 対策3+5）。

    一致・不一致の**両方**を残す。不一致だけ記録すると「何件中の不一致か」が
    分からず、能力差による雑音の割合を後から評価できないため。
    """
    import hashlib
    from datetime import UTC, datetime

    conn.executescript(
        "CREATE TABLE IF NOT EXISTS triage_second_opinion ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,"
        "content_sha256 TEXT NOT NULL, content_head TEXT NOT NULL,"
        "primary_verdict TEXT NOT NULL, second_verdict TEXT NOT NULL,"
        "agreed INTEGER NOT NULL, flagged_terms TEXT NOT NULL,"
        "model TEXT NOT NULL DEFAULT '', raw TEXT);"
    )
    conn.execute(
        "INSERT INTO triage_second_opinion (ts, kind, content_sha256, content_head,"
        " primary_verdict, second_verdict, agreed, flagged_terms, model, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), kind,
         hashlib.sha256(content.encode("utf-8")).hexdigest(), content[:200],
         primary_verdict, second_verdict, int(primary_verdict == second_verdict),
         ",".join(flagged_terms), model, raw),
    )
    conn.commit()


def tool_call_anchor(conn: "_sqlite3.Connection") -> dict | None:
    """`tool_calls` の連鎖の頭（最新 entry_hash）と件数を返す（§4.4 の外部アンカー用）。

    **これを外部の追記専用の場所に日次で固定することで、内部で完結した連鎖の弱点を補う。**
    連鎖の検証者が改竄されうる側と同じプロセス・同じ UNIX ユーザで動く以上、
    エントリと連鎖の頭を両方書き換えられてしまう。外に置いた過去の値と矛盾すれば、
    その日以降の改竄が確定する。
    """
    if not table_exists(conn, "tool_calls"):
        return None
    row = conn.execute(
        "SELECT entry_hash, ts FROM tool_calls ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    n = conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0]
    d = dict(row)
    return {"entry_hash": d["entry_hash"], "ts": d["ts"], "count": n}


def normalize_assignee(name: str | None) -> str | None:
    """担当者名を正規化する。
    - 未展開の Slack メンション ID (<@UXXX> / @UXXX / 生の UXXX) を除去
    - 複数担当者（カンマ区切り）の場合は各要素を処理してIDのみのものを除外
    - 日本語を含む姓名間スペース（半角・全角）を除去
    """
    if not name:
        return name
    # <@UXXX> 形式を除去
    name = _re.sub(r"<@([A-Z0-9]+)>", "", name)
    # 後置の「氏」「さん」を除去
    name = _re.sub(r"(?<=[A-Za-z0-9\u3040-\u9fff])[氏さん]", "", name)
    # カンマ・読点区切りで複数担当者に分割して各要素を処理
    parts = _re.split(r"[,、]\s*", name)
    cleaned = []
    for p in parts:
        p = p.strip()
        # 生のユーザーID（U で始まる英数字8文字以上）のみの要素は除外
        if _re.fullmatch(r"U[A-Z0-9]{8,}", p):
            continue
        if p:
            # 日本語名のスペース除去
            if _re.search(r"[\u3040-\u9fff]", p):
                p = p.replace(" ", "").replace("\u3000", "")
            cleaned.append(p)
    result = ", ".join(cleaned) if cleaned else None
    return result


# --------------------------------------------------------------------------- #
# pm.db 高レベルユーティリティ
# --------------------------------------------------------------------------- #
import sys as _sys  # noqa: E402


def open_pm_db(db_path: "Path", no_encrypt: bool = False) -> "_sqlite3.Connection":
    """
    pm.db を開いて接続を返す。ファイルが存在しない場合は sys.exit(1)。

    acknowledged_at マイグレーションを自動適用する。
    """
    if not db_path.exists():
        print(f"ERROR: pm.db が見つかりません: {db_path}", file=_sys.stderr)
        _sys.exit(1)
    return open_db(
        db_path,
        encrypt=not no_encrypt,
        migrations=[
            "ALTER TABLE decisions ADD COLUMN acknowledged_at TEXT",
            # extracted_at を発生日に修正: meeting は held_at、slack は変更なし
            ("UPDATE action_items SET extracted_at = "
             "(SELECT held_at FROM meetings WHERE meetings.meeting_id = action_items.meeting_id) "
             "WHERE source = 'meeting' AND meeting_id IS NOT NULL "
             "AND extracted_at LIKE '____-__-__T%'"),
            ("UPDATE decisions SET extracted_at = "
             "(SELECT held_at FROM meetings WHERE meetings.meeting_id = decisions.meeting_id) "
             "WHERE source = 'meeting' AND meeting_id IS NOT NULL "
             "AND extracted_at LIKE '____-__-__T%'"),
            "ALTER TABLE action_items ADD COLUMN deleted INTEGER DEFAULT 0",
            "ALTER TABLE decisions ADD COLUMN deleted INTEGER DEFAULT 0",
            # Knowledge-Augmented Extraction: エンリッチメント用カラム
            "ALTER TABLE decisions ADD COLUMN decided_by TEXT",
            "ALTER TABLE decisions ADD COLUMN decided_by_confidence TEXT",
            "ALTER TABLE decisions ADD COLUMN rationale TEXT",
            "ALTER TABLE decisions ADD COLUMN related_ids TEXT",
            "ALTER TABLE action_items ADD COLUMN source_context TEXT",
            "ALTER TABLE action_items ADD COLUMN requested_by TEXT",
            "ALTER TABLE action_items ADD COLUMN requested_by_confidence TEXT",
            "ALTER TABLE action_items ADD COLUMN rationale TEXT",
            "ALTER TABLE action_items ADD COLUMN related_ids TEXT",
            # 2026-05-18: Slack チャンネル ID をフィルタ・集計のキーとして正規化
            "ALTER TABLE action_items ADD COLUMN channel_id TEXT",
            "ALTER TABLE decisions ADD COLUMN channel_id TEXT",
            # 2026-06-25: 用語辞書テーブル（Whisper prompt 動的拡張・reconcile 用）
            (
                "CREATE TABLE IF NOT EXISTS terminology ("
                "term TEXT PRIMARY KEY, category TEXT, aliases TEXT, source TEXT, "
                "last_seen TEXT, frequency INTEGER DEFAULT 1, meeting_kinds TEXT)"
            ),
            # 2026-07-01: Argus 垂直軸 — 前提・意思決定台帳（有向グラフ）
            "ALTER TABLE decisions ADD COLUMN trade_off TEXT",
            "ALTER TABLE decisions ADD COLUMN reversal_condition TEXT",
            (
                "CREATE TABLE IF NOT EXISTS ledger_goals ("
                "goal_id TEXT PRIMARY KEY, kind TEXT, layer TEXT, is_top_goal INTEGER DEFAULT 0, "
                "name TEXT, identification_test TEXT, weight TEXT, weight_status TEXT, "
                "source TEXT, source_status TEXT, state TEXT DEFAULT 'active', "
                "created_at TEXT, last_reviewed_at TEXT)"
            ),
            (
                "CREATE TABLE IF NOT EXISTS ledger_assumptions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, confidence TEXT, "
                "evidence TEXT, monitor_target TEXT, source TEXT, state TEXT DEFAULT 'active', "
                "created_at TEXT, last_reviewed_at TEXT)"
            ),
            (
                "CREATE TABLE IF NOT EXISTS ledger_issues ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT UNIQUE, content TEXT, "
                "owner TEXT, due_date TEXT, state TEXT DEFAULT 'open', created_at TEXT)"
            ),
            (
                "CREATE TABLE IF NOT EXISTS ledger_edges ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, edge_type TEXT, from_kind TEXT, from_id TEXT, "
                "to_kind TEXT, to_id TEXT, weight REAL, source TEXT, rationale TEXT, "
                "state TEXT DEFAULT 'active', created_at TEXT, "
                "UNIQUE(edge_type, from_kind, from_id, to_kind, to_id))"
            ),
            # 2026-07-05: 選別ゲート（設計書§4: 荷重を持つ決定だけを台帳へ取り込む）
            "ALTER TABLE decisions ADD COLUMN ledger_gate TEXT",
            "ALTER TABLE decisions ADD COLUMN ledger_gate_reason TEXT",
            # 2026-07-16: 実績台帳（achievements ledger）Phase 1
            (
                "CREATE TABLE IF NOT EXISTS achievements ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT NOT NULL, title TEXT NOT NULL, "
                "category TEXT, achieved_on TEXT, evidence_ref TEXT, evidence_quote TEXT, "
                "confidence TEXT DEFAULT 'low', status TEXT DEFAULT 'proposed', "
                "source TEXT DEFAULT 'argus_auto', dedup_key TEXT UNIQUE, "
                "created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0)"
            ),
            "CREATE INDEX IF NOT EXISTS idx_achievements_app ON achievements(app)",
            # 2026-07-27: pm_xlsx_sync.py の鮮度ガードが SELECT するため既存 DB にも保証する
            (
                "CREATE TABLE IF NOT EXISTS audit_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, "
                "record_id TEXT NOT NULL, field TEXT NOT NULL, old_value TEXT, "
                "new_value TEXT, changed_at TEXT NOT NULL, source TEXT)"
            ),
        ],
    )


def _build_channel_kind_condition(
    channel_ids: list[str] | None,
    minutes_names: list[str] | None,
    table_alias: str = "",
) -> tuple[str, list[str]]:
    """channel_ids / minutes_names から action_items/decisions 用のフィルタ条件を構築する。
    両方 None の場合は無条件（全体検索）。

    table_alias: テーブルの alias（例: "a" → "a.source"。既定は空文字で alias なし
    （"source"）。主テーブルに alias がある場合はそれを指定する。
    Returns: (where_clause_fragment, params)
    """
    tbl = f"{table_alias}." if table_alias else ""
    clauses: list[str] = []
    params: list[str] = []
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        clauses.append(f"({tbl}source='slack' AND {tbl}channel_id IN ({ph}))")
        params.extend(channel_ids)
    if minutes_names:
        ph = ",".join("?" * len(minutes_names))
        clauses.append(
            f"({tbl}source='meeting' AND {tbl}meeting_id IN "
            f"(SELECT meeting_id FROM meetings WHERE kind IN ({ph})))"
        )
        params.extend(minutes_names)
    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


def fetch_milestone_progress(conn: "_sqlite3.Connection") -> list[dict]:
    """マイルストーンごとのアクションアイテム完了率を取得する"""
    try:
        rows = conn.execute(
            """
            SELECT m.milestone_id, m.goal_id, m.name, m.due_date, m.area,
                   m.status, m.success_criteria,
                   COUNT(DISTINCT CASE WHEN a.status='open'   AND COALESCE(a.deleted,0)=0 THEN a.id END) AS open_count,
                   COUNT(DISTINCT CASE WHEN a.status='closed' AND COALESCE(a.deleted,0)=0 THEN a.id END) AS closed_count
            FROM milestones m
            LEFT JOIN action_items a ON a.milestone_id = m.milestone_id
            WHERE m.status = 'active'
            GROUP BY m.milestone_id
            ORDER BY m.due_date ASC NULLS LAST
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def fetch_assignee_workload(
    conn: "_sqlite3.Connection",
    today: str,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> list[dict]:
    """担当者別の負荷（オープンアイテム数・期限超過数・期限未設定数）を取得する（LLM不使用）"""
    try:
        base_query = "SELECT assignee, due_date FROM action_items WHERE status = 'open' AND COALESCE(deleted,0)=0"
        cond, params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
        if cond:
            base_query += cond
        rows = conn.execute(base_query, params).fetchall()
    except Exception:
        return []

    counts: dict[str, dict] = {}
    for row in rows:
        name = normalize_assignee(row["assignee"]) or "未定"
        entry = counts.setdefault(name, {"total_open": 0, "overdue": 0, "no_due_date": 0})
        entry["total_open"] += 1
        if row["due_date"] and row["due_date"] < today:
            entry["overdue"] += 1
        if not row["due_date"]:
            entry["no_due_date"] += 1

    result = [{"assignee": k, **v} for k, v in counts.items()]
    result.sort(key=lambda x: (-x["overdue"], -x["total_open"]))
    return result


def fetch_overdue_items(
    conn: "_sqlite3.Connection",
    today: str,
    since: str | None,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> list[dict]:
    """期限超過（status='open' かつ due_date < today）のアイテムを取得"""
    query = """
        SELECT id, content, assignee, due_date, milestone_id,
               requested_by, rationale
        FROM action_items
        WHERE status = 'open' AND COALESCE(deleted,0)=0 AND due_date IS NOT NULL AND due_date < ?
    """
    params: list = [today]
    cond, cond_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    if cond:
        query += cond
        params.extend(cond_params)
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    query += " ORDER BY due_date ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_unlinked_items_count(
    conn: "_sqlite3.Connection",
    since: str | None,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> int:
    """milestone_id が未設定の open アイテム数（計画の穴）"""
    query = "SELECT COUNT(*) FROM action_items WHERE status='open' AND COALESCE(deleted,0)=0 AND milestone_id IS NULL"
    params: list = []
    cond, cond_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    if cond:
        query += cond
        params.extend(cond_params)
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    return conn.execute(query, params).fetchone()[0]


def fetch_no_assignee_count(
    conn: "_sqlite3.Connection",
    since: str | None,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> int:
    """担当者なしの open アイテム数"""
    query = "SELECT COUNT(*) FROM action_items WHERE status='open' AND COALESCE(deleted,0)=0 AND (assignee IS NULL OR assignee = '')"
    params: list = []
    cond, cond_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    if cond:
        query += cond
        params.extend(cond_params)
    if since:
        query += " AND extracted_at >= ?"
        params.append(since)
    return conn.execute(query, params).fetchone()[0]


def fetch_weekly_trends(
    conn: "_sqlite3.Connection",
    weeks: int = 4,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> list[dict]:
    """直近 N 週の「作成件数」と「完了件数」の近似トレンド"""
    from datetime import date, timedelta
    today_dt = date.today()
    cond, cond_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    result = []
    for w in range(weeks, 0, -1):
        week_start = (today_dt - timedelta(weeks=w)).isoformat()
        week_end   = (today_dt - timedelta(weeks=w - 1)).isoformat()
        params_created = [week_start, week_end] + cond_params
        created_query = "SELECT COUNT(*) FROM action_items WHERE COALESCE(deleted,0)=0 AND extracted_at >= ? AND extracted_at < ?"
        if cond:
            created_query += cond
        created = conn.execute(created_query, params_created).fetchone()[0]
        params_closed = [week_start, week_end] + cond_params
        closed_query = "SELECT COUNT(*) FROM action_items WHERE status='closed' AND COALESCE(deleted,0)=0 AND extracted_at >= ? AND extracted_at < ?"
        if cond:
            closed_query += cond
        closed = conn.execute(closed_query, params_closed).fetchone()[0]
        result.append({
            "week_start": week_start,
            "week_end": week_end,
            "created": created,
            "closed": closed,
        })
    return result


def fetch_unacknowledged_decisions(
    conn: "_sqlite3.Connection",
    since: str | None,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> list[dict]:
    """未確認（acknowledged_at IS NULL）の決定事項（最大20件）"""
    query = "SELECT id, content, decided_at, decided_by, rationale FROM decisions WHERE COALESCE(deleted,0)=0 AND acknowledged_at IS NULL"
    params: list = []
    cond, cond_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    if cond:
        query += cond
        params.extend(cond_params)
    if since:
        query += " AND decided_at >= ?"
        params.append(since)
    query += " ORDER BY decided_at DESC LIMIT 20"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_summary_stats(
    conn: "_sqlite3.Connection",
    since: str | None,
    today: str,
    channel_ids: list[str] | None = None,
    minutes_names: list[str] | None = None,
) -> dict:
    """統計（channel_ids/minutes_names でフィルタ可能）"""
    def _count(query: str, params: list) -> int:
        return conn.execute(query, params).fetchone()[0]

    p_since = [since] if since else []
    since_filter_ai = " AND extracted_at >= ?" if since else ""
    since_filter_d  = " AND decided_at >= ?" if since else ""
    cond_ai, cond_ai_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")
    cond_d, cond_d_params = _build_channel_kind_condition(channel_ids, minutes_names, table_alias="")

    return {
        "total_open": _count(
            f"SELECT COUNT(*) FROM action_items WHERE COALESCE(deleted,0)=0 AND status='open'{cond_ai}{since_filter_ai}",
            cond_ai_params + p_since,
        ),
        "total_closed": _count(
            f"SELECT COUNT(*) FROM action_items WHERE COALESCE(deleted,0)=0 AND status='closed'{cond_ai}{since_filter_ai}",
            cond_ai_params + p_since,
        ),
        "overdue_count": _count(
            f"SELECT COUNT(*) FROM action_items WHERE COALESCE(deleted,0)=0 AND status='open' AND due_date IS NOT NULL AND due_date < ?{cond_ai}",
            [today] + cond_ai_params,
        ),
        "total_decisions": _count(
            f"SELECT COUNT(*) FROM decisions WHERE COALESCE(deleted,0)=0{cond_d}{since_filter_d}",
            cond_d_params + p_since,
        ),
        "unacknowledged_decisions": _count(
            f"SELECT COUNT(*) FROM decisions WHERE COALESCE(deleted,0)=0 AND acknowledged_at IS NULL{cond_d}",
            cond_d_params,
        ),
    }


# --------------------------------------------------------------------------- #
# 平文DB → 暗号化DB 変換
# --------------------------------------------------------------------------- #
def is_encrypted(db_path: Path) -> bool:
    """DBが暗号化済みかどうかを判定する"""
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        conn.close()
        return False
    except _sqlite3.DatabaseError:
        return True


def migrate_db(db_path: Path, *, backup: bool = True, dry_run: bool = False) -> bool:
    """
    平文DBを SQLCipher 暗号化DBに変換する。

    Parameters
    ----------
    db_path  : 変換対象のDBファイルパス
    backup   : True なら変換前に .db.bak を作成する（デフォルト: True）
    dry_run  : True なら変換せず確認のみ行う

    Returns
    -------
    bool: 変換を実施した場合 True、スキップの場合 False
    """
    import shutil
    import tempfile

    if not SQLCIPHER_AVAILABLE:
        raise RuntimeError(
            "sqlcipher3 がインストールされていません。\n"
            "  uv pip install sqlcipher3"
        )

    print(f"\n[INFO] 対象: {db_path}")

    if not db_path.exists():
        print("  [SKIP] ファイルが存在しません")
        return False

    if is_encrypted(db_path):
        print("  [SKIP] 既に暗号化済みです")
        return False

    plain_conn = _sqlite3.connect(db_path)
    plain_conn.row_factory = _sqlite3.Row
    tables = [
        r["name"]
        for r in plain_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    row_counts = {}
    for t in tables:
        count = plain_conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        row_counts[t] = count
        print(f"  テーブル: {t} ({count} 行)")

    if dry_run:
        print("  [dry-run] 変換をスキップしました")
        plain_conn.close()
        return True

    key = load_key()
    escaped = key.replace("'", "''")

    with tempfile.NamedTemporaryFile(
        suffix=".db", dir=db_path.parent, delete=False
    ) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        enc_conn = _sqlcipher3.connect(tmp_path)
        enc_conn.execute(f"PRAGMA key='{escaped}'")

        schema_rows = plain_conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE sql IS NOT NULL AND tbl_name != 'sqlite_sequence'"
            " ORDER BY rootpage"
        ).fetchall()
        for row in schema_rows:
            enc_conn.execute(row[0])
        enc_conn.commit()

        for t in tables:
            if t == "sqlite_sequence":
                continue
            rows = plain_conn.execute(f"SELECT * FROM [{t}]").fetchall()
            if not rows:
                continue
            placeholders = ", ".join(["?"] * len(rows[0]))
            enc_conn.executemany(
                f"INSERT INTO [{t}] VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
        enc_conn.commit()

        plain_conn.close()
        enc_conn.close()

        if backup:
            bak_path = db_path.with_suffix(".db.bak")
            shutil.copy2(db_path, bak_path)
            print(f"  バックアップ: {bak_path}")

        shutil.move(tmp_path, db_path)
        print(f"  [OK] 暗号化完了: {db_path}")

    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        plain_conn.close()
        print(f"  [ERROR] 変換失敗: {e}")
        return False

    # 検証
    try:
        conn = open_db(db_path)
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            expected = row_counts[t]
            if count != expected:
                print(f"  [WARN] {t}: 期待={expected}行, 実際={count}行")
            else:
                print(f"  検証OK: {t} ({count} 行)")
        conn.close()
    except Exception as e:
        print(f"  [ERROR] 検証失敗: {e}")
        return False

    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cli_utils import make_logger

    parser = argparse.ArgumentParser(description="db_utils CLI")
    parser.add_argument("--gen-key", action="store_true", help="暗号化鍵を生成して保存する")
    parser.add_argument("--show-key-path", action="store_true", help="鍵ファイルのパスを表示する")
    parser.add_argument("--migrate", nargs="+", metavar="DB", help="平文DBを SQLCipher 暗号化DBに変換する")
    parser.add_argument("--no-backup", action="store_true", help="--migrate 時にバックアップを作成しない")
    parser.add_argument("--dry-run", action="store_true", help="--migrate 時に変換せず確認のみ")
    parser.add_argument("--audit-log", action="store_true", help="audit_log を表示する")
    parser.add_argument("--db", default=None, metavar="PATH", help="--audit-log 時の pm.db パス（必須）")
    parser.add_argument("--no-encrypt", action="store_true", help="--audit-log 時に平文モードで接続する")
    parser.add_argument("--limit", type=int, default=30, metavar="N", help="--audit-log 時の表示件数（デフォルト: 30）")
    parser.add_argument("--source", metavar="SOURCE", help="--audit-log 時にソースで絞り込む（canvas_sync / relink）")
    parser.add_argument("--id", type=int, metavar="ID", help="--audit-log 時にアクションアイテムIDで絞り込む")
    parser.add_argument("--output", default=None, metavar="PATH", help="--audit-log 時に出力をファイルにも保存")
    args = parser.parse_args()

    if args.gen_key:
        if DEFAULT_KEY_FILE.exists():
            print(f"[WARN] 既に鍵ファイルが存在します: {DEFAULT_KEY_FILE}")
            ans = input("上書きしますか？ [y/N]: ").strip().lower()
            if ans != "y":
                print("キャンセルしました")
                sys.exit(0)
        key = gen_key()
        print(f"[OK] 鍵を生成しました: {DEFAULT_KEY_FILE}")
        print(f"     パーミッション: {oct(DEFAULT_KEY_FILE.stat().st_mode)}")
    elif args.show_key_path:
        print(DEFAULT_KEY_FILE)
    elif args.migrate:
        try:
            load_key()
            print(f"[INFO] 鍵ファイル: {DEFAULT_KEY_FILE}")
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print("\n鍵を生成するには: python3 scripts/db_utils.py --gen-key", file=sys.stderr)
            sys.exit(1)
        if args.dry_run:
            print("[INFO] --dry-run モード（変換しない）")
        success = skipped = 0
        for db_file in args.migrate:
            if migrate_db(Path(db_file), backup=not args.no_backup, dry_run=args.dry_run):
                success += 1
            else:
                skipped += 1
        print(f"\n完了: 変換={success}件, スキップ={skipped}件")
    elif args.audit_log:
        if not args.db:
            print("[ERROR] --db オプションが未指定です。対象DBを明示してください。", file=sys.stderr)
            print("  例: --db data/pm.db", file=sys.stderr)
            sys.exit(1)
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"ERROR: {db_path} が見つかりません", file=sys.stderr)
            sys.exit(1)
        conn = open_db(db_path, encrypt=not args.no_encrypt)
        # テーブルが存在するか確認
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone()
        if not exists:
            print("audit_log テーブルが存在しません。pm_sync_canvas.py または pm_relink.py を実行すると自動作成されます。")
            conn.close()
            sys.exit(0)
        where_clauses = []
        params: list = []
        if args.source:
            where_clauses.append("source = ?")
            params.append(args.source)
        if args.id:
            where_clauses.append("record_id = ?")
            params.append(str(args.id))
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(args.limit)
        rows = conn.execute(
            f"SELECT changed_at, source, record_id, field, old_value, new_value "
            f"FROM audit_log {where} ORDER BY changed_at DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
        log, close_log = make_logger(args.output)
        if not rows:
            log("該当する変更履歴はありません。")
            close_log()
            sys.exit(0)
        log(f"{'日時':20s}  {'ソース':12s}  {'ID':4s}  {'フィールド':15s}  {'変更前':20s}  変更後")
        log("-" * 90)
        for r in rows:
            dt = r["changed_at"][:19].replace("T", " ")
            old = str(r["old_value"]) if r["old_value"] is not None else "NULL"
            new = str(r["new_value"]) if r["new_value"] is not None else "NULL"
            log(f"{dt:20s}  {r['source']:12s}  {r['record_id']:4s}  {r['field']:15s}  {old:20s}  {new}")
        close_log()
    else:
        parser.print_help()
