"""
Agent HTTP Client Service.

Provides a clean interface for communicating with agent containers.
Centralizes URL construction, timeout handling, error handling,
circuit breaking, and retry logic (RELIABILITY-001).

Circuit breaker (#631): state is held in Redis so multiple uvicorn workers
share one source of truth and cannot duplicate-probe a dead agent. State
machine transitions are atomic Lua scripts (no TOCTOU races between workers).
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Any, Dict

import httpx
import redis as _redis
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Circuit Breaker (per-agent, Redis-backed for cross-worker coordination, #631)
# ============================================================================
#
# Why Redis: backend runs with N uvicorn workers. Per-process state means
# each worker probed independently, doubled DB writes, doubled log noise.
# Single Redis hash + Lua scripts give atomic state machine transitions and
# the "only one worker probes at a time" semantics for free.
#
# Redis layout (per agent):
#     agent:circuit:{name}             HASH  state, failures, last_failure_ts,
#                                            next_probe_at, probe_count_since_open
#     agent:circuit:{name}:probe-lock  STRING (NX EX 10) — short-lived probe permit
#
# State machine:
#     closed                    — happy path; every request goes through.
#     open                      — failure_threshold hit; only one half-open probe
#                                 per cooldown window (per cluster, not per worker).
#     dormant                   — too many consecutive failed probes; stops probing
#                                 entirely until the agent container restarts or an
#                                 operator manually triggers a health check.

_CIRCUIT_HASH_PREFIX = "agent:circuit:"
_CIRCUIT_PROBE_LOCK_SUFFIX = ":probe-lock"

# Tunables — exposed at module level so tests / ops can monkeypatch.
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_BASE_COOLDOWN_SECONDS = 30.0
CIRCUIT_MAX_COOLDOWN_SECONDS = 300.0
CIRCUIT_PROBE_LOCK_TTL_SECONDS = 10
# After this many consecutive open-state probes without recovery, give up
# active probing and wait for an external signal (container restart, manual
# health-check trigger). 10 × exponentially-growing backoff ≈ 40min of
# attempts before falling silent.
CIRCUIT_DORMANT_AFTER_OPEN_PROBES = 10


# ----- Redis client (lazy, cached, fail-open on unreachability) -------------

_redis_client: Optional[_redis.Redis] = None
_redis_client_lock = threading.Lock()


def _get_circuit_redis() -> Optional[_redis.Redis]:
    """Return a Redis client, or None if Redis is unreachable.

    Mirrors the fail-open pattern used by webhooks.py: the circuit breaker
    is best-effort coordination — if Redis is down we fall through to
    allowing the request and let the underlying HTTP failure surface.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_client_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            from config import REDIS_URL
            client = _redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            _redis_client = client
        except Exception as e:
            logger.warning("Circuit breaker: Redis unavailable (%s) — failing open", e)
            return None
    return _redis_client


def _reset_circuit_redis_client() -> None:
    """Drop the cached client so the next call rebuilds. For tests + recovery."""
    global _redis_client, _ALLOW_SCRIPT, _RECORD_FAILURE_SCRIPT, _RECORD_SUCCESS_SCRIPT
    with _redis_client_lock:
        _redis_client = None
        _ALLOW_SCRIPT = None
        _RECORD_FAILURE_SCRIPT = None
        _RECORD_SUCCESS_SCRIPT = None


# ----- Lua scripts (atomic state machine transitions) -----------------------

# allow_request: returns "allow" | "probe" | "deny".
#   closed   → "allow"
#   dormant  → "deny"
#   open     → if past next_probe_at AND we win SET-NX-EX on probe-lock → "probe"
#              otherwise → "deny"
_ALLOW_REQUEST_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'closed' then
    return 'allow'
end
if state == 'dormant' then
    return 'deny'
end
local now = tonumber(ARGV[1])
local next_probe_at = tonumber(redis.call('HGET', KEYS[1], 'next_probe_at') or '0')
if now < next_probe_at then
    return 'deny'
end
local lock_ttl = tonumber(ARGV[2])
local locked = redis.call('SET', KEYS[2], '1', 'NX', 'EX', lock_ttl)
if locked then
    return 'probe'
else
    return 'deny'
