"""
Unit tests for execution_log + schedule_executions + agent_health_checks
retention pruning (Issue #772, bug fix #862).

Covers:
- ``ScheduleOperations.prune_execution_logs``  — nulls execution_log on
  terminal rows older than the cutoff, preserves recent + non-terminal.
- ``ScheduleOperations.prune_execution_rows``  — deletes terminal rows
  older than the cutoff.
- ``MonitoringOperations.cleanup_old_records`` — chunked DELETE on
  agent_health_checks older than the cutoff.
- ``retention_days <= 0`` disables every sweep.
- ``chunk_size`` is respected (multi-pass drain).

Bug #862 regression coverage (appended at the bottom):
- Pruning uses the correct TaskExecutionStatus values: 'success', 'failed',
  'cancelled', 'skipped'.  The original #772 code used 'completed',
  'terminated' — values that never existed in the enum — so only 'failed'
  rows were ever pruned.  Tests verify all four correct statuses are pruned
  and that old wrong status values are NOT pruned.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402


_SCHEDULE_EXECUTIONS_DDL = """
CREATE TABLE schedule_executions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    message TEXT NOT NULL,
    response TEXT,
    error TEXT,
    triggered_by TEXT NOT NULL,
    context_used INTEGER,
    context_max INTEGER,
    cost REAL,
    tool_calls TEXT,
    execution_log TEXT,
    model_used TEXT,
    subscription_id TEXT,
    attempt_number INTEGER DEFAULT 1,
    retry_of_execution_id TEXT,
    retry_scheduled_at TEXT,
    business_status TEXT,
    validated_at TEXT,
    validation_execution_id TEXT,
    validates_execution_id TEXT,
    compact_metadata TEXT
)
"""

_AGENT_HEALTH_CHECKS_DDL = """
CREATE TABLE agent_health_checks (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    container_status TEXT,
    cpu_percent REAL,
    memory_percent REAL,
    memory_mb REAL,
    restart_count INTEGER,
    oom_killed INTEGER,
    reachable INTEGER,
    latency_ms REAL,
    runtime_available INTEGER,
    claude_available INTEGER,
    context_percent REAL,
    active_executions INTEGER,
    error_rate REAL,
    error_message TEXT,
    checked_at TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


@pytest.fixture
def db_setup(db_backend):
    """Active backend with a fresh full schema (db_harness, #300).

    Returns (backend_marker, schedule_ops, monitoring_ops). The marker is the
    leading positional arg the engine-based helpers accept (and ignore).
    Pops any sibling-stubbed db modules so imports re-resolve fresh.
    """
    for mod in ("db.connection", "db.schedules", "db.monitoring"):
        sys.modules.pop(mod, None)
    from db.schedules import ScheduleOperations
    from db.monitoring import MonitoringOperations

    # ScheduleOperations needs user_ops + agent_ops in __init__, but the
    # prune methods don't touch them. Pass None placeholders.
    schedule_ops = ScheduleOperations(None, None)
    monitoring_ops = MonitoringOperations()
    yield db_backend, schedule_ops, monitoring_ops


def _iso(days_ago: float) -> str:
    """utc_now_iso()-format timestamp `days_ago` days in the past."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _rows(sql: str, **binds):
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return conn.execute(text(sql), binds).fetchall()


def _insert_execution(
    _db,
    *,
    id_: str,
    status: str,
    completed_days_ago: float | None,
    execution_log: str | None,
) -> None:
    started = _iso(completed_days_ago or 0.1)
    completed = _iso(completed_days_ago) if completed_days_ago is not None else None
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, completed_at, "
        " message, triggered_by, execution_log) "
        "VALUES (:id, 'sched-x', 'agent-x', :st, :sa, :ca, 'msg', 'user', :log)",
        id=id_, st=status, sa=started, ca=completed, log=execution_log,
    )


def _insert_health_check(_db, *, id_: str, days_ago: float) -> None:
    checked = _iso(days_ago)
    _hrun(
        "INSERT INTO agent_health_checks "
        "(id, agent_name, check_type, status, checked_at, created_at) "
        "VALUES (:id, 'agent-x', 'aggregate', 'ok', :c, :c)",
        id=id_, c=checked,
    )


def _execution_logs(_db) -> dict[str, str | None]:
    return {r[0]: r[1] for r in _rows("SELECT id, execution_log FROM schedule_executions")}


def _execution_ids(_db) -> set[str]:
    return {r[0] for r in _rows("SELECT id FROM schedule_executions")}


def _health_ids(_db) -> set[str]:
    return {r[0] for r in _rows("SELECT id FROM agent_health_checks")}


# ---------------------------------------------------------------------------
# prune_execution_logs
# ---------------------------------------------------------------------------


def test_prune_execution_logs_nulls_terminal_past_cutoff(db_setup):
    db_path, schedule_ops, _ = db_setup

    # Past 30-day cutoff, terminal (correct status values): should be nulled.
    _insert_execution(db_path, id_="old-success", status="success",
                      completed_days_ago=45, execution_log="big-jsonl")
    _insert_execution(db_path, id_="old-failed", status="failed",
                      completed_days_ago=60, execution_log="big-jsonl")
    _insert_execution(db_path, id_="old-cancelled", status="cancelled",
                      completed_days_ago=90, execution_log="big-jsonl")
    # Within retention window: keep the log.
    _insert_execution(db_path, id_="recent-success", status="success",
                      completed_days_ago=5, execution_log="keep-me")
    # Non-terminal, must never be touched.
    _insert_execution(db_path, id_="old-running", status="running",
                      completed_days_ago=None, execution_log="active-jsonl")
    # Terminal but log already NULL — should not be counted again.
    _insert_execution(db_path, id_="old-success-nulled", status="success",
                      completed_days_ago=100, execution_log=None)

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=1000)
    assert nulled == 3

    logs = _execution_logs(db_path)
    assert logs["old-success"] is None
    assert logs["old-failed"] is None
    assert logs["old-cancelled"] is None
    assert logs["recent-success"] == "keep-me"
    assert logs["old-running"] == "active-jsonl"
    assert logs["old-success-nulled"] is None


def test_prune_execution_logs_respects_chunk_size(db_setup):
    """Multi-pass drain — chunked loop must drain everything in scope."""
    db_path, schedule_ops, _ = db_setup

    for i in range(7):
        _insert_execution(
            db_path, id_=f"old-{i}", status="success",
            completed_days_ago=60, execution_log="payload",
        )

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=2)
    assert nulled == 7
    assert all(v is None for v in _execution_logs(db_path).values())


def test_prune_execution_logs_disabled_when_retention_zero(db_setup):
    db_path, schedule_ops, _ = db_setup
    _insert_execution(db_path, id_="old", status="success",
                      completed_days_ago=400, execution_log="payload")

    assert schedule_ops.prune_execution_logs(retention_days=0) == 0
    assert schedule_ops.prune_execution_logs(retention_days=-1) == 0
    assert _execution_logs(db_path)["old"] == "payload"


def test_prune_execution_logs_empty_table(db_setup):
    _, schedule_ops, _ = db_setup
    assert schedule_ops.prune_execution_logs(retention_days=30) == 0


# ---------------------------------------------------------------------------
# prune_execution_rows
# ---------------------------------------------------------------------------


def test_prune_execution_rows_deletes_terminal_past_cutoff(db_setup):
    db_path, schedule_ops, _ = db_setup

    _insert_execution(db_path, id_="old-success", status="success",
                      completed_days_ago=120, execution_log=None)
    _insert_execution(db_path, id_="old-failed", status="failed",
                      completed_days_ago=200, execution_log=None)
    _insert_execution(db_path, id_="recent-success", status="success",
                      completed_days_ago=30, execution_log=None)
    _insert_execution(db_path, id_="old-running", status="running",
                      completed_days_ago=None, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=1000)
    assert deleted == 2
    assert _execution_ids(db_path) == {"recent-success", "old-running"}


def test_prune_execution_rows_disabled_when_retention_zero(db_setup):
    db_path, schedule_ops, _ = db_setup
    _insert_execution(db_path, id_="old", status="success",
                      completed_days_ago=400, execution_log=None)

    assert schedule_ops.prune_execution_rows(retention_days=0) == 0
    assert _execution_ids(db_path) == {"old"}


# ---------------------------------------------------------------------------
# cleanup_old_records (agent_health_checks)
# ---------------------------------------------------------------------------


def test_cleanup_old_health_records_deletes_past_cutoff(db_setup):
    db_path, _, monitoring_ops = db_setup

    _insert_health_check(db_path, id_="old-1", days_ago=10)
    _insert_health_check(db_path, id_="old-2", days_ago=14)
    _insert_health_check(db_path, id_="recent", days_ago=1)

    deleted = monitoring_ops.cleanup_old_records(days=7, chunk_size=1000)
    assert deleted == 2
    assert _health_ids(db_path) == {"recent"}


def test_cleanup_old_health_records_disabled_when_days_zero(db_setup):
    db_path, _, monitoring_ops = db_setup
    _insert_health_check(db_path, id_="old", days_ago=400)

    assert monitoring_ops.cleanup_old_records(days=0) == 0
    assert _health_ids(db_path) == {"old"}


def test_cleanup_old_health_records_respects_chunk_size(db_setup):
    db_path, _, monitoring_ops = db_setup
    for i in range(5):
        _insert_health_check(db_path, id_=f"old-{i}", days_ago=30)

    deleted = monitoring_ops.cleanup_old_records(days=7, chunk_size=2)
    assert deleted == 5
    assert _health_ids(db_path) == set()


# ---------------------------------------------------------------------------
# Bug #862 regression: correct TaskExecutionStatus values
# ---------------------------------------------------------------------------


def test_prune_logs_all_four_terminal_statuses(db_setup):
    """'success', 'failed', 'cancelled', 'skipped' must all be pruned (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("success", "failed", "cancelled", "skipped"):
        _insert_execution(db_path, id_=f"old-{status}", status=status,
                          completed_days_ago=40, execution_log="payload")

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=100)
    assert nulled == 4
    for status in ("success", "failed", "cancelled", "skipped"):
        assert _execution_logs(db_path)[f"old-{status}"] is None


def test_prune_rows_all_four_terminal_statuses(db_setup):
    """'success', 'failed', 'cancelled', 'skipped' rows are all deleted (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("success", "failed", "cancelled", "skipped"):
        _insert_execution(db_path, id_=f"old-{status}", status=status,
                          completed_days_ago=100, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=100)
    assert deleted == 4
    assert _execution_ids(db_path) == set()


def test_prune_logs_skips_wrong_legacy_status_values(db_setup):
    """'completed' and 'terminated' never existed in TaskExecutionStatus — not pruned (#862)."""
    db_path, schedule_ops, _ = db_setup

    _insert_execution(db_path, id_="legacy-completed", status="completed",
                      completed_days_ago=60, execution_log="payload")
    _insert_execution(db_path, id_="legacy-terminated", status="terminated",
                      completed_days_ago=60, execution_log="payload")

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=100)
    assert nulled == 0
    assert _execution_logs(db_path)["legacy-completed"] == "payload"
    assert _execution_logs(db_path)["legacy-terminated"] == "payload"


def test_prune_rows_skips_non_terminal_statuses(db_setup):
    """running, queued, pending_retry rows survive the row-delete sweep (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("running", "queued", "pending_retry"):
        _insert_execution(db_path, id_=f"nterm-{status}", status=status,
                          completed_days_ago=None, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=100)
    assert deleted == 0
    assert len(_execution_ids(db_path)) == 3
