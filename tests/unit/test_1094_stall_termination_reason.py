"""Unit tests for #1094: stall-watchdog kill must not be mislabeled as a
max-duration timeout.

The per-tool no-output stall watchdog and the max-duration budget are two
distinct termination paths that both raise ``subprocess.TimeoutExpired`` out
of ``_run_headless_subprocess``. Before #1094 the terminal 504 always claimed
"Task execution timed out after {effective_timeout} seconds" regardless of
which fired — so a 300s stall-kill at ~531s wall-clock was recorded as
"timed out after 2400 seconds", misleading operators into bumping the
execution timeout (the wrong knob).

Fix: the watchdog records ``ctx.termination_reason`` ("stall_no_output" vs
"max_duration") and ``ctx.stalled_tool`` before re-raising, and the
orchestrator builds a reason-specific 504 from those fields.

These tests assert the context-level signal (the load-bearing propagation).

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import subprocess
import time

import pytest

# Reuse the fake-Popen / context harness from the #970 wait-loop tests.
from test_headless_executor_970_timeout import (  # noqa: E402
    _ctx,
    _FakePopen,
    _install_popen,
    patched_loop,  # noqa: F401  (pytest fixture)
    _TOOL_USE_LINE,
)
from agent_server.services import headless_executor as he  # noqa: E402


def test_stall_sets_stall_no_output_reason(patched_loop):  # noqa: F811
    """An open tool_use silent past the stall limit records
    termination_reason='stall_no_output' and the offending tool name."""
    monkeypatch, _term = patched_loop
    monkeypatch.setattr(he, "_STALL_LIMIT_S", 0.0)
    _install_popen(monkeypatch, _FakePopen([_TOOL_USE_LINE], never_exits=True))
    ctx = _ctx(effective_timeout=30)  # large — budget must NOT be what fires

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        he._run_headless_subprocess(ctx)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, "stall should fire fast, not at the 30s budget"
    assert ctx.termination_reason == "stall_no_output"
    assert ctx.stalled_tool == "mcp__x__hang"


def test_budget_exhaustion_sets_max_duration_reason(patched_loop):  # noqa: F811
    """No result and no open tool → the budget bounds the wait and records
    termination_reason='max_duration' with no stalled tool."""
    monkeypatch, _term = patched_loop
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.1)
    _install_popen(monkeypatch, _FakePopen([], never_exits=True))
    ctx = _ctx(effective_timeout=0.3)

    with pytest.raises(subprocess.TimeoutExpired):
        he._run_headless_subprocess(ctx)

    assert ctx.termination_reason == "max_duration"
    assert ctx.stalled_tool is None
