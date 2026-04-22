"""
Pre-check endpoint (#454).

Optional hook that lets an agent template deterministically decide whether a
scheduled invocation should actually fire, without waking Claude. The scheduler
calls this endpoint before dispatching a cron-triggered chat; if the endpoint
is absent the scheduler falls back to today's behavior (fire as usual).

Template authors implement the gate by dropping a Python file at
``/home/developer/.trinity/pre-check.py`` with a top-level ``check()`` function
that returns::

    {"fire": False, "reason": "..."}
    # or
    {"fire": True, "message": "optional chat message override"}

If ``check()`` returns ``fire=False`` the scheduler records a skipped
execution and does not call ``/api/chat``. If it returns ``fire=True`` the
scheduler fires the chat, using the returned ``message`` if present.

Fail-open by design: any exception or malformed response at this layer
propagates to the scheduler as "no decision", which falls back to the
default fire behavior. A broken pre-check must never silently suppress
scheduled work.

Security note: ``check()`` is executed with the agent-server's full Python
interpreter access — templates can import ``subprocess``, open sockets,
etc. This is the same sandbox as chat-mode tool calls (non-root container,
dropped capabilities, isolated network), so it introduces no new privilege,
but operators reviewing templates should treat ``.trinity/pre-check.py``
with the same scrutiny as ``CLAUDE.md`` and any skill files.

Module-reload caching is intentionally absent: every request re-runs
``exec_module`` so template edits take effect on the next call without
a restart. Given the file is small and the poll cadence is minutes, the
cost is negligible. If polling tightens to sub-second, add mtime-based
caching — see PR #455 review thread for history.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

PRE_CHECK_PATH = Path("/home/developer/.trinity/pre-check.py")
MAX_MESSAGE_BYTES = 32_000


def _load_check_callable():
    """Dynamically load ``check`` from the template's pre-check file."""
    if not PRE_CHECK_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "_trinity_pre_check", PRE_CHECK_PATH
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # may raise — caller logs and returns 500
    fn = getattr(module, "check", None)
    if not callable(fn):
        return None
    return fn


def _normalise_result(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or "fire" not in raw:
        raise ValueError(
            "pre-check check() must return a dict with a 'fire' bool"
        )
    fire = bool(raw.get("fire"))
    out: Dict[str, Any] = {"fire": fire}
    if not fire:
        reason = raw.get("reason")
        if reason is not None:
            out["reason"] = str(reason)[:2000]
        return out
    message = raw.get("message")
    if message is not None:
        message = str(message)
        size = len(message.encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            # Silently falling back to schedule.message would hide a real
            # template bug. Log loudly and expose the reason in the response
            # so the scheduler/operator can see what happened.
            logger.error(
                "[pre-check] message override %d bytes exceeds cap %d — "
                "falling back to schedule.message",
                size,
                MAX_MESSAGE_BYTES,
            )
            out["message_truncated"] = (
                f"override dropped: {size} bytes exceeds {MAX_MESSAGE_BYTES} cap"
            )
        else:
            out["message"] = message
    return out


@router.post("/api/pre-check")
async def pre_check() -> Dict[str, Any]:
    """Run the template's ``check()`` if present; 404 if absent (fail-open)."""
    fn = _load_check_callable()
    if fn is None:
        raise HTTPException(status_code=404, detail="pre-check not implemented")
    try:
        if inspect.iscoroutinefunction(fn):
            raw = await fn()
        else:
            raw = await asyncio.get_running_loop().run_in_executor(None, fn)
    except Exception as e:
        logger.exception("[pre-check] check() raised: %s", e)
        raise HTTPException(status_code=500, detail=f"pre-check error: {e}")
    try:
        return _normalise_result(raw)
    except Exception as e:
        logger.warning("[pre-check] invalid return shape: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
