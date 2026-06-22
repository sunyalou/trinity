from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

import services.system_agent_service as system_agent_service
from services.system_agent_service import (
    SystemAgentService,
    SystemAgentRuntimeTarget,
    _resolve_system_agent_target,
    _system_agent_identity_from_container,
    _system_agent_is_drifted,
)


DEESEEK_OPENAI_PROVIDER_CONFIGS = {
    "deepseek-openai": {
        "id": "deepseek-openai",
        "name": "DeepSeek OpenAI",
        "protocol": "openai-compatible",
        "base_url": "https://api.deepseek.com/v1",
        "auth": {"api_key": "secret-key", "api_key_configured": True},
        "models": [{"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"}],
    }
}


SYSTEM_AGENT_ENV_KEYS = (
    "SYSTEM_AGENT_RUNTIME",
    "SYSTEM_AGENT_RUNTIME_PROVIDER_ID",
    "SYSTEM_AGENT_RUNTIME_MODEL_ID",
    "SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT",
)


@pytest.fixture(autouse=True)
def clear_system_agent_env(monkeypatch):
    for key in SYSTEM_AGENT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_resolve_system_agent_target_defaults_to_unconfigured_claude_code():
    target = _resolve_system_agent_target()

    assert target.configured is False
    assert target.runtime == "claude-code"
    assert target.provider_id is None
    assert target.model_id is None
    assert target.auto_recreate_on_drift is False
    assert target.error is None


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_resolve_system_agent_target_truthy_auto_recreate_values(monkeypatch, value):
    monkeypatch.setenv("SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT", value)

    target = _resolve_system_agent_target()

    assert target.auto_recreate_on_drift is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "anything-else"])
def test_resolve_system_agent_target_false_default_auto_recreate_values(monkeypatch, value):
    monkeypatch.setenv("SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT", value)

    target = _resolve_system_agent_target()

    assert target.auto_recreate_on_drift is False


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("deepseek-openai", None),
        (None, "deepseek-v4-flash"),
        ("deepseek-openai", "deepseek-v4-flash"),
    ],
)
def test_resolve_system_agent_target_requires_runtime_when_provider_or_model_configured(
    monkeypatch, provider_id, model_id
):
    if provider_id is not None:
        monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID", provider_id)
    if model_id is not None:
        monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_MODEL_ID", model_id)

    target = _resolve_system_agent_target()

    assert target.error == "SYSTEM_AGENT_RUNTIME is required when provider or model is configured"


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        (None, None),
        ("deepseek-openai", None),
        (None, "deepseek-v4-flash"),
    ],
)
def test_resolve_system_agent_target_requires_provider_and_model_for_opencode(
    monkeypatch, provider_id, model_id
):
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME", "opencode")
    if provider_id is not None:
        monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID", provider_id)
    if model_id is not None:
        monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_MODEL_ID", model_id)

    target = _resolve_system_agent_target()

    assert (
        target.error
        == "SYSTEM_AGENT_RUNTIME_PROVIDER_ID and SYSTEM_AGENT_RUNTIME_MODEL_ID are required for opencode"
    )


