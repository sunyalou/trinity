"""Issue #970 (D18): the chat path is fire-and-forget about the drain outcome.

``_drain_bounded`` now returns a 3-value outcome
("completed"/"budget_exceeded"/"errored") instead of ``None``. The headless
path acts on it (snapshot + JSONL recovery); the interactive chat path
intentionally IGNORES it — a leaked reader there is human-noticed and the chat
finalize has no JSONL recovery surface, so budget-exceeded recovery for chat is
a deferred follow-up. These tests pin that contract so a future refactor can't
silently start depending on (or break on) the chat callsites' return value.

Module under test:
    docker/base-image/agent_server/services/claude_code.py
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import MagicMock

# conftest.py registers the real agent_server namespace package.
from agent_server.services import claude_code  # noqa: E402
from agent_server.services.subprocess_lifecycle import _drain_bounded  # noqa: E402

_CHAT_SRC = Path(claude_code.__file__).read_text()


def test_chat_module_imports_with_new_drain_return_type():
    """If _drain_bounded's new return type broke the chat callsites, importing
    claude_code above would have failed at collection."""
    assert hasattr(claude_code, "_drain_bounded")


def test_chat_drain_callsites_are_fire_and_forget():
    """Both chat callsites call _drain_bounded without binding/branching on
    the return, and the contract is documented inline (#970/D18)."""
    # Normal-path + TimeoutExpired-path callsites.
    assert _CHAT_SRC.count("_drain_bounded(") >= 2
    # No callsite binds the outcome (e.g. `x = _drain_bounded(` or
    # `x=_drain_bounded(` or `ctx.x = _drain_bounded(`), in any spacing.
    # The negative lookbehind excludes comparisons (`==`/`!=`/`<=`/`>=`).
    assert not re.search(r"(?<![=!<>])=\s*_drain_bounded\(", _CHAT_SRC), (
        "chat must not bind _drain_bounded's return — it's fire-and-forget (#970/D18)"
    )
    # The deliberate-ignore contract note is present.
    assert "outcome intentionally ignored" in _CHAT_SRC


def test_chat_does_not_recover_on_budget_exceeded():
    """Chat must NOT route the budget-exceeded outcome into a recovery path in
    this PR — guard against someone wiring drain_budget_exceeded into chat."""
    assert "drain_budget_exceeded" not in _CHAT_SRC


def test_drain_bounded_return_is_safely_ignorable(monkeypatch):
    """The value chat drops is always one of the three documented literals."""

    async def _fast(*_a, **_kw):
        return None

    monkeypatch.setattr(
        "agent_server.services.subprocess_lifecycle._drain_reader_threads", _fast
    )
    process = MagicMock()
    process.pid = 1234
    outcome = _drain_bounded(process, MagicMock(spec=threading.Thread))
    assert outcome in ("completed", "budget_exceeded", "errored")
