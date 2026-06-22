from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
import httpx

from models import AgentConfig, ModelChangeRequest, ParallelTaskRequest
from services.agent_service.crud import (
    _apply_runtime_template_config,
    _inject_opencode_custom_provider_config,
    _inject_opencode_provider_envs,
)
from services.docker_service import get_agent_status_from_container
from services.task_execution_service import _get_agent_runtime_defaults


def _opencode_provider_config(provider_id: str = "deepseek-openai", model_id: str = "deepseek-v4-flash") -> str:
    return json.dumps({
        "provider": {
            provider_id: {
                "models": {
                    model_id: {"name": model_id},
                }
            }
        },
        "model": f"{provider_id}/{model_id}",
    })


def _fake_agent_container(name: str, env_vars: dict) -> SimpleNamespace:
    return SimpleNamespace(
        labels={
            "trinity.agent-type": "business-assistant",
            "trinity.created": "2026-06-20T00:00:00Z",
            "trinity.agent-runtime": "opencode",
            "trinity.ssh-port": "1234",
            "trinity.cpu": "2",
            "trinity.memory": "4g",
        },
        name=f"agent-{name}",
        status="running",
        id="container-id",
        attrs={"Config": {"Env": [f"{key}={value}" for key, value in env_vars.items()]}},
        image=SimpleNamespace(labels={}),
    )


