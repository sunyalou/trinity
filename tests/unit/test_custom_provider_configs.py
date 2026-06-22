from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.custom_provider_configs import (
    CUSTOM_PROVIDER_CONFIGS_KEY,
    OPENAI_COMPATIBLE_PROTOCOL,
    mask_custom_provider_configs,
    parse_custom_provider_configs,
    serialize_custom_provider_configs,
    validate_custom_provider_configs,
)


class AsyncNoop:
    async def __call__(self, *args, **kwargs):
        return None


def _setting(key: str, value: str):
    from database import SystemSetting

    return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))


def test_constants_match_expected_setting_keys():
    assert CUSTOM_PROVIDER_CONFIGS_KEY == "custom_provider_configs"
    assert OPENAI_COMPATIBLE_PROTOCOL == "openai-compatible"


def test_validate_normalizes_trailing_slash_and_preserves_existing_key_when_blank():
    existing = {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key": "existing-secret",
        }
    }
    incoming = {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1/",
            "api_key": "   ",
        }
    }

    assert validate_custom_provider_configs(incoming, existing=existing) == {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key": "existing-secret",
        }
    }


def test_serialize_custom_provider_configs_validates_normalizes_and_sorts_json():
    serialized = serialize_custom_provider_configs(
        {
            "z-provider": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://z.example.test/v1/",
                "api_key": "z-secret",
            },
            "a-provider": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "http://a.example.test/v1/",
                "api_key": "a-secret",
            },
        }
    )

    assert serialized == json.dumps(
        {
            "a-provider": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "http://a.example.test/v1",
                "api_key": "a-secret",
            },
            "z-provider": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://z.example.test/v1",
                "api_key": "z-secret",
            },
        },
        sort_keys=True,
    )


def test_first_save_requires_api_key():
    with pytest.raises(ValueError, match="API key is required"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "",
                }
            }
        )


def test_provider_name_is_required():
    with pytest.raises(ValueError, match="Provider name is required"):
        validate_custom_provider_configs(
            {
                "": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


def test_provider_name_must_be_string():
    with pytest.raises(ValueError, match="Provider name must be a string"):
        validate_custom_provider_configs(
            {
                123: {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


@pytest.mark.parametrize("provider", ["bad/name", "bad name", "bad\tname", " leading", "trailing "])
def test_rejects_bad_provider_names_containing_slash_or_whitespace(provider):
    with pytest.raises(ValueError):
        validate_custom_provider_configs(
            {
                provider: {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


@pytest.mark.parametrize("protocol", [None, ""])
def test_protocol_is_required(protocol):
    with pytest.raises(ValueError, match="Protocol is required"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": protocol,
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


def test_rejects_unsupported_protocol():
    with pytest.raises(ValueError, match="Unsupported protocol"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": "anthropic-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


@pytest.mark.parametrize("protocol", [" openai-compatible", "openai-compatible "])
def test_rejects_protocol_with_leading_or_trailing_whitespace(protocol):
    with pytest.raises(ValueError, match="Unsupported protocol"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": protocol,
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


@pytest.mark.parametrize("base_url", [None, ""])
def test_base_url_is_required(base_url):
    with pytest.raises(ValueError, match="Base URL is required"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": base_url,
                    "api_key": "secret",
                }
            }
        )


def test_rejects_base_url_not_starting_with_http_or_https():
    with pytest.raises(ValueError, match="Base URL must start"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "ftp://example.test/v1",
                    "api_key": "secret",
                }
            }
        )


@pytest.mark.parametrize(
    "base_url",
    ["https://example.test/my path", " https://example.test/v1", "https://example.test/v1 "],
)
def test_rejects_base_url_with_whitespace(base_url):
    with pytest.raises(ValueError, match="Base URL cannot contain whitespace"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": base_url,
                    "api_key": "secret",
                }
            }
        )


def test_mask_custom_provider_configs_removes_raw_key_and_returns_key_status():
    masked = mask_custom_provider_configs(
        {
            "short-key": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://short.example.test/v1",
                "api_key": "short",
            },
            "long-key": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://long.example.test/v1",
                "api_key": "sk-secret-1234",
            },
            "empty-key": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://empty.example.test/v1",
                "api_key": "",
            },
        }
    )

    assert masked == {
        "short-key": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://short.example.test/v1",
            "api_key_configured": True,
            "api_key_masked": "****",
        },
        "long-key": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://long.example.test/v1",
            "api_key_configured": True,
            "api_key_masked": "...1234",
        },
        "empty-key": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://empty.example.test/v1",
            "api_key_configured": False,
            "api_key_masked": None,
        },
    }
    assert all("api_key" not in entry for entry in masked.values())


