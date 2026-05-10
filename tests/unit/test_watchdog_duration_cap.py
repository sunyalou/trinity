"""Regression tests for #767 — restart recovery inflating duration_ms on timeline.

When the backend restarts mid-execution, recover_orphaned_executions() calls
mark_execution_failed_by_watchdog() which previously used now() (restart time)
as completed_at. For executions created hours before restart, this produced
enormous duration_ms values that rendered as huge red blocks on the timeline.

Fix: completed_at = min(now, started_at + effective_timeout)
Timeout resolution: schedule.timeout_seconds → agent_ownership.execution_timeout_seconds → 900 s

Issue: https://github.com/abilityai/trinity/issues/767
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap — same shadow-handling pattern as test_cancelled_not_overwritten.py
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Schema with schedule_executions, agent_schedules, and agent_ownership."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL DEFAULT '',
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'scheduler',
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            tool_calls TEXT,
            execution_log TEXT,
            claude_session_id TEXT,
            compact_metadata TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            cron_expression TEXT NOT NULL DEFAULT '0 * * * *',
            message TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1,
            timezone TEXT DEFAULT 'UTC',
            owner_id INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            timeout_seconds INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT '1',
            execution_timeout_seconds INTEGER DEFAULT 900
        )
    """)
    conn.commit()
    conn.close()

    def _evict():
        for mod in ("db.connection", "db.schedules", "database"):
            sys.modules.pop(mod, None)

    _evict()
    try:
        yield db_path
    finally:
        _evict()


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations
    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_execution(
    db_path: Path,
    *,
    execution_id: str,
    agent_name: str = "agent-a",
    schedule_id: str | None = None,
    started_at: datetime,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES (?, ?, ?, 'running', ?, '', 'scheduler')",
        (execution_id, schedule_id, agent_name, started_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')),
    )
    conn.commit()
    conn.close()


def _insert_schedule(db_path: Path, *, schedule_id: str, agent_name: str, timeout_seconds: int | None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO agent_schedules (id, agent_name, timeout_seconds) VALUES (?, ?, ?)",
        (schedule_id, agent_name, timeout_seconds),
    )
    conn.commit()
    conn.close()


def _insert_agent(db_path: Path, *, agent_name: str, execution_timeout_seconds: int | None = 900) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, execution_timeout_seconds) VALUES (?, ?)",
        (agent_name, execution_timeout_seconds),
    )
    conn.commit()
    conn.close()


def _get_row(db_path: Path, execution_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, completed_at, duration_ms FROM schedule_executions WHERE id = ?",
        (execution_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWatchdogDurationCap:
    """mark_execution_failed_by_watchdog must cap duration_ms at effective timeout."""

    def test_duration_capped_when_execution_older_than_timeout(self, tmp_db, schedule_ops):
        """Core regression: an execution started 2 hours ago must not show 2h duration."""
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        _insert_execution(tmp_db, execution_id="exec-old", agent_name="agent-a", started_at=started)
        _insert_agent(tmp_db, agent_name="agent-a", execution_timeout_seconds=900)

        ok = schedule_ops.mark_execution_failed_by_watchdog(
            execution_id="exec-old",
            error_message="Execution orphaned — recovered on backend restart",
        )

        assert ok is True
        row = _get_row(tmp_db, "exec-old")
        assert row["status"] == "failed"
        # 900 s ceiling — must NOT be anywhere near 7200 s (2 hours)
        assert row["duration_ms"] <= 900_000, (
            f"duration_ms={row['duration_ms']} exceeds 900 s ceiling — "
            "restart recovery is inflating failure duration"
        )

    def test_duration_uses_schedule_timeout_when_present(self, tmp_db, schedule_ops):
        """Schedule-level timeout_seconds takes priority over agent default."""
        started = datetime.now(timezone.utc) - timedelta(hours=3)
        _insert_schedule(tmp_db, schedule_id="sched-1", agent_name="agent-b", timeout_seconds=300)
        _insert_execution(
            tmp_db,
            execution_id="exec-sched",
            agent_name="agent-b",
            schedule_id="sched-1",
            started_at=started,
        )
        _insert_agent(tmp_db, agent_name="agent-b", execution_timeout_seconds=900)

        schedule_ops.mark_execution_failed_by_watchdog("exec-sched", "orphaned")

        row = _get_row(tmp_db, "exec-sched")
        assert row["duration_ms"] <= 300_000, (
            f"duration_ms={row['duration_ms']} exceeds schedule's 300 s timeout"
        )

    def test_duration_falls_back_to_agent_timeout(self, tmp_db, schedule_ops):
        """No schedule timeout → falls back to agent_ownership.execution_timeout_seconds."""
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        _insert_schedule(tmp_db, schedule_id="sched-2", agent_name="agent-c", timeout_seconds=None)
        _insert_execution(
            tmp_db,
            execution_id="exec-agent-to",
            agent_name="agent-c",
            schedule_id="sched-2",
            started_at=started,
        )
        _insert_agent(tmp_db, agent_name="agent-c", execution_timeout_seconds=600)

        schedule_ops.mark_execution_failed_by_watchdog("exec-agent-to", "orphaned")

        row = _get_row(tmp_db, "exec-agent-to")
        assert row["duration_ms"] <= 600_000

    def test_duration_falls_back_to_900s_default(self, tmp_db, schedule_ops):
        """No schedule, no agent row → caps at the 900 s hardcoded default."""
        started = datetime.now(timezone.utc) - timedelta(hours=4)
        _insert_execution(
            tmp_db,
            execution_id="exec-default",
            agent_name="agent-missing",
            started_at=started,
        )
        # No agent_ownership row inserted.

        schedule_ops.mark_execution_failed_by_watchdog("exec-default", "orphaned")

        row = _get_row(tmp_db, "exec-default")
        assert row["duration_ms"] <= 900_000

    def test_fast_failure_not_capped(self, tmp_db, schedule_ops):
        """Execution that actually ran for 10 s keeps its real duration."""
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        _insert_execution(tmp_db, execution_id="exec-fast", agent_name="agent-a", started_at=started)
        _insert_agent(tmp_db, agent_name="agent-a", execution_timeout_seconds=900)

        schedule_ops.mark_execution_failed_by_watchdog("exec-fast", "connection error")

        row = _get_row(tmp_db, "exec-fast")
        # Should be ~10 000 ms — well below the 900 000 ms cap.
        assert row["duration_ms"] <= 900_000
        assert row["duration_ms"] >= 5_000, "Expected ~10 s execution to have realistic duration"

    def test_already_terminal_returns_false(self, tmp_db, schedule_ops):
        """CAS guard: already-FAILED row returns False (no overwrite)."""
        started = datetime.now(timezone.utc) - timedelta(minutes=5)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO schedule_executions "
            "(id, agent_name, status, started_at, message, triggered_by) "
            "VALUES ('exec-done', 'agent-a', 'failed', ?, '', 'scheduler')",
            (started.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),),
        )
        conn.commit()
        conn.close()

        result = schedule_ops.mark_execution_failed_by_watchdog("exec-done", "duplicate")
        assert result is False