def _patch_create_agent_dependencies(monkeypatch, crud, *, provider_configs=None):
    async def fake_containers_run(*args, **kwargs):
        return _fake_agent_container("provider-validation-agent", kwargs["environment"])

    async def fake_volume_get(name):
        return SimpleNamespace(name=name)

    monkeypatch.setattr(crud, "docker_client", object())
    monkeypatch.setattr(crud, "get_agent_by_name", lambda name: None)
    monkeypatch.setattr(crud, "get_next_available_port", lambda: 1234)
    monkeypatch.setattr(crud, "validate_base_image", lambda image: None)
    monkeypatch.setattr(crud, "volume_get", fake_volume_get)
    monkeypatch.setattr(crud, "containers_run", fake_containers_run)
    monkeypatch.setattr(crud, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(crud, "get_agent_default_require_email", lambda: True)
    monkeypatch.setattr(crud.settings_service, "get_provider_configs", lambda: provider_configs or {})
    monkeypatch.setattr(crud.db, "get_agent_owner", lambda name: None)
    monkeypatch.setattr(crud.db, "is_agent_name_reserved", lambda name: False)
    monkeypatch.setattr(crud.db, "get_agents_by_owner", lambda username: [])
    monkeypatch.setattr(crud.db, "get_guardrails_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_least_used_subscription", lambda: None)
    monkeypatch.setattr(crud.db, "get_shared_folder_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_file_sharing_enabled", lambda name: False)
    monkeypatch.setattr(crud.db, "grant_default_permissions", lambda name, username: 0)
    monkeypatch.setattr(crud.db, "register_agent_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        crud.db,
        "create_agent_mcp_api_key",
        lambda **kwargs: SimpleNamespace(key_prefix="trinity_mcp_test", api_key="trinity_mcp_secret"),
    )


async def _assert_create_agent_http_400(config: AgentConfig, expected_detail: str):
    import services.agent_service.crud as crud

    with pytest.raises(HTTPException) as exc_info:
        await crud.create_agent_internal(
            config,
            SimpleNamespace(username="creator", role="creator"),
            SimpleNamespace(client=None, state=SimpleNamespace()),
        )
    assert exc_info.value.status_code == 400
    assert expected_detail in str(exc_info.value.detail)


def test_opencode_provider_env_injection_preserves_anthropic(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    env_vars = {"ANTHROPIC_API_KEY": "anthropic-key"}

    _inject_opencode_provider_envs(env_vars)

    assert env_vars["ANTHROPIC_API_KEY"] == "anthropic-key"
    assert env_vars["GOOGLE_API_KEY"] == "google-key"
    assert env_vars["GEMINI_API_KEY"] == "gemini-key"
    assert env_vars["OPENAI_API_KEY"] == "openai-key"


def test_opencode_custom_provider_injection_writes_config_without_raw_key():
    env_vars = {"AGENT_RUNTIME_MODEL": "deepseek/deepseek-chat"}

    applied = _inject_opencode_custom_provider_config(
        env_vars,
        "deepseek/deepseek-chat",
        {
            "deepseek": {
                "protocol": "openai-compatible",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-custom-secret",
            }
        },
    )

    assert applied is True
    assert env_vars["TRINITY_CUSTOM_PROVIDER_DEEPSEEK_API_KEY"] == "sk-custom-secret"
    assert "sk-custom-secret" not in env_vars["OPENCODE_CONFIG_CONTENT"]
    data = json.loads(env_vars["OPENCODE_CONFIG_CONTENT"])
    provider = data["provider"]["deepseek"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://api.deepseek.com/v1"
    assert provider["options"]["apiKey"] == "{env:TRINITY_CUSTOM_PROVIDER_DEEPSEEK_API_KEY}"
    assert provider["models"]["deepseek-chat"]["name"] == "deepseek-chat"
    assert data["model"] == "deepseek/deepseek-chat"


def test_opencode_custom_provider_injection_ignores_builtin_provider():
    env_vars = {"AGENT_RUNTIME_MODEL": "openai/gpt-5"}

    applied = _inject_opencode_custom_provider_config(
        env_vars,
        "openai/gpt-5",
        {
            "deepseek": {
                "protocol": "openai-compatible",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-custom-secret",
            }
        },
    )

    assert applied is False
    assert "OPENCODE_CONFIG_CONTENT" not in env_vars


def test_runtime_template_materialize_env_resolves_secret_refs():
    from services.provider_configs import validate_provider_configs
    from services.runtime_provider_templates import build_runtime_template

    provider = validate_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    })["deepseek-openai"]
    template = build_runtime_template("opencode", provider, "deepseek-v4-pro")
    env = template.materialize_env({"provider:deepseek-openai:api_key": "sk-secret"})
    assert env["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"] == "sk-secret"
    assert "sk-secret" not in env["OPENCODE_CONFIG_CONTENT"]


@pytest.mark.asyncio
async def test_create_agent_internal_uses_runtime_provider_model_selection(monkeypatch):
    import services.agent_service.crud as crud

    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "host-openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "host-google-key")
    monkeypatch.setenv("GEMINI_API_KEY", "host-gemini-key")

    async def fake_containers_run(*args, **kwargs):
        captured["environment"] = kwargs["environment"]
        return _fake_agent_container("provider-v2-agent", kwargs["environment"])

    async def fake_volume_get(name):
        return SimpleNamespace(name=name)

    monkeypatch.setattr(crud, "docker_client", object())
    monkeypatch.setattr(crud, "get_agent_by_name", lambda name: None)
    monkeypatch.setattr(crud, "get_next_available_port", lambda: 1234)
    monkeypatch.setattr(crud, "validate_base_image", lambda image: None)
    monkeypatch.setattr(crud, "volume_get", fake_volume_get)
    monkeypatch.setattr(crud, "containers_run", fake_containers_run)
    monkeypatch.setattr(crud, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(crud, "get_agent_default_require_email", lambda: True)
    monkeypatch.setattr(crud.settings_service, "get_provider_configs", lambda: {
        "deepseek-openai": {
            "id": "deepseek-openai",
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"}],
        }
    })
    monkeypatch.setattr(crud.db, "get_agent_owner", lambda name: None)
    monkeypatch.setattr(crud.db, "is_agent_name_reserved", lambda name: False)
    monkeypatch.setattr(crud.db, "get_agents_by_owner", lambda username: [])
    monkeypatch.setattr(crud.db, "get_guardrails_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_least_used_subscription", lambda: None)
    monkeypatch.setattr(crud.db, "get_shared_folder_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_file_sharing_enabled", lambda name: False)
    monkeypatch.setattr(crud.db, "grant_default_permissions", lambda name, username: 0)
    monkeypatch.setattr(crud.db, "register_agent_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        crud.db,
        "create_agent_mcp_api_key",
        lambda **kwargs: SimpleNamespace(key_prefix="trinity_mcp_test", api_key="trinity_mcp_secret"),
    )

    await crud.create_agent_internal(
        AgentConfig(
            name="provider-v2-agent",
            runtime="opencode",
            runtime_provider_id="deepseek-openai",
            runtime_model_id="deepseek-v4-pro",
        ),
        SimpleNamespace(username="creator", role="creator"),
        SimpleNamespace(client=None, state=SimpleNamespace()),
    )

    env_vars = captured["environment"]
    assert env_vars["AGENT_RUNTIME"] == "opencode"
    assert env_vars["AGENT_RUNTIME_MODEL"] == "deepseek-openai/deepseek-v4-pro"
    assert env_vars["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"] == "sk-secret"
    assert env_vars["TRINITY_RUNTIME_PROVIDER_ID"] == "deepseek-openai"
    assert env_vars["TRINITY_RUNTIME_MODEL_ID"] == "deepseek-v4-pro"
    assert "sk-secret" not in env_vars["OPENCODE_CONFIG_CONTENT"]
    assert "OPENAI_API_KEY" not in env_vars
    assert "GOOGLE_API_KEY" not in env_vars
    assert "GEMINI_API_KEY" not in env_vars


@pytest.mark.asyncio
async def test_create_agent_internal_legacy_opencode_injects_provider_envs(monkeypatch):
    import services.agent_service.crud as crud

    captured = {}

    async def fake_containers_run(*args, **kwargs):
        captured["environment"] = kwargs["environment"]
        return _fake_agent_container("legacy-opencode-agent", kwargs["environment"])

    _patch_create_agent_dependencies(monkeypatch, crud)
    monkeypatch.setattr(crud, "containers_run", fake_containers_run)
    monkeypatch.setenv("OPENAI_API_KEY", "host-openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "host-google-key")
    monkeypatch.setenv("GEMINI_API_KEY", "host-gemini-key")

    await crud.create_agent_internal(
        AgentConfig(name="legacy-opencode-agent", runtime="opencode", runtime_model="openai/gpt-5"),
        SimpleNamespace(username="creator", role="creator"),
        SimpleNamespace(client=None, state=SimpleNamespace()),
    )

    env_vars = captured["environment"]
    assert env_vars["OPENAI_API_KEY"] == "host-openai-key"
    assert env_vars["GOOGLE_API_KEY"] == "host-google-key"
    assert env_vars["GEMINI_API_KEY"] == "host-gemini-key"


@pytest.mark.asyncio
async def test_create_agent_internal_unknown_runtime_model_id_returns_400(monkeypatch):
    import services.agent_service.crud as crud

    _patch_create_agent_dependencies(monkeypatch, crud, provider_configs={
        "deepseek-openai": {
            "id": "deepseek-openai",
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"}],
        }
    })

    await _assert_create_agent_http_400(
        AgentConfig(
            name="unknown-model-agent",
            runtime="opencode",
            runtime_provider_id="deepseek-openai",
            runtime_model_id="does-not-exist",
        ),
        "does-not-exist",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_unsupported_runtime_provider_protocol_returns_400(monkeypatch):
    import services.agent_service.crud as crud

    _patch_create_agent_dependencies(monkeypatch, crud, provider_configs={
        "anthropic-direct": {
            "id": "anthropic-direct",
            "name": "Anthropic Direct",
            "protocol": "anthropic-messages",
            "base_url": "https://api.anthropic.com/v1/messages",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "claude-sonnet-4-5", "claude_alias": "sonnet"}],
        }
    })

    await _assert_create_agent_http_400(
        AgentConfig(
            name="bad-protocol-agent",
            runtime="opencode",
            runtime_provider_id="anthropic-direct",
            runtime_model_id="claude-sonnet-4-5",
        ),
        "OpenCode v1 supports openai-compatible providers only",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_only_runtime_provider_id_returns_400(monkeypatch):
    import services.agent_service.crud as crud

    _patch_create_agent_dependencies(monkeypatch, crud)

    await _assert_create_agent_http_400(
        AgentConfig(
            name="provider-only-agent",
            runtime="opencode",
            runtime_provider_id="deepseek-openai",
        ),
        "Both runtime_provider_id and runtime_model_id are required",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_only_runtime_model_id_returns_400(monkeypatch):
    import services.agent_service.crud as crud

    _patch_create_agent_dependencies(monkeypatch, crud)

    await _assert_create_agent_http_400(
        AgentConfig(
            name="model-only-agent",
            runtime="opencode",
            runtime_model_id="deepseek-v4-pro",
        ),
        "Both runtime_provider_id and runtime_model_id are required",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_missing_runtime_provider_returns_400(monkeypatch):
    import services.agent_service.crud as crud

    _patch_create_agent_dependencies(monkeypatch, crud, provider_configs={})

    await _assert_create_agent_http_400(
        AgentConfig(
            name="missing-provider-agent",
            runtime="opencode",
            runtime_provider_id="deepseek-openai",
            runtime_model_id="deepseek-v4-pro",
        ),
        "Provider 'deepseek-openai' not found",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_invalid_local_template_runtime_override_returns_400(monkeypatch, tmp_path):
    import services.agent_service.crud as crud

    template_dir = tmp_path / "bad-runtime-template"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text("runtime:\n  type: vim\n", encoding="utf-8")
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (tmp_path.resolve(), tmp_path.resolve()))
    _patch_create_agent_dependencies(monkeypatch, crud)

    await _assert_create_agent_http_400(
        AgentConfig(name="bad-template-agent", template="local:bad-runtime-template"),
        "Unsupported runtime",
    )


@pytest.mark.asyncio
async def test_create_agent_internal_injects_saved_custom_provider(monkeypatch):
    import services.agent_service.crud as crud

    captured = {}

    async def fake_containers_run(*args, **kwargs):
        captured["environment"] = kwargs["environment"]
        return _fake_agent_container("custom-provider-agent", kwargs["environment"])

    async def fake_volume_get(name):
        return SimpleNamespace(name=name)

    monkeypatch.setattr(crud, "docker_client", object())
    monkeypatch.setattr(crud, "get_agent_by_name", lambda name: None)
    monkeypatch.setattr(crud, "get_next_available_port", lambda: 1234)
    monkeypatch.setattr(crud, "validate_base_image", lambda image: None)
    monkeypatch.setattr(crud, "volume_get", fake_volume_get)
    monkeypatch.setattr(crud, "containers_run", fake_containers_run)
    monkeypatch.setattr(crud, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(crud, "get_agent_default_require_email", lambda: True)
    monkeypatch.setattr(crud.settings_service, "get_custom_provider_configs", lambda: {
        "deepseek": {
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-custom-secret",
        }
    })
    monkeypatch.setattr(crud.db, "get_agent_owner", lambda name: None)
    monkeypatch.setattr(crud.db, "is_agent_name_reserved", lambda name: False)
    monkeypatch.setattr(crud.db, "get_agents_by_owner", lambda username: [])
    monkeypatch.setattr(crud.db, "get_guardrails_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_least_used_subscription", lambda: None)
    monkeypatch.setattr(crud.db, "get_shared_folder_config", lambda name: None)
    monkeypatch.setattr(crud.db, "get_file_sharing_enabled", lambda name: False)
    monkeypatch.setattr(crud.db, "grant_default_permissions", lambda name, username: 0)
    monkeypatch.setattr(crud.db, "register_agent_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        crud.db,
        "create_agent_mcp_api_key",
        lambda **kwargs: SimpleNamespace(key_prefix="trinity_mcp_test", api_key="trinity_mcp_secret"),
    )

    await crud.create_agent_internal(
        AgentConfig(name="custom-provider-agent", runtime="opencode", runtime_model="deepseek/deepseek-chat"),
        SimpleNamespace(username="creator", role="creator"),
        SimpleNamespace(client=None, state=SimpleNamespace()),
    )

    env_vars = captured["environment"]
    assert env_vars["AGENT_RUNTIME"] == "opencode"
    assert env_vars["AGENT_RUNTIME_MODEL"] == "deepseek/deepseek-chat"
    assert env_vars["TRINITY_CUSTOM_PROVIDER_DEEPSEEK_API_KEY"] == "sk-custom-secret"
    assert "sk-custom-secret" not in env_vars["OPENCODE_CONFIG_CONTENT"]
    data = json.loads(env_vars["OPENCODE_CONFIG_CONTENT"])
    assert data["provider"]["deepseek"]["options"]["baseURL"] == "https://api.deepseek.com/v1"
    assert data["provider"]["deepseek"]["options"]["apiKey"] == "{env:TRINITY_CUSTOM_PROVIDER_DEEPSEEK_API_KEY}"
    assert data["model"] == "deepseek/deepseek-chat"


def test_get_agent_status_prefers_runtime_label_over_env():
    container = SimpleNamespace(
        labels={
            "trinity.agent-type": "test-agent",
            "trinity.created": "2026-06-20T00:00:00Z",
            "trinity.agent-runtime": "opencode",
            "trinity.runtime": "gemini-cli",
        },
        name="agent-runtime-test",
        status="running",
        id="container-id",
        attrs={"Config": {"Env": ["AGENT_RUNTIME=claude-code"]}},
        image=SimpleNamespace(labels={}),
    )

    status = get_agent_status_from_container(container)

    assert status.runtime == "opencode"


def test_get_agent_status_uses_legacy_runtime_label_before_default():
    container = SimpleNamespace(
        labels={
            "trinity.agent-type": "test-agent",
            "trinity.created": "2026-06-20T00:00:00Z",
            "trinity.runtime": "opencode",
        },
        name="agent-runtime-test",
        status="running",
        id="container-id",
        attrs={"Config": {"Env": ["AGENT_RUNTIME=claude-code"]}},
        image=SimpleNamespace(labels={}),
    )

    status = get_agent_status_from_container(container)

    assert status.runtime == "opencode"


def test_get_agent_status_falls_back_to_env_runtime_when_labels_missing():
    container = SimpleNamespace(
        labels={
            "trinity.agent-type": "test-agent",
            "trinity.created": "2026-06-20T00:00:00Z",
        },
        name="agent-runtime-test",
        status="running",
        id="container-id",
        attrs={"Config": {"Env": ["AGENT_RUNTIME=opencode"]}},
        image=SimpleNamespace(labels={}),
    )

    status = get_agent_status_from_container(container)

    assert status.runtime == "opencode"


def test_task_runtime_defaults_use_shared_label_then_env_order(monkeypatch):
    container = SimpleNamespace(
        labels={"trinity.agent-runtime": "opencode"},
        attrs={"Config": {"Env": ["AGENT_RUNTIME=claude-code", "AGENT_RUNTIME_MODEL=openai/gpt-5"]}},
    )

    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    runtime, model = _get_agent_runtime_defaults("runtime-test")

    assert runtime == "opencode"
    assert model == "openai/gpt-5"


def test_task_runtime_defaults_fall_back_to_env_runtime(monkeypatch):
    container = SimpleNamespace(
        labels={},
        attrs={"Config": {"Env": ["AGENT_RUNTIME=opencode", "AGENT_RUNTIME_MODEL=openai/gpt-5"]}},
    )

    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    runtime, model = _get_agent_runtime_defaults("runtime-test")

    assert runtime == "opencode"
    assert model == "openai/gpt-5"


def test_resolve_default_model_prefers_agent_runtime_model(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("opencode", "openai/gpt-5"))
    monkeypatch.setattr("services.task_execution_service.settings_service.resolve_model_for_runtime", lambda runtime: "anthropic/claude-sonnet-4-5")

    assert _resolve_execution_model("agent-a", None) == "openai/gpt-5"


def test_resolve_default_model_uses_runtime_defaults_when_no_agent_override(monkeypatch):
    from services.task_execution_service import _resolve_execution_model
    seen = []

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("opencode", None))
    monkeypatch.setattr("services.task_execution_service.settings_service.resolve_model_for_runtime", lambda runtime: seen.append(runtime) or "openai/gpt-5")

    assert _resolve_execution_model("agent-a", None) == "openai/gpt-5"
    assert seen == ["opencode"]


def test_resolve_default_model_normalizes_legacy_gemini_runtime(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    seen = []
    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("gemini", None))
    monkeypatch.setattr("services.task_execution_service.settings_service.resolve_model_for_runtime", lambda runtime: seen.append(runtime) or "gemini-3-flash")

    assert _resolve_execution_model("agent-a", None) == "gemini-3-flash"
    assert seen == ["gemini-cli"]


def test_resolve_default_model_uses_legacy_fallback_if_settings_lookup_fails(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("gemini", None))
    monkeypatch.setattr("services.task_execution_service.settings_service.resolve_model_for_runtime", lambda runtime: (_ for _ in ()).throw(RuntimeError("db unavailable")))

    assert _resolve_execution_model("agent-a", None) == "gemini-3-flash"


def test_resolve_default_model_unknown_runtime_uses_platform_legacy(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("unknown", None))
    monkeypatch.setattr("services.task_execution_service.settings_service.resolve_model_for_runtime", lambda runtime: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr("services.task_execution_service.settings_service.get_platform_default_model", lambda: "claude-sonnet-4-6")

    assert _resolve_execution_model("agent-a", None) == "claude-sonnet-4-6"


def test_resolve_default_model_keeps_explicit_model(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: (_ for _ in ()).throw(AssertionError("should not inspect runtime")))

    assert _resolve_execution_model("agent-a", "explicit-model") == "explicit-model"


def test_resolve_explicit_provider_model_uses_claude_default_for_claude_code(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("claude-code", None))
    monkeypatch.setattr(
        "services.task_execution_service.settings_service.resolve_model_for_runtime",
        lambda runtime: "claude-sonnet-4-6",
    )

    assert _resolve_execution_model("agent-a", "deepseek/deepseek-v4-flash") == "claude-sonnet-4-6"


def test_resolve_explicit_provider_model_uses_claude_legacy_if_default_is_incompatible(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("claude-code", None))
    monkeypatch.setattr(
        "services.task_execution_service.settings_service.resolve_model_for_runtime",
        lambda runtime: "deepseek-v4-flash",
    )
    monkeypatch.setattr(
        "services.task_execution_service.settings_service.get_platform_default_model",
        lambda: "claude-sonnet-4-6",
    )

    assert _resolve_execution_model("agent-a", "deepseek/deepseek-v4-flash") == "claude-sonnet-4-6"


def test_resolve_explicit_provider_model_uses_sonnet_if_all_claude_defaults_are_incompatible(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    monkeypatch.setattr("services.task_execution_service._get_agent_runtime_defaults", lambda name: ("claude-code", None))
    monkeypatch.setattr(
        "services.task_execution_service.settings_service.resolve_model_for_runtime",
        lambda runtime: "deepseek-v4-flash",
    )
    monkeypatch.setattr(
        "services.task_execution_service.settings_service.get_platform_default_model",
        lambda: "deepseek-v4-flash",
    )

    assert _resolve_execution_model("agent-a", "deepseek/deepseek-v4-flash") == "sonnet"


def test_resolve_explicit_opencode_template_model_normalizes_stale_provider(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    container = _fake_agent_container("runtime-test", {
        "AGENT_RUNTIME": "opencode",
        "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        "TRINITY_RUNTIME_PROVIDER_ID": "deepseek-openai",
        "OPENCODE_CONFIG_CONTENT": _opencode_provider_config(),
    })
    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    assert (
        _resolve_execution_model("runtime-test", "deepseek/deepseek-v4-flash")
        == "deepseek-openai/deepseek-v4-flash"
    )


def test_resolve_explicit_opencode_template_model_leaves_unmappable(monkeypatch):
    from services.task_execution_service import _resolve_execution_model

    container = _fake_agent_container("runtime-test", {
        "AGENT_RUNTIME": "opencode",
        "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        "TRINITY_RUNTIME_PROVIDER_ID": "deepseek-openai",
        "OPENCODE_CONFIG_CONTENT": _opencode_provider_config(),
    })
    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    assert (
        _resolve_execution_model("runtime-test", "deepseek/not-present")
        == "deepseek/not-present"
    )


@pytest.mark.asyncio
async def test_chat_payload_normalizes_stale_opencode_template_model(monkeypatch):
    import routers.chat as chat
    from models import ChatMessageRequest

    container = _fake_agent_container("runtime-test", {
        "AGENT_RUNTIME": "opencode",
        "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        "TRINITY_RUNTIME_PROVIDER_ID": "deepseek-openai",
        "OPENCODE_CONFIG_CONTENT": _opencode_provider_config(),
    })
    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    captured = {}

    async def fake_post(agent_name, endpoint, payload, **kwargs):
        captured["payload"] = payload
        return httpx.Response(200, json={
            "response": "ok",
            "metadata": {"cost_usd": 0.01},
            "session": {"context_tokens": 1, "context_window": 200000},
            "session_id": "00000000-0000-4000-8000-000000000000",
            "execution_log": [],
            "execution_log_simplified": [],
        }, request=httpx.Request("POST", "http://agent-runtime-test:8000/api/chat"))

    monkeypatch.setattr(chat, "agent_post_with_retry", fake_post)
    monkeypatch.setattr(chat, "compose_system_prompt", lambda **kwargs: "system")
    monkeypatch.setattr(chat, "is_execution_context_enabled", lambda: False)

    async def fake_complete_activity(*args, **kwargs):
        return None

    monkeypatch.setattr(chat.activity_service, "complete_activity", fake_complete_activity)
    monkeypatch.setattr(chat.db, "mark_execution_dispatched", lambda execution_id: None)
    monkeypatch.setattr(chat.db, "add_chat_message", lambda **kwargs: SimpleNamespace(id="msg1"))
    monkeypatch.setattr(chat.db, "update_execution_status", lambda **kwargs: None)

    idem = SimpleNamespace()
    monkeypatch.setattr(chat.idempotency_service, "complete", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.idempotency_service, "fail", lambda *args, **kwargs: None)
    capacity = SimpleNamespace(release=fake_complete_activity)

    await chat._run_chat_and_finalize(
        name="runtime-test",
        request=ChatMessageRequest(message="hi", model="deepseek/deepseek-v4-flash"),
        current_user=SimpleNamespace(id=1, email="u@example.com", username="u"),
        x_source_agent=None,
        x_mcp_key_name=None,
        triggered_by="chat",
        task_execution_id="exec1",
        _chat_subscription_id=None,
        chat_activity_id="act1",
        collaboration_activity_id=None,
        session=SimpleNamespace(id="session1"),
        execution=SimpleNamespace(id="exec1"),
        queue_result="running",
        is_queued=False,
        chat_timeout=30,
        idem=idem,
        capacity=capacity,
    )

    assert captured["payload"]["model"] == "deepseek-openai/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_task_creation_records_normalized_stale_opencode_template_model(monkeypatch):
    import routers.chat as chat

    container = _fake_agent_container("runtime-test", {
        "AGENT_RUNTIME": "opencode",
        "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        "TRINITY_RUNTIME_PROVIDER_ID": "deepseek-openai",
        "OPENCODE_CONFIG_CONTENT": _opencode_provider_config(),
    })
    monkeypatch.setattr(chat, "get_agent_container", lambda name: container)
    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    created = {}

    def fake_create_task_execution(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(id="exec1")

    monkeypatch.setattr(chat.db, "get_agent_subscription_id", lambda name: None)
    monkeypatch.setattr(chat.db, "create_task_execution", fake_create_task_execution)
    monkeypatch.setattr(chat.db, "get_max_parallel_tasks", lambda name: 1)
    monkeypatch.setattr(chat.db, "get_execution_timeout", lambda name: 30)
    monkeypatch.setattr(chat.db, "update_execution_status", lambda **kwargs: None)
    monkeypatch.setattr(chat, "dispatch_breaker_active", lambda name: False)

    idem = SimpleNamespace(replay=False, execution_id=None, in_flight=False, snapshot=None)
    monkeypatch.setattr(chat.idempotency_service, "make_agent_scope", lambda name: name)
    monkeypatch.setattr(chat.idempotency_service, "begin", lambda *args, **kwargs: idem)
    monkeypatch.setattr(chat.idempotency_service, "attach_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.idempotency_service, "fail", lambda *args, **kwargs: None)

    class FullCapacity:
        async def acquire(self, **kwargs):
            raise chat.CapacityFull("runtime-test", 1, "persistent_full")

    monkeypatch.setattr(chat, "get_capacity_manager", lambda: FullCapacity())

    with pytest.raises(HTTPException) as exc:
        await chat.execute_parallel_task(
            request=ParallelTaskRequest(
                message="hi",
                model="deepseek/deepseek-v4-flash",
                async_mode=True,
            ),
            name="runtime-test",
            current_user=SimpleNamespace(id=1, email="u@example.com", username="u", agent_name=None),
            x_source_agent=None,
            x_via_mcp=None,
            x_mcp_key_id=None,
            x_mcp_key_name=None,
            idempotency_key=None,
        )

    assert exc.value.status_code == 429
    assert created["model_used"] == "deepseek-openai/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_set_agent_model_normalizes_stale_opencode_template_model(monkeypatch):
    import routers.chat as chat

    container = _fake_agent_container("runtime-test", {
        "AGENT_RUNTIME": "opencode",
        "AGENT_RUNTIME_MODEL": "deepseek-openai/deepseek-v4-flash",
        "TRINITY_RUNTIME_PROVIDER_ID": "deepseek-openai",
        "OPENCODE_CONFIG_CONTENT": _opencode_provider_config(),
    })
    monkeypatch.setattr(chat, "get_agent_container", lambda name: container)
    monkeypatch.setattr("services.docker_service.get_agent_container", lambda name: container)

    captured = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def put(self, url, json, timeout):
            captured["json"] = json
            return httpx.Response(200, json={"model": json["model"]}, request=httpx.Request("PUT", url))

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeAsyncClient)

    result = await chat.set_agent_model(
        request=ModelChangeRequest(model="deepseek/deepseek-v4-flash"),
        name="runtime-test",
        current_user=SimpleNamespace(id=1, email="u@example.com", username="u"),
    )

    assert captured["json"]["model"] == "deepseek-openai/deepseek-v4-flash"
    assert result["model"] == "deepseek-openai/deepseek-v4-flash"


def test_template_runtime_override_rejects_invalid_runtime():
    config = AgentConfig(name="bad-template")

    try:
        _apply_runtime_template_config(config, {"type": "vim"})
    except ValueError as exc:
        assert "Unsupported runtime" in str(exc)
    else:
        raise AssertionError("Invalid template runtime accepted")


def test_template_runtime_override_rejects_invalid_permission():
    config = AgentConfig(name="bad-template")

    try:
        _apply_runtime_template_config(config, {"type": "opencode", "permission": "root"})
    except ValueError as exc:
        assert "Unsupported runtime_permission" in str(exc)
    else:
        raise AssertionError("Invalid template runtime permission accepted")
