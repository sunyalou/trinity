"""
Unit tests for non-state primitives in services/agent_client.py.

The circuit breaker state machine moved to Redis in #631; its tests live in
tests/integration/test_circuit_breaker.py because they need a real Redis to
exercise the atomic Lua transitions. What remains here is the in-process
machinery that has nothing to do with circuit state:

  * the per-base-URL httpx.AsyncClient pool
  * the exception hierarchy

If you want to test allow_request / record_failure / record_success / dormant
transitions / cross-worker probe-lock semantics, run the integration suite.
"""

import asyncio
import importlib
import os
import sys

import httpx
import pytest

# Add backend to path and import only agent_client (avoid triggering
# the full services __init__.py which pulls in Docker, models, etc.)
_backend = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'backend')
sys.path.insert(0, _backend)

# Load module directly to avoid services/__init__.py import chain.
_spec = importlib.util.spec_from_file_location(
    "agent_client",
    os.path.join(_backend, "services", "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_client)

AgentClient = agent_client.AgentClient
AgentCircuitOpenError = agent_client.AgentCircuitOpenError
AgentNotReachableError = agent_client.AgentNotReachableError
AgentClientError = agent_client.AgentClientError
_client_pool = agent_client._client_pool
_get_http_client = agent_client._get_http_client
close_all_clients = agent_client.close_all_clients
is_circuit_failure = agent_client.is_circuit_failure


pytestmark = pytest.mark.unit


# ============================================================================
# Connection Pool Tests
# ============================================================================

class TestConnectionPool:
    """Test the HTTP client connection pool."""

    def setup_method(self):
        # Close any leftover clients from previous tests.
        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_all_clients())
        loop.close()

    def test_get_http_client_creates_client(self):
        client = _get_http_client("http://agent-test:8000")
        assert client is not None
        assert not client.is_closed

    def test_get_http_client_reuses_client(self):
        c1 = _get_http_client("http://agent-test:8000")
        c2 = _get_http_client("http://agent-test:8000")
        assert c1 is c2

    def test_different_urls_get_different_clients(self):
        c1 = _get_http_client("http://agent-a:8000")
        c2 = _get_http_client("http://agent-b:8000")
        assert c1 is not c2

    def test_close_all_clients(self):
        _get_http_client("http://agent-test:8000")
        assert len(_client_pool) > 0

        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_all_clients())
        loop.close()
        assert len(_client_pool) == 0


# ============================================================================
# Exception Hierarchy Tests
# ============================================================================

class TestExceptionHierarchy:
    """Verify exception inheritance for backward compatibility."""

    def test_circuit_open_is_agent_client_error(self):
        err = AgentCircuitOpenError("test")
        assert isinstance(err, AgentClientError)

    def test_not_reachable_is_agent_client_error(self):
        err = AgentNotReachableError("test")
        assert isinstance(err, AgentClientError)

    def test_existing_catch_blocks_catch_circuit_open(self):
        """Callers using 'except AgentClientError' will catch circuit open errors."""
        try:
            raise AgentCircuitOpenError("circuit open")
        except AgentClientError:
            pass  # This should catch it

    # ─── #474: is_circuit_failure() classification contract ─────────────────

    def test_classifier_connect_error_counts(self):
        """ConnectError = TCP refused / DNS unresolvable → agent unreachable."""
        assert is_circuit_failure(httpx.ConnectError("refused")) is True

    def test_classifier_connect_timeout_counts(self):
        """ConnectTimeout = TCP handshake didn't ack → agent unreachable.

        Must be matched as a CIRCUIT_FAILURE_EXCEPTION even though it's a
        TimeoutException subclass.
        """
        assert is_circuit_failure(httpx.ConnectTimeout("timed out")) is True

    def test_classifier_read_timeout_does_not_count(self):
        """ReadTimeout = agent stopped responding mid-request → usually busy."""
        assert is_circuit_failure(httpx.ReadTimeout("slow")) is False

    def test_classifier_write_error_does_not_count(self):
        """WriteError covers wrapped BrokenPipeError / ConnectionResetError."""
        assert is_circuit_failure(httpx.WriteError("epipe")) is False

    def test_classifier_pool_timeout_does_not_count(self):
        """PoolTimeout = client-side pool exhaustion, not agent unhealth."""
        assert is_circuit_failure(httpx.PoolTimeout("pool")) is False

    def test_classifier_read_error_does_not_count(self):
        """ReadError = mid-read socket loss → transient."""
        assert is_circuit_failure(httpx.ReadError("dropped")) is False

    def test_classifier_remote_protocol_error_does_not_count(self):
        """RemoteProtocolError = garbled response framing → transient."""
        assert is_circuit_failure(httpx.RemoteProtocolError("bad frame")) is False

    def test_classifier_raw_broken_pipe_does_not_count(self):
        """Raw BrokenPipeError (some transports surface it un-wrapped)."""
        assert is_circuit_failure(BrokenPipeError("epipe")) is False

    def test_classifier_raw_connection_reset_does_not_count(self):
        """Raw ConnectionResetError (sibling of BrokenPipeError)."""
        assert is_circuit_failure(ConnectionResetError("reset")) is False

    def test_classifier_raw_os_error_does_not_count(self):
        """OSError(EPIPE) directly — must NOT trip the circuit."""
        assert is_circuit_failure(OSError(32, "Broken pipe")) is False

    def test_classifier_runtime_error_does_not_count(self):
        """Unrelated RuntimeError → never a circuit signal."""
        assert is_circuit_failure(RuntimeError("oops")) is False

    def test_classifier_tuples_are_disjoint(self):
        """No exception type should be in both CIRCUIT_FAILURE and TRANSIENT
        tuples — that would make classification ambiguous."""
        cf = set(agent_client.CIRCUIT_FAILURE_EXCEPTIONS)
        tt = set(agent_client.TRANSIENT_TRANSPORT_EXCEPTIONS)
        assert cf.isdisjoint(tt), f"overlap: {cf & tt}"
