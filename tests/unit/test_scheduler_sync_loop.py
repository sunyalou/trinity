"""
Regression test for #420 — scheduler sync feedback loop.

Pre-fix: `update_schedule_run_times` unconditionally bumped `updated_at`,
which `_sync_agent_schedules` then detected as a config change, triggering
`_add_job` which called `update_schedule_run_times`, looping forever at the
sync-interval cadence (default 60s).

Post-fix: `update_schedule_run_times` is bookkeeping-only. It must not touch
`updated_at` — that column is the sync loop's config-change signal.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest


def _load_scheduler_db_module():
    """Direct-load scheduler/database.py — runs without backend deps."""
    candidates = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "scheduler")),
        "/app/scheduler",
    ]
    sched_dir = next(
        (p for p in candidates if os.path.isfile(os.path.join(p, "database.py"))), None
    )
    assert sched_dir, "Could not locate src/scheduler/database.py"

    # Scheduler package has relative imports, so we need it on sys.path.
    parent = os.path.dirname(sched_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec = importlib.util.spec_from_file_location(
        "scheduler.database", os.path.join(sched_dir, "database.py"),
        submodule_search_locations=[sched_dir],
    )
    mod = importlib.util.module_from_spec(spec)
    # Register the parent package first so relative imports resolve.
    sys.modules.setdefault("scheduler", sys.modules.get("scheduler") or importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("scheduler", os.path.join(sched_dir, "__init__.py"))
    ))
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Fallback: manually register module then execute
        sys.modules["scheduler.database"] = mod
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scheduler_db(monkeypatch):
    """Isolated SchedulerDatabase bound to a temp SQLite file."""
    db_module = _load_scheduler_db_module()

    db_file = tempfile.NamedTemporaryFile(suffix="_sched_test.db", delete=False)
    db_file.close()
    db_path = db_file.name

    # Build minimal schema the scheduler expects.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            timezone TEXT DEFAULT 'UTC',
            description TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT,
            model TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    # SchedulerDatabase accepts an explicit path, bypassing `config.database_path`.
    db = db_module.SchedulerDatabase(database_path=db_path)

    # Seed one schedule with a known updated_at.
    baseline = datetime(2026, 1, 1, 12, 0, 0)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO agent_schedules
            (id, agent_name, name, cron_expression, message, enabled, timezone,
             owner_id, created_at, updated_at, last_run_at, next_run_at)
        VALUES (?, ?, ?, ?, ?, 1, 'UTC', 1, ?, ?, NULL, NULL)
        """,
        ("sched-1", "agent-a", "Hourly", "0 * * * *", "ping",
         baseline.isoformat(), baseline.isoformat()),
    )
    conn.commit()
    conn.close()

    yield db, db_path, baseline
    os.unlink(db_path)


def _get_updated_at(db_path: str, schedule_id: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT updated_at FROM agent_schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    return row[0]


def test_update_run_times_does_not_bump_updated_at(scheduler_db):
    """#420 regression: run-time updates are bookkeeping; updated_at must stay."""
    db, db_path, baseline = scheduler_db

    next_run = baseline + timedelta(hours=1)
    ok = db.update_schedule_run_times("sched-1", next_run_at=next_run)
    assert ok is True

    assert _get_updated_at(db_path, "sched-1") == baseline.isoformat(), (
        "update_schedule_run_times must not modify updated_at — sync loop would "
        "misread that as a config change and re-add the job every tick (#420)"
    )


def test_update_run_times_persists_next_run_at(scheduler_db):
    """Sanity: the call still does its job for next_run_at."""
    db, db_path, _ = scheduler_db
    next_run = datetime(2026, 6, 1, 12, 0, 0)
    db.update_schedule_run_times("sched-1", next_run_at=next_run)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT next_run_at FROM agent_schedules WHERE id = ?", ("sched-1",)
    ).fetchone()
    conn.close()
    assert row[0] == next_run.isoformat()


def test_update_run_times_persists_last_run_at(scheduler_db):
    """Sanity: the call still does its job for last_run_at."""
    db, db_path, _ = scheduler_db
    last_run = datetime(2026, 6, 1, 11, 0, 0)
    db.update_schedule_run_times("sched-1", last_run_at=last_run)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT last_run_at FROM agent_schedules WHERE id = ?", ("sched-1",)
    ).fetchone()
    conn.close()
    assert row[0] == last_run.isoformat()


def test_update_run_times_noop_when_both_args_none(scheduler_db):
    """Defensive: calling with no args is a no-op, not a bare `UPDATE SET ,`."""
    db, db_path, baseline = scheduler_db
    result = db.update_schedule_run_times("sched-1")
    assert result is False
    assert _get_updated_at(db_path, "sched-1") == baseline.isoformat()