def test_parse_custom_provider_configs_returns_empty_for_malformed_json():
    assert parse_custom_provider_configs("not-json") == {}


@pytest.mark.parametrize("raw", ['["not", "object"]', '"not-object"', "123", "null"])
def test_parse_custom_provider_configs_returns_empty_for_non_object_json(raw):
    assert parse_custom_provider_configs(raw) == {}


def test_parse_custom_provider_configs_ignores_malformed_entries():
    raw = json.dumps(
        {
            "valid": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://example.test/v1/",
                "api_key": "secret",
            },
            "bad/name": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://example.test/v1",
                "api_key": "secret",
            },
            "also-bad": "not-an-object",
        }
    )

    assert parse_custom_provider_configs(raw) == {
        "valid": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key": "secret",
        }
    }


def test_settings_service_saves_and_masks_configs(monkeypatch):
    import services.settings_service as settings_module

    stored = {}
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value) or _setting(key, value))

    svc = settings_module.SettingsService()
    saved = svc.set_custom_provider_configs(
        {
            "local-llm": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://example.test/v1/",
                "api_key": "sk-secret-1234",
            }
        }
    )

    assert saved == {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key": "sk-secret-1234",
        }
    }
    assert json.loads(stored[CUSTOM_PROVIDER_CONFIGS_KEY]) == saved
    assert svc.get_masked_custom_provider_configs() == {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key_configured": True,
            "api_key_masked": "...1234",
        }
    }


def test_settings_service_blank_api_key_preserves_existing_key(monkeypatch):
    import services.settings_service as settings_module

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: serialize_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://old.example.test/v1",
                    "api_key": "existing-secret",
                }
            }
        )
    }
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value) or _setting(key, value))

    saved = settings_module.SettingsService().set_custom_provider_configs(
        {
            "local-llm": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://new.example.test/v1",
                "api_key": "   ",
            }
        }
    )

    assert saved["local-llm"]["api_key"] == "existing-secret"
    assert json.loads(stored[CUSTOM_PROVIDER_CONFIGS_KEY])["local-llm"]["api_key"] == "existing-secret"


def test_settings_service_omitted_saved_provider_configs_are_preserved(monkeypatch):
    import services.settings_service as settings_module

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: serialize_custom_provider_configs(
            {
                "existing": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://existing.example.test/v1",
                    "api_key": "existing-secret",
                }
            }
        )
    }
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value) or _setting(key, value))

    saved = settings_module.SettingsService().set_custom_provider_configs(
        {
            "new": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://new.example.test/v1",
                "api_key": "new-secret",
            }
        }
    )

    assert set(saved) == {"existing", "new"}
    assert saved["existing"]["api_key"] == "existing-secret"