end
"""

# record_failure: increments failure count, transitions to open / dormant
# with exponential backoff. Returns {prior_state, new_state} so the Python
# layer can log the transition exactly once per cluster (atomic Lua means
# only one worker observes the transition).
_RECORD_FAILURE_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local base = tonumber(ARGV[3])
local max_cd = tonumber(ARGV[4])
local dormant_threshold = tonumber(ARGV[5])

local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
redis.call('HSET', KEYS[1], 'last_failure_ts', ARGV[1])

-- Below threshold from a clean closed state: stay closed, no backoff yet.
if prior_state == 'closed' and failures < threshold then
    return {'closed', 'closed'}
end

-- We're transitioning to (or staying in) open. Tick probe counter.
local probe_count = redis.call('HINCRBY', KEYS[1], 'probe_count_since_open', 1)
local new_state = 'open'
if probe_count >= dormant_threshold then
    new_state = 'dormant'
end

-- Cooldown = min(base * 2^(probe_count-1), max_cd). Cap exponent for safety.
local exp = probe_count - 1
if exp > 20 then exp = 20 end
local cooldown = base * math.pow(2, exp)
if cooldown > max_cd then cooldown = max_cd end
local next_probe_at = now + cooldown

redis.call('HSET', KEYS[1], 'state', new_state, 'next_probe_at', next_probe_at)
-- Release the probe-lock — whoever called us holds it; clearing here lets
-- the next eligible probe race fairly after the cooldown.
redis.call('DEL', KEYS[2])

return {prior_state, new_state}
"""

# record_success: full reset to closed. Returns prior_state for logging.
_RECORD_SUCCESS_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0,
           'probe_count_since_open', 0, 'next_probe_at', 0)
