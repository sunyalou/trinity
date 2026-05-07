"""Integration tests for the Redis-backed circuit breaker (#631).

State and transitions live in Redis (atomic Lua scripts), so the only
faithful tests run against a real Redis. Connect to the live `trinity-redis`
container via the `backend` ACL credentials in .env. Each test uses a
unique agent name and cleans its keys on the way out so the suite can
re-run against a stack that's seeing real traffic.

Covered:
- closed → open → dormant state machine with correct backoff growth
- one-worker-probes-at-a-time semantics via the SET-NX-EX probe lock
- record_success full reset
- single transition log per cluster (atomic Lua), not per worker
- get_all_circuit_states scans only state hashes (skips probe-locks)
- force_circuit_dormant / reset_circuit operator hooks
- fail-open when Redis is unreachable
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
import redis as _redis

# Add backend to sys.path BEFORE importing — agent_client imports `config` which
# fails fast if REDIS_URL lacks credentials. We override REDIS_URL with the
# backend ACL credentials sourced from .env so the backend's config.py accepts.
_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_env_password() -> str:
    """Pull REDIS_BACKEND_PASSWORD out of the repo .env."""
    env_path = _REPO / ".env"
    if not env_path.exists():
        pytest.skip(".env missing — cannot derive Redis credentials")
    for line in env_path.read_text().splitlines():
        if line.startswith("REDIS_BACKEND_PASSWORD="):
            return line.split("=", 1)[1].strip()
    pytest.skip("REDIS_BACKEND_PASSWORD not found in .env")


# Point config.py at the local stack BEFORE importing agent_client.
_PASSWORD = _load_env_password()
os.environ["REDIS_URL"] = f"redis://backend:{_PASSWORD}@localhost:6379"
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", _PASSWORD)


# Import via importlib to avoid pulling in the full services/__init__.py
# (which drags in Docker, models, FastAPI, etc.).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "agent_client_under_test",
    str(_BACKEND / "services" / "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_client)


pytestmark = pytest.mark.integration


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def redis_client():
    """Direct Redis client used to inspect state and clean up after tests."""
    client = _redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    except Exception as e:
        pytest.skip(f"Redis unavailable: {e}")
    yield client
    client.close()


@pytest.fixture
def agent_name(redis_client):
    """Unique per-test agent name; auto-cleans both the state hash and
    probe-lock from Redis after the test."""
    name = f"cb-test-{uuid.uuid4().hex[:10]}"
    yield name
    redis_client.delete(
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}",
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}",
    )


@pytest.fixture(autouse=True)
def _ensure_redis_client_cached():
    """Force agent_client to re-resolve its Redis client between tests
    (each test may have skewed env / monkeypatched reset)."""
    agent_client._reset_circuit_redis_client()
    yield
    agent_client._reset_circuit_redis_client()


# ── State machine ────────────────────────────────────────────────────────────

class TestStateMachine:

    def test_closed_state_allows(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        assert cs.allow_request() is True
        assert cs.state == "closed"
        assert cs.failure_count == 0

    def test_below_threshold_stays_closed(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD - 1):
            assert cs.record_failure() == "closed"
        assert cs.state == "closed"
        assert cs.failure_count == agent_client.CIRCUIT_FAILURE_THRESHOLD - 1

    def test_threshold_reached_opens(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        last_state = None
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            last_state = cs.record_failure()
        assert last_state == "open"
        assert cs.state == "open"

    def test_open_in_cooldown_denies(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        # Immediately after open, we're well before next_probe_at.
        assert cs.allow_request() is False

    def test_record_success_resets_to_closed(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        assert cs.state == "open"
        cs.record_success()
        assert cs.state == "closed"
        assert cs.failure_count == 0
        assert cs.allow_request() is True


# ── Backoff curve ────────────────────────────────────────────────────────────

class TestBackoffSchedule:

    def _read_next_probe(self, redis_client, agent_name) -> float:
        return float(
            redis_client.hget(
                f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}", "next_probe_at"
            )
            or "0"
        )

    def test_backoff_grows_then_caps(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)

        # Push past the failure threshold.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()

        # First open transition: cooldown ≈ base * 2^0 = base.
        cooldown_first = self._read_next_probe(redis_client, agent_name) - time.time()
        assert (
            agent_client.CIRCUIT_BASE_COOLDOWN_SECONDS - 2
            <= cooldown_first
            <= agent_client.CIRCUIT_BASE_COOLDOWN_SECONDS + 2
        ), f"first open cooldown {cooldown_first} not near base"

        # Subsequent open-state failures should grow the cooldown until cap.
        # After enough failures we should plateau at max.
        prev_cd = cooldown_first
        capped = False
        for _ in range(8):
            cs.record_failure()
            cd = self._read_next_probe(redis_client, agent_name) - time.time()
            if cd >= agent_client.CIRCUIT_MAX_COOLDOWN_SECONDS - 5:
                capped = True
                break
            assert cd >= prev_cd - 1, (
                f"cooldown shrank: prev={prev_cd} cur={cd}"
            )
            prev_cd = cd
        assert capped, "cooldown never reached the cap within 8 extra failures"


# ── Dormant state ────────────────────────────────────────────────────────────

class TestDormantState:

    def test_enters_dormant_after_threshold_open_probes(self, agent_name, monkeypatch):
        # Tighten the dormant threshold so we don't slow the test down.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_AFTER_OPEN_PROBES", 4)

        cs = agent_client.CircuitState(agent_name)
        last = None
        # First call: failures 1..threshold-1 stay closed; threshold opens; then
        # each additional failure increments probe_count_since_open.
        # Total iterations = failure_threshold + dormant_threshold to cross the line.
        for _ in range(
            agent_client.CIRCUIT_FAILURE_THRESHOLD
            + agent_client.CIRCUIT_DORMANT_AFTER_OPEN_PROBES
            + 2
        ):
            last = cs.record_failure()
            if last == "dormant":
                break
        assert last == "dormant", f"never transitioned to dormant; last={last}"
        assert cs.state == "dormant"

    def test_dormant_denies_all_requests(self, agent_name):
        agent_client.force_circuit_dormant(agent_name, reason="test")
        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "dormant"
        assert cs.allow_request() is False
        # Repeated calls stay dormant — no half-open attempts.
        for _ in range(5):
            assert cs.allow_request() is False


# ── Probe-lock cross-worker semantics ────────────────────────────────────────

class TestProbeLock:
    """Two CircuitState instances sharing one Redis simulate two uvicorn workers.

    With the cooldown elapsed, only one of them can claim the probe-lock and
    issue the half-open request.
    """

    def test_only_one_worker_probes(self, agent_name, redis_client, monkeypatch):
        # Tiny cooldown so we can elapse it without sleeping for 30 s.
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.05)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.05)

        worker_a = agent_client.CircuitState(agent_name)
        worker_b = agent_client.CircuitState(agent_name)

        # Drive to open via worker_a.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            worker_a.record_failure()
        assert worker_a.state == "open"
        # Worker B observes the same state — Redis is the single source of truth.
        assert worker_b.state == "open"

        # Wait past the cooldown so allow_request is eligible to probe.
        time.sleep(0.1)

        # Race: both workers ask permission. Exactly one should be admitted
        # (probe lock acquired); the other denied.
        verdicts = [worker_a.allow_request(), worker_b.allow_request()]
        assert verdicts.count(True) == 1, (
            f"expected exactly one worker to win probe-lock, got {verdicts}"
        )
        assert verdicts.count(False) == 1


# ── Logging on cluster transitions ───────────────────────────────────────────

class TestTransitionLogging:

    def test_open_transition_logs_once(self, agent_name, caplog):
        # Simulate two workers calling record_failure concurrently. The atomic
        # Lua means only one observes the closed→open transition.
        cs_a = agent_client.CircuitState(agent_name)
        cs_b = agent_client.CircuitState(agent_name)

        with caplog.at_level(logging.WARNING, logger=agent_client.logger.name):
            # First two failures from A keep us closed (assuming threshold=3).
            for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD - 1):
                cs_a.record_failure()
            # The transition fires when failures hit the threshold. Whichever
            # worker tips it logs; the other (if it tips later) sees prior=open.
            cs_a.record_failure()
            cs_b.record_failure()

        opened_logs = [
            r for r in caplog.records
            if "Circuit OPENED" in r.getMessage() and agent_name in r.getMessage()
        ]
        assert len(opened_logs) == 1, (
            f"expected 1 OPENED log, got {len(opened_logs)}: {[r.getMessage() for r in opened_logs]}"
        )

    def test_recovery_logs_closed(self, agent_name, caplog):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()

        with caplog.at_level(logging.INFO, logger=agent_client.logger.name):
            cs.record_success()

        closed_logs = [
            r for r in caplog.records
            if "Circuit CLOSED" in r.getMessage() and agent_name in r.getMessage()
        ]
        assert len(closed_logs) == 1


# ── Operator hooks ───────────────────────────────────────────────────────────

class TestOperatorHooks:

    def test_force_dormant_idempotent(self, agent_name, redis_client):
        agent_client.force_circuit_dormant(agent_name, reason="test")
        agent_client.force_circuit_dormant(agent_name, reason="test")
        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "dormant"

    def test_reset_clears_redis_state(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        assert redis_client.exists(f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}")

        agent_client.reset_circuit(agent_name)
        assert not redis_client.exists(f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}")
        # Fresh facade reads as closed.
        assert agent_client.CircuitState(agent_name).state == "closed"


# ── get_all_circuit_states ───────────────────────────────────────────────────

class TestGetAllStates:

    def test_scan_returns_only_state_hashes(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        # Manually plant a probe-lock so the scan would pick it up if the
        # filter was wrong.
        redis_client.set(
            f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}",
            "1",
            ex=10,
        )
        try:
            states = agent_client.get_all_circuit_states()
            assert agent_name in states, "state hash missing from scan"
            # No `probe-lock`-suffixed entry should appear as an agent name.
            assert not any(
                name.endswith(agent_client._CIRCUIT_PROBE_LOCK_SUFFIX)
                for name in states.keys()
            )
            entry = states[agent_name]
            assert entry["state"] == "open"
            assert entry["failure_count"] >= agent_client.CIRCUIT_FAILURE_THRESHOLD
        finally:
            redis_client.delete(
                f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}"
            )


# ── Fail-open when Redis is unreachable ──────────────────────────────────────

class TestFailOpen:

    def test_unreachable_redis_returns_allow(self, agent_name, monkeypatch):
        """If Redis is down we fall through to allowing requests — graceful
        degradation, since the underlying HTTP failure will surface anyway."""

        def _fail(*_a, **_kw):
            raise _redis.exceptions.ConnectionError("simulated outage")

        # Force every redis op to error.
        monkeypatch.setattr(agent_client, "_get_circuit_redis", lambda: None)

        cs = agent_client.CircuitState(agent_name)
        assert cs.allow_request() is True
        assert cs.record_failure() == "closed"
        assert cs.state == "closed"
        # record_success is a no-op in fail-open mode but must not raise.
        cs.record_success()