def test_resolve_system_agent_target_unsupported_runtime_returns_error(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME", "bad-runtime")

    target = _resolve_system_agent_target()

    assert target.error.startswith("Unsupported SYSTEM_AGENT_RUNTIME:")


def test_build_launch_plan_default_claude_code_includes_base_env_labels_and_port(monkeypatch):
    monkeypatch.setattr(system_agent_service, "get_anthropic_api_key", lambda: "anthropic-key")
    target = _resolve_system_agent_target()

    plan = SystemAgentService()._build_launch_plan(
        target,
        ssh_port=2222,
        agent_mcp_key=SimpleNamespace(api_key="mcp-key"),
    )

    assert plan.env["ANTHROPIC_API_KEY"] == "anthropic-key"
    assert plan.env["TRINITY_MCP_API_KEY"] == "mcp-key"
    assert plan.labels["trinity.agent-runtime"] == "claude-code"
    assert plan.labels["trinity.is-system"] == "true"
    assert plan.ssh_port == 2222


def test_build_launch_plan_opencode_materializes_provider_runtime_env_and_labels(
    monkeypatch,
):
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME", "opencode")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID", "deepseek-openai")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setattr(system_agent_service, "get_anthropic_api_key", lambda: "anthropic-key")
    monkeypatch.setattr(
        system_agent_service.settings_service,
        "get_provider_configs",
        lambda: DEESEEK_OPENAI_PROVIDER_CONFIGS,
    )
    target = _resolve_system_agent_target()

    plan = SystemAgentService()._build_launch_plan(
        target,
        ssh_port=2222,
        agent_mcp_key=SimpleNamespace(api_key="mcp-key"),
    )

    assert plan.env["AGENT_RUNTIME"] == "opencode"
    assert plan.env["AGENT_RUNTIME_MODEL"] == "deepseek-openai/deepseek-v4-flash"
    assert plan.env["TRINITY_RUNTIME_PROVIDER_ID"] == "deepseek-openai"
    assert plan.env["TRINITY_RUNTIME_MODEL_ID"] == "deepseek-v4-flash"
    assert "OPENCODE_CONFIG_CONTENT" in plan.env
    assert plan.env["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"] == "secret-key"
    assert plan.labels["trinity.agent-runtime"] == "opencode"
    assert plan.labels["trinity.runtime-provider-id"] == "deepseek-openai"
    assert plan.labels["trinity.runtime-model-id"] == "deepseek-v4-flash"


def _container(labels=None, env=None, status="running"):
    merged_labels = {"trinity.ssh-port": "1234"}
    if labels:
        merged_labels.update(labels)
    env_list = [f"{key}={value}" for key, value in (env or {}).items()]
    return SimpleNamespace(labels=merged_labels, attrs={"Config": {"Env": env_list}}, status=status)


def test_system_agent_identity_reads_labels_before_env():
    container = _container(
        labels={
            "trinity.agent-runtime": "opencode",
            "trinity.runtime-provider-id": "label-provider",
            "trinity.runtime-model-id": "label-model",
        },
        env={
            "AGENT_RUNTIME": "claude-code",
            "TRINITY_RUNTIME_PROVIDER_ID": "env-provider",
            "TRINITY_RUNTIME_MODEL_ID": "env-model",
        },
    )

    identity = _system_agent_identity_from_container(container)

    assert identity.runtime == "opencode"
    assert identity.provider_id == "label-provider"
    assert identity.model_id == "label-model"


def test_system_agent_identity_falls_back_to_env_and_parses_opencode_runtime_model():
    container = _container(
        env={
            "AGENT_RUNTIME": "opencode",
            "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        }
    )

    identity = _system_agent_identity_from_container(container)

    assert identity.runtime == "opencode"
    assert identity.provider_id == "deepseek-openai"
    assert identity.model_id == "deepseek-v4-flash"


def test_legacy_claude_container_drifts_when_target_is_opencode():
    container = _container(env={"AGENT_RUNTIME": "claude-code"})
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
    )

    assert _system_agent_is_drifted(container, target) is True


def test_matching_opencode_container_is_not_drifted():
    container = _container(
        labels={
            "trinity.agent-runtime": "opencode",
            "trinity.runtime-provider-id": "deepseek-openai",
            "trinity.runtime-model-id": "deepseek-v4-flash",
        }
    )
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
    )

    assert _system_agent_is_drifted(container, target) is False


def test_no_target_mode_does_not_drift_existing_opencode_container():
    container = _container(
        labels={
            "trinity.agent-runtime": "opencode",
            "trinity.runtime-provider-id": "deepseek-openai",
            "trinity.runtime-model-id": "deepseek-v4-flash",
        }
    )
    target = SystemAgentRuntimeTarget(configured=False)

    assert _system_agent_is_drifted(container, target) is False