redis.call('DEL', KEYS[2])
return prior_state
"""

_ALLOW_SCRIPT = None
_RECORD_FAILURE_SCRIPT = None
_RECORD_SUCCESS_SCRIPT = None


def _ensure_scripts(client: _redis.Redis):
    global _ALLOW_SCRIPT, _RECORD_FAILURE_SCRIPT, _RECORD_SUCCESS_SCRIPT
    if _ALLOW_SCRIPT is None:
        _ALLOW_SCRIPT = client.register_script(_ALLOW_REQUEST_LUA)
        _RECORD_FAILURE_SCRIPT = client.register_script(_RECORD_FAILURE_LUA)
        _RECORD_SUCCESS_SCRIPT = client.register_script(_RECORD_SUCCESS_LUA)
    return _ALLOW_SCRIPT, _RECORD_FAILURE_SCRIPT, _RECORD_SUCCESS_SCRIPT


# ----- Failure classification (#474) ---------------------------------------
#
# Single source of truth for which exception types should increment the
# circuit-breaker failure counter. Imported by services/monitoring_service.py
# so the /health probe applies the same rule as inline /api/* requests.
#
# Rationale (#474): a dropped MCP sync connection produces a fan-out of
# transient socket teardowns (broken pipe, connection reset, mid-write
# errors) plus read-timeouts from background pollers polling a *busy* (not
# unhealthy) agent. Those signals are noisy — only TCP-level unreachability
# should open the circuit.

CIRCUIT_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

# Exceptions we should re-raise as AgentNotReachableError (so existing
# callers' `except AgentClientError` blocks keep working) but NOT count
# toward the circuit threshold. httpx.PoolTimeout is included because
# pool exhaustion is a client-side resource issue, not agent unhealth.
TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.WriteError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def is_circuit_failure(exc: BaseException) -> bool:
    """Return True if `exc` represents a real "agent unreachable" signal.

    Single source of truth shared by AgentClient._request() and
    monitoring_service.check_network_health() so both surfaces apply the
    same rule. See CIRCUIT_FAILURE_EXCEPTIONS for the canonical list.
    """
    return isinstance(exc, CIRCUIT_FAILURE_EXCEPTIONS)


# ----- Public state object --------------------------------------------------

class CircuitState:
    """Per-agent circuit breaker, Redis-backed (#631).

    The class is a thin facade over Redis ops — no in-process state to drift
    between workers. Construction is cheap (no DB / network I/O); state is
    fetched per call. Callers should still cache the instance per request
    rather than re-constructing for each method call.
    """

    def __init__(self, agent_name: str, redis_client: Optional[_redis.Redis] = None):
        self.agent_name = agent_name
        self._key = f"{_CIRCUIT_HASH_PREFIX}{agent_name}"
        self._lock_key = f"{self._key}{_CIRCUIT_PROBE_LOCK_SUFFIX}"
        self._redis = redis_client  # None → resolve lazily, supports per-call swap

    def _client(self) -> Optional[_redis.Redis]:
        return self._redis if self._redis is not None else _get_circuit_redis()

    def allow_request(self) -> bool:
        """Decide whether the caller may issue an HTTP request to the agent."""
        client = self._client()
        if client is None:
            return True  # Fail-open when Redis is unreachable
        try:
            allow, _, _ = _ensure_scripts(client)
            verdict = allow(
                keys=[self._key, self._lock_key],
                args=[time.time(), CIRCUIT_PROBE_LOCK_TTL_SECONDS],
                client=client,
            )
            # decode_responses=True returns str; older paths may still hand
            # back bytes (defensive).
            if isinstance(verdict, bytes):
                verdict = verdict.decode()
            return verdict in ("allow", "probe")
        except Exception as e:
            logger.warning("Circuit allow_request fell back to allow (%s)", e)
            _reset_circuit_redis_client()
            return True

    def record_failure(self) -> str:
        """Record a failure. Returns the new state ('closed'|'open'|'dormant')."""
        client = self._client()
        if client is None:
            return "closed"  # Fail-open: pretend nothing changed
        try:
            _, record_failure, _ = _ensure_scripts(client)
            result = record_failure(
                keys=[self._key, self._lock_key],
                args=[
                    time.time(),
                    CIRCUIT_FAILURE_THRESHOLD,
                    CIRCUIT_BASE_COOLDOWN_SECONDS,
                    CIRCUIT_MAX_COOLDOWN_SECONDS,
                    CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
                ],
                client=client,
            )
            prior_state, new_state = _decode_pair(result)
            if prior_state != new_state:
                if new_state == "open":
                    failures = self._read_int("failures")
                    logger.warning(
                        "Circuit OPENED for agent %s after %d failures",
                        self.agent_name, failures,
                    )
                elif new_state == "dormant":
                    logger.warning(
                        "Circuit DORMANT for agent %s — stopped probing after %d "
                        "consecutive open-probe failures (manual recovery required)",
                        self.agent_name, CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
                    )
            return new_state
        except Exception as e:
            logger.warning("Circuit record_failure swallowed (%s)", e)
            _reset_circuit_redis_client()
            return "closed"

    def record_success(self) -> None:
        client = self._client()
        if client is None:
            return
        try:
            _, _, record_success = _ensure_scripts(client)
            prior = record_success(
                keys=[self._key, self._lock_key],
                args=[],
                client=client,
            )
            if isinstance(prior, bytes):
                prior = prior.decode()
            if prior and prior != "closed":
                logger.info(
                    "Circuit CLOSED for agent %s (recovered from %s)",
                    self.agent_name, prior,
                )
        except Exception as e:
            logger.warning("Circuit record_success swallowed (%s)", e)
            _reset_circuit_redis_client()

    def _read_int(self, field_name: str) -> int:
        client = self._client()
        if client is None:
            return 0
        raw = client.hget(self._key, field_name)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def to_dict(self) -> dict:
        client = self._client()
        if client is None:
            return {"state": "closed", "failure_count": 0, "cooldown_remaining": 0.0}
        data = client.hgetall(self._key) or {}
        return _state_dict(data)

    # --- Compatibility shims so callers that read .state / .failure_count
    # directly continue to work. Each property does a Redis read; callers in
    # hot paths should prefer to_dict() to bundle them into one HGETALL.

    @property
    def state(self) -> str:
        client = self._client()
        if client is None:
            return "closed"
        return client.hget(self._key, "state") or "closed"

    @property
    def failure_count(self) -> int:
        return self._read_int("failures")


def _decode_pair(result: Any) -> tuple[str, str]:
    """Lua MULTI return → (prior_state, new_state) as strings."""
    if not result or len(result) != 2:
        return ("closed", "closed")
    prior, new = result
    if isinstance(prior, bytes):
        prior = prior.decode()
    if isinstance(new, bytes):
        new = new.decode()
    return prior, new


def _state_dict(data: Dict[str, Any]) -> dict:
    """Translate a raw HGETALL result into the public to_dict shape."""
    state = data.get("state") or "closed"
    try:
        failures = int(data.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    try:
        next_probe_at = float(data.get("next_probe_at") or 0)
    except (TypeError, ValueError):
        next_probe_at = 0.0
    cooldown_remaining = max(0.0, next_probe_at - time.time()) if state == "open" else 0.0
    return {
        "state": state,
        "failure_count": failures,
        "cooldown_remaining": cooldown_remaining,
    }


def _get_circuit(agent_name: str) -> CircuitState:
    """Construct a fresh CircuitState facade for the agent.

    No registry — state lives in Redis. Construction is cheap.
    """
    return CircuitState(agent_name=agent_name)


def get_all_circuit_states() -> Dict[str, dict]:
    """Return the state dict for every agent that has any circuit history."""
    client = _get_circuit_redis()
    if client is None:
        return {}
    result: Dict[str, dict] = {}
    try:
        for key in client.scan_iter(match=f"{_CIRCUIT_HASH_PREFIX}*", count=200):
            if key.endswith(_CIRCUIT_PROBE_LOCK_SUFFIX):
                continue
            agent_name = key[len(_CIRCUIT_HASH_PREFIX):]
            data = client.hgetall(key)
            result[agent_name] = _state_dict(data or {})
    except Exception as e:
        logger.warning("Circuit get_all_states failed: %s", e)
        _reset_circuit_redis_client()
    return result


def force_circuit_dormant(agent_name: str, *, reason: str = "manual") -> None:
    """Park an agent's circuit in dormant state. Used by autonomy-off (#631 AC#5).

    Idempotent. Safe to call from any worker.
    """
    client = _get_circuit_redis()
    if client is None:
        return
    try:
        client.hset(
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}",
            mapping={
                "state": "dormant",
                "next_probe_at": time.time() + CIRCUIT_MAX_COOLDOWN_SECONDS,
            },
        )
        client.delete(f"{_CIRCUIT_HASH_PREFIX}{agent_name}{_CIRCUIT_PROBE_LOCK_SUFFIX}")
        logger.info("Circuit forced DORMANT for %s (reason=%s)", agent_name, reason)
    except Exception as e:
        logger.warning("force_circuit_dormant(%s) swallowed: %s", agent_name, e)


def reset_circuit(agent_name: str) -> None:
    """Reset an agent's circuit to closed. Used by autonomy-on / manual recovery."""
    client = _get_circuit_redis()
    if client is None:
        return
    try:
        client.delete(
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}",
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}{_CIRCUIT_PROBE_LOCK_SUFFIX}",
        )
        logger.info("Circuit reset to CLOSED for %s", agent_name)
    except Exception as e:
        logger.warning("reset_circuit(%s) swallowed: %s", agent_name, e)


