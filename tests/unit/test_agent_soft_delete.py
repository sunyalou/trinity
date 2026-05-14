"""
Tests for agent soft-delete + retention purge (Issue #834 Phase 1a).

Loads `db/agents.py` (specifically `AgentOperations.delete_agent_ownership`
+ `purge_agent_ownership` + `find_soft_deleted_agents_past_retention`) and
`db/agent_cleanup.py` (the #816 cascade_delete primitive that purge
chains into) against an ephemeral SQLite DB. Stays stdlib-only via the
same importlib trick the #816 backfill script uses — agent_service
package init eagerly pulls docker / fastapi.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load(name: str, rel: str):
    """Load a backend module by file path so test stays stdlib-only."""
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path) -> str:
    """Minimal schema covering agent_ownership + a sample of child tables."""
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    # Child tables from #816 registry — to verify cascade fires on purge.
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        cur.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT)")
    cur.execute(
        "CREATE TABLE schedule_executions (id INTEGER PRIMARY KEY, agent_name TEXT)"  # KEEP-policy
    )
    cur.execute(
        "CREATE TABLE nevermined_payment_log (id INTEGER PRIMARY KEY, agent_name TEXT)"  # KEEP
    )
    cur.execute(
        "CREATE TABLE mcp_api_keys (id INTEGER PRIMARY KEY, agent_name TEXT, scope TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_permissions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT, target_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_event_subscriptions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_agent TEXT, source_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_events "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT)"
    )
    cur.execute("CREATE INDEX idx_agent_ownership_deleted_at ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL")
    conn.commit()
    conn.close()
    return str(db_path)


def _seed(conn, name: str, deleted_at: str | None = None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_ownership(agent_name, owner_id, created_at, deleted_at) "
        "VALUES (?, 1, '2026-01-01T00:00:00Z', ?)",
        (name, deleted_at),
    )
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        cur.execute(f"INSERT INTO {t}(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO schedule_executions(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO nevermined_payment_log(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO mcp_api_keys(agent_name, scope) VALUES (?, 'agent')", (name,))
    conn.commit()


def _count_owner_row(conn, name: str) -> int:
    return conn.cursor().execute(
        "SELECT COUNT(*) FROM agent_ownership WHERE agent_name = ?", (name,)
    ).fetchone()[0]


def _deleted_at(conn, name: str):
    row = conn.cursor().execute(
        "SELECT deleted_at FROM agent_ownership WHERE agent_name = ?", (name,)
    ).fetchone()
    return row[0] if row else None


# -----------------------------------------------------------------------------
# delete_agent_ownership → soft-delete
# -----------------------------------------------------------------------------

def test_delete_sets_deleted_at(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)

    # Load utils.helpers first so we get the same utc_now_iso the prod code uses
    helpers = _load("utils.helpers", "utils/helpers.py")
    # Then load connection + agents
    _load("db.connection", "db/connection.py")
    # AgentOperations imports from .agent_settings — needs the package on sys.path
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("agent_settings package depends on pydantic — backend venv required")
        return

    user_ops = users_mod.UserOperations()
    ops = AgentOperations(user_ops)

    conn = sqlite3.connect(db_path)
    _seed(conn, "doomed")
    conn.close()

    assert ops.delete_agent_ownership("doomed") is True

    conn = sqlite3.connect(db_path)
    assert _count_owner_row(conn, "doomed") == 1, "row must persist after soft-delete"
    assert _deleted_at(conn, "doomed") is not None, "deleted_at must be set"

    # Children untouched
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags",
              "schedule_executions", "nevermined_payment_log"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE agent_name=?", ("doomed",)).fetchone()[0]
        assert n == 1, f"{t} child row must survive soft-delete"


def test_delete_idempotent_on_already_soft_deleted(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)
    _load("utils.helpers", "utils/helpers.py")
    _load("db.connection", "db/connection.py")
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("backend venv required")
        return

    ops = AgentOperations(users_mod.UserOperations())

    conn = sqlite3.connect(db_path)
    _seed(conn, "twice", deleted_at="2026-01-01T00:00:00Z")
    conn.close()

    # Second delete on already-deleted row → True (idempotent)
    assert ops.delete_agent_ownership("twice") is True
    # Nonexistent → False
    assert ops.delete_agent_ownership("ghost") is False


# -----------------------------------------------------------------------------
# purge_agent_ownership → cascade_delete + final row removal
# -----------------------------------------------------------------------------

def test_purge_runs_cascade_and_removes_owner_row(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)
    _load("utils.helpers", "utils/helpers.py")
    _load("db.connection", "db/connection.py")
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("backend venv required")
        return
    ops = AgentOperations(users_mod.UserOperations())

    conn = sqlite3.connect(db_path)
    _seed(conn, "purgeme", deleted_at="2026-01-01T00:00:00Z")
    conn.close()

    assert ops.purge_agent_ownership("purgeme") is True

    conn = sqlite3.connect(db_path)
    # Owner row gone
    assert _count_owner_row(conn, "purgeme") == 0
    # CASCADE children gone
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE agent_name=?", ("purgeme",)).fetchone()[0]
        assert n == 0, f"{t} should be wiped after purge"
    # KEEP children survive (billing rollups)
    assert conn.execute("SELECT COUNT(*) FROM schedule_executions WHERE agent_name=?", ("purgeme",)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM nevermined_payment_log WHERE agent_name=?", ("purgeme",)).fetchone()[0] == 1


def test_purge_refuses_live_agent(tmp_path, monkeypatch):
    """Safety: can't purge a row that hasn't been soft-deleted first."""
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)
    _load("utils.helpers", "utils/helpers.py")
    _load("db.connection", "db/connection.py")
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("backend venv required")
        return
    ops = AgentOperations(users_mod.UserOperations())

    conn = sqlite3.connect(db_path)
    _seed(conn, "alive")  # deleted_at IS NULL
    conn.close()

    # Refuses — returns False, leaves everything intact
    assert ops.purge_agent_ownership("alive") is False
    conn = sqlite3.connect(db_path)
    assert _count_owner_row(conn, "alive") == 1
    assert conn.execute("SELECT COUNT(*) FROM agent_sharing WHERE agent_name=?", ("alive",)).fetchone()[0] == 1


# -----------------------------------------------------------------------------
# find_soft_deleted_agents_past_retention
# -----------------------------------------------------------------------------

def test_find_soft_deleted_respects_cutoff(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)
    _load("utils.helpers", "utils/helpers.py")
    _load("db.connection", "db/connection.py")
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("backend venv required")
        return
    ops = AgentOperations(users_mod.UserOperations())

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    conn = sqlite3.connect(db_path)
    _seed(conn, "live")  # not soft-deleted
    _seed(conn, "old_ghost", deleted_at=old)
    _seed(conn, "recent_ghost", deleted_at=recent)
    conn.close()

    # 30-day retention: only the 60-day-old ghost qualifies
    eligible = ops.find_soft_deleted_agents_past_retention(retention_days=30)
    assert "old_ghost" in eligible
    assert "recent_ghost" not in eligible
    assert "live" not in eligible


def test_find_soft_deleted_disabled_when_retention_zero(tmp_path, monkeypatch):
    """retention_days=0 disables the sweep — return [] regardless of state."""
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("TRINITY_DB_PATH", db_path)
    _load("utils.helpers", "utils/helpers.py")
    _load("db.connection", "db/connection.py")
    sys.path.insert(0, str(_BACKEND))
    try:
        from db.agents import AgentOperations
        from db import users as users_mod
    except ImportError:
        pytest.skip("backend venv required")
        return
    ops = AgentOperations(users_mod.UserOperations())

    conn = sqlite3.connect(db_path)
    _seed(conn, "ancient", deleted_at="2020-01-01T00:00:00Z")
    conn.close()

    assert ops.find_soft_deleted_agents_past_retention(retention_days=0) == []
