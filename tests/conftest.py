"""Pytest configuration and shared fixtures."""
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure scripts/ and scripts/argus/, scripts/data-pipeline/ are on sys.path
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_SCRIPTS_ARGUS = _SCRIPTS / "argus"
_SCRIPTS_DATA_PIPELINE = _SCRIPTS / "data-pipeline"
for _p in (_SCRIPTS_ARGUS, _SCRIPTS_DATA_PIPELINE, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --------------------------------------------------------------------------- #
# Environment: avoid touching production DBs during tests
# --------------------------------------------------------------------------- #

# 実行環境（qa デーモン起動シェル等）から流入しうる実験・運用フラグ。
# ~/.claude/settings.json の env 経由で Claude Code セッションのシェルにも展開されるため、
# テストが one-shot 分岐や再ランキング無効化等の意図しない経路に入らないよう全テスト前に除去する。
_LEAKY_ENV_VARS = (
    "ARGUS_ONESHOT",
    "ARGUS_ONESHOT_TOP_K",
    "ARGUS_ONESHOT_CHAR_BUDGET",
    "ARGUS_ONESHOT_MAX_TOKENS",
    "ARGUS_ONESHOT_LLM_URL",
    "ARGUS_ONESHOT_LLM_MODEL",
    "ARGUS_ONESHOT_LLM_TOKEN",
    "ARGUS_ONESHOT_LLM_TEMPERATURE",
    "ARGUS_PRESERVE_REASONING",
    "ARGUS_REASONING_EFFORT",
    "ARGUS_LLM_TEMPERATURE",
    "ARGUS_INVESTIGATE_TIMEOUT",
    "ARGUS_STEP_THINK",
    "ARGUS_STEP_MAX_TOKENS",
    "ARGUS_DISABLE_LLM_RERANK",
    "ARGUS_TOP_K_RERANK",
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Redirect all DB / log paths to a per-test tmp dir.

    Many modules read paths from env vars at import time OR at call time.
    We set the common ones defensively here.
    """
    for _var in _LEAKY_ENV_VARS:
        monkeypatch.delenv(_var, raising=False)
    # net_guard（scripts/utils/net_guard.py）はプロセスグローバルな socket フックなので、
    # テスト中は無効化する（tests/utils/test_net_guard.py はフックの挙動そのものを
    # 検証するため、各テスト内で ARGUS_NETGUARD を明示的に上書きしてこの既定を覆す）。
    monkeypatch.setenv("ARGUS_NETGUARD", "off")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LOCAL_LLM_TOKEN", "dummy")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setenv("RIVAULT_URL", "")
    monkeypatch.setenv("RIVAULT_TOKEN", "")
    monkeypatch.setenv("RIVAULT_MODEL", "")
    monkeypatch.setenv("ARGUS_PREFER_RIVAULT", "0")
    monkeypatch.setenv("ARGUS_SKIP_LLM_SECRETS", "1")
    try:
        from utils import llm as _llm
        monkeypatch.setattr(_llm, "_llm_secrets_mtime_cache", None)
    except ImportError:
        pass
    yield


# --------------------------------------------------------------------------- #
# Fixture: redirect the audit-DB funnel (slack_post._open_audit_conn) away
# from the production data/pm.db
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_audit_conn(tmp_path, monkeypatch):
    """`utils.slack_post._open_audit_conn()` を一時 DB へ差し替える。

    `post_message` 等の Slack 投稿ファネル、および `canvas_utils.py` /
    `box_cli.py` のガード関数は、いずれも conn 未指定時にこの関数を呼んで
    自前で監査用 pm.db 接続を開く（差し替え点は1箇所に集約済み）。ここを
    monkeypatch しないと、テストがこれらのファネルを呼ぶだけで本番
    `data/pm.db` の `tool_calls` にテスト由来の行が書き込まれてしまう
    （実測: コミット b6b95e5 で本番 tool_calls 72 行がすべてテスト由来だった）。

    `canvas_utils.py` / `box_cli.py` はガード関数内で `utils.slack_post` を
    遅延 import してから `_open_audit_conn` を参照するため、モジュール import
    のタイミングに関わらずここでの monkeypatch が効く。

    `None` を返すと canary 検査・egress 記録そのものが無効化されてしまい、
    「検査が効いている」ことを前提にしたテストが回帰を見逃す。そのため
    必ず有効な一時 DB 接続を返す。
    """
    audit_db_path = tmp_path / "_audit_pm.db"

    def _fake_open_audit_conn():
        conn = sqlite3.connect(str(audit_db_path))
        conn.row_factory = sqlite3.Row
        return conn

    from utils import slack_post
    monkeypatch.setattr(slack_post, "_open_audit_conn", _fake_open_audit_conn)
    yield


# --------------------------------------------------------------------------- #
# Fixture: in-memory pm.db via init_pm_db schema
# --------------------------------------------------------------------------- #

from db_utils import init_pm_db

_PM_DB_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id   TEXT PRIMARY KEY,
    name      TEXT,
    imported_at TEXT
);
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id     TEXT PRIMARY KEY,
    goal_id          TEXT,
    name             TEXT,
    due_date         TEXT,
    area             TEXT,
    status           TEXT DEFAULT 'active',
    success_criteria TEXT,
    imported_at      TEXT
);
"""

_PM_DB_EXTRA_MIGRATIONS = [
    # open_pm_db にのみあるマイグレーション
    "ALTER TABLE decisions ADD COLUMN acknowledged_at TEXT",
]


@pytest.fixture
def pm_db_path(tmp_path: Path) -> Path:
    """Return path to a freshly-created pm.db (plain sqlite, no SQLCipher)."""
    import sqlite3 as _sqlite3
    p = tmp_path / "pm.db"
    init_pm_db(p, no_encrypt=True)
    # Apply extra schema (milestones, goals) and migrations not in init_pm_db
    conn = _sqlite3.connect(str(p))
    for stmt in _PM_DB_EXTRA_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                conn.execute(stmt)
            except Exception:
                pass
    for sql in _PM_DB_EXTRA_MIGRATIONS:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()
    conn.close()
    return p


# --------------------------------------------------------------------------- #
# AgentContext fixture for tool tests
# --------------------------------------------------------------------------- #

@pytest.fixture
def agent_context(pm_db_path: Path, tmp_path: Path):
    """Build a minimal AgentContext backed by an in-memory pm.db."""
    from argus.pm_argus_agent import AgentContext
    conn = sqlite3.connect(pm_db_path)
    conn.row_factory = sqlite3.Row
    ctx = AgentContext(
        conns=[conn],
        today="2026-06-19",
        since="2026-01-01",
        no_encrypt=False,
        data_dir=tmp_path,
        minutes_dir=tmp_path / "minutes",
        index_db=tmp_path / "qa_index.db",
        index_name="test",
        channels=[],
        cited_chunks=[],
    )
    yield ctx
    conn.close()
