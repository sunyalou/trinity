from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.provider_configs import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GOOGLE_GEMINI_PROTOCOL,
    GOOGLE_PROTOCOL_GATEWAY_PROTOCOL,
    GOOGLE_VERTEX_PROTOCOL,
    OPENAI_COMPATIBLE_PROTOCOL,
    find_provider_model,
    PROVIDER_CONFIGS_KEY,
    mask_provider_configs,
    parse_provider_configs,
    provider_env_var_name,
    runtime_supports_provider,
    serialize_provider_configs,
    validate_provider_configs,
)


class AsyncNoop:
    async def __call__(self, *args, **kwargs):
        return None


def _setting(key: str, value: str):
    from database import SystemSetting

    return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))


def _provider_payload(api_key: str = "sk-secret-1234"):
    return {
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": api_key},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    }


def test_constants():
    assert PROVIDER_CONFIGS_KEY == "provider_configs"
    assert OPENAI_COMPATIBLE_PROTOCOL == "openai-compatible"
    assert ANTHROPIC_MESSAGES_PROTOCOL == "anthropic-messages"
    assert GOOGLE_GEMINI_PROTOCOL == "google-gemini"
    assert GOOGLE_VERTEX_PROTOCOL == "google-vertex"
    assert GOOGLE_PROTOCOL_GATEWAY_PROTOCOL == "google-protocol-gateway"


def test_validate_openai_compatible_provider_with_models():
    result = validate_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1/",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [
                {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "tool_call": True, "context": 128000, "output": 8192},
                {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
            ],
        }
    })

    provider = result["deepseek-openai"]
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["auth"]["api_key"] == "sk-secret"
    assert provider["models"][0]["id"] == "deepseek-v4-pro"


def test_validate_model_tool_call_must_be_bool():
    with pytest.raises(ValueError, match="tool_call must be a boolean for deepseek-openai/deepseek-v4-pro"):
        validate_provider_configs({
            "deepseek-openai": {
                "name": "DeepSeek OpenAI",
                "protocol": "openai-compatible",
                "base_url": "https://api.deepseek.com/v1",
                "auth": {"type": "api_key", "api_key": "sk-secret"},
                "models": [{"id": "deepseek-v4-pro", "tool_call": "false"}],
            }
        })


def test_validate_model_context_must_be_integer():
    with pytest.raises(ValueError, match="context must be a positive integer for deepseek-openai/deepseek-v4-pro"):
        validate_provider_configs({
            "deepseek-openai": {
                "name": "DeepSeek OpenAI",
                "protocol": "openai-compatible",
                "base_url": "https://api.deepseek.com/v1",
                "auth": {"type": "api_key", "api_key": "sk-secret"},
                "models": [{"id": "deepseek-v4-pro", "context": "abc"}],
            }
        })


def test_validate_model_context_must_be_positive():
    with pytest.raises(ValueError, match="context must be a positive integer for deepseek-openai/deepseek-v4-pro"):
        validate_provider_configs({
            "deepseek-openai": {
                "name": "DeepSeek OpenAI",
                "protocol": "openai-compatible",
                "base_url": "https://api.deepseek.com/v1",
                "auth": {"type": "api_key", "api_key": "sk-secret"},
                "models": [{"id": "deepseek-v4-pro", "context": 0}],
            }
        })


def test_validate_anthropic_messages_provider_rejects_duplicate_alias():
    with pytest.raises(ValueError, match="Duplicate Claude alias"):
        validate_provider_configs({
            "deepseek-anthropic": {
                "name": "DeepSeek Anthropic",
                "protocol": "anthropic-messages",
                "base_url": "https://api.deepseek.com/anthropic",
                "auth": {"type": "api_key", "header_mode": "x-api-key", "api_key": "sk-secret"},
                "models": [
                    {"id": "deepseek-v4-pro", "claude_alias": "sonnet"},
                    {"id": "deepseek-v4-pro-alt", "claude_alias": "sonnet"},
                ],
            }
        })


