from __future__ import annotations

import json
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def cleanup_settings_service_caches():
    try:
        import services.settings_service as settings_module
    except ImportError:
        yield
        return

    settings_module.invalidate_runtime_model_caches()
    yield
    settings_module.invalidate_runtime_model_caches()


def test_synthesizes_defaults_when_setting_missing():
    from services.runtime_model_defaults import get_default_runtime_models

    defaults = get_default_runtime_models(None, legacy_platform_default_model="claude-sonnet-4-6")

    assert defaults == {
        "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
    }


def test_legacy_platform_default_populates_missing_claude_code():
    from services.runtime_model_defaults import get_default_runtime_models

    defaults = get_default_runtime_models(
        json.dumps({
            "gemini-cli": {"provider": "google", "model": "gemini-2.5-pro"},
            "opencode": {"provider": "openai", "model": "gpt-5"},
        }),
        legacy_platform_default_model="claude-opus-4-8",
    )

    assert defaults["claude-code"] == {"provider": "anthropic", "model": "claude-opus-4-8"}
    assert defaults["gemini-cli"] == {"provider": "google", "model": "gemini-2.5-pro"}
    assert defaults["opencode"] == {"provider": "openai", "model": "gpt-5"}


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"vim": {"provider": "anthropic", "model": "claude-sonnet-4-6"}}, "Unsupported runtime"),
        ({"claude-code": {"provider": "", "model": "claude-sonnet-4-6"}}, "Provider is required"),
        ({"claude-code": {"provider": "bad/provider", "model": "claude-sonnet-4-6"}}, "Provider cannot contain"),
        ({"claude-code": {"provider": None, "model": "claude-sonnet-4-6"}}, "Provider must be a string"),
        ({"claude-code": {"provider": 123, "model": "claude-sonnet-4-6"}}, "Provider must be a string"),
        ({"claude-code": {"provider": "anthropic", "model": ""}}, "Model is required"),
        ({"claude-code": {"provider": "anthropic", "model": None}}, "Model must be a string"),
        ({"claude-code": {"provider": "anthropic", "model": 123}}, "Model must be a string"),
        ({"claude-code": {"provider": "anthropic", "model": "bad model"}}, "Model cannot contain whitespace"),
    ],
)
def test_validate_runtime_default_models_rejects_malformed_entries(payload, error):
    from services.runtime_model_defaults import validate_runtime_default_models

    with pytest.raises(ValueError, match=error):
        validate_runtime_default_models(payload)


def test_validate_runtime_default_models_normalizes_supported_entries():
    from services.runtime_model_defaults import validate_runtime_default_models
    result = validate_runtime_default_models({
        "claude-code": {"provider": " anthropic ", "model": " claude-sonnet-4-6 "},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "openai", "model": "gpt-5"},
    })

    assert result["claude-code"] == {"provider": "anthropic", "model": "claude-sonnet-4-6"}
    assert result["opencode"] == {"provider": "openai", "model": "gpt-5"}


def test_stored_non_string_values_fall_back_to_legacy_or_builtin_defaults():
    from services.runtime_model_defaults import get_default_runtime_models

    defaults = get_default_runtime_models(
        json.dumps({
            "claude-code": {"provider": "anthropic", "model": None},
            "gemini-cli": {"provider": 123, "model": "gemini-3-flash"},
            "opencode": {"provider": "openai", "model": 123},
        }),
        legacy_platform_default_model="claude-opus-4-8",
    )

    assert defaults == {
        "claude-code": {"provider": "anthropic", "model": "claude-opus-4-8"},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
    }


def test_invalid_legacy_platform_default_model_falls_back_to_builtin_default():
    from services.runtime_model_defaults import get_default_runtime_models

    assert get_default_runtime_models(None, legacy_platform_default_model=" ")["claude-code"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }
    assert get_default_runtime_models(None, legacy_platform_default_model="bad model")["claude-code"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }


def test_legacy_platform_default_model_is_trimmed_when_valid():
    from services.runtime_model_defaults import get_default_runtime_models

    assert get_default_runtime_models(None, legacy_platform_default_model=" claude-opus-4-8 ")["claude-code"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
    }


