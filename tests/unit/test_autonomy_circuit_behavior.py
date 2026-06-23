import pytest


@pytest.mark.asyncio
async def test_disabling_autonomy_does_not_force_transport_circuit_dormant(monkeypatch):
    """Manual chat/task dispatch must still work when autonomy is disabled."""
    from services.agent_service import autonomy

    calls = []

    class FakeDB:
        def can_user_share_agent(self, username, agent_name):
            return True

        def is_system_agent(self, agent_name):
            return False

        def set_autonomy_enabled(self, agent_name, enabled):
            calls.append(("set_autonomy_enabled", agent_name, enabled))

        def list_agent_schedules(self, agent_name):
            return []

    class FakeUser:
        username = "admin"

    monkeypatch.setattr(autonomy, "db", FakeDB())
    monkeypatch.setattr(autonomy, "get_agent_container", lambda agent_name: object())

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("unused")

    monkeypatch.setattr(
        "services.agent_client.force_circuit_dormant",
        lambda *args, **kwargs: calls.append(("force_circuit_dormant", args, kwargs)),
    )
    monkeypatch.setattr(
        "services.agent_client.reset_circuit",
        lambda *args, **kwargs: calls.append(("reset_circuit", args, kwargs)),
    )

    result = await autonomy.set_autonomy_status_logic(
        "demo6", {"enabled": False}, FakeUser()
    )

    assert result["autonomy_enabled"] is False
    assert ("set_autonomy_enabled", "demo6", False) in calls
    assert not any(call[0] == "force_circuit_dormant" for call in calls)
    assert not any(call[0] == "reset_circuit" for call in calls)