def test_validate_anthropic_messages_provider_accepts_fable_alias():
    result = validate_provider_configs({
        "story-provider": {
            "name": "Story Provider",
            "protocol": "anthropic-messages",
            "base_url": "https://example.test/anthropic",
            "auth": {"type": "api_key", "header_mode": "bearer", "api_key": "token"},
            "models": [{"id": "story-model", "claude_alias": "fable"}],
        }
    })
    assert result["story-provider"]["models"][0]["claude_alias"] == "fable"


def test_validate_google_gemini_provider_does_not_require_base_url():
    result = validate_provider_configs({
        "google-ai-studio": {
            "name": "Google AI Studio",
            "protocol": "google-gemini",
            "auth": {"type": "api_key", "api_key": "google-secret"},
            "models": [{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"}],
        }
    })
    assert result["google-ai-studio"]["models"][0]["id"] == "gemini-2.5-pro"


def test_validate_google_vertex_provider_requires_project_and_location():
    with pytest.raises(ValueError, match="project is required"):
        validate_provider_configs({
            "vertex-prod": {
                "name": "Vertex Prod",
                "protocol": "google-vertex",
                "location": "us-central1",
                "auth": {"type": "adc"},
                "models": [{"id": "gemini-2.5-pro"}],
            }
        })


def test_validate_google_vertex_provider_accepts_adc_without_api_key():
    result = validate_provider_configs({
        "vertex-prod": {
            "name": "Vertex Prod",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "adc"},
            "models": [{"id": "gemini-2.5-pro"}],
        }
    })
    assert result["vertex-prod"]["auth"] == {"type": "adc"}


def test_validate_google_vertex_provider_accepts_service_account():
    result = validate_provider_configs({
        "vertex-sa": {
            "name": "Vertex Service Account",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "service_account", "credential_ref": "secret://vertex-sa"},
            "models": [{"id": "gemini-2.5-pro"}],
        }
    })
    assert result["vertex-sa"]["auth"]["credential_ref"] == "secret://vertex-sa"


@pytest.mark.parametrize(
    ("protocol", "auth"),
    [
        ("openai-compatible", {"type": "adc"}),
        ("anthropic-messages", {"type": "service_account", "credential_ref": "secret://anthropic"}),
        ("google-gemini", {"type": "adc"}),
        ("google-vertex", {"type": "api_key", "api_key": "google-secret"}),
        ("google-protocol-gateway", {"type": "adc"}),
    ],
)
def test_validate_rejects_invalid_protocol_auth_combinations(protocol, auth):
    provider = {
        "name": "Provider",
        "protocol": protocol,
        "auth": auth,
        "models": [{"id": "model-1"}],
    }
    if protocol in {"openai-compatible", "anthropic-messages", "google-protocol-gateway"}:
        provider["base_url"] = "https://example.test/v1"
    if protocol == "google-vertex":
        provider["project"] = "project-id"
        provider["location"] = "us-central1"

    with pytest.raises(ValueError, match="Unsupported auth type"):
        validate_provider_configs({"bad-provider": provider})