# ============================================================================
# Connection Pool (shared httpx.AsyncClient per agent)
# ============================================================================

_client_pool: Dict[str, httpx.AsyncClient] = {}


def _get_http_client(base_url: str) -> httpx.AsyncClient:
    """Get or create a persistent HTTP client for a base URL."""
    client = _client_pool.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=base_url,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
        _client_pool[base_url] = client
    return client


async def close_all_clients():
    """Close all pooled HTTP clients. Call on app shutdown."""
    for client in _client_pool.values():
        await client.aclose()
    _client_pool.clear()


# ============================================================================
# Response Models
# ============================================================================

@dataclass
class AgentChatMetrics:
    """Observability data extracted from agent chat response."""
    context_used: int
    context_max: int
    context_percent: float
    cost_usd: Optional[float]
    tool_calls_json: Optional[str]
    execution_log_json: Optional[str]


@dataclass
class AgentChatResponse:
    """Parsed response from agent chat endpoint."""
    response_text: str
    metrics: AgentChatMetrics
    raw_response: Dict[str, Any]


@dataclass
class AgentSessionInfo:
    """Agent context/session information."""
    context_tokens: int
    context_window: int
    context_percent: float
    total_cost_usd: Optional[float] = None


# ============================================================================
# Exceptions
# ============================================================================

class AgentClientError(Exception):
    """Base exception for agent client errors."""
    pass


class AgentNotReachableError(AgentClientError):
    """Agent container is not responding."""
    pass


class AgentCircuitOpenError(AgentClientError):
    """Circuit breaker is open — agent is known to be unhealthy."""
    pass


class AgentRequestError(AgentClientError):
    """Agent returned an error response."""
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


# ============================================================================
# Agent Client
# ============================================================================

