"""
Agent-server hardening tests (#333).

Covers three pieces of accumulator-leak hardening shipped against the
"futex spin after days of uptime" report:

  - AgentState.add_message FIFO-trims conversation_history once over the cap.
  - gemini_runtime._executor is a module-level singleton, not allocated per
    call (mirrors the claude_code.py:63 pattern).
  - /health includes a `diagnostics` block with the runtime gauges
    (thread_count, asyncio_task_count, running_executions, history size)
    so future repros can be diagnosed with one curl instead of strace.

These do not prove the original symptom is gone — that needs a multi-day
soak. They lock in the leak-surface reductions and the observability hook.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# tests/unit/conftest.py:_preload_real_agent_server() already registers
# docker/base-image/agent_server as a namespace package in sys.modules,
# so plain `from agent_server.<sub> import <X>` works here without any
# importlib gymnastics in this file.

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"


# ---------------------------------------------------------------------------
# Conversation history bound
# ---------------------------------------------------------------------------


class TestConversationHistoryBound:
    """state.AgentState.add_message must FIFO-trim once over history_limit."""

    def _fresh_state(self, limit: int):
        # Re-import the state module against AGENT_HISTORY_LIMIT for this test.
        from agent_server import state as state_mod  # noqa: WPS433

        s = state_mod.AgentState()
        s.history_limit = limit
        s.conversation_history = []
        return s

    def test_under_limit_keeps_all_messages(self):
        s = self._fresh_state(limit=10)
        for i in range(5):
            s.add_message("user", f"msg-{i}")
        assert len(s.conversation_history) == 5
        assert s.conversation_history[0].content == "msg-0"
        assert s.conversation_history[-1].content == "msg-4"

    def test_over_limit_drops_oldest(self):
        s = self._fresh_state(limit=3)
        for i in range(7):
            s.add_message("user", f"msg-{i}")
        assert len(s.conversation_history) == 3
        # Oldest dropped; tail preserved.
        assert s.conversation_history[0].content == "msg-4"
        assert s.conversation_history[-1].content == "msg-6"

    def test_resolve_history_limit_default(self, monkeypatch):
        from agent_server import state as state_mod  # noqa: WPS433

        monkeypatch.delenv("AGENT_HISTORY_LIMIT", raising=False)
        assert state_mod._resolve_history_limit() == state_mod._DEFAULT_HISTORY_LIMIT

    def test_resolve_history_limit_override(self, monkeypatch):
        from agent_server import state as state_mod  # noqa: WPS433

        monkeypatch.setenv("AGENT_HISTORY_LIMIT", "42")
        assert state_mod._resolve_history_limit() == 42

    def test_resolve_history_limit_invalid_falls_back(self, monkeypatch):
        from agent_server import state as state_mod  # noqa: WPS433

        monkeypatch.setenv("AGENT_HISTORY_LIMIT", "not-an-int")
        assert state_mod._resolve_history_limit() == state_mod._DEFAULT_HISTORY_LIMIT

    def test_resolve_history_limit_non_positive_falls_back(self, monkeypatch):
        from agent_server import state as state_mod  # noqa: WPS433

        monkeypatch.setenv("AGENT_HISTORY_LIMIT", "0")
        assert state_mod._resolve_history_limit() == state_mod._DEFAULT_HISTORY_LIMIT


# ---------------------------------------------------------------------------
# Gemini executor singleton
# ---------------------------------------------------------------------------


class TestGeminiExecutorSingleton:
    """gemini_runtime._executor must be a module-level singleton.

    The pre-#333 code allocated `ThreadPoolExecutor(max_workers=1)` on every
    call to execute(). Per-call executors rely on CPython weakref-callback
    cleanup of worker threads which is not deterministic under load; the
    long-uptime futex-spin symptom matches the kind of pthread_cond_timedwait
    activity those would produce.
    """

    def test_executor_is_module_level_threadpoolexecutor(self):
        from agent_server.services import gemini_runtime  # noqa: WPS433

        assert hasattr(gemini_runtime, "_executor"), (
            "gemini_runtime._executor must exist as a module-level singleton"
        )
        assert isinstance(gemini_runtime._executor, ThreadPoolExecutor)

    def test_executor_identity_is_stable_across_module_lookups(self):
        from agent_server.services import gemini_runtime  # noqa: WPS433

        first = gemini_runtime._executor
        second = gemini_runtime._executor
        assert first is second, "executor must not be replaced on attribute access"

    def test_no_per_call_threadpoolexecutor_in_execute_methods(self):
        """Catch a regression where someone re-introduces per-call allocation."""
        path = (
            _BASE_IMAGE / "agent_server" / "services" / "gemini_runtime.py"
        )
        source = path.read_text()
        # The legacy pattern was `executor = ThreadPoolExecutor(max_workers=1)`
        # at function scope, paired with an immediate `run_in_executor(executor,
        # ...)`. The module-level singleton uses `_executor` (underscore-prefixed)
        # plus `run_in_executor(_executor, ...)`, so a bare `executor` reference
        # to run_in_executor would mean someone re-introduced the per-call form.
        assert "run_in_executor(executor" not in source, (
            "gemini_runtime should not allocate a fresh ThreadPoolExecutor per call"
        )
        # Belt-and-braces: only one ThreadPoolExecutor instantiation in the file.
        assert source.count("ThreadPoolExecutor(") == 1, (
            "gemini_runtime should instantiate exactly one ThreadPoolExecutor "
            "(the module-level singleton)"
        )


# ---------------------------------------------------------------------------
# /health diagnostics
# ---------------------------------------------------------------------------


class TestHealthDiagnostics:
    """info._diagnostics must expose the leak-spotting gauges."""

    def test_diagnostics_keys_present(self):
        from agent_server.routers import info as info_mod  # noqa: WPS433

        diag = info_mod._diagnostics()
        for key in (
            "thread_count",
            "asyncio_task_count",
            "running_executions",
            "conversation_history_size",
            "conversation_history_limit",
        ):
            assert key in diag, f"diagnostics missing key {key!r}"

    def test_diagnostics_thread_count_positive(self):
        from agent_server.routers import info as info_mod  # noqa: WPS433

        diag = info_mod._diagnostics()
        # At least the main thread is alive.
        assert diag["thread_count"] >= 1

    def test_diagnostics_history_size_reflects_state(self):
        from agent_server.routers import info as info_mod  # noqa: WPS433
        from agent_server.state import agent_state  # noqa: WPS433

        before = info_mod._diagnostics()["conversation_history_size"]
        agent_state.add_message("user", "ping for diagnostic test")
        try:
            after = info_mod._diagnostics()["conversation_history_size"]
            assert after == before + 1
        finally:
            # Pop the test message so the global agent_state isn't dirtied
            # for sibling tests in this process.
            agent_state.conversation_history.pop()

    @pytest.mark.asyncio
    async def test_health_endpoint_includes_diagnostics(self):
        """Hit the /health route directly via FastAPI test client."""
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi.testclient not available")

        from fastapi import FastAPI

        from agent_server.routers import info as info_mod  # noqa: WPS433

        app = FastAPI()
        app.include_router(info_mod.router)

        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "healthy"
        assert "diagnostics" in payload, "/health must include diagnostics block"
        diag = payload["diagnostics"]
        assert "thread_count" in diag
        assert "conversation_history_limit" in diag

    @pytest.mark.asyncio
    async def test_health_omits_opencode_availability_for_non_opencode(self, monkeypatch):
        from agent_server.routers import info as info_mod  # noqa: WPS433

        monkeypatch.setattr(info_mod.agent_state, "agent_runtime", "claude-code")
        monkeypatch.setattr(info_mod.agent_state, "runtime_available", True)

        payload = await info_mod.health_check()

        assert "claude_available" in payload
        assert "runtime_available" in payload
        assert "opencode_available" not in payload
