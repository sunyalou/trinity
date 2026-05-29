"""Issue #970 — headless executor decoupling from leaked reader threads.

When a headless task's drain exceeds its 90s budget (the #728 safe_close_pipes
deadlock), a reader thread is leaked and may still be mutating the shared run
context. These tests pin the Phase 1 contract:

  * ``_run_headless_subprocess`` records ``ctx.drain_budget_exceeded`` from the
    drain outcome and re-raises the PRE-drain ``stdout_exc`` (the #285
    permission-mode fast-fail) without masking it (D16).
  * A compound failure (process timeout AND wedged reader) re-raises
    ``TimeoutExpired`` and is bounded (D10).
  * ``_finalize_headless_result`` snapshots every read field on the
    budget-exceeded path, so finalize / JSONL-recovery mutations land on an
    isolated copy and the caller's live context is never torn (D19).
  * The orchestrator still finalizes via JSONL recovery
    (``recovered_from_jsonl=True``) when the result line was lost (D17).

These are in-process tests of the executor functions — they do NOT need a
running backend. The true end-to-end "wall-clock ≤ timeout + 90s with a real
wedged pipe" assertion lives in the live ``tests/test_817_subprocess_leak.py``
integration suite, which the canary deploy exercises (a real pipe wedge can't
be reproduced deterministically in-process).

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# tests/unit/conftest.py registers docker/base-image/agent_server as a
# namespace package, so these imports resolve without importlib gymnastics.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services import headless_executor  # noqa: E402
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _finalize_headless_result,
    _run_headless_subprocess,
)


class _FakeRegistry:
    """No-op process registry — _run_headless_subprocess registers/publishes;
    execute_headless_task unregisters in its finally."""

    def register(self, *_a, **_kw):
        return None

    def unregister(self, *_a, **_kw):
        return None

    def publish_log_entry(self, *_a, **_kw):
        return None


def _make_run_ctx(cmd, *, effective_timeout=10) -> HeadlessRunContext:
    return HeadlessRunContext(
        cmd=cmd,
        task_session_id="task-970",
        task_start_iso="2026-05-29T00:00:00Z",
        effective_timeout=effective_timeout,
        images=None,
        prompt="hello",
    )


def _make_finalize_ctx(*, drain_budget_exceeded: bool) -> HeadlessRunContext:
    """A post-subprocess context for the empty-result/JSONL recovery path:
    clean exit, no in-memory text, no result-line metadata."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"],
        task_session_id="task-970-finalize",
        task_start_iso="2026-05-29T00:00:00Z",
        effective_timeout=900,
        images=None,
        prompt="dummy",
    )
    ctx.return_code = 0
    ctx.metadata = ExecutionMetadata()  # cost_usd unset → empty-result path
    ctx.response_parts = []
    ctx.raw_messages = []
    ctx.drain_budget_exceeded = drain_budget_exceeded
    return ctx


# ---------------------------------------------------------------------------
# _run_headless_subprocess — drain outcome plumbing (T2)
# ---------------------------------------------------------------------------


def test_run_headless_sets_drain_budget_exceeded_on_normal_path(monkeypatch):
    """Normal exit + drain returns "budget_exceeded" → ctx flag set True,
    no exception (no pre-drain stdout_exc)."""
    monkeypatch.setattr(
        headless_executor, "get_process_registry", lambda: _FakeRegistry()
    )

    def _fake_drain(_process, *_threads, **_kw):
        return "budget_exceeded"

    monkeypatch.setattr(headless_executor, "_drain_bounded", _fake_drain)

    ctx = _make_run_ctx([sys.executable, "-c", "import time; time.sleep(0.3)"])
    _run_headless_subprocess(ctx)

    assert ctx.drain_budget_exceeded is True
    assert ctx.return_code == 0


def test_run_headless_raises_pre_drain_stdout_exc_on_budget_exceeded(monkeypatch):
    """D16: a permission-mode RuntimeError captured BEFORE the drain must
    still be raised even when the drain leaks (budget_exceeded). A spurious
    exception appended by the leaked reader DURING the drain must be ignored
    — not masked over the real cause and not raised in its place."""
    monkeypatch.setattr(
        headless_executor, "get_process_registry", lambda: _FakeRegistry()
    )

    ctx = _make_run_ctx([sys.executable, "-c", "import time; time.sleep(0.3)"])
    pre = RuntimeError("PRE-DRAIN permission failure")
    ctx.stdout_exc.append(pre)  # captured before process.wait() returns

    def _fake_drain(_process, *_threads, **_kw):
        # Simulate the leaked reader thread mutating ctx during the drain.
        ctx.stdout_exc.append(ValueError("POST-DRAIN leaked-reader noise"))
        return "budget_exceeded"

    monkeypatch.setattr(headless_executor, "_drain_bounded", _fake_drain)

    with pytest.raises(RuntimeError, match="PRE-DRAIN permission failure"):
        _run_headless_subprocess(ctx)

    assert ctx.drain_budget_exceeded is True


