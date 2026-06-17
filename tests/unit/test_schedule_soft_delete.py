"""
Tests for schedule soft-delete + retention purge (Issue #834 Phase 1b).

Backend-agnostic via ``db_harness`` (#300): every test runs on SQLite and,
when ``TEST_POSTGRES_URL`` is set, PostgreSQL too. Schema comes from the
canonical Core metadata (no hand-rolled DDL); the ``_seed_*`` / ``_count`` /
``_deleted_at`` helpers write/read through the active engine so they hit
whichever backend ``db_backend`` selected.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import (  # noqa: E402
    db_backend,
    seed_user,
    run as _hrun,
    scalar as _hscalar,
)


# Sibling tests stub `sys.modules["db.<sub>"]` with importlib-loaded modules
# bound to *their* tmp DBs and never restore on teardown. Snapshot + pop any
# stale stubs so this file's imports re-resolve fresh, and restore on teardown
# so we don't pollute siblings either. (Precedent: test_agent_cleanup_parity.)
_STUBBED_MODULE_NAMES = [
    "db.schedules",
    "db.users",
    "db.agents",
    "db.monitoring",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    for name in _STUBBED_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def tmp_schedule_db(db_backend):
    """Active backend with a fresh schema; seeds the baseline owner (id=1) and
    admin (id=2) the old hand-rolled schema created. Returns the backend marker
    (kept as the leading positional arg the seed/count helpers accept)."""
    seed_user(1, "owner", "user")
    seed_user(2, "admin", "admin")
    return db_backend


@pytest.fixture
def schedule_ops(tmp_schedule_db):
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    agent_ops = AgentOperations(user_ops)
    return ScheduleOperations(user_ops, agent_ops)


# ---------------------------------------------------------------------------
# Engine-based seed/read helpers. First positional arg is the backend marker
# from `tmp_schedule_db` (ignored — all I/O goes through the active engine).
# ---------------------------------------------------------------------------

def _seed_schedule(_db, sid: str, agent_name: str = "agent-1",
                   enabled: bool = True, deleted_at: str | None = None,
                   webhook_token: str | None = None):
    # Live owner row so the agent-level JOIN in list_all_enabled_schedules()
    # passes (these tests exercise *schedule* soft-delete; the agent stays
    # live). ON CONFLICT DO NOTHING works on both SQLite and PostgreSQL.
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:a, 1, '2026-01-01T00:00:00Z', NULL) "
        "ON CONFLICT (agent_name) DO NOTHING",
        a=agent_name,
    )
    _hrun(
        "INSERT INTO agent_schedules "
        "(id, agent_name, name, cron_expression, message, enabled, owner_id, "
        " created_at, updated_at, deleted_at, webhook_token, webhook_enabled) "
        "VALUES (:id, :a, 'sched', '0 0 * * *', 'hi', :en, 1, "
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', :deleted, :wt, :we)",
        id=sid, a=agent_name, en=1 if enabled else 0, deleted=deleted_at,
        wt=webhook_token, we=1 if webhook_token else 0,
    )
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES (:id, :sid, :a, 'completed', '2026-01-01T00:00:00Z', 'hi', 'schedule')",
        id=f"exec-{sid}", sid=sid, a=agent_name,
    )


def _count(_db, table: str, where: str = "1=1", params: tuple = ()) -> int:
    # Translate the legacy ?-placeholder where-clauses to named binds so the
    # same call sites work on both backends.
    binds = {f"p{i}": v for i, v in enumerate(params)}
    clause = where
    for i in range(len(params)):
        clause = clause.replace("?", f":p{i}", 1)
    return _hscalar(f"SELECT COUNT(*) FROM {table} WHERE {clause}", **binds) or 0


def _deleted_at(_db, sid: str):
    return _hscalar(
        "SELECT deleted_at FROM agent_schedules WHERE id = :sid", sid=sid
    )


# -----------------------------------------------------------------------------
# delete_schedule → soft-delete
# -----------------------------------------------------------------------------

def test_delete_schedule_sets_deleted_at(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1")

    assert schedule_ops.delete_schedule("sid-1", "owner") is True

    # Row still present, deleted_at set
    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("sid-1",)) == 1
    assert _deleted_at(tmp_schedule_db, "sid-1") is not None
    # Execution row preserved (KEEP-policy via subscription rollup parity)
    assert _count(tmp_schedule_db, "schedule_executions", "schedule_id=?", ("sid-1",)) == 1


def test_delete_schedule_idempotent(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")

    # Second delete on already-soft-deleted row → True (idempotent)
    assert schedule_ops.delete_schedule("sid-1", "owner") is True


def test_delete_schedule_rejects_non_owner(tmp_schedule_db, schedule_ops):
    """A non-owner non-admin can't soft-delete someone else's schedule."""
    _seed_schedule(tmp_schedule_db, "sid-1")
    # Create another user
    seed_user(3, "eve", "user")

    assert schedule_ops.delete_schedule("sid-1", "eve") is False
    assert _deleted_at(tmp_schedule_db, "sid-1") is None  # still live


def test_admin_can_soft_delete_any_schedule(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1")
    assert schedule_ops.delete_schedule("sid-1", "admin") is True
    assert _deleted_at(tmp_schedule_db, "sid-1") is not None


# -----------------------------------------------------------------------------
# Read paths excluding soft-deleted
# -----------------------------------------------------------------------------

def test_get_schedule_returns_none_for_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")
    assert schedule_ops.get_schedule("sid-1") is None


def test_list_agent_schedules_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-1")
    _seed_schedule(tmp_schedule_db, "deleted-1", deleted_at="2026-01-01T00:00:00Z")

    schedules = schedule_ops.list_agent_schedules("agent-1")
    ids = {s.id for s in schedules}
    assert "live-1" in ids
    assert "deleted-1" not in ids


def test_list_enabled_schedules_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    """Critical for the scheduler firing list — soft-deleted must NOT appear."""
    _seed_schedule(tmp_schedule_db, "live-on", enabled=True)
    _seed_schedule(tmp_schedule_db, "deleted-on", enabled=True,
                   deleted_at="2026-01-01T00:00:00Z")
    _seed_schedule(tmp_schedule_db, "live-off", enabled=False)

    schedules = schedule_ops.list_all_enabled_schedules()
    ids = {s.id for s in schedules}
    assert "live-on" in ids
    assert "deleted-on" not in ids
    assert "live-off" not in ids  # disabled — not enabled


def test_get_schedule_by_webhook_token_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-wh", webhook_token="tok-live")
    _seed_schedule(tmp_schedule_db, "del-wh",
                   webhook_token="tok-del", deleted_at="2026-01-01T00:00:00Z")

    assert schedule_ops.get_schedule_by_webhook_token("tok-live") is not None
    assert schedule_ops.get_schedule_by_webhook_token("tok-del") is None


# -----------------------------------------------------------------------------
# purge_schedule + retention sweep helpers
# -----------------------------------------------------------------------------

def test_purge_schedule_removes_row_and_executions(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")
    assert schedule_ops.purge_schedule("sid-1") is True

    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("sid-1",)) == 0
    # Executions for this schedule_id also gone
    assert _count(tmp_schedule_db, "schedule_executions", "schedule_id=?", ("sid-1",)) == 0


def test_purge_schedule_refuses_live_row(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-1")  # deleted_at NULL
    assert schedule_ops.purge_schedule("live-1") is False
    # Untouched
    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("live-1",)) == 1


def test_find_soft_deleted_schedules_respects_cutoff(tmp_schedule_db, schedule_ops):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _seed_schedule(tmp_schedule_db, "live")
    _seed_schedule(tmp_schedule_db, "old-ghost", deleted_at=old)
    _seed_schedule(tmp_schedule_db, "recent-ghost", deleted_at=recent)

    eligible = schedule_ops.find_soft_deleted_schedules_past_retention(retention_days=30)
    assert "old-ghost" in eligible
    assert "recent-ghost" not in eligible
    assert "live" not in eligible


def test_find_soft_deleted_disabled_when_retention_zero(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "ancient", deleted_at="2020-01-01T00:00:00Z")
    assert schedule_ops.find_soft_deleted_schedules_past_retention(retention_days=0) == []
