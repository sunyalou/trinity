"""
Sync HTTP long-poll waiter (issue #498).

When a sync `/task` (parallel=true, async=false) call hits an at-capacity agent,
it spills to the same persistent backlog the async path uses (BACKLOG-001) and
holds the HTTP connection open until the execution reaches a terminal status.
This module owns the in-process registry and signal/wait primitives the chat
router uses to coordinate that.

Key invariants:
- Registry is in-process — multi-worker deployments would need pubsub to fan
  signals across processes; that's not the current backend shape.
- Signal is a no-op when no waiter is registered (the common async path
  doesn't register one).
- Wait combines an asyncio.Future (set by the drain happy path) with a 5s
  DB-poll fallback so terminal flips that don't go through the drain
  (corrupt-metadata, expire_stale, cleanup recovery) still wake the caller.
- Registry is cleaned in the wait helper's finally — caller cancellation,
  timeout, and normal completion all leave the registry empty.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from models import TaskExecutionStatus

logger = logging.getLogger(__name__)


# Module-level state ---------------------------------------------------------

# execution_id -> Future that resolves to {"result": ..., "chat_session_id": ...}
_sync_waiters: Dict[str, asyncio.Future] = {}

# DB-poll cadence safety net for terminal flips that don't signal directly.
# Module-level constant so tests can monkeypatch a tighter interval.
SYNC_WAITER_POLL_INTERVAL = 5.0  # seconds

# Anything that's NOT queued/running counts as terminal for sync waiter purposes.
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskExecutionStatus.SUCCESS,
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.CANCELLED,
        TaskExecutionStatus.SKIPPED,
    }
)


def signal_sync_waiter(
    execution_id: Optional[str],
    result: Any,
    chat_session_id: Optional[str],
) -> None:
    """Notify a sync HTTP caller that the execution has reached terminal state.

    Called from the drain happy path (`_run_async_task_with_persistence`
    finally). Safe no-op when no waiter is registered (the normal async fire-
    and-forget path does not register one), the future is already done
    (caller cancelled), or the execution_id is missing.
    """
    if not execution_id:
        return
    fut = _sync_waiters.get(execution_id)
    if fut is None or fut.done():
        return
    try:
        fut.set_result({"result": result, "chat_session_id": chat_session_id})
    except asyncio.InvalidStateError:
        # Already cancelled between the .done() check and set_result.
        # Safe to ignore — caller is gone.
        pass


async def wait_for_sync_terminal(
    execution_id: str,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """Wait for an execution to reach terminal status.

    Returns:
        - {"result": TaskExecutionResult, "chat_session_id": Optional[str]}
          when the drain happy path completed and signaled directly.
        - None when the polling fallback caught a non-drain terminal flip
          (caller must reconstruct response from the DB row).

    Raises:
        asyncio.TimeoutError if neither fires within `timeout` seconds.
    """
    # Late import keeps this module dependency-light for testing.
    from database import db

    fut = asyncio.get_running_loop().create_future()
    _sync_waiters[execution_id] = fut

    async def _poll_db():
        # Cheap safety net: peek at the row directly every poll interval.
        # Catches terminal flips fired by code paths that don't (or can't)
        # signal the waiter — drain spawn-failure, expire_stale, cleanup
        # service recovery. Latency cost is bounded at one poll interval.
        while True:
            await asyncio.sleep(SYNC_WAITER_POLL_INTERVAL)
            row = db.get_execution(execution_id)
            if row is not None and row.status in TERMINAL_TASK_STATUSES:
                return None  # signals: poll-fallback hit, no rich result

    poll_task = asyncio.create_task(_poll_db())
    try:
        done, pending = await asyncio.wait(
            [fut, poll_task],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError()
        winner = next(iter(done))
        return winner.result()
    finally:
        _sync_waiters.pop(execution_id, None)
        if not poll_task.done():
            poll_task.cancel()
        if not fut.done():
            fut.cancel()