def test_run_headless_compound_timeout_and_budget_exceeded(monkeypatch):
    """D10: process times out AND the reader is wedged. ``TimeoutExpired`` must
    re-raise (orchestrator → 504) and the run must be bounded — the drain on
    the timeout path is the budgeted ``_drain_bounded``, not an unbounded join."""
    monkeypatch.setattr(
        headless_executor, "get_process_registry", lambda: _FakeRegistry()
    )

    def _fake_drain(_process, *_threads, **_kw):
        return "budget_exceeded"

    monkeypatch.setattr(headless_executor, "_drain_bounded", _fake_drain)

    # Never-exiting child + a 1s inner timeout forces TimeoutExpired.
    ctx = _make_run_ctx(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        effective_timeout=1,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_headless_subprocess(ctx)

    assert ctx.drain_budget_exceeded is True
    # The terminate on the timeout path reaped the child; make sure nothing is
    # left running if the assertion above changes.
    if ctx.process is not None and ctx.process.poll() is None:
        ctx.process.kill()


# ---------------------------------------------------------------------------
# _finalize_headless_result — full-field snapshot (T3 / D19) + precedence (D17)
# ---------------------------------------------------------------------------


def _patch_jsonl_recovery(monkeypatch, *, recovered_text="SALVAGED-FROM-JSONL"):
    """Drive the empty-result → JSONL text-recovery branch deterministically."""
    monkeypatch.setattr(
        headless_executor,
        "_classify_empty_result",
        lambda metadata, **_kw: (502, {"message": "lost result line", "metadata": {}}),
    )
    monkeypatch.setattr(
        headless_executor, "_recover_metadata_from_jsonl", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        headless_executor,
        "_recover_response_from_jsonl",
        lambda *_a, **_kw: recovered_text,
    )
    monkeypatch.setattr(
        headless_executor, "_extract_compact_events_from_jsonl", lambda *_a, **_kw: []
    )


def test_finalize_snapshots_all_fields_against_mutating_state(monkeypatch):
    """D19: on the budget-exceeded path, finalize operates on a snapshot, so
    recovery's in-place mutations (append to response_parts, set
    recovered_from_jsonl) land on the COPY — the caller's live context (still
    referenced by the leaked reader) is left untouched."""
    _patch_jsonl_recovery(monkeypatch)

    ctx = _make_finalize_ctx(drain_budget_exceeded=True)
    response_text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    # The returned (snapshot) result carries the recovery.
    assert response_text == "SALVAGED-FROM-JSONL"
    assert metadata.recovered_from_jsonl is True

    # The ORIGINAL context the leaked reader still holds was NOT mutated.
    assert ctx.response_parts == [], "snapshot leaked: live response_parts mutated"
    assert not ctx.metadata.recovered_from_jsonl, (
        "snapshot leaked: live metadata mutated"
    )
    # The returned metadata is a distinct object from the live one.
    assert metadata is not ctx.metadata


def test_finalize_clean_drain_mutates_live_context(monkeypatch):
    """Contrast: with drain_budget_exceeded=False there is no leaked reader, so
    finalize keeps the zero-copy fast path and recovery mutates the live ctx
    in place (the proven pre-#970 behaviour)."""
    _patch_jsonl_recovery(monkeypatch)

    ctx = _make_finalize_ctx(drain_budget_exceeded=False)
    response_text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert response_text == "SALVAGED-FROM-JSONL"
    assert metadata.recovered_from_jsonl is True
    # No snapshot → finalize operated on the live ctx directly.
    assert ctx.response_parts == ["SALVAGED-FROM-JSONL"]
    assert ctx.metadata.recovered_from_jsonl is True
    assert metadata is ctx.metadata


def test_finalize_keeps_in_memory_response_parts_first(monkeypatch):
    """D17: when in-memory response_parts has text, JSONL recovery must NOT
    override it — even on the snapshot path. The proven #678 precedence."""
    # _recover_response_from_jsonl would return this if (wrongly) preferred.
    _patch_jsonl_recovery(monkeypatch, recovered_text="WRONG-JSONL-OVERRIDE")

    ctx = _make_finalize_ctx(drain_budget_exceeded=True)
    ctx.response_parts = ["real in-memory answer"]

    response_text, _raw, _meta, _sid = _finalize_headless_result(ctx)
    assert response_text == "real in-memory answer"
    assert "WRONG-JSONL-OVERRIDE" not in response_text


# ---------------------------------------------------------------------------
# _snapshot_for_finalize — the snapshot is retry-guarded against the same race
# it defends finalize from (the metadata deep-copy can lose to a leaked reader)
# ---------------------------------------------------------------------------


def test_snapshot_for_finalize_retries_then_succeeds(monkeypatch):
    """#970: metadata.model_copy(deep=True) can raise "changed size during
    iteration" if the leaked reader sets a field mid-copy. The helper retries
    and returns an ISOLATED copy once the race clears — not the live ctx."""
    import dataclasses

    ctx = _make_finalize_ctx(drain_budget_exceeded=True)
    ctx.response_parts = ["answer"]

    calls = {"n": 0}
    real_replace = dataclasses.replace

    def _flaky_replace(obj, **changes):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("dictionary changed size during iteration")
        return real_replace(obj, **changes)

    monkeypatch.setattr(headless_executor, "replace", _flaky_replace)

    snap = headless_executor._snapshot_for_finalize(ctx)

    assert calls["n"] == 3, "must retry until the race clears"
    assert snap is not ctx, "a successful snapshot is a fresh context"
    assert snap.metadata is not ctx.metadata, "metadata must be a deep copy"
    assert snap.response_parts == ["answer"]
    assert snap.response_parts is not ctx.response_parts


def test_snapshot_for_finalize_falls_back_to_live_ctx_and_warns(monkeypatch, caplog):
    """#970: if every attempt loses the race, the helper returns the LIVE ctx
    (no worse than pre-#970 — finalize just reads the live buffers) and logs a
    warning rather than raising an unhandled RuntimeError out of finalize."""
    import logging

    ctx = _make_finalize_ctx(drain_budget_exceeded=True)

    def _always_raise(_obj, **_changes):
        raise RuntimeError("set changed size during iteration")

    monkeypatch.setattr(headless_executor, "replace", _always_raise)

    with caplog.at_level(
        logging.WARNING, logger="agent_server.services.headless_executor"
    ):
        snap = headless_executor._snapshot_for_finalize(ctx)

    assert snap is ctx, "fallback returns the live context, not a copy"
    assert any(
        "snapshot lost the race" in r.getMessage() for r in caplog.records
    ), "the give-up fallback must be logged, not silent"


def test_finalize_survives_snapshot_race_via_fallback(monkeypatch):
    """End-to-end: even when the snapshot can never win the race, finalize still
    completes (against the live ctx) instead of leaking a RuntimeError."""
    _patch_jsonl_recovery(monkeypatch)

    def _always_raise(_obj, **_changes):
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(headless_executor, "replace", _always_raise)

    ctx = _make_finalize_ctx(drain_budget_exceeded=True)
    response_text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert response_text == "SALVAGED-FROM-JSONL"
    assert metadata.recovered_from_jsonl is True


# ---------------------------------------------------------------------------
# Orchestrator-level: wedged reader → JSONL recovery within the outer margin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_headless_recovers_via_jsonl_when_reader_wedged(monkeypatch):
    """End-to-end through ``execute_headless_task``: the subprocess exited clean
    but the result line was lost to a wedged reader (drain budget exceeded);
    the task is finalized via JSONL recovery (recovered_from_jsonl=True), not
    failed. The outer wait_for (timeout + 90 + 30) does not fire."""
    monkeypatch.setattr(
        headless_executor.agent_state, "claude_code_available", True
    )
    monkeypatch.setattr(
        headless_executor, "get_process_registry", lambda: _FakeRegistry()
    )

    base_ctx = _make_finalize_ctx(drain_budget_exceeded=True)

    def _fake_setup(**_kw):
        return base_ctx

    def _fake_run(ctx):
        # Simulate: clean exit, reader wedged → budget exceeded, no in-memory
        # text (result line lost).
        ctx.return_code = 0
        ctx.drain_budget_exceeded = True

    monkeypatch.setattr(headless_executor, "_setup_headless_command", _fake_setup)
    monkeypatch.setattr(headless_executor, "_run_headless_subprocess", _fake_run)
    _patch_jsonl_recovery(monkeypatch)

    response_text, _log, metadata, _sid = await headless_executor.execute_headless_task(
        prompt="do the thing", timeout_seconds=5
    )

    assert response_text == "SALVAGED-FROM-JSONL"
    assert metadata.recovered_from_jsonl is True
