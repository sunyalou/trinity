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
import sys
import threading
from typing import IO, Iterable, Optional

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


def _kill_pipe_write_holders(pipes: Iterable[Optional[IO]]) -> int:
    """SIGKILL processes holding the write end of any of ``pipes`` open.

    Issue #548: Async Stop hooks (Claude Code's ``async: true`` Stop hooks)
    and some MCP launchers spawn detached children via ``setsid()``. Those
    children land in a brand-new session/process-group, invisible to
    ``killpg(pgid, ...)`` — so the prior ``terminate_process_group()`` does
    not reach them. They keep our stdout/stderr write-ends open and the
    reader's ``readline()`` never sees EOF.

    Recovery: enumerate ``/proc/*/fd``, find any process (other than self)
    holding a writable handle to a pipe whose inode matches one of ours,
    and SIGKILL it. Inode keying makes this race-safe across concurrent
    executions in the same container — every ``Popen`` allocates a fresh
    pipe with a unique inode, so we only ever kill writers of the specific
    stuck execution's pipes.

    Linux only (depends on ``/proc``). Returns the number of processes
    killed; returns 0 silently on macOS/Windows or if nothing is found.
    """
    if not sys.platform.startswith("linux"):
        return 0

    inodes: set[str] = set()
    for pipe in pipes:
        if pipe is None:
            continue
        try:
            if getattr(pipe, "closed", False):
                continue
            ino = os.fstat(pipe.fileno()).st_ino
        except (OSError, ValueError):
            continue
        inodes.add(f"pipe:[{ino}]")

    if not inodes:
        return 0

    try:
        pid_entries = os.listdir("/proc")
    except OSError:
        return 0

    our_pid = os.getpid()
    killed = 0
    for entry in pid_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == our_pid:
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            continue  # process exited mid-scan or no permission

        for fd_name in fd_names:
            try:
                target = os.readlink(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if target not in inodes:
                continue

            access_mode = None
            try:
                with open(f"/proc/{pid}/fdinfo/{fd_name}") as fh:
                    for line in fh:
                        if line.startswith("flags:"):
                            access_mode = int(line.split()[1], 8) & 0o3
                            break
            except OSError:
                continue
            if not access_mode:  # 0 == O_RDONLY (the reader); skip
                continue

            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
            break  # one match per process is enough

    return killed


def drain_reader_threads(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    post_kill_grace: int = 30,
    pgid: Optional[int] = None,
) -> None:
    """Join subprocess reader threads with a bounded timeout.

    If any thread is still alive after ``grace`` seconds, a surviving
    process-group member is holding a pipe write end open. Kill the group
    (via ``pgid`` if provided, else looked up), then wait up to
    ``post_kill_grace`` seconds for the reader to drain naturally before
    force-closing pipes as a last resort.

    Ordering matters: once all writers (claude + hook grandchildren) are
    dead the kernel will EOF the read end as soon as the buffered bytes are
    consumed. If we close our read FD first the reader raises
    ``ValueError: I/O operation on closed file`` and the kernel buffer —
    including the final ``{"type":"result"}`` JSON line — is discarded
    silently. Waiting for natural drain preserves that data. (#531)

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
        "[Subprocess] Reader thread(s) still busy after process exit "
        "(pid=%s, stuck_count=%s) — killing process group, then waiting "
        "%ss for natural drain",
        process.pid, len(stuck), post_kill_grace,
    )
    terminate_process_group(process, graceful_timeout=1, pgid=pgid)

    # Issue #548: catch setsid() escapees that the group kill missed
    # (e.g. ssh spawned by git-push from an async Stop hook). They live in
    # their own session, invisible to killpg, and keep our pipe write-end
    # open. SIGKILL them by inode match so the kernel can EOF the read end.
    escapees_killed = _kill_pipe_write_holders((process.stdout, process.stderr))
    if escapees_killed:
        logger.warning(
            "[Subprocess] Killed %d setsid-escapee process(es) holding pipe "
            "write-end after group kill (pid=%s)",
            escapees_killed, process.pid,
        )

    # Grandchildren are gone → kernel will EOF the pipe as the last write
    # FD is reaped → reader's readline() returns '' once it drains the
    # buffered tail (including the final result JSON line on long tasks).
    for t in stuck:
        t.join(timeout=post_kill_grace)

    still_stuck = [t for t in stuck if t.is_alive()]
    if not still_stuck:
        logger.info(
            "[Subprocess] Reader thread(s) drained naturally after "
            "grandchild termination (pid=%s)",
            process.pid,
        )
        return

    # Genuine wedge — reader did not return even after grandchildren died
    # and the kernel should have EOF'd. Force-close and accept data loss.
    logger.error(
        "[Subprocess] Reader thread(s) still stuck after %ss post-kill "
        "grace — force-closing pipes; some buffered data may be lost "
        "(pid=%s, stuck_count=%s)",
        post_kill_grace, process.pid, len(still_stuck),
    )
    safe_close_pipes(process)
    for t in still_stuck:
        t.join(timeout=2)

    leaked = [t for t in still_stuck if t.is_alive()]
    if leaked:
        logger.error(
            "[Subprocess] %s reader thread(s) leaked for pid=%s after "
            "force-close; continuing anyway",
            len(leaked), process.pid,
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