def test_validate_google_protocol_gateway_shape():
    result = validate_provider_configs({
        "google-gateway": {
            "name": "Google Protocol Gateway",
            "protocol": "google-protocol-gateway",
            "base_url": "https://gateway.example.test/v1/",
            "auth": {"type": "api_key", "api_key": "gateway-secret"},
            "models": [{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"}],
        }
    })

    assert result["google-gateway"]["protocol"] == GOOGLE_PROTOCOL_GATEWAY_PROTOCOL
    assert result["google-gateway"]["base_url"] == "https://gateway.example.test/v1"
    assert result["google-gateway"]["auth"]["api_key"] == "gateway-secret"


def test_mask_provider_configs_never_returns_raw_key():
    masked = mask_provider_configs(validate_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret-value"},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    }))
    assert masked["deepseek-openai"]["auth"]["api_key_configured"] is True
    assert "sk-secret-value" not in json.dumps(masked)


def test_mask_provider_configs_preserves_adc_auth_shape_without_api_key():
    masked = mask_provider_configs(validate_provider_configs({
        "vertex-prod": {
            "name": "Vertex Prod",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "adc"},
            "models": [{"id": "gemini-2.5-pro"}],
        }
    }))

    assert masked["vertex-prod"]["auth"] == {
        "type": "adc",
        "api_key_configured": False,
        "api_key_masked": None,
    }
    assert "api_key\":" not in json.dumps(masked)


def test_mask_provider_configs_preserves_service_account_credential_ref_without_api_key():
    masked = mask_provider_configs(validate_provider_configs({
        "vertex-sa": {
            "name": "Vertex Service Account",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "service_account", "credential_ref": "secret://vertex-sa"},
            "models": [{"id": "gemini-2.5-pro"}],
        }
    }))

    assert masked["vertex-sa"]["auth"]["credential_ref"] == "secret://vertex-sa"
    assert masked["vertex-sa"]["auth"]["api_key_configured"] is False
    assert "api_key\":" not in json.dumps(masked)


def test_provider_env_var_name_is_stable_and_sanitized():
    assert provider_env_var_name("deepseek-openai") == "TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"
    assert provider_env_var_name("foo.bar") == "TRINITY_PROVIDER_FOO_BAR_API_KEY"


def test_provider_env_var_name_rejects_bad_direct_input():
    with pytest.raises(ValueError, match="alphanumeric"):
        provider_env_var_name("...")


def test_validate_provider_id_requires_alphanumeric_env_stem():
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_provider_configs({
            "...": {
                "name": "Bad Provider",
                "protocol": "openai-compatible",
                "base_url": "https://example.test/v1",
                "auth": {"type": "api_key", "api_key": "sk-secret"},
                "models": [{"id": "model-1"}],
            }
        })


def test_validate_provider_env_var_collision_rejected():
    provider = {
        "name": "Provider",
        "protocol": "openai-compatible",
        "base_url": "https://example.test/v1",
        "auth": {"type": "api_key", "api_key": "sk-secret"},
        "models": [{"id": "model-1"}],
    }

    with pytest.raises(ValueError, match="Provider env var collision"):
        validate_provider_configs({"foo.bar": provider, "foo-bar": provider})


@pytest.mark.parametrize(
    ("runtime", "protocol", "expected"),
    [
        ("claude-code", "anthropic-messages", True),
        ("claude-code", "openai-compatible", False),
        ("opencode", "openai-compatible", True),
        ("opencode", "anthropic-messages", False),
        ("gemini-cli", "google-gemini", True),
        ("gemini-cli", "google-vertex", True),
        ("gemini-cli", "google-protocol-gateway", False),
        ("gemini-cli", "openai-compatible", False),
    ],
)
def test_runtime_supports_provider(runtime, protocol, expected):
    assert runtime_supports_provider(runtime, protocol) is expected


def test_find_provider_model_success():
    provider = validate_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"}],
        }
    })["deepseek-openai"]

    assert find_provider_model(provider, "deepseek-v4-pro")["label"] == "DeepSeek V4 Pro"


def test_find_provider_model_missing_raises():
    provider = validate_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    })["deepseek-openai"]

    with pytest.raises(ValueError, match="Model not found"):
        find_provider_model(provider, "missing-model")


def test_parse_invalid_json_returns_empty():
    assert parse_provider_configs("not-json") == {}


def test_parse_valid_json_with_invalid_provider_schema_raises():
    with pytest.raises(ValueError, match="Unsupported protocol"):
        parse_provider_configs(json.dumps({
            "bad-provider": {
                "name": "Bad Provider",
                "protocol": "bad-protocol",
                "auth": {"type": "api_key", "api_key": "sk-secret"},
                "models": [{"id": "model-1"}],
            }
        }))


