"""Process-group lifecycle helpers for Claude Code subprocess management.

Issue #407: Claude Code spawns hooks (bash-guardrail.py, file-guardrail.py,
output-scanner.py, …) as child subprocesses that inherit our stdout/stderr
pipes. If a hook — or any grandchild it forks — outlives claude itself, the
inherited pipe write-ends stay open and our readline() blocks forever, even
though ``claude`` is already a ``<defunct>`` zombie. Result: agent-server
wedges at ~83% CPU and stops serving HTTP.

Fix: launch claude with ``start_new_session=True`` so it becomes its own
process-group leader, and kill the entire group on shutdown. That reaps
hook grandchildren too, which closes the inherited pipe FDs and lets our
reader threads unwind naturally.

Important: the caller must capture the pgid **right after** ``Popen()``
(via ``capture_pgid``) and pass it to every helper that operates on the
group. Once ``process.wait()`` / ``process.poll()`` reaps the parent, the
pid is gone and ``os.getpgid(pid)`` raises — but the process group itself
lives on in the kernel as long as it has any member (the grandchildren we
need to kill).

This module is kept free of package-relative imports so it can be
unit-tested without loading the rest of ``agent_server``.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def capture_pgid(process: subprocess.Popen) -> Optional[int]:
    """Return the process group id for ``process``.

    Must be called before the process is reaped (``wait()`` / ``poll()``
    collecting the exit status) — afterwards the pid is gone and
    ``os.getpgid`` raises ``ProcessLookupError``.

    Returns ``None`` on error; helpers fall back to single-process
    signaling in that case.
    """
    try:
        return os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _signal_group_or_process(
    process: subprocess.Popen,
    pgid: Optional[int],
    sig: int,
) -> None:
    """Send ``sig`` to the process group if ``pgid`` is known, otherwise
    fall back to signaling the single process. Silent on ESRCH / EPERM."""
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def terminate_process_group(
    process: subprocess.Popen,
    graceful_timeout: int = 5,
    *,
    pgid: Optional[int] = None,
) -> None:
    """Terminate the subprocess AND its entire process group.

    Sends SIGTERM to the group, waits up to ``graceful_timeout`` seconds
    for the direct child to exit, then sends SIGKILL to the group.

    If ``pgid`` is not provided, it's looked up from ``process.pid`` —
    which only works while the process is still alive or is a zombie.
    After ``process.wait()`` has reaped it, callers MUST pass the pgid
    they captured at spawn time, otherwise the helper falls back to
    signaling the single (already-reaped) pid and grandchildren are
    left running.

    Safe to call on already-exited processes and safe to call multiple
    times.
    """
    if pgid is None:
        pgid = capture_pgid(process)

    # Always attempt a SIGTERM on the group — even if the direct child is
    # already reaped, grandchildren may still be alive in the group.
    _signal_group_or_process(process, pgid, signal.SIGTERM)

    if process.poll() is None:
        try:
            process.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            pass

    # SIGKILL the group unconditionally. If it's already empty, the
    # kernel returns ESRCH and we swallow it.
    _signal_group_or_process(process, pgid, signal.SIGKILL)

    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.error(
                "[Subprocess] pid=%s did not exit after SIGKILL", process.pid
            )


def safe_close_pipes(process: subprocess.Popen) -> None:
    """Close subprocess stdout/stderr without raising.

    Used to unblock reader threads that are stuck on readline() because a
    surviving grandchild still holds the write end of the pipe open.
    Closing our read end causes readline() to raise ValueError or return
    EOF, which lets the thread exit.
    """
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def drain_reader_threads(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    pgid: Optional[int] = None,
) -> None:
    """Join subprocess reader threads with a bounded timeout.

    If any thread is still alive after ``grace`` seconds, a surviving
    process-group member is holding a pipe write end open. Kill the
    group (via ``pgid`` if provided, else looked up) and force-close
    our pipe FDs so readline() returns and threads exit.

    Callers that have already reaped the parent via ``process.wait()``
    must pass ``pgid`` — after reaping, the pid is gone and we'd
    otherwise lose the ability to signal grandchildren.
    """
    alive_threads = [t for t in threads if t is not None]
    for t in alive_threads:
        t.join(timeout=grace)

    stuck = [t for t in alive_threads if t.is_alive()]
    if not stuck:
        return

    logger.warning(
        "[Subprocess] Reader thread(s) stuck after process exit "
        "(pid=%s, stuck_count=%s) — killing process group and closing pipes to unwind",
        process.pid, len(stuck),
    )
    terminate_process_group(process, graceful_timeout=1, pgid=pgid)
    safe_close_pipes(process)
    for t in stuck:
        t.join(timeout=2)

    still_alive = [t for t in stuck if t.is_alive()]
    if still_alive:
        logger.error(
            "[Subprocess] %s reader thread(s) leaked for pid=%s after "
            "close+killpg; continuing anyway",
            len(still_alive), process.pid,
        )


def signal_process_tree(
    process: subprocess.Popen,
    sig: int,
    *,
    pgid: Optional[int] = None,
) -> None:
    """Send ``sig`` to the subprocess's process group if it is a group
    leader; otherwise fall back to signaling the single process.

    Used by ProcessRegistry.terminate() to propagate SIGINT/SIGKILL to
    the full tree instead of just the subprocess parent.
    """
    if pgid is None:
        pgid = capture_pgid(process)
    _signal_group_or_process(process, pgid, sig)
