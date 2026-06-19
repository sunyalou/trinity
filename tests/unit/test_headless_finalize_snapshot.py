"""Issue #1025 (salvaged from #980 / D19): _finalize_headless_result isolates
itself from a leaked reader thread by snapshotting the run context.

When _drain_bounded reports ``budget_exceeded`` or ``errored``, a reader
thread is leaked and may still be appending to ctx's shared buffers / setting
metadata fields. _finalize_headless_result rebinds ``ctx`` to a deep snapshot
so iteration can't tear and a late append can't be half-read. Clean drains
keep the zero-copy fast path.

These tests exercise the snapshot helper directly (the concurrency-prone
part) plus the gating flag — without spawning a real claude subprocess.
"""
from __future__ import annotations

from unittest.mock import MagicMock

# conftest.py preloads the real agent_server package; just import.
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _snapshot_for_finalize,
)


def _make_ctx() -> HeadlessRunContext:
    ctx = HeadlessRunContext(
        cmd=["claude"],
        task_session_id="task-1025",
        task_start_iso="2026-06-05T00:00:00Z",
        effective_timeout=600,
        images=None,
        prompt="hi",
    )
    ctx.raw_messages.append({"type": "assistant", "n": 1})
    ctx.response_parts.append("hello")
    ctx.metadata.cost_usd = 0.01
    return ctx


def test_snapshot_decouples_lists_from_later_mutation():
    """A post-snapshot append to the live ctx (simulating a leaked reader)
    must NOT leak into the snapshot used by finalize."""
    ctx = _make_ctx()
    snap = _snapshot_for_finalize(ctx)

    # Snapshot must be a different context object with copied buffers.
    assert snap is not ctx
    assert snap.raw_messages is not ctx.raw_messages
    assert snap.response_parts is not ctx.response_parts

    # Leaked reader keeps mutating the ORIGINAL ctx after the snapshot.
    ctx.raw_messages.append({"type": "assistant", "n": 2})
    ctx.response_parts.append(" world")

    # Snapshot is frozen at snapshot time.
    assert snap.raw_messages == [{"type": "assistant", "n": 1}]
    assert snap.response_parts == ["hello"]


def test_snapshot_deep_copies_metadata():
    """metadata must be deep-copied so a leaked reader writing a metadata
    field after the snapshot can't mutate finalize's view."""
    ctx = _make_ctx()
    snap = _snapshot_for_finalize(ctx)

    assert snap.metadata is not ctx.metadata
    assert snap.metadata.cost_usd == 0.01

    # Leaked reader writes more metadata onto the live ctx.
    ctx.metadata.cost_usd = 0.99
    ctx.metadata.num_turns = 7

    assert snap.metadata.cost_usd == 0.01
    assert snap.metadata.num_turns is None


def test_snapshot_falls_back_to_live_ctx_when_every_retry_loses():
    """If the deep-copy loses the race on every attempt, the helper returns the
    live ctx rather than raising — no worse than the pre-#1025 behaviour."""
    ctx = _make_ctx()

    # Stand in a metadata whose deep-copy always loses the race (pydantic
    # forbids patching model_copy on a real instance).
    fake_metadata = MagicMock()
    fake_metadata.model_copy.side_effect = RuntimeError(
        "dictionary changed size during iteration"
    )
    ctx.metadata = fake_metadata

    # Must not raise; falls back to the same ctx instance.
    result = _snapshot_for_finalize(ctx)
    assert result is ctx
    assert fake_metadata.model_copy.call_count == 3  # _SNAPSHOT_RETRY_ATTEMPTS


def test_reader_may_be_live_defaults_false():
    """Clean runs must keep the zero-copy fast path (flag off by default)."""
    assert _make_ctx().reader_may_be_live is False


def test_snapshot_freezes_auth_abort_signal():
    """A leaked stderr reader that sets auth-abort AFTER the snapshot must not
    flip finalize's view — auth_abort_event/reason are frozen at snapshot time
    (regression guard for the review finding on spurious 503s)."""
    ctx = _make_ctx()
    snap = _snapshot_for_finalize(ctx)

    # Snapshot copies must be distinct objects, frozen at the not-set state.
    assert snap.auth_abort_event is not ctx.auth_abort_event
    assert snap.auth_abort_reason is not ctx.auth_abort_reason
    assert snap.auth_abort_event.is_set() is False

    # Leaked reader fires an auth abort on the live ctx after the snapshot.
    ctx.auth_abort_reason.append("Not logged in")
    ctx.auth_abort_event.set()

    # Finalize's view (the snapshot) is unaffected.
    assert snap.auth_abort_event.is_set() is False
    assert snap.auth_abort_reason == []
