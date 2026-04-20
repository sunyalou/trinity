"""
Regression test for #374 — proactive message audit logging.

Pre-fix: `proactive_message_service._audit_send` called
`platform_audit_service.log_event(...)` which does not exist; the warning
was swallowed and no audit row was ever written for proactive messages.

Post-fix: `_audit_send` is async, awaits `platform_audit_service.log(...)`
with the correct kwargs (`actor_agent_name` instead of `actor_type`/`actor_id`).
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


@pytest.fixture
def proactive_service(monkeypatch):
    # Stub database.db — only used by other methods, not _audit_send.
    fake_db_mod = types.ModuleType("database")
    fake_db_mod.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_mod)

    # Stub platform_audit_service with an AsyncMock for .log so we can assert call kwargs.
    fake_audit_mod = types.ModuleType("services.platform_audit_service")

    class _AuditEventType:
        PROACTIVE_MESSAGE = "proactive_message"

    fake_service = MagicMock()
    fake_service.log = AsyncMock(return_value="evt-1")
    fake_audit_mod.platform_audit_service = fake_service
    fake_audit_mod.AuditEventType = _AuditEventType
    monkeypatch.setitem(sys.modules, "services.platform_audit_service", fake_audit_mod)

    # Fresh import so stubs take effect.
    for mod in ("services.proactive_message_service", "proactive_message_service"):
        if mod in sys.modules:
            del sys.modules[mod]

    from services.proactive_message_service import ProactiveMessageService

    return ProactiveMessageService(), fake_service


def test_audit_send_invokes_platform_audit_log(proactive_service):
    """_audit_send must call platform_audit_service.log with actor_agent_name kwarg."""
    service, audit = proactive_service
    asyncio.run(
        service._audit_send(
            agent_name="research-bot",
            recipient_email="alice@example.com",
            channel="telegram",
            success=True,
            message_preview="hello",
        )
    )

    audit.log.assert_awaited_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["event_action"] == "send"
    assert kwargs["source"] == "proactive_message_service"
    assert kwargs["actor_agent_name"] == "research-bot"
    assert kwargs["target_type"] == "user"
    assert kwargs["target_id"] == "alice@example.com"
    assert kwargs["details"]["channel"] == "telegram"
    assert kwargs["details"]["success"] is True
    # No legacy log_event / actor_type / actor_id keys.
    assert "actor_type" not in kwargs
    assert "actor_id" not in kwargs


def test_audit_send_swallows_exceptions(proactive_service):
    """Audit failures must never propagate to caller (best-effort contract)."""
    service, audit = proactive_service
    audit.log.side_effect = RuntimeError("db down")

    # Should not raise.
    asyncio.run(
        service._audit_send(
            agent_name="bot",
            recipient_email="x@example.com",
            channel="web",
            success=False,
            error="oops",
        )
    )


def test_audit_send_truncates_long_preview(proactive_service):
    service, audit = proactive_service
    long_text = "x" * 500
    asyncio.run(
        service._audit_send(
            agent_name="bot",
            recipient_email="x@example.com",
            channel="web",
            success=True,
            message_preview=long_text,
        )
    )
    kwargs = audit.log.call_args.kwargs
    assert len(kwargs["details"]["message_preview"]) == 100