def test_validate_runtime_default_models_requires_all_supported_runtimes_on_save():
    from services.runtime_model_defaults import validate_runtime_default_models

    with pytest.raises(ValueError, match="Missing runtime default"):
        validate_runtime_default_models({
            "opencode": {"provider": "openai", "model": "gpt-5"},
        })


def test_resolved_model_value_is_provider_slash_model():
    from services.runtime_model_defaults import resolve_provider_model

    assert resolve_provider_model({"provider": "openai", "model": "gpt-5"}) == "openai/gpt-5"


def test_normalize_runtime_alias_preserves_legacy_gemini_name():
    from services.runtime_model_defaults import normalize_runtime

    assert normalize_runtime("gemini") == "gemini-cli"
    assert normalize_runtime("opencode") == "opencode"


def test_settings_service_returns_synthesized_runtime_defaults(monkeypatch):
    import services.settings_service as settings_module

    stored = {}
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    svc = settings_module.SettingsService()

    assert svc.get_runtime_default_models()["opencode"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
    }


def test_settings_service_saves_runtime_defaults_and_updates_legacy_claude_default(monkeypatch):
    import services.settings_service as settings_module

    stored = {}
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value))

    svc = settings_module.SettingsService()
    saved = svc.set_runtime_default_models({
        "claude-code": {"provider": "anthropic", "model": "claude-opus-4-8"},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "openai", "model": "gpt-5"},
    })

    assert saved["claude-code"] == {"provider": "anthropic", "model": "claude-opus-4-8"}
    assert stored["platform_default_model"] == "claude-opus-4-8"
    assert "runtime_default_models" in stored


def test_settings_service_resolves_runtime_model_for_execution(monkeypatch):
    import services.settings_service as settings_module

    stored = {
        "runtime_default_models": json.dumps({
            "claude-code": {"provider": "anthropic", "model": "claude-opus-4-8"},
            "gemini-cli": {"provider": "google", "model": "gemini-2.5-pro"},
            "opencode": {"provider": "openai", "model": "gpt-5"},
        })
    }
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    svc = settings_module.SettingsService()

    assert svc.resolve_model_for_runtime("claude-code") == "claude-opus-4-8"
    assert svc.resolve_model_for_runtime("gemini-cli") == "gemini-2.5-pro"
    assert svc.resolve_model_for_runtime("gemini") == "gemini-2.5-pro"
    assert svc.resolve_model_for_runtime("opencode") == "openai/gpt-5"


def test_runtime_default_models_return_deep_copies(monkeypatch):
    import services.settings_service as settings_module

    stored = {}
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    svc = settings_module.SettingsService()
    first = svc.get_runtime_default_models()
    first["opencode"]["model"] = "mutated"

    assert svc.get_runtime_default_models()["opencode"]["model"] == "claude-sonnet-4-5"


def test_runtime_default_models_get_route_returns_models_and_resolved(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    monkeypatch.setattr(settings_router.settings_service, "get_runtime_default_models", lambda: {
        "claude-code": {"provider": "anthropic", "model": "claude-opus-4-8"},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "openai", "model": "gpt-5"},
    })

    try:
        with TestClient(app) as client:
            response = client.get("/api/settings/runtime-default-models")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "runtime_default_models": {
            "claude-code": {"provider": "anthropic", "model": "claude-opus-4-8"},
            "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
            "opencode": {"provider": "openai", "model": "gpt-5"},
        },
        "resolved": {
            "claude-code": "anthropic/claude-opus-4-8",
            "gemini-cli": "google/gemini-3-flash",
            "opencode": "openai/gpt-5",
        },
    }