@pytest.mark.asyncio
async def test_create_system_agent_uses_opencode_launch_plan(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME", "opencode")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID", "deepseek-openai")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setattr(system_agent_service, "get_anthropic_api_key", lambda: "anthropic-key")
    monkeypatch.setattr(system_agent_service, "get_next_available_port", lambda: 2222)
    monkeypatch.setattr(
        system_agent_service.settings_service,
        "get_provider_configs",
        lambda: DEESEEK_OPENAI_PROVIDER_CONFIGS,
    )
    monkeypatch.setattr(
        system_agent_service.db,
        "get_user_by_username",
        lambda username: SimpleNamespace(username=username),
    )
    monkeypatch.setattr(
        system_agent_service.db,
        "create_agent_mcp_api_key",
        lambda **kwargs: SimpleNamespace(id="key-id", key_prefix="mcp", api_key="mcp-key"),
    )
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(system_agent_service.db, "grant_default_permissions", lambda *args, **kwargs: None)
    monkeypatch.setattr(SystemAgentService, "_set_system_scope", lambda self, key_id: None)
    containers_run = AsyncMock(return_value=SimpleNamespace(short_id="container-id"))
    monkeypatch.setattr(system_agent_service, "containers_run", containers_run)

    result = await SystemAgentService()._create_system_agent()

    containers_run.assert_awaited_once()
    _, kwargs = containers_run.call_args
    assert kwargs["environment"]["AGENT_RUNTIME"] == "opencode"
    assert kwargs["environment"]["AGENT_RUNTIME_MODEL"] == "deepseek-openai/deepseek-v4-flash"
    assert kwargs["environment"]["TRINITY_RUNTIME_PROVIDER_ID"] == "deepseek-openai"
    assert kwargs["environment"]["TRINITY_RUNTIME_MODEL_ID"] == "deepseek-v4-flash"
    assert "OPENCODE_CONFIG_CONTENT" in kwargs["environment"]
    assert kwargs["labels"]["trinity.agent-runtime"] == "opencode"
    assert kwargs["labels"]["trinity.runtime-provider-id"] == "deepseek-openai"
    assert kwargs["labels"]["trinity.runtime-model-id"] == "deepseek-v4-flash"
    assert kwargs["ports"] == {"22/tcp": 2222}
    assert result["ssh_port"] == 2222


