"""
Tests for admin soft-delete recovery (#834 Phase 1c).

Exercises `AgentOperations.recover_agent_ownership` / `list_soft_deleted_agents`
and `ScheduleOperations.recover_schedule` / `list_soft_deleted_schedules`.

Backend-agnostic via ``db_harness`` (#300): every test runs on SQLite and,
when ``TEST_POSTGRES_URL`` is set, PostgreSQL too. Schema is the full
production schema; the ``_seed_*`` helpers + inline reads go through the
active engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_user, run as _hrun, scalar as _hscalar  # noqa: E402


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema; seeds owner (id=1) + admin
    (id=2). Returns the backend marker (leading positional arg for helpers)."""
    seed_user(1, "owner", "user")
    seed_user(2, "admin", "admin")
    return db_backend


@pytest.fixture
def agent_ops(tmp_db):
    try:
        from db.agents import AgentOperations
        from db.users import UserOperations
    except ImportError:
        pytest.skip("backend venv required")
    return AgentOperations(UserOperations())


@pytest.fixture
def schedule_ops(tmp_db):
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    return ScheduleOperations(user_ops, AgentOperations(user_ops))


def _seed_agent(_db, name: str, deleted_at: str | None = None):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z', :deleted)",
        n=name, deleted=deleted_at,
    )


def _seed_schedule(_db, sid: str, deleted_at: str | None = None):
    _hrun(
        "INSERT INTO agent_schedules "
        "(id, agent_name, name, cron_expression, message, enabled, owner_id, "
        " created_at, updated_at, deleted_at) "
        "VALUES (:id, 'agent-1', 'sched', '0 0 * * *', 'hi', 1, 1, "
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', :deleted)",
        id=sid, deleted=deleted_at,
    )


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------

def test_recover_agent_clears_deleted_at(tmp_db, agent_ops):
    _seed_agent(tmp_db, "ghost", deleted_at="2026-01-01T00:00:00Z")
    assert agent_ops.recover_agent_ownership("ghost") is True

    assert _hscalar(
        "SELECT deleted_at FROM agent_ownership WHERE agent_name = :n", n="ghost"
    ) is None


def test_recover_agent_refuses_live_row(tmp_db, agent_ops):
    """A row that isn't soft-deleted isn't a recovery target."""
    _seed_agent(tmp_db, "alive")  # deleted_at NULL
    assert agent_ops.recover_agent_ownership("alive") is False


def test_recover_agent_returns_false_for_nonexistent(tmp_db, agent_ops):
    assert agent_ops.recover_agent_ownership("never_existed") is False


def test_list_soft_deleted_agents(tmp_db, agent_ops):
    _seed_agent(tmp_db, "alive")  # excluded
    _seed_agent(tmp_db, "ghost-1", deleted_at="2026-01-01T00:00:00Z")
    _seed_agent(tmp_db, "ghost-2", deleted_at="2026-01-02T00:00:00Z")

    rows = agent_ops.list_soft_deleted_agents(limit=10)
    names = {r["agent_name"] for r in rows}
    assert names == {"ghost-1", "ghost-2"}
    # Newest-first order
    assert rows[0]["agent_name"] == "ghost-2"


def test_list_soft_deleted_agents_respects_limit(tmp_db, agent_ops):
    for i in range(5):
        _seed_agent(tmp_db, f"g-{i}", deleted_at=f"2026-01-0{i+1}T00:00:00Z")

    rows = agent_ops.list_soft_deleted_agents(limit=3)
    assert len(rows) == 3


# -----------------------------------------------------------------------------
# Schedules
# -----------------------------------------------------------------------------

def test_recover_schedule_clears_deleted_at(tmp_db, schedule_ops):
    _seed_schedule(tmp_db, "s-1", deleted_at="2026-01-01T00:00:00Z")
    assert schedule_ops.recover_schedule("s-1") is True

    assert _hscalar(
        "SELECT deleted_at FROM agent_schedules WHERE id = :id", id="s-1"
    ) is None


def test_recover_schedule_refuses_live(tmp_db, schedule_ops):
    _seed_schedule(tmp_db, "s-live")  # deleted_at NULL
    assert schedule_ops.recover_schedule("s-live") is False


def test_recover_schedule_returns_false_for_nonexistent(tmp_db, schedule_ops):
    assert schedule_ops.recover_schedule("never") is False


def test_list_soft_deleted_schedules_unscoped(tmp_db, schedule_ops):
    _seed_schedule(tmp_db, "live")
    _seed_schedule(tmp_db, "ghost-1", deleted_at="2026-01-01T00:00:00Z")
    _seed_schedule(tmp_db, "ghost-2", deleted_at="2026-01-02T00:00:00Z")

    rows = schedule_ops.list_soft_deleted_schedules()
    ids = {r["id"] for r in rows}
    assert ids == {"ghost-1", "ghost-2"}


def test_list_soft_deleted_schedules_scoped_to_agent(tmp_db, schedule_ops):
    """`agent_name=` filter limits results to one agent's soft-deleted
    schedules — the URL-pattern the admin endpoint exposes."""
    # Insert one for default 'agent-1' and one for a different agent
    _seed_schedule(tmp_db, "a1-ghost", deleted_at="2026-01-01T00:00:00Z")
    _hrun(
        "INSERT INTO agent_schedules "
        "(id, agent_name, name, cron_expression, message, enabled, owner_id, "
        " created_at, updated_at, deleted_at) "
        "VALUES ('a2-ghost', 'agent-2', 'sched', '0 0 * * *', 'hi', 1, 1, "
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )

    rows = schedule_ops.list_soft_deleted_schedules(agent_name="agent-1")
    ids = {r["id"] for r in rows}
    assert ids == {"a1-ghost"}, f"got {ids}"
