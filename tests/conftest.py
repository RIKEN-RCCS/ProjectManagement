"""Pytest configuration and shared fixtures."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure scripts/ and scripts/argus/ are on sys.path
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_SCRIPTS_ARGUS = _SCRIPTS / "argus"
for _p in (_SCRIPTS_ARGUS, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --------------------------------------------------------------------------- #
# Environment: avoid touching production DBs during tests
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Redirect all DB / log paths to a per-test tmp dir.

    Many modules read paths from env vars at import time OR at call time.
    We set the common ones defensively here.
    """
    monkeypatch.setenv("LOCAL_LLM_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LOCAL_LLM_TOKEN", "dummy")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setenv("RIVAULT_URL", "")
    monkeypatch.setenv("RIVAULT_TOKEN", "")
    monkeypatch.setenv("RIVAULT_MODEL", "")
    monkeypatch.setenv("ARGUS_PREFER_RIVAULT", "0")
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