def test_runtime_default_models_put_route_rejects_bad_payload(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    def reject_runtime_defaults(value):
        raise ValueError("Unsupported runtime: vim")

    monkeypatch.setattr(settings_router.settings_service, "set_runtime_default_models", reject_runtime_defaults)

    try:
        with TestClient(app) as client:
            response = client.put(
                "/api/settings/runtime-default-models",
                json={
                    "runtime_default_models": {
                        "vim": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                    }
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported runtime: vim"


@pytest.mark.asyncio
async def test_legacy_platform_default_update_invalidates_runtime_defaults_cache(monkeypatch):
    import routers.settings as settings_router
    import services.settings_service as settings_module
    from database import SystemSetting, SystemSettingUpdate
    from models import User

    stored = {"platform_default_model": "claude-sonnet-4-6"}
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    svc = settings_module.SettingsService()
    assert svc.get_runtime_default_models()["claude-code"]["model"] == "claude-sonnet-4-6"

    def set_setting(key, value):
        stored[key] = value
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    def get_setting(key):
        value = stored.get(key)
        if value is None:
            return None
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    await settings_router.update_setting(
        "platform_default_model",
        SystemSettingUpdate(value="claude-opus-4-8"),
        SimpleNamespace(client=None, url=SimpleNamespace(path="/api/settings/platform_default_model"), state=SimpleNamespace()),
        User(id=1, username="admin", role="admin"),
    )

    assert svc.get_runtime_default_models()["claude-code"]["model"] == "claude-opus-4-8"


def test_legacy_platform_default_put_route_persists_runtime_default_claude_entry(monkeypatch):
    import routers.settings as settings_router
    import services.settings_service as settings_module
    from database import SystemSetting
    from models import User

    stored = {
        "platform_default_model": "claude-sonnet-4-6",
        "runtime_default_models": json.dumps({
            "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "gemini-cli": {"provider": "google", "model": "gemini-2.5-pro"},
            "opencode": {"provider": "openai", "model": "gpt-5"},
        }),
    }
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    svc = settings_module.SettingsService()
    assert svc.get_runtime_default_models()["claude-code"]["model"] == "claude-sonnet-4-6"

    def set_setting(key, value):
        stored[key] = value
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    def get_setting(key):
        value = stored.get(key)
        if value is None:
            return None
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            response = client.put("/api/settings/platform_default_model", json={"value": "claude-opus-4-8"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    persisted = json.loads(stored["runtime_default_models"])
    assert persisted["claude-code"] == {"provider": "anthropic", "model": "claude-opus-4-8"}
    assert persisted["gemini-cli"] == {"provider": "google", "model": "gemini-2.5-pro"}
    assert persisted["opencode"] == {"provider": "openai", "model": "gpt-5"}


def test_invalid_legacy_platform_default_put_returns_400_without_persisting(monkeypatch):
    import routers.settings as settings_router
    import services.settings_service as settings_module
    from database import SystemSetting
    from models import User

    original_runtime_defaults = json.dumps({
        "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "gemini-cli": {"provider": "google", "model": "gemini-2.5-pro"},
        "opencode": {"provider": "openai", "model": "gpt-5"},
    })
    stored = {
        "platform_default_model": "claude-sonnet-4-6",
        "runtime_default_models": original_runtime_defaults,
    }
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    def set_setting(key, value):
        stored[key] = value
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    def get_setting(key):
        value = stored.get(key)
        if value is None:
            return None
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            response = client.put("/api/settings/platform_default_model", json={"value": "bad model"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert stored["platform_default_model"] == "claude-sonnet-4-6"
    assert stored["runtime_default_models"] == original_runtime_defaults


def test_whitespace_legacy_platform_default_put_returns_and_persists_normalized_value(monkeypatch):
    import routers.settings as settings_router
    import services.settings_service as settings_module
    from database import SystemSetting
    from models import User

    stored = {
        "platform_default_model": "claude-sonnet-4-6",
        "runtime_default_models": json.dumps({
            "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "gemini-cli": {"provider": "google", "model": "gemini-2.5-pro"},
            "opencode": {"provider": "openai", "model": "gpt-5"},
        }),
    }
    settings_module.invalidate_runtime_model_caches()
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))

    def set_setting(key, value):
        stored[key] = value
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    def get_setting(key):
        value = stored.get(key)
        if value is None:
            return None
        return SystemSetting(key=key, value=value, updated_at=datetime.now(UTC))

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.db, "set_setting", set_setting)
    monkeypatch.setattr(settings_router.db, "get_setting", get_setting)
    monkeypatch.setattr(settings_router.platform_audit_service, "log", AsyncNoop())

    try:
        with TestClient(app) as client:
            response = client.put("/api/settings/platform_default_model", json={"value": " claude-opus-4-8 "})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["value"] == "claude-opus-4-8"
    assert stored["platform_default_model"] == "claude-opus-4-8"
    persisted = json.loads(stored["runtime_default_models"])
    assert persisted["claude-code"] == {"provider": "anthropic", "model": "claude-opus-4-8"}


class AsyncNoop:
    async def __call__(self, *args, **kwargs):
        return None