@pytest.mark.asyncio
async def test_create_system_agent_does_not_create_mcp_key_when_launch_plan_validation_fails(
    monkeypatch,
):
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME", "opencode")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID", "missing-provider")
    monkeypatch.setenv("SYSTEM_AGENT_RUNTIME_MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setattr(system_agent_service, "get_anthropic_api_key", lambda: "anthropic-key")
    monkeypatch.setattr(system_agent_service, "get_next_available_port", lambda: 2222)
    monkeypatch.setattr(
        system_agent_service.settings_service,
        "get_provider_configs",
        lambda: DEESEEK_OPENAI_PROVIDER_CONFIGS,
    )
    monkeypatch.setattr(
        system_agent_service.db,
        "get_user_by_username",
        lambda username: SimpleNamespace(username=username),
    )
    create_agent_mcp_api_key = Mock()
    set_system_scope = Mock()
    containers_run = AsyncMock(return_value=SimpleNamespace(short_id="container-id"))
    monkeypatch.setattr(system_agent_service.db, "create_agent_mcp_api_key", create_agent_mcp_api_key)
    monkeypatch.setattr(SystemAgentService, "_set_system_scope", set_system_scope)
    monkeypatch.setattr(system_agent_service, "containers_run", containers_run)

    with pytest.raises(ValueError, match="Provider 'missing-provider' not found"):
        await SystemAgentService()._create_system_agent()

    create_agent_mcp_api_key.assert_not_called()
    set_system_scope.assert_not_called()
    containers_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_returns_config_invalid_before_container_actions(monkeypatch):
    target = SystemAgentRuntimeTarget(error="bad runtime config")
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    get_agent_container = Mock()
    create_system_agent = AsyncMock()
    monkeypatch.setattr(system_agent_service, "get_agent_container", get_agent_container)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "config_invalid"
    assert result["status"] == "error"
    assert "bad runtime config" in result["message"]
    get_agent_container.assert_not_called()
    create_system_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_drift_without_auto_recreate_leaves_container_unchanged(monkeypatch):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=False,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    register_agent_owner = Mock()
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", register_agent_owner)
    container_start = AsyncMock()
    container_remove = AsyncMock()
    container_rename = AsyncMock()
    create_system_agent = AsyncMock()
    monkeypatch.setattr(system_agent_service, "container_start", container_start)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "drift_detected"
    assert result["status"] == "warning"
    assert "auto recreate" in result["message"].lower()
    assert "left unchanged" in result["message"].lower()
    register_agent_owner.assert_called_once_with("trinity-system", "admin", is_system=True)
    container_start.assert_not_awaited()
    container_remove.assert_not_awaited()
    container_rename.assert_not_awaited()
    create_system_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreates_drifted_container_after_preflight_with_preserved_port(
    monkeypatch,
):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    container.labels["trinity.ssh-port"] = "2345"
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    get_next_available_port = Mock(return_value=3456)
    monkeypatch.setattr(system_agent_service, "get_next_available_port", get_next_available_port)
    container_remove = AsyncMock()
    container_stop = AsyncMock()
    lifecycle_events = []
    async def rename_side_effect(container_arg, new_name):
        lifecycle_events.append(("rename", new_name))
    async def stop_side_effect(container_arg):
        lifecycle_events.append(("stop", None))
        container.status = "exited"
    async def create_side_effect(target_arg, ssh_port):
        lifecycle_events.append(("create", ssh_port))
        return {"container_id": "new-id", "ssh_port": ssh_port}
    async def remove_side_effect(container_arg, **kwargs):
        lifecycle_events.append(("remove", kwargs))
    container_rename = AsyncMock(side_effect=rename_side_effect)
    container_stop.side_effect = stop_side_effect
    container_remove.side_effect = remove_side_effect
    create_system_agent = AsyncMock(side_effect=create_side_effect)
    build_launch_plan = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)
    monkeypatch.setattr(SystemAgentService, "_build_launch_plan", build_launch_plan)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreated"
    assert result["status"] == "running"
    assert result["details"] == {"container_id": "new-id", "ssh_port": 2345}
    get_next_available_port.assert_not_called()
    build_launch_plan.assert_called_once_with(target, ssh_port=2345, agent_mcp_key=None)
    assert lifecycle_events[0][0] == "rename"
    assert lifecycle_events[0][1].startswith("agent-trinity-system-backup-")
    assert lifecycle_events[1] == ("stop", None)
    assert lifecycle_events[2] == ("create", 2345)
    assert lifecycle_events[3] == ("remove", {"force": True})
    container_stop.assert_awaited_once_with(container)
    container_remove.assert_awaited_once_with(container, force=True)
    create_system_agent.assert_awaited_once_with(target, ssh_port=2345)


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreate_create_failure_rolls_backup_back(monkeypatch):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    container.labels["trinity.ssh-port"] = "2345"
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    monkeypatch.setattr(SystemAgentService, "_build_launch_plan", Mock(return_value=SimpleNamespace()))
    lifecycle_events = []
    async def rename_side_effect(container_arg, new_name):
        lifecycle_events.append(("rename", new_name))
    async def stop_side_effect(container_arg):
        lifecycle_events.append(("stop", None))
    async def create_side_effect(target_arg, ssh_port):
        lifecycle_events.append(("create", ssh_port))
        raise RuntimeError("boom")
    container_rename = AsyncMock(side_effect=rename_side_effect)
    container_stop = AsyncMock(side_effect=stop_side_effect)
    container_start = AsyncMock(side_effect=lambda container_arg: lifecycle_events.append(("start", None)))
    create_system_agent = AsyncMock(side_effect=create_side_effect)
    container_remove = AsyncMock()
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(system_agent_service, "container_start", container_start)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreate_failed"
    assert result["status"] == "error"
    assert "boom" in result["message"]
    assert "rollback" in result["message"].lower()
    assert "restored" in result["message"].lower()
    assert lifecycle_events[0][0] == "rename"
    assert lifecycle_events[0][1].startswith("agent-trinity-system-backup-")
    assert lifecycle_events[1] == ("stop", None)
    assert lifecycle_events[2] == ("create", 2345)
    assert lifecycle_events[3] == ("rename", "agent-trinity-system")
    assert lifecycle_events[4] == ("start", None)
    container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreate_backup_rename_failure_does_not_claim_restored(
    monkeypatch,
):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    container.labels["trinity.ssh-port"] = "2345"
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    monkeypatch.setattr(SystemAgentService, "_build_launch_plan", Mock(return_value=SimpleNamespace()))
    lifecycle_events = []
    async def rename_side_effect(container_arg, new_name):
        lifecycle_events.append(("rename", new_name))
        raise RuntimeError("rename failed")
    container_rename = AsyncMock(side_effect=rename_side_effect)
    container_stop = AsyncMock()
    container_start = AsyncMock()
    create_system_agent = AsyncMock()
    container_remove = AsyncMock()
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(system_agent_service, "container_start", container_start)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreate_failed"
    assert result["status"] == "error"
    assert "rename failed" in result["message"]
    assert "restored" not in result["message"].lower()
    assert len(lifecycle_events) == 1
    container_rename.assert_awaited_once()
    container_stop.assert_not_awaited()
    container_start.assert_not_awaited()
    create_system_agent.assert_not_awaited()
    container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreate_partial_replacement_failure_removes_replacement_then_rolls_back(
    monkeypatch,
):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    backup_container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    backup_container.labels["trinity.ssh-port"] = "2345"
    replacement_container = _container(
        labels={"trinity.agent-runtime": "opencode"},
        status="running",
    )
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    get_agent_container = Mock(side_effect=[backup_container, replacement_container])
    monkeypatch.setattr(system_agent_service, "get_agent_container", get_agent_container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    monkeypatch.setattr(SystemAgentService, "_build_launch_plan", Mock(return_value=SimpleNamespace()))
    lifecycle_events = []
    async def rename_side_effect(container_arg, new_name):
        lifecycle_events.append(("rename", container_arg, new_name))
    async def stop_side_effect(container_arg):
        lifecycle_events.append(("stop", container_arg))
    async def create_side_effect(target_arg, ssh_port):
        lifecycle_events.append(("create", ssh_port))
        raise RuntimeError("post-create registration failed")
    async def remove_side_effect(container_arg, **kwargs):
        lifecycle_events.append(("remove", container_arg, kwargs))
    async def start_side_effect(container_arg):
        lifecycle_events.append(("start", container_arg))
    container_rename = AsyncMock(side_effect=rename_side_effect)
    container_stop = AsyncMock(side_effect=stop_side_effect)
    container_start = AsyncMock(side_effect=start_side_effect)
    container_remove = AsyncMock(side_effect=remove_side_effect)
    create_system_agent = AsyncMock(side_effect=create_side_effect)
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(system_agent_service, "container_start", container_start)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreate_failed"
    assert result["status"] == "error"
    assert "post-create registration failed" in result["message"]
    assert lifecycle_events[0][0] == "rename"
    assert lifecycle_events[1] == ("stop", backup_container)
    assert lifecycle_events[2] == ("create", 2345)
    assert lifecycle_events[3] == ("remove", replacement_container, {"force": True})
    assert lifecycle_events[4] == ("rename", backup_container, "agent-trinity-system")
    assert lifecycle_events[5] == ("start", backup_container)
    assert all(
        event[0] != "remove" or event[1] is not backup_container
        for event in lifecycle_events
    )


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreate_stop_failure_rolls_backup_back_without_create(
    monkeypatch,
):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="deepseek-openai",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    container.labels["trinity.ssh-port"] = "2345"
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    monkeypatch.setattr(SystemAgentService, "_build_launch_plan", Mock(return_value=SimpleNamespace()))
    lifecycle_events = []
    async def rename_side_effect(container_arg, new_name):
        lifecycle_events.append(("rename", new_name))
    async def stop_side_effect(container_arg):
        lifecycle_events.append(("stop", None))
        raise RuntimeError("stop failed")
    container_rename = AsyncMock(side_effect=rename_side_effect)
    container_stop = AsyncMock(side_effect=stop_side_effect)
    container_start = AsyncMock(side_effect=lambda container_arg: lifecycle_events.append(("start", None)))
    create_system_agent = AsyncMock()
    container_remove = AsyncMock()
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(system_agent_service, "container_start", container_start)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreate_failed"
    assert result["status"] == "error"
    assert "stop failed" in result["message"]
    assert "rollback" in result["message"].lower()
    assert "restored" in result["message"].lower()
    assert lifecycle_events[0][0] == "rename"
    assert lifecycle_events[0][1].startswith("agent-trinity-system-backup-")
    assert lifecycle_events[1] == ("stop", None)
    assert lifecycle_events[2] == ("rename", "agent-trinity-system")
    assert lifecycle_events[3] == ("start", None)
    create_system_agent.assert_not_awaited()
    container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_auto_recreate_preflight_failure_has_no_lifecycle_actions(monkeypatch):
    target = SystemAgentRuntimeTarget(
        runtime="opencode",
        provider_id="missing-provider",
        model_id="deepseek-v4-flash",
        configured=True,
        auto_recreate_on_drift=True,
    )
    container = _container(env={"AGENT_RUNTIME": "claude-code"}, status="running")
    container.labels["trinity.ssh-port"] = "2345"
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: True)
    monkeypatch.setattr(system_agent_service, "get_agent_container", lambda name: container)
    monkeypatch.setattr(system_agent_service, "container_reload", AsyncMock())
    monkeypatch.setattr(system_agent_service.db, "register_agent_owner", Mock())
    monkeypatch.setattr(
        SystemAgentService,
        "_build_launch_plan",
        Mock(side_effect=ValueError("Provider 'missing-provider' not found")),
    )
    container_rename = AsyncMock()
    container_stop = AsyncMock()
    container_remove = AsyncMock()
    create_system_agent = AsyncMock()
    monkeypatch.setattr(system_agent_service, "container_rename", container_rename)
    monkeypatch.setattr(system_agent_service, "container_stop", container_stop)
    monkeypatch.setattr(system_agent_service, "container_remove", container_remove)
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "recreate_failed"
    assert result["status"] == "error"
    assert "missing-provider" in result["message"]
    container_rename.assert_not_awaited()
    container_stop.assert_not_awaited()
    container_remove.assert_not_awaited()
    create_system_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_deployed_absent_container_creates_with_resolved_target(monkeypatch):
    target = SystemAgentRuntimeTarget(runtime="claude-code", configured=True)
    monkeypatch.setattr(system_agent_service, "_resolve_system_agent_target", lambda: target)
    monkeypatch.setattr(SystemAgentService, "is_deployed", lambda self: False)
    create_system_agent = AsyncMock(return_value={"container_id": "new-id", "ssh_port": 2222})
    monkeypatch.setattr(SystemAgentService, "_create_system_agent", create_system_agent)

    result = await SystemAgentService().ensure_deployed()

    assert result["action"] == "created"
    assert result["status"] == "running"
    assert result["details"] == {"container_id": "new-id", "ssh_port": 2222}
    create_system_agent.assert_awaited_once_with(target)