def test_custom_provider_config_routes_mask_and_persist(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    stored = {}
    monkeypatch.setattr(settings_router.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(
        settings_router.settings_service,
        "get_runtime_default_models",
        lambda: {"opencode": {"provider": "local-llm", "model": "llama-3.3"}},
    )
    monkeypatch.setattr(settings_router.db, "set_setting", lambda key, value: stored.__setitem__(key, value) or _setting(key, value))
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            put_response = client.put(
                "/api/settings/custom-provider-configs",
                json={
                    "custom_provider_configs": {
                        "local-llm": {
                            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                            "base_url": "https://example.test/v1/",
                            "api_key": "sk-secret-1234",
                        }
                    }
                },
            )
            get_response = client.get("/api/settings/custom-provider-configs")
    finally:
        app.dependency_overrides.clear()

    assert put_response.status_code == 200
    assert get_response.status_code == 200
    assert "sk-secret-1234" not in put_response.text
    assert "sk-secret-1234" not in get_response.text
    assert put_response.json() == {
        "success": True,
        "custom_provider_configs": {
            "local-llm": {
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "base_url": "https://example.test/v1",
                "api_key_configured": True,
                "api_key_masked": "...1234",
            }
        },
    }
    assert get_response.json()["custom_provider_configs"] == put_response.json()["custom_provider_configs"]
    assert json.loads(stored[CUSTOM_PROVIDER_CONFIGS_KEY])["local-llm"]["api_key"] == "sk-secret-1234"


def test_custom_provider_discovery_allows_creator_without_secret_or_base_url(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=2, username="creator", role="creator")

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: serialize_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "sk-secret-1234",
                },
            }
        )
    }
    monkeypatch.setattr(settings_router.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(
        settings_router.settings_service,
        "get_runtime_default_models",
        lambda: {"opencode": {"provider": "local-llm", "model": "llama-3.3"}},
    )

    try:
        with TestClient(app) as client:
            discovery_response = client.get("/api/settings/custom-provider-configs/discovery")
            admin_response = client.get("/api/settings/custom-provider-configs")
    finally:
        app.dependency_overrides.clear()

    assert discovery_response.status_code == 200
    assert admin_response.status_code == 403
    assert discovery_response.json() == {
        "custom_providers": [
            {
                "provider": "local-llm",
                "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                "api_key_configured": True,
                "models": ["llama-3.3"],
            }
        ]
    }
    assert "sk-secret-1234" not in discovery_response.text
    assert "https://example.test" not in discovery_response.text


def test_generic_settings_routes_do_not_expose_or_write_custom_provider_secret(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: serialize_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "sk-secret-1234",
                },
            }
        ),
        "other": "safe",
    }
    set_calls = []

    def get_setting(key):
        value = stored.get(key)
        return _setting(key, value) if value is not None else None

    def get_all_settings():
        return [_setting(key, value) for key, value in stored.items()]

    def set_setting(key, value):
        set_calls.append((key, value))
        stored[key] = value
        return _setting(key, value)

    monkeypatch.setattr(settings_router.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.db, "get_all_settings", get_all_settings)
    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            all_response = client.get("/api/settings")
            one_response = client.get("/api/settings/custom_provider_configs")
            put_response = client.put("/api/settings/custom_provider_configs", json={"value": "{}"})
    finally:
        app.dependency_overrides.clear()

    assert all_response.status_code == 200
    assert one_response.status_code == 200
    assert put_response.status_code == 400
    assert "sk-secret-1234" not in all_response.text
    assert "sk-secret-1234" not in one_response.text
    assert json.loads(one_response.json()["value"]) == {
        "local-llm": {
            "protocol": OPENAI_COMPATIBLE_PROTOCOL,
            "base_url": "https://example.test/v1",
            "api_key_configured": True,
            "api_key_masked": "...1234",
        }
    }
    assert set_calls == []


def test_generic_delete_route_blocks_custom_provider_configs_without_exposing_secret(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: serialize_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": OPENAI_COMPATIBLE_PROTOCOL,
                    "base_url": "https://example.test/v1",
                    "api_key": "sk-secret-1234",
                },
            }
        )
    }
    delete_calls = []

    def delete_setting(key):
        delete_calls.append(key)
        return stored.pop(key, None) is not None

    monkeypatch.setattr(settings_router.db, "delete_setting", delete_setting)

    try:
        with TestClient(app) as client:
            response = client.delete("/api/settings/custom_provider_configs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "custom-provider-configs" in response.text
    assert "sk-secret-1234" not in response.text
    assert delete_calls == []
    assert CUSTOM_PROVIDER_CONFIGS_KEY in stored
