"""Subprocess lifecycle helpers for Claude Code execution.

Extracted from `claude_code.py` per #122 (issue split). Wraps the
`utils.subprocess_pgroup` primitives with `_drain_bounded` — a daemon-thread
budgeted variant of `drain_reader_threads` that protects the executor
thread from the TextIOWrapper-lock deadlock described in Issue #728.

Re-exports the four `utils.subprocess_pgroup` helpers used by the chat and
headless paths so callers only need a single import target.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from typing import Optional

from ..utils.subprocess_pgroup import (
    capture_pgid as _capture_pgid,
    terminate_process_group as _terminate_process_group,
    safe_close_pipes as _safe_close_pipes,
    drain_reader_threads as _drain_reader_threads,
)

__all__ = [
    "_DRAIN_BUDGET_SECONDS",
    "_drain_bounded",
    "_capture_pgid",
    "_terminate_process_group",
    "_safe_close_pipes",
    "_drain_reader_threads",
]

logger = logging.getLogger(__name__)

# Hard budget for drain_reader_threads when called from an executor thread.
# Python's buffered I/O holds an internal lock during readline(); if that
# thread is stuck waiting for pipe data, a concurrent pipe.close() will
# deadlock on the same lock and block asyncio.run() indefinitely (Issue #728).
# Wrapping every asyncio.run(_drain_reader_threads(...)) call in a daemon
# thread with this budget prevents the executor thread from wedging for the
# full task timeout (up to 7200 s). Leaked reader threads are daemon threads
# and die with the container.
_DRAIN_BUDGET_SECONDS = 90


def _drain_bounded(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    pgid: Optional[int] = None,
    execution_tag: Optional[str] = None,
) -> None:
    """Run drain_reader_threads with a hard _DRAIN_BUDGET_SECONDS time cap.

    Prevents a TextIOWrapper lock deadlock in safe_close_pipes (Issue #728)
    from wedging the executor thread for the full task timeout.
    Uses the same asyncio.run() pattern required by drain_reader_threads'
    async-to-sync callers (established in #657); adds a daemon-thread wrapper
    so the budget is enforced at the threading level, not the asyncio level.

    Issue #817: ``execution_tag`` is threaded through to the underlying
    ``drain_reader_threads`` so the env-tag sweep runs after every drain.
    """
    done = threading.Event()

    def _target() -> None:
        try:
            asyncio.run(_drain_reader_threads(
                process, *threads, grace=grace, pgid=pgid,
                execution_tag=execution_tag,
            ))
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_target, daemon=True).start()
    if not done.wait(timeout=_DRAIN_BUDGET_SECONDS):
        logger.warning(
            "[Subprocess] Drain budget (%ds) exceeded — safe_close_pipes may have "
            "deadlocked with reader thread's TextIOWrapper lock; reader threads are "
            "leaked daemon threads (pid=%s). Issue #728.",
            _DRAIN_BUDGET_SECONDS, process.pid,
        )