class AgentClient:
    """
    HTTP client for agent container communication.

    Centralizes:
    - URL construction
    - Timeout handling
    - Error handling
    - Response parsing
    """

    # Default timeouts
    CHAT_TIMEOUT = 900.0      # 15 minutes for chat
    SESSION_TIMEOUT = 5.0     # 5 seconds for session info
    DEFAULT_TIMEOUT = 30.0    # 30 seconds default

    def __init__(self, agent_name: str):
        """
        Initialize client for a specific agent.

        Args:
            agent_name: Name of the agent (without 'agent-' prefix)
        """
        self.agent_name = agent_name
        self.base_url = f"http://agent-{agent_name}:8000"
        self._circuit = _get_circuit(agent_name)

    # ========================================================================
    # Core HTTP Methods
    # ========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        timeout: float = None,
        **kwargs
    ) -> httpx.Response:
        """
        Make an HTTP request to the agent.

        Checks circuit breaker before sending. Records success/failure
        to the per-agent circuit state.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: URL path (e.g., "/api/chat")
            timeout: Request timeout in seconds
            **kwargs: Additional arguments for httpx request

        Returns:
            httpx.Response

        Raises:
            AgentCircuitOpenError: If circuit breaker is open
            AgentNotReachableError: If connection fails
            AgentRequestError: If request fails with error status
        """
        if not self._circuit.allow_request():
            raise AgentCircuitOpenError(
                f"Circuit open for agent {self.agent_name} "
                f"(failures={self._circuit.failure_count})"
            )

        timeout = timeout or self.DEFAULT_TIMEOUT
        client = _get_http_client(self.base_url)

        try:
            response = await client.request(
                method, path, timeout=timeout, **kwargs
            )
            self._circuit.record_success()
            return response

        except asyncio.CancelledError:
            # Cancellation (e.g. MCP client drop propagating through FastAPI)
            # is not an agent-health signal. Explicit re-raise so a future
            # maintainer can't shadow it with a broader catch.
            raise

        except CIRCUIT_FAILURE_EXCEPTIONS as e:
            # ConnectError / ConnectTimeout — agent is genuinely unreachable.
            # ConnectTimeout is a TimeoutException subclass, so this branch
            # must come before any TimeoutException catch.
            self._circuit.record_failure()
            raise AgentNotReachableError(
                f"Cannot reach agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

        except TRANSIENT_TRANSPORT_EXCEPTIONS as e:
            # Read/Write timeouts, pool exhaustion, mid-write broken-pipe /
            # reset, garbled HTTP framing. Surface to the caller as the
            # existing typed error so `except AgentClientError` blocks keep
            # working, but DO NOT count toward the circuit threshold (#474).
            #
            # NOT caught here (propagate raw, bypass the AgentClientError
            # typing contract): httpx.CloseError, httpx.LocalProtocolError,
            # httpx.ProxyError, httpx.UnsupportedProtocol, httpx.InvalidURL,
            # and raw OSError subclasses (BrokenPipeError, ConnectionResetError).
            # Those are client-side / configuration bugs, not agent-health
            # signals — letting them surface loudly is intentional.
            raise AgentNotReachableError(
                f"Transient transport error to agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

    async def get(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a GET request to the agent."""
        return await self._request("GET", path, timeout, **kwargs)

    async def post(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a POST request to the agent."""
        return await self._request("POST", path, timeout, **kwargs)

    async def put(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a PUT request to the agent."""
        return await self._request("PUT", path, timeout, **kwargs)

    async def delete(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a DELETE request to the agent."""
        return await self._request("DELETE", path, timeout, **kwargs)

    # ========================================================================
    # Chat Operations
    # ========================================================================

    async def chat(
        self,
        message: str,
        stream: bool = False,
        timeout: float = None
    ) -> AgentChatResponse:
        """
        Send a chat message to the agent.

        Args:
            message: Message to send
            stream: Whether to stream the response
            timeout: Request timeout (default: 5 minutes)

        Returns:
            AgentChatResponse with parsed metrics

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        response = await self.post(
            "/api/chat",
            json={"message": message, "stream": stream},
            timeout=timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_chat_response(result)

    async def task(
        self,
        message: str,
        timeout: float = None,
        execution_id: Optional[str] = None
    ) -> AgentChatResponse:
        """
        Execute a stateless task on the agent (no conversation context).

        Unlike chat(), this endpoint:
        - Does NOT maintain conversation history
        - Each call is independent (no --continue flag)
        - Returns raw Claude Code execution log (full transcript)

        Use this for scheduled executions and independent tasks.

        Args:
            message: Task prompt to execute
            timeout: Request timeout (default: 15 minutes)
            execution_id: Optional execution ID for process registry (enables termination and live streaming)

        Returns:
            AgentChatResponse with parsed metrics and raw execution log

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        payload = {"message": message, "timeout_seconds": int(timeout)}
        if execution_id:
            payload["execution_id"] = execution_id

        response = await self.post(
            "/api/task",
            json=payload,
            timeout=timeout + 10  # Add buffer to agent timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_task_response(result)

    def _parse_task_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent task response into structured data.

        Similar to _parse_chat_response but handles /api/task format
        which returns raw Claude Code execution log.
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data (task response has metadata but no session)
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage from metadata
        context_used = metadata.get("input_tokens", 0)
        context_max = metadata.get("context_window", 200000)
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd")

        # Execution log - raw Claude Code transcript
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    def _extract_error_detail(self, response: httpx.Response) -> str:
        """Extract detailed error message from agent HTTP response."""
        try:
            error_data = response.json()
            if "detail" in error_data:
                return error_data["detail"]
        except Exception:
            pass
        # Fall back to response text if JSON parsing fails
        if response.text:
            return response.text[:500]
        return f"HTTP {response.status_code} error"

    def _parse_chat_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent chat response into structured data.

        Extracts:
        - Response text (truncated if > 10000 chars)
        - Context usage (tokens, window, percentage)
        - Cost
        - Tool calls / execution log
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data
        session_data = result.get("session", {})
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage
        # NOTE: cache_creation_tokens and cache_read_tokens are SUBSETS of input_tokens
        # for billing purposes, NOT additional tokens. Do NOT sum them.
        context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
        context_max = session_data.get("context_window") or metadata.get("context_window", 200000)
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

        # Tool calls / execution log
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    # ========================================================================
    # Session / Context Operations
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def get_session(self, timeout: float = None) -> Optional[AgentSessionInfo]:
        """
        Get current session/context information.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            AgentSessionInfo or None if request fails
        """
        timeout = timeout or self.SESSION_TIMEOUT

        try:
            response = await self.get("/api/chat/session", timeout=timeout)
            if response.status_code == 200:
                session = response.json()
                context_tokens = session.get("context_tokens", 0)
                context_window = session.get("context_window", 200000)
                return AgentSessionInfo(
                    context_tokens=context_tokens,
                    context_window=context_window,
                    context_percent=round(
                        context_tokens / max(context_window, 1) * 100, 1
                    ),
                    total_cost_usd=session.get("total_cost_usd")
                )
        except AgentClientError:
            pass
        return None

    # ========================================================================
    # File Operations
    # ========================================================================

    async def read_file(
        self,
        path: str,
        timeout: float = 30.0
    ) -> dict:
        """
        Read content from a file in the agent's workspace.

        Args:
            path: File path within /home/developer
            timeout: Request timeout

        Returns:
            dict with success status and content
        """
        try:
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            response = await self.get(
                f"/api/files/download?path={encoded_path}",
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, "content": response.text}
            elif response.status_code == 404:
                return {"success": True, "content": None, "not_found": True}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    async def write_file(
        self,
        path: str,
        content: str,
        timeout: float = 30.0,
        platform: bool = False
    ) -> dict:
        """
        Write content to a file in the agent's workspace.
        Creates parent directories if they don't exist.

        Args:
            path: File path within /home/developer
            content: File content to write
            timeout: Request timeout
            platform: If True, allows writes to .trinity directory (platform-initiated)

        Returns:
            dict with success status and file info
        """
        try:
            # URL encode the path for query parameter
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            # Add platform flag if needed
            query = f"path={encoded_path}"
            if platform:
                query += "&platform=true"

            response = await self.put(
                f"/api/files?{query}",
                json={"content": content},
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, **response.json()}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # Health Check
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check if agent is healthy and responding.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            True if agent responds to health check
        """
        try:
            response = await self.get("/api/health", timeout=timeout)
            return response.status_code == 200
        except AgentCircuitOpenError:
            return False
        except AgentClientError:
            return False


# ============================================================================
# Factory Function
# ============================================================================

def get_agent_client(agent_name: str) -> AgentClient:
    """
    Factory function to create an AgentClient.

    Args:
        agent_name: Name of the agent

    Returns:
        AgentClient instance
    """
    return AgentClient(agent_name)