def test_serialize_roundtrip():
    value = {
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1/",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    }
    assert parse_provider_configs(serialize_provider_configs(value))["deepseek-openai"]["base_url"] == "https://api.deepseek.com/v1"


def test_settings_service_roundtrips_provider_configs(monkeypatch):
    from services import settings_service

    stored = {}
    monkeypatch.setattr(settings_service.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(
        settings_service.db,
        "set_setting",
        lambda key, value: stored.__setitem__(key, value) or True,
    )

    settings_service.set_provider_configs({
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-secret"},
            "models": [{"id": "deepseek-v4-pro"}],
        }
    })

    assert settings_service.get_provider_configs()["deepseek-openai"]["auth"]["api_key"] == "sk-secret"
    masked = settings_service.get_masked_provider_configs()
    assert masked["deepseek-openai"]["auth"]["api_key_configured"] is True
    assert "sk-secret" not in json.dumps(masked)


def test_provider_configs_route_is_admin_only_and_masks_secret(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)

    stored = {PROVIDER_CONFIGS_KEY: serialize_provider_configs(_provider_payload())}
    monkeypatch.setattr(settings_router.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    try:
        app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=2, username="creator", role="creator")
        with TestClient(app) as client:
            creator_response = client.get("/api/settings/provider-configs")

        app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
        with TestClient(app) as client:
            admin_response = client.get("/api/settings/provider-configs")
    finally:
        app.dependency_overrides.clear()

    assert creator_response.status_code == 403
    assert admin_response.status_code == 200
    assert "sk-secret-1234" not in admin_response.text
    assert admin_response.json()["providers"]["deepseek-openai"]["auth"]["api_key_configured"] is True


def test_provider_configs_put_requires_admin(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=2, username="creator", role="creator")

    set_calls = []
    monkeypatch.setattr(settings_router.settings_service, "set_provider_configs", lambda *args, **kwargs: set_calls.append(args))

    try:
        with TestClient(app) as client:
            response = client.put("/api/settings/provider-configs", json={"providers": _provider_payload()})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert set_calls == []


def test_provider_configs_put_returns_service_masked_after_save(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    monkeypatch.setattr(settings_router.settings_service, "set_provider_configs", lambda value, updated_by="system": _provider_payload("sk-new-secret"))
    monkeypatch.setattr(
        settings_router.settings_service,
        "get_masked_provider_configs",
        lambda: {"from-service": {"auth": {"api_key_configured": True, "api_key_masked": "...cret"}}},
    )
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            response = client.put("/api/settings/provider-configs", json={"providers": _provider_payload("sk-new-secret")})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["providers"] == {"from-service": {"auth": {"api_key_configured": True, "api_key_masked": "...cret"}}}
    assert "sk-new-secret" not in response.text


def test_generic_settings_routes_block_provider_configs(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    stored = {
        PROVIDER_CONFIGS_KEY: serialize_provider_configs(_provider_payload()),
        "other": "safe",
    }
    set_calls = []
    delete_calls = []

    def get_setting(key):
        value = stored.get(key)
        return _setting(key, value) if value is not None else None

    def set_setting(key, value):
        set_calls.append((key, value))
        stored[key] = value
        return _setting(key, value)

    def delete_setting(key):
        delete_calls.append(key)
        return stored.pop(key, None) is not None

    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.db, "get_all_settings", lambda: [_setting(key, value) for key, value in stored.items()])
    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.db, "delete_setting", delete_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            all_response = client.get("/api/settings")
            get_response = client.get("/api/settings/provider_configs")
            put_response = client.put("/api/settings/provider_configs", json={"value": "{}"})
            delete_response = client.delete("/api/settings/provider_configs")
    finally:
        app.dependency_overrides.clear()

    assert all_response.status_code == 200
    assert get_response.status_code == 404
    assert put_response.status_code == 400
    assert delete_response.status_code == 400
    assert PROVIDER_CONFIGS_KEY not in [item["key"] for item in all_response.json()]
    assert "sk-secret-1234" not in all_response.text
    assert "sk-secret-1234" not in get_response.text
    assert "sk-secret-1234" not in put_response.text
    assert "sk-secret-1234" not in delete_response.text
    assert set_calls == []
    assert delete_calls == []
    assert PROVIDER_CONFIGS_KEY in stored
