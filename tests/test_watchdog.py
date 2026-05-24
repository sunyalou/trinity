"""
Watchdog Integration Tests (test_watchdog.py)

Tests for Issue #129: Active watchdog remediation of stuck executions.
Integration tests against the running backend — verify cleanup report
includes watchdog fields.

Issue #921: two-cycle confirmation regression — guards against the false-
positive recovery race between agent unregister and backend success-write.

Feature Flow: docs/memory/feature-flows/cleanup-service.md
"""

import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_json_response,
)

_BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
# trinity-system is a stable, always-running agent on every Trinity instance.
# Its /api/executions/running endpoint returns an empty list when nothing's
# in flight, which is the exact state the watchdog sees during the race.
_SENTINEL_AGENT = "trinity-system"


def _exec_backend(python_code: str) -> str:
    """Run Python inside the trinity-backend container — used for DB/Redis
    fixture setup. Mirrors the pattern in test_operator_queue.py."""
    result = subprocess.run(
        ["docker", "exec", _BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    return result.stdout.strip()


def _insert_running_row(execution_id: str, age_seconds: int = 120) -> None:
    """Insert a schedule_executions row that looks like an in-flight task.

    `claude_session_id` is set so the #106 no-session fast-fail path doesn't
    fire before the orphan-recovery path can run.
    """
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _exec_backend(f"""
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute(
    'INSERT INTO schedule_executions (id, schedule_id, agent_name, status, '
    'started_at, message, triggered_by, claude_session_id) '
    'VALUES (?,?,?,?,?,?,?,?)',
    ('{execution_id}', 'sched-921-int', '{_SENTINEL_AGENT}', 'running',
     '{started_at}', 'integration #921', 'manual', 'sess-921-int'),
)
c.commit(); c.close()
print('OK')
""")


def _row_state(execution_id: str) -> dict:
    """Return {status, error, response} for a row, or {} if missing."""
    out = _exec_backend(f"""
import json, sqlite3
c = sqlite3.connect('/data/trinity.db'); c.row_factory = sqlite3.Row
r = c.execute('SELECT status, error, response FROM schedule_executions WHERE id=?', ('{execution_id}',)).fetchone()
print(json.dumps(dict(r) if r else {{}}))
c.close()
""")
    import json as _json
    return _json.loads(out)


def _sentinel_exists(execution_id: str) -> bool:
    out = _exec_backend(f"""
import redis, os
from config import REDIS_URL
client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
print('1' if client.exists('watchdog:suspected_orphan:{execution_id}') > 0 else '0')
""")
    return out == "1"


def _force_success(execution_id: str) -> None:
    """Simulate task_execution_service writing the success status that the
    watchdog races against — see services/task_execution_service.py:728."""
    _exec_backend(f"""
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute(
    'UPDATE schedule_executions SET status=?, completed_at=?, response=? WHERE id=?',
    ('success', '2099-01-01T00:00:00Z', 'task completed normally', '{execution_id}'),
)
c.commit(); c.close()
print('OK')
""")


def _delete_row(execution_id: str) -> None:
    _exec_backend(f"""
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute('DELETE FROM schedule_executions WHERE id=?', ('{execution_id}',))
c.commit(); c.close()
print('OK')
""")


def _clear_sentinel(execution_id: str) -> None:
    _exec_backend(f"""
import redis
from config import REDIS_URL
client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
client.delete('watchdog:suspected_orphan:{execution_id}')
print('OK')
""")


class TestWatchdogCleanupReportFields:
    """Tests that cleanup report includes Issue #129 watchdog fields."""

    pytestmark = pytest.mark.smoke

    def test_cleanup_status_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """GET /api/monitoring/cleanup-status report includes watchdog fields."""
        response = api_client.get("/api/monitoring/cleanup-status")
        assert_status(response, 200)
        data = response.json()

        if data.get("last_report"):
            report = data["last_report"]
            assert "orphaned_executions" in report, "Missing orphaned_executions field"
            assert "auto_terminated" in report, "Missing auto_terminated field"
            assert isinstance(report["orphaned_executions"], int)
            assert isinstance(report["auto_terminated"], int)

    def test_cleanup_trigger_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """POST /api/monitoring/cleanup-trigger report includes watchdog fields."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        data = assert_json_response(response)

        report = data["report"]
        assert "orphaned_executions" in report, "Missing orphaned_executions field"
        assert "auto_terminated" in report, "Missing auto_terminated field"
        assert isinstance(report["orphaned_executions"], int)
        assert isinstance(report["auto_terminated"], int)
        assert report["orphaned_executions"] >= 0
        assert report["auto_terminated"] >= 0

    def test_cleanup_total_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """Cleanup total correctly sums all fields including watchdog additions."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        report = response.json()["report"]

        expected_total = (
            report["orphaned_executions"]
            + report["auto_terminated"]
            + report["stale_executions"]
            + report["no_session_executions"]
            + report["orphaned_skipped"]
            + report["stale_activities"]
            + report["stale_slots"]
        )
        assert report["total"] == expected_total

    def test_cleanup_trigger_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """Watchdog cleanup trigger requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/cleanup-trigger")
        assert response.status_code in [401, 403]


# ============================================================================
# Two-Cycle Orphan Confirmation (Issue #921)
# ============================================================================


class TestTwoCycleOrphanConfirmation:
    """Live-stack regression for #921.

    The agent's `claude_code.py` unregisters its process registry in a
    `finally` block BEFORE `task_execution_service` writes `success` to
    the DB. A single watchdog snapshot can't distinguish that race window
    from a true orphan, so recovery now requires two consecutive sightings
    of (DB-running + agent-missing).

    These tests drive `POST /api/monitoring/cleanup-trigger` directly to
    invoke the watchdog synchronously, then inspect the DB row and the
    Redis sentinel to verify the two-cycle protocol behaves correctly.
    """

    @pytest.fixture
    def execution_id(self):
        """Generate a unique execution_id and guarantee cleanup of both the
        DB row and the Redis sentinel — even if the test fails mid-way."""
        eid = f"test-921-int-{uuid.uuid4().hex[:12]}"
        yield eid
        # Teardown: always tidy up, ignore individual failures
        try:
            _delete_row(eid)
        except Exception:
            pass
        try:
            _clear_sentinel(eid)
        except Exception:
            pass

    def test_first_cycle_defers_without_recovery(
        self, api_client: TrinityApiClient, execution_id: str
    ):
        """The race window itself: agent says missing, DB still running.

        Expected: watchdog records the sentinel and reports
        `orphans_suspected=1`, but does NOT mark the row failed. This is
        the heart of the #921 fix — under the old code the same scenario
        produced `orphaned_executions=1` and a watchdog-failed row.
        """
        _insert_running_row(execution_id)

        before = _row_state(execution_id)
        assert before["status"] == "running"
        assert not _sentinel_exists(execution_id)

        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        report = response.json()["report"]

        assert "orphans_suspected" in report, "Missing orphans_suspected field"
        assert report["orphans_suspected"] >= 1
        # Row must still be running — recovery was deferred.
        after = _row_state(execution_id)
        assert after["status"] == "running"
        assert after["error"] is None
        # Sentinel must be in Redis to gate the next cycle.
        assert _sentinel_exists(execution_id)

    def test_second_cycle_confirms_and_recovers(
        self, api_client: TrinityApiClient, execution_id: str
    ):
        """True orphan: two consecutive cycles see (DB-running + agent-missing).

        Expected: first cycle defers, second cycle recovers and clears the
        sentinel. Confirms the fix doesn't break legitimate orphan recovery —
        it only delays it by one cycle.
        """
        _insert_running_row(execution_id)

        # Cycle 1 — defer
        report_1 = api_client.post("/api/monitoring/cleanup-trigger").json()["report"]
        assert report_1["orphans_suspected"] >= 1
        assert _row_state(execution_id)["status"] == "running"

        # Cycle 2 — recover
        report_2 = api_client.post("/api/monitoring/cleanup-trigger").json()["report"]
        assert report_2["orphaned_executions"] >= 1

        after = _row_state(execution_id)
        assert after["status"] == "failed"
        assert "recovered by watchdog" in (after["error"] or "")
        # Sentinel cleared so a future row with the same id doesn't skip
        # straight to recovery.
        assert not _sentinel_exists(execution_id)

    def test_natural_completion_race_no_false_positive(
        self, api_client: TrinityApiClient, execution_id: str
    ):
        """The #921 smoking-gun scenario reproduced end-to-end.

        Cycle 1: row is `running`, agent has unregistered (race window).
        Between cycles: backend lands the `success` write (we simulate
          task_execution_service's update directly).
        Cycle 2: the row is no longer in the running query, so the watchdog
          never even looks at it. The row's natural `success` state survives
          intact — no false orphan recovery overwrites it.

        Under the old code, cycle 1 alone would mark the row failed with a
        watchdog error message, destroying the real completion data.
        """
        _insert_running_row(execution_id)

        # Cycle 1: race window — watchdog defers.
        report_1 = api_client.post("/api/monitoring/cleanup-trigger").json()["report"]
        assert report_1["orphans_suspected"] >= 1
        assert _row_state(execution_id)["status"] == "running"

        # Between cycles: the backend's task_execution_service lands its
        # success-write that was in flight all along.
        _force_success(execution_id)

        # Cycle 2: the row is in a terminal status and is no longer returned
        # by get_running_executions_with_agent_info — watchdog ignores it.
        api_client.post("/api/monitoring/cleanup-trigger")

        final = _row_state(execution_id)
        # The crucial assertion: the natural success was NOT overwritten.
        assert final["status"] == "success"
        assert final["error"] is None
        assert final["response"] == "task completed normally"
