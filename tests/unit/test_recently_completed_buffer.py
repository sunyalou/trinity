"""Unit test for #921 simplification: agent-side recently-completed
buffer in `process_registry`.

The buffer is what closes the natural-completion race the backend used
to two-cycle-confirm around. This test exercises the primitive directly:
- `unregister()` writes an entry with the current timestamp
- `list_recently_completed_ids()` returns IDs within the TTL window
- Entries past the TTL are evicted lazily on read
- `register()` does NOT touch the buffer

Module under test: docker/base-image/agent_server/services/process_registry.py
"""
from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Same import shim as test_terminate_async_executor.py — avoid booting the
# full agent_server package (its routers/__init__.py imports optional deps).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub
if "agent_server.services" not in sys.modules:
    _services_stub = types.ModuleType("agent_server.services")
    _services_stub.__path__ = [str(_AGENT_SERVER_DIR / "services")]
    sys.modules["agent_server.services"] = _services_stub
if "agent_server.utils" not in sys.modules:
    _utils_stub = types.ModuleType("agent_server.utils")
    _utils_stub.__path__ = [str(_AGENT_SERVER_DIR / "utils")]
    sys.modules["agent_server.utils"] = _utils_stub

from agent_server.services.process_registry import (  # noqa: E402
    ProcessRegistry,
    RECENTLY_COMPLETED_TTL_SECONDS,
)


def _fake_process(pid: int = 1234) -> MagicMock:
    """A subprocess.Popen stand-in just realistic enough for register()."""
    p = MagicMock(spec=subprocess.Popen)
    p.pid = pid
    p.poll.return_value = None
    return p


@pytest.mark.unit
class TestRecentlyCompletedBuffer:
    def test_unregister_records_id_in_buffer(self):
        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        assert reg.list_recently_completed_ids() == []  # not finished yet

        reg.unregister("exec-1")
        assert "exec-1" in reg.list_recently_completed_ids()

    def test_register_does_not_touch_buffer(self):
        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        assert reg.list_recently_completed_ids() == []

    def test_buffer_evicts_entries_past_ttl(self, monkeypatch):
        """Lazy expiry — past-TTL entries are dropped on the next read."""
        # Tighten the TTL so the test doesn't need to wait minutes.
        import agent_server.services.process_registry as pr_mod
        monkeypatch.setattr(pr_mod, "RECENTLY_COMPLETED_TTL_SECONDS", 0.05)

        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        reg.unregister("exec-1")
        assert "exec-1" in reg.list_recently_completed_ids()

        time.sleep(0.1)
        assert reg.list_recently_completed_ids() == []
        # And the underlying dict is cleared, not just filtered on read.
        assert "exec-1" not in reg._recently_completed

    def test_multiple_unregisters_accumulate(self):
        reg = ProcessRegistry()
        for eid in ("a", "b", "c"):
            reg.register(eid, _fake_process())
            reg.unregister(eid)
        assert set(reg.list_recently_completed_ids()) == {"a", "b", "c"}

    def test_unregister_unknown_id_still_buffers(self):
        """The buffer tracks the lifecycle event, not just known processes.
        Even if `register` was never called (e.g. a defensive unregister
        from an error path), the ID gets buffered so any racing watchdog
        view sees it as 'just completed'."""
        reg = ProcessRegistry()
        reg.unregister("ghost-id")
        assert "ghost-id" in reg.list_recently_completed_ids()

    def test_ttl_sized_above_observed_race_window(self):
        """#921's incident showed a 55s gap between agent unregister and
        backend success-write. The default TTL must comfortably exceed
        that with headroom for backend slowness — this guards a future
        refactor from accidentally shrinking it."""
        assert RECENTLY_COMPLETED_TTL_SECONDS >= 120
