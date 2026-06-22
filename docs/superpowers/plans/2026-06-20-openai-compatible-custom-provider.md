# OpenAI-Compatible Custom Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add saved OpenAI-compatible custom provider configuration and connection testing to Trinity Settings.

**Architecture:** Keep runtime defaults as `{ provider, model }` and add a separate `custom_provider_configs` system setting for connection details. Backend owns validation, secret masking/preservation, and provider testing; frontend Settings renders custom provider fields and sends unsaved values for tests.

**Tech Stack:** FastAPI, Pydantic, SQLite-backed `system_settings`, httpx, pytest, Vue 3 Composition API, Pinia, Axios, Tailwind.

---

## File Structure

- Create: `src/backend/services/custom_provider_configs.py`
  - Validates, normalizes, serializes, masks, and merges custom provider configs.
- Modify: `src/backend/services/settings_service.py`
  - Adds service methods around `custom_provider_configs` storage.
- Modify: `src/backend/routers/settings.py`
  - Adds GET/PUT custom provider config endpoints, protects generic settings routes from secret leakage/bypass, and extends provider connection test request model.
- Modify: `src/backend/services/provider_connection_test_service.py`
  - Accepts optional custom provider config and implements OpenAI-compatible test sequence.
- Modify: `tests/unit/test_provider_connection_test.py`
  - Updates previous custom-provider unsupported test and adds custom provider connection behavior tests.
- Create: `tests/unit/test_custom_provider_configs.py`
  - Tests config validation, masking, preserving existing API keys, and endpoints.
- Modify: `src/frontend/src/stores/settings.js`
  - Adds `fetchCustomProviderConfigs()` and `updateCustomProviderConfigs()`.
- Modify: `src/frontend/src/utils/runtimeModelPresets.js`
  - Adds helpers/defaults for custom provider UI state and request payloads.
- Modify: `src/frontend/src/views/Settings.vue`
  - Renders custom provider base URL/API key/protocol fields, loads/saves configs, and includes custom provider config in connection tests.

## Constraints

- Do not return raw API keys from GET endpoints.
- Do not return raw API keys from generic Settings endpoints.
- Block generic `PUT /api/settings/custom_provider_configs`; only the dedicated endpoint may write this setting.
- Do not log or render raw API keys after save.
- Do not store custom provider credentials inside `runtime_default_models`.
- Custom provider config saves are merge updates: omitted existing providers are preserved until an explicit delete action exists.
- Connection test must not persist unsaved request values.
- Commits are listed as plan steps for normal execution discipline, but only commit if the user explicitly approves committing during execution.

---

### Task 1: Backend custom provider config module

**Files:**
- Create: `src/backend/services/custom_provider_configs.py`
- Test: `tests/unit/test_custom_provider_configs.py`

- [ ] **Step 1: Write failing validation and masking tests**

Add this new file:

```python
from __future__ import annotations

import pytest


def test_validate_custom_provider_configs_normalizes_and_preserves_existing_key():
    from services.custom_provider_configs import validate_custom_provider_configs

    result = validate_custom_provider_configs(
        {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1/",
                "api_key": "",
            }
        },
        existing={
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://old.example.com/v1",
                "api_key": "sk-existing",
            }
        },
    )

    assert result == {
        "local-llm": {
            "protocol": "openai-compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-existing",
        }
    }


def test_validate_custom_provider_configs_requires_api_key_on_first_save():
    from services.custom_provider_configs import validate_custom_provider_configs

    with pytest.raises(ValueError, match="API key is required"):
        validate_custom_provider_configs(
            {
                "local-llm": {
                    "protocol": "openai-compatible",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "",
                }
            }
        )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"bad/provider": {"protocol": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key": "sk"}}, "Provider cannot contain '/'"),
        ({"bad provider": {"protocol": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key": "sk"}}, "Provider cannot contain whitespace"),
        ({"local-llm": {"protocol": "anthropic-compatible", "base_url": "https://api.example.com/v1", "api_key": "sk"}}, "Unsupported custom provider protocol"),
        ({"local-llm": {"protocol": "openai-compatible", "base_url": "ftp://api.example.com/v1", "api_key": "sk"}}, "Base URL must start"),
        ({"local-llm": {"protocol": "openai-compatible", "base_url": "https://api example.com/v1", "api_key": "sk"}}, "Base URL cannot contain whitespace"),
    ],
)
def test_validate_custom_provider_configs_rejects_bad_values(payload, error):
    from services.custom_provider_configs import validate_custom_provider_configs

    with pytest.raises(ValueError, match=error):
        validate_custom_provider_configs(payload)


def test_mask_custom_provider_configs_removes_raw_key():
    from services.custom_provider_configs import mask_custom_provider_configs

    result = mask_custom_provider_configs(
        {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-1234567890",
            }
        }
    )

    assert result == {
        "local-llm": {
            "protocol": "openai-compatible",
            "base_url": "https://api.example.com/v1",
            "api_key_configured": True,
            "api_key_masked": "...7890",
        }
    }
    assert "sk-1234567890" not in str(result)


def test_parse_custom_provider_configs_ignores_malformed_json():
    from services.custom_provider_configs import parse_custom_provider_configs

    assert parse_custom_provider_configs("not-json") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'services.custom_provider_configs'`.

- [ ] **Step 3: Implement the module**

Create `src/backend/services/custom_provider_configs.py`:

```python
from __future__ import annotations

import json
import re
from typing import Any

CUSTOM_PROVIDER_CONFIGS_KEY = "custom_provider_configs"
OPENAI_COMPATIBLE_PROTOCOL = "openai-compatible"

_WHITESPACE_RE = re.compile(r"\s")


def _mask_api_key(key: str) -> str:
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"


def parse_custom_provider_configs(raw: str | None) -> dict[str, dict[str, str]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}

    parsed: dict[str, dict[str, str]] = {}
    for provider, config in value.items():
        if not isinstance(provider, str) or not isinstance(config, dict):
            continue
        protocol = str(config.get("protocol") or "").strip()
        base_url = str(config.get("base_url") or "").strip().rstrip("/")
        api_key = str(config.get("api_key") or "")
        if provider and protocol and base_url and api_key:
            parsed[provider] = {"protocol": protocol, "base_url": base_url, "api_key": api_key}
    return parsed


def validate_custom_provider_configs(
    value: dict[str, Any],
    existing: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("custom_provider_configs must be an object")

    existing = existing or {}
    normalized: dict[str, dict[str, str]] = {}

    for raw_provider, raw_config in value.items():
        provider = str(raw_provider or "").strip()
        if not provider:
            raise ValueError("Provider is required")
        if "/" in provider:
            raise ValueError("Provider cannot contain '/'")
        if _WHITESPACE_RE.search(provider):
            raise ValueError("Provider cannot contain whitespace")
        if not isinstance(raw_config, dict):
            raise ValueError(f"Custom provider config for {provider} must be an object")

        protocol = str(raw_config.get("protocol") or "").strip()
        if protocol != OPENAI_COMPATIBLE_PROTOCOL:
            raise ValueError("Unsupported custom provider protocol")

        base_url = str(raw_config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("Base URL is required")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("Base URL must start with http:// or https://")
        if _WHITESPACE_RE.search(base_url):
            raise ValueError("Base URL cannot contain whitespace")

        incoming_key = str(raw_config.get("api_key") or "")
        api_key = incoming_key or existing.get(provider, {}).get("api_key", "")
        if not api_key:
            raise ValueError("API key is required")

        normalized[provider] = {
            "protocol": protocol,
            "base_url": base_url,
            "api_key": api_key,
        }

    return normalized


def serialize_custom_provider_configs(value: dict[str, Any]) -> str:
    return json.dumps(validate_custom_provider_configs(value), sort_keys=True)


def mask_custom_provider_configs(value: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    masked: dict[str, dict[str, Any]] = {}
    for provider, config in value.items():
        api_key = str(config.get("api_key") or "")
        masked[provider] = {
            "protocol": str(config.get("protocol") or ""),
            "base_url": str(config.get("base_url") or ""),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key) if api_key else None,
        }
    return masked
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py -q
```

Expected: `5 passed`.

- [ ] **Step 5: Commit if authorized**

Run only if the user has approved committing:

```bash
git add src/backend/services/custom_provider_configs.py tests/unit/test_custom_provider_configs.py
git commit -m "feat: add custom provider config validation"
```

---

### Task 2: Settings service and API endpoints

**Files:**
- Modify: `src/backend/services/settings_service.py`
- Modify: `src/backend/routers/settings.py`
- Test: `tests/unit/test_custom_provider_configs.py`

- [ ] **Step 1: Add failing settings service and route tests**

Append to `tests/unit/test_custom_provider_configs.py`:

```python

def test_settings_service_saves_and_masks_custom_provider_configs(monkeypatch):
    import services.settings_service as settings_module

    stored = {}
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value))

    svc = settings_module.SettingsService()
    saved = svc.set_custom_provider_configs(
        {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1/",
                "api_key": "sk-secret-1234",
            }
        }
    )

    assert saved["local-llm"]["base_url"] == "https://api.example.com/v1"
    assert svc.get_custom_provider_configs()["local-llm"]["api_key"] == "sk-secret-1234"
    assert svc.get_masked_custom_provider_configs()["local-llm"] == {
        "protocol": "openai-compatible",
        "base_url": "https://api.example.com/v1",
        "api_key_configured": True,
        "api_key_masked": "...1234",
    }


def test_settings_service_preserves_existing_custom_provider_key(monkeypatch):
    import json
    import services.settings_service as settings_module
    from services.custom_provider_configs import CUSTOM_PROVIDER_CONFIGS_KEY

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: json.dumps(
            {
                "local-llm": {
                    "protocol": "openai-compatible",
                    "base_url": "https://old.example.com/v1",
                    "api_key": "sk-existing",
                }
            }
        )
    }
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value))

    svc = settings_module.SettingsService()
    saved = svc.set_custom_provider_configs(
        {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://new.example.com/v1",
                "api_key": "",
            }
        }
    )

    assert saved["local-llm"]["api_key"] == "sk-existing"
    assert saved["local-llm"]["base_url"] == "https://new.example.com/v1"


def test_settings_service_preserves_omitted_custom_provider_configs(monkeypatch):
    import json
    import services.settings_service as settings_module
    from services.custom_provider_configs import CUSTOM_PROVIDER_CONFIGS_KEY

    stored = {
        CUSTOM_PROVIDER_CONFIGS_KEY: json.dumps(
            {
                "local-llm": {
                    "protocol": "openai-compatible",
                    "base_url": "https://old.example.com/v1",
                    "api_key": "sk-existing",
                },
                "other-llm": {
                    "protocol": "openai-compatible",
                    "base_url": "https://other.example.com/v1",
                    "api_key": "sk-other",
                },
            }
        )
    }
    monkeypatch.setattr(settings_module.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_module.db, "set_setting", lambda key, value: stored.__setitem__(key, value))

    svc = settings_module.SettingsService()
    saved = svc.set_custom_provider_configs(
        {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://new.example.com/v1",
                "api_key": "sk-new",
            }
        }
    )

    assert saved["local-llm"]["api_key"] == "sk-new"
    assert saved["other-llm"]["api_key"] == "sk-other"


def test_custom_provider_configs_routes_mask_and_persist(monkeypatch):
    import routers.settings as settings_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from models import User

    saved = {}

    def fake_get_masked():
        return {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_configured": True,
                "api_key_masked": "...1234",
            }
        }

    def fake_set(value):
        saved.update(value)
        return value

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.settings_service, "get_masked_custom_provider_configs", fake_get_masked)
    monkeypatch.setattr(settings_router.settings_service, "set_custom_provider_configs", fake_set)
    monkeypatch.setattr(settings_router.settings_service, "get_custom_provider_configs", lambda: saved)

    try:
        with TestClient(app) as client:
            get_response = client.get("/api/settings/custom-provider-configs")
            put_response = client.put(
                "/api/settings/custom-provider-configs",
                json={
                    "custom_provider_configs": {
                        "local-llm": {
                            "protocol": "openai-compatible",
                            "base_url": "https://api.example.com/v1",
                            "api_key": "sk-secret-1234",
                        }
                    }
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert get_response.status_code == 200
    assert get_response.json() == {
        "custom_provider_configs": {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_configured": True,
                "api_key_masked": "...1234",
            }
        }
    }
    assert "sk-secret-1234" not in str(get_response.json())
    assert put_response.status_code == 200
    assert put_response.json()["success"] is True
    assert put_response.json() == {
        "success": True,
        "custom_provider_configs": {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_configured": True,
                "api_key_masked": "...1234",
            }
        },
    }
    assert "sk-secret-1234" not in str(put_response.json())


def test_generic_settings_routes_do_not_expose_or_write_custom_provider_secret(monkeypatch):
    import json
    import routers.settings as settings_router
    from database import SystemSetting
    from datetime import UTC, datetime
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from models import User

    raw_secret_json = json.dumps({
        "local-llm": {
            "protocol": "openai-compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-secret-1234",
        }
    })
    raw_setting = SystemSetting(key="custom_provider_configs", value=raw_secret_json, updated_at=datetime.now(UTC))
    normal_setting = SystemSetting(key="public_chat_url", value="https://trinity.example.com", updated_at=datetime.now(UTC))

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.db, "get_all_settings", lambda: [normal_setting, raw_setting])
    monkeypatch.setattr(settings_router.db, "get_setting", lambda key: raw_setting if key == "custom_provider_configs" else None)
    monkeypatch.setattr(settings_router.db, "set_setting", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("generic write must be blocked")))
    monkeypatch.setattr(
        settings_router.settings_service,
        "get_masked_custom_provider_configs",
        lambda: {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_configured": True,
                "api_key_masked": "...1234",
            }
        },
    )

    try:
        with TestClient(app) as client:
            all_response = client.get("/api/settings")
            get_response = client.get("/api/settings/custom_provider_configs")
            put_response = client.put("/api/settings/custom_provider_configs", json={"value": raw_secret_json})
    finally:
        app.dependency_overrides.clear()

    assert all_response.status_code == 200
    assert get_response.status_code == 200
    assert put_response.status_code == 400
    assert "sk-secret-1234" not in str(all_response.json())
    assert "sk-secret-1234" not in str(get_response.json())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py -q
```

Expected: FAIL because `SettingsService` and routes do not yet expose custom provider config methods/endpoints.

- [ ] **Step 3: Add settings service methods**

In `src/backend/services/settings_service.py`, add imports near the existing runtime defaults imports:

```python
from services.custom_provider_configs import (
    CUSTOM_PROVIDER_CONFIGS_KEY,
    mask_custom_provider_configs,
    parse_custom_provider_configs,
    serialize_custom_provider_configs,
    validate_custom_provider_configs,
)
```

Inside `class SettingsService`, after `set_runtime_default_models()`, add:

```python
    def get_custom_provider_configs(self) -> dict:
        raw = self.get_setting(CUSTOM_PROVIDER_CONFIGS_KEY)
        return copy.deepcopy(parse_custom_provider_configs(raw))

    def get_masked_custom_provider_configs(self) -> dict:
        return mask_custom_provider_configs(self.get_custom_provider_configs())

    def set_custom_provider_configs(self, value: dict) -> dict:
        existing = self.get_custom_provider_configs()
        incoming = validate_custom_provider_configs(value, existing=existing)
        normalized = {**existing, **incoming}
        db.set_setting(CUSTOM_PROVIDER_CONFIGS_KEY, serialize_custom_provider_configs(normalized))
        return copy.deepcopy(normalized)
```

- [ ] **Step 4: Add Pydantic models and endpoints**

In `src/backend/routers/settings.py`, add models near `RuntimeDefaultModelsUpdate`:

```python
class CustomProviderConfig(BaseModel):
    protocol: str
    base_url: str
    api_key: Optional[str] = ""


class CustomProviderConfigsUpdate(BaseModel):
    custom_provider_configs: Dict[str, CustomProviderConfig]
```

Add endpoints before `/provider-connection-test`:

```python
@router.get("/custom-provider-configs")
async def get_custom_provider_configs(current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    return {"custom_provider_configs": settings_service.get_masked_custom_provider_configs()}


@router.put("/custom-provider-configs")
async def update_custom_provider_configs(
    body: CustomProviderConfigsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    payload = {
        provider: (config.model_dump() if hasattr(config, "model_dump") else config.dict())
        for provider, config in body.custom_provider_configs.items()
    }
    try:
        normalized = settings_service.set_custom_provider_configs(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"setting": "custom_provider_configs", "action": "update"},
    )
    return {
        "success": True,
        "custom_provider_configs": settings_service.get_masked_custom_provider_configs(),
    }
```

Then protect generic settings routes in the same file. Add a helper near `mask_api_key()`:

```python
def _masked_custom_provider_setting() -> SystemSetting:
    return SystemSetting(
        key="custom_provider_configs",
        value=json.dumps(settings_service.get_masked_custom_provider_configs(), sort_keys=True),
        updated_at=datetime.now(UTC),
    )
```

Add `json` import at the top if it is not already present:

```python
import json
```

In `get_all_settings()`, replace:

```python
        settings = db.get_all_settings()

        return settings
```

with:

```python
        settings = db.get_all_settings()
        return [
            _masked_custom_provider_setting() if setting.key == "custom_provider_configs" else setting
            for setting in settings
        ]
```

In `get_setting()`, after `require_admin(current_user)`, add:

```python
    if key == "custom_provider_configs":
        return _masked_custom_provider_setting()
```

In `update_setting()`, after `require_admin(current_user)`, add:

```python
    if key == "custom_provider_configs":
        raise HTTPException(
            status_code=400,
            detail="Use /api/settings/custom-provider-configs to update custom provider configs.",
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py -q
```

Expected: all tests in this file pass.

- [ ] **Step 6: Commit if authorized**

Run only if the user has approved committing:

```bash
git add src/backend/services/settings_service.py src/backend/routers/settings.py tests/unit/test_custom_provider_configs.py
git commit -m "feat: expose custom provider settings"
```

---

### Task 3: OpenAI-compatible provider connection tests

**Files:**
- Modify: `src/backend/services/provider_connection_test_service.py`
- Modify: `src/backend/routers/settings.py`
- Modify: `tests/unit/test_provider_connection_test.py`

- [ ] **Step 1: Replace unsupported custom provider test with missing-config test**

In `tests/unit/test_provider_connection_test.py`, replace `test_custom_provider_returns_unsupported_without_network` with:

```python
@pytest.mark.asyncio
async def test_custom_provider_without_config_returns_missing_config_without_network(monkeypatch):
    import services.provider_connection_test_service as service_module

    def fail_client(*args, **kwargs):
        raise AssertionError("network should not be called when custom config is missing")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})

    result = await service_module.ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result == {
        "ok": False,
        "status": "missing_config",
        "runtime": "opencode",
        "provider": "local-llm",
        "model": "my-model",
        "resolved_model": "local-llm/my-model",
        "message": "Custom provider base URL, API key, or protocol is missing.",
    }
```

- [ ] **Step 2: Add failing OpenAI-compatible custom provider service tests**

Append to `tests/unit/test_provider_connection_test.py`:

```python

@pytest.mark.asyncio
async def test_custom_provider_uses_request_config_and_model_endpoint(monkeypatch):
    import services.provider_connection_test_service as service_module

    captured = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["timeout"] = kwargs["timeout"]
            return httpx.Response(200, json={"id": "my-model"})

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {
            "protocol": "openai-compatible",
            "base_url": "https://api.example.com/v1/",
            "api_key": "sk-secret",
        },
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert captured == {
        "url": "https://api.example.com/v1/models/my-model",
        "headers": {"Authorization": "Bearer sk-secret"},
        "timeout": 10.0,
    }


@pytest.mark.asyncio
async def test_custom_provider_falls_back_to_saved_config_and_models_list(monkeypatch):
    import services.provider_connection_test_service as service_module

    calls = []

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            calls.append(url)
            if url.endswith("/models/my-model"):
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json={"data": [{"id": "my-model"}]})

    monkeypatch.setattr(
        service_module.settings_service,
        "get_custom_provider_configs",
        lambda: {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-saved",
            }
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert calls == ["https://api.example.com/v1/models/my-model", "https://api.example.com/v1/models"]


@pytest.mark.asyncio
async def test_custom_provider_chat_completion_fallback(monkeypatch):
    import services.provider_connection_test_service as service_module

    calls = []

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, None))
            if url.endswith("/models/my-model"):
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json={"data": [{"id": "other-model"}]})

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs["json"]))
            return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key": "sk-secret"},
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert calls[-1] == (
        "POST",
        "https://api.example.com/v1/chat/completions",
        {"model": "my-model", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
    )


@pytest.mark.asyncio
async def test_custom_provider_authentication_failure_redacts_secret(monkeypatch):
    import services.provider_connection_test_service as service_module

    secret = "sk-secret-value"

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(401, text=f"bad key {secret}")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key": secret},
    )

    assert result["ok"] is False
    assert result["status"] == "authentication_failed"
    assert secret not in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_saved_key_error_is_redacted(monkeypatch):
    import services.provider_connection_test_service as service_module

    secret = "sk-saved-secret-value"

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(500, text=f"upstream included {secret}")

    monkeypatch.setattr(
        service_module.settings_service,
        "get_custom_provider_configs",
        lambda: {
            "local-llm": {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": secret,
            }
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result["ok"] is False
    assert result["status"] == "provider_unreachable"
    assert secret not in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_unknown_exception_redacts_secret(monkeypatch):
    import services.provider_connection_test_service as service_module

    secret = "sk-exception-secret"

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            raise RuntimeError(f"boom {secret}")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "openai-compatible", "base_url": "https://api.example.com/v1", "api_key": secret},
    )

    assert result["ok"] is False
    assert result["status"] == "unknown_error"
    assert secret not in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_unsupported_protocol_returns_unsupported_provider(monkeypatch):
    import services.provider_connection_test_service as service_module

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "anthropic-compatible", "base_url": "https://api.example.com/v1", "api_key": "sk-secret"},
    )

    assert result["ok"] is False
    assert result["status"] == "unsupported_provider"
```

- [ ] **Step 3: Update FastAPI delegation test for custom_provider argument**

In `test_fastapi_provider_connection_test_delegates_and_does_not_persist`, replace `fake_test_connection` and assertions:

```python
    async def fake_test_connection(runtime, provider, model, custom_provider=None):
        calls.append((runtime, provider, model, custom_provider))
        return {
            "ok": True,
            "status": "connected",
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "resolved_model": f"{provider}/{model}",
            "message": "Connection verified.",
        }
```

Use this POST body:

```python
                json={
                    "runtime": "opencode",
                    "provider": "local-llm",
                    "model": "my-model",
                    "custom_provider": {
                        "protocol": "openai-compatible",
                        "base_url": "https://api.example.com/v1",
                        "api_key": "sk-secret",
                    },
                },
```

Replace final call assertion:

```python
    assert calls == [
        (
            "opencode",
            "local-llm",
            "my-model",
            {
                "protocol": "openai-compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-secret",
            },
        )
    ]
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_provider_connection_test.py -q
```

Expected: FAIL because `test_connection()` does not accept a custom provider config yet.

- [ ] **Step 5: Extend router request model and delegation**

In `src/backend/routers/settings.py`, change `ProviderConnectionTestRequest` to:

```python
class ProviderConnectionTestRequest(BaseModel):
    runtime: str
    provider: str
    model: str
    custom_provider: Optional[CustomProviderConfig] = None
```

Change the endpoint body to:

```python
        custom_provider = body.custom_provider.model_dump() if body.custom_provider else None
        return await provider_connection_test_service.test_connection(
            body.runtime,
            body.provider,
            body.model,
            custom_provider,
        )
```

- [ ] **Step 6: Implement custom provider connection logic**

In `src/backend/services/provider_connection_test_service.py`, add import:

```python
from services.custom_provider_configs import OPENAI_COMPATIBLE_PROTOCOL, validate_custom_provider_configs
```

Change the method signature:

```python
    async def test_connection(
        self,
        runtime: str,
        provider: str,
        model: str,
        custom_provider: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
```

Replace the current custom provider branch with:

```python
        if provider not in BUILT_IN_PROVIDERS:
            return await self._test_custom_provider(runtime, provider, model, resolved_model, custom_provider)
```

Add these methods to `ProviderConnectionTestService` before `_get_key()`:

```python
    async def _test_custom_provider(
        self,
        runtime: str,
        provider: str,
        model: str,
        resolved_model: str,
        custom_provider: dict[str, Any] | None,
    ) -> dict[str, Any]:
        saved_configs = settings_service.get_custom_provider_configs()
        saved = saved_configs.get(provider, {})
        merged = {
            "protocol": (custom_provider or {}).get("protocol") or saved.get("protocol", ""),
            "base_url": (custom_provider or {}).get("base_url") or saved.get("base_url", ""),
            "api_key": (custom_provider or {}).get("api_key") or saved.get("api_key", ""),
        }

        if not merged["protocol"] or not merged["base_url"] or not merged["api_key"]:
            return self._result(False, "missing_config", runtime, provider, model, resolved_model, "Custom provider base URL, API key, or protocol is missing.")
        if merged["protocol"] != OPENAI_COMPATIBLE_PROTOCOL:
            return self._result(False, "unsupported_provider", runtime, provider, model, resolved_model, "Only OpenAI-compatible custom providers are supported.")

        try:
            normalized = validate_custom_provider_configs({provider: merged}, existing=saved_configs)[provider]
        except ValueError as e:
            return self._result(False, "missing_config", runtime, provider, model, resolved_model, str(e))

        key = normalized["api_key"]
        try:
            status, message = await self._request_openai_compatible_custom_provider(normalized["base_url"], model, key)
            return self._result(status == "connected", status, runtime, provider, model, resolved_model, message)
        except httpx.TimeoutException as e:
            return self._result(False, "timed_out", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Request timed out.", [key]))
        except httpx.ConnectError as e:
            return self._result(False, "provider_unreachable", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Provider unreachable.", [key]))
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, resolved_model, _sanitize_message(str(e), [key]))

    async def _request_openai_compatible_custom_provider(self, base_url: str, model: str, key: str) -> tuple[str, str]:
        headers = {"Authorization": f"Bearer {key}"}
        encoded_model = quote(model, safe="")
        async with httpx.AsyncClient() as client:
            model_response = await client.get(
                f"{base_url}/models/{encoded_model}",
                headers=headers,
                timeout=10.0,
            )
            if model_response.status_code == 200:
                return "connected", "Connection verified."
            if model_response.status_code in (401, 403):
                return self._map_response(model_response, [key])
            if model_response.status_code not in (404,):
                status, message = self._map_response(model_response, [key])
                if status != "unknown_error":
                    return status, message

            list_response = await client.get(f"{base_url}/models", headers=headers, timeout=10.0)
            if list_response.status_code == 200 and self._model_list_contains(list_response, model):
                return "connected", "Connection verified."
            if list_response.status_code in (401, 403):
                return self._map_response(list_response, [key])

            chat_response = await client.post(
                f"{base_url}/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=10.0,
            )
            if chat_response.status_code == 200:
                return "connected", "Connection verified."
            return self._map_response(chat_response, [key])

    def _model_list_contains(self, response: httpx.Response, model: str) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return False
        return any(isinstance(item, dict) and item.get("id") == model for item in data)
```

- [ ] **Step 7: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_provider_connection_test.py tests/unit/test_custom_provider_configs.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit if authorized**

Run only if the user has approved committing:

```bash
git add src/backend/services/provider_connection_test_service.py src/backend/routers/settings.py tests/unit/test_provider_connection_test.py tests/unit/test_custom_provider_configs.py
git commit -m "feat: test openai-compatible custom providers"
```

---

### Task 4: Frontend store and utilities

**Files:**
- Modify: `src/frontend/src/stores/settings.js`
- Modify: `src/frontend/src/utils/runtimeModelPresets.js`

- [ ] **Step 1: Add settings store actions**

In `src/frontend/src/stores/settings.js`, defensively drop `custom_provider_configs` from the generic settings fetch. In `fetchSettings()`, change the loop to:

```javascript
        for (const setting of response.data) {
          if (setting.key === 'custom_provider_configs') continue
          settingsObj[setting.key] = setting.value
        }
```

This is defense-in-depth. Backend generic routes must already avoid returning raw keys, but the broad frontend settings cache should not retain this sensitive setting.

Then add the dedicated custom provider actions.

In `src/frontend/src/stores/settings.js`, add after `updateRuntimeDefaultModels()`:

```javascript
    async fetchCustomProviderConfigs() {
      try {
        const response = await axios.get('/api/settings/custom-provider-configs')
        this.settings.custom_provider_configs = response.data.custom_provider_configs || {}
        return response.data
      } catch (error) {
        console.error('Failed to fetch custom provider configs:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch custom provider configs'
        throw error
      }
    },

    async updateCustomProviderConfigs(customProviderConfigs) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put('/api/settings/custom-provider-configs', {
          custom_provider_configs: customProviderConfigs,
        })
        this.settings.custom_provider_configs = response.data.custom_provider_configs || {}
        return response.data
      } catch (error) {
        console.error('Failed to update custom provider configs:', error)
        this.error = error.response?.data?.detail || 'Failed to update custom provider configs'
        throw error
      } finally {
        this.saving = false
      }
    },
```

- [ ] **Step 2: Add frontend utility helpers**

In `src/frontend/src/utils/runtimeModelPresets.js`, add after `DEFAULT_RUNTIME_MODELS`:

```javascript
export const OPENAI_COMPATIBLE_PROTOCOL = 'openai-compatible'

export function blankCustomProviderConfig() {
  return {
    protocol: OPENAI_COMPATIBLE_PROTOCOL,
    base_url: '',
    api_key: '',
    api_key_configured: false,
    api_key_masked: null,
  }
}
```

Add after `payloadRuntimeDefaults()`:

```javascript
export function cloneCustomProviderConfigs(value) {
  const source = value && typeof value === 'object' ? value : {}
  const clone = {}

  for (const [provider, config] of Object.entries(source)) {
    clone[provider] = {
      protocol: String(config?.protocol || OPENAI_COMPATIBLE_PROTOCOL).trim(),
      base_url: String(config?.base_url || '').trim(),
      api_key: '',
      api_key_configured: Boolean(config?.api_key_configured),
      api_key_masked: config?.api_key_masked || null,
    }
  }

  return clone
}

export function payloadCustomProviderConfigs(runtimeDefaults, customProviderConfigs) {
  const payload = {}
  for (const entry of Object.values(runtimeDefaults || {})) {
    if (entry?.providerMode !== CUSTOM_PROVIDER_VALUE) continue
    const provider = String(entry?.provider || '').trim()
    if (!provider) continue
    const config = customProviderConfigs?.[provider] || blankCustomProviderConfig()
    payload[provider] = {
      protocol: String(config.protocol || OPENAI_COMPATIBLE_PROTOCOL).trim(),
      base_url: String(config.base_url || '').trim(),
      api_key: String(config.api_key || ''),
    }
  }
  return payload
}
```

- [ ] **Step 3: Run frontend build to catch syntax errors**

Run:

```bash
cd src/frontend && npm run build
```

Expected: build succeeds, existing chunk-size warnings are acceptable.

- [ ] **Step 4: Commit if authorized**

Run only if the user has approved committing:

```bash
git add src/frontend/src/stores/settings.js src/frontend/src/utils/runtimeModelPresets.js
git commit -m "feat: add custom provider settings store helpers"
```

---

### Task 5: Frontend Settings UI

**Files:**
- Modify: `src/frontend/src/views/Settings.vue`

- [ ] **Step 1: Import custom provider helpers**

In `src/frontend/src/views/Settings.vue`, extend the import from `runtimeModelPresets`:

```javascript
  blankCustomProviderConfig,
  cloneCustomProviderConfigs,
  payloadCustomProviderConfigs,
```

- [ ] **Step 2: Add custom provider state**

After `runtimeDefaultModels` state, add:

```javascript
const customProviderConfigs = ref({})
```

- [ ] **Step 3: Load custom provider configs with runtime defaults**

Change the parallel load list in `loadSettings()` from:

```javascript
      loadRuntimeDefaultModels(),
```

to:

```javascript
      loadRuntimeModelSettings(),
```

Replace `loadRuntimeDefaultModels()` with:

```javascript
async function loadRuntimeModelSettings() {
  try {
    const [runtimeResponse, customResponse] = await Promise.all([
      settingsStore.fetchRuntimeDefaultModels(),
      settingsStore.fetchCustomProviderConfigs(),
    ])
    runtimeDefaultModels.value = cloneRuntimeDefaults(runtimeResponse.runtime_default_models)
    customProviderConfigs.value = cloneCustomProviderConfigs(customResponse.custom_provider_configs)
    ensureCustomProviderConfigsForRows()
    runtimeConnectionStatuses.value = Object.fromEntries(
      RUNTIME_MODEL_ROWS.map(row => [row.runtime, { status: 'not_tested', message: '' }])
    )
  } catch {
    // non-critical; UI shows code defaults
  }
}

async function loadRuntimeDefaultModels() {
  return loadRuntimeModelSettings()
}
```

Add helper functions below it:

```javascript
function ensureCustomProviderConfigsForRows() {
  for (const entry of Object.values(runtimeDefaultModels.value)) {
    if (entry.providerMode !== CUSTOM_PROVIDER_VALUE) continue
    const provider = String(entry.provider || '').trim()
    if (!provider) continue
    if (!customProviderConfigs.value[provider]) {
      customProviderConfigs.value[provider] = blankCustomProviderConfig()
    }
  }
}

function customProviderConfigForRuntime(runtime) {
  const provider = String(runtimeDefaultModels.value[runtime]?.provider || '').trim()
  if (!provider) return blankCustomProviderConfig()
  if (!customProviderConfigs.value[provider]) {
    customProviderConfigs.value[provider] = blankCustomProviderConfig()
  }
  return customProviderConfigs.value[provider]
}

function resetCustomRuntimeConnectionStatus(runtime) {
  ensureCustomProviderConfigsForRows()
  resetRuntimeConnectionStatus(runtime)
}
```

- [ ] **Step 4: Save custom provider configs with runtime defaults**

In `saveRuntimeDefaultModels()`, after updating runtime defaults, add custom provider save before success:

```javascript
    const customPayload = payloadCustomProviderConfigs(runtimeDefaultModels.value, customProviderConfigs.value)
    if (Object.keys(customPayload).length) {
      const customResponse = await settingsStore.updateCustomProviderConfigs(customPayload)
      customProviderConfigs.value = cloneCustomProviderConfigs(customResponse.custom_provider_configs)
      ensureCustomProviderConfigsForRows()
    }
```

The function body should keep saving status/error handling and set `runtimeDefaultModelsSaveSuccess` only after both saves finish.

- [ ] **Step 5: Include custom provider config in connection test payload and stale response guard**

In `testRuntimeProviderConnection(runtime)`, after `requestedModel`, add:

```javascript
  const requestedCustomConfig = entry?.providerMode === CUSTOM_PROVIDER_VALUE
    ? { ...customProviderConfigForRuntime(runtime) }
    : null
```

Replace `requestStillCurrent` with:

```javascript
  const requestStillCurrent = () => {
    const currentEntry = runtimeDefaultModels.value[runtime]
    if (String(currentEntry?.provider || '').trim() !== requestedProvider) return false
    if (String(currentEntry?.model || '').trim() !== requestedModel) return false
    if (!requestedCustomConfig) return true
    const currentConfig = customProviderConfigForRuntime(runtime)
    return String(currentConfig.base_url || '').trim() === String(requestedCustomConfig.base_url || '').trim() &&
      String(currentConfig.api_key || '') === String(requestedCustomConfig.api_key || '') &&
      String(currentConfig.protocol || '').trim() === String(requestedCustomConfig.protocol || '').trim()
  }
```

Replace request payload construction with:

```javascript
    const payload = {
      runtime,
      provider: requestedProvider,
      model: requestedModel,
    }
    if (requestedCustomConfig) {
      payload.custom_provider = {
        protocol: requestedCustomConfig.protocol,
        base_url: requestedCustomConfig.base_url,
        api_key: requestedCustomConfig.api_key,
      }
    }
    const response = await settingsStore.testProviderConnection(payload)
```

- [ ] **Step 6: Render custom provider fields**

In the template, after the custom provider name `<input>` at lines 166-174, add:

```vue
                            <div
                              v-if="runtimeDefaultModels[row.runtime].providerMode === CUSTOM_PROVIDER_VALUE"
                              class="mt-3 space-y-2 rounded-md border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3 sm:col-span-2"
                            >
                              <div class="grid gap-3 sm:grid-cols-2">
                                <div>
                                  <label class="block text-xs font-medium text-gray-600 dark:text-gray-400">Protocol</label>
                                  <select
                                    v-model="customProviderConfigForRuntime(row.runtime).protocol"
                                    :disabled="savingRuntimeDefaultModels"
                                    @change="resetCustomRuntimeConnectionStatus(row.runtime)"
                                    class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                                  >
                                    <option value="openai-compatible">OpenAI-compatible</option>
                                  </select>
                                </div>
                                <div>
                                  <label class="block text-xs font-medium text-gray-600 dark:text-gray-400">Base URL</label>
                                  <input
                                    v-model="customProviderConfigForRuntime(row.runtime).base_url"
                                    :disabled="savingRuntimeDefaultModels"
                                    @input="resetCustomRuntimeConnectionStatus(row.runtime)"
                                    type="url"
                                    placeholder="https://api.example.com/v1"
                                    class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                                  />
                                </div>
                              </div>
                              <div>
                                <label class="block text-xs font-medium text-gray-600 dark:text-gray-400">API Key</label>
                                <input
                                  v-model="customProviderConfigForRuntime(row.runtime).api_key"
                                  :disabled="savingRuntimeDefaultModels"
                                  @input="resetCustomRuntimeConnectionStatus(row.runtime)"
                                  type="password"
                                  :placeholder="customProviderConfigForRuntime(row.runtime).api_key_configured ? customProviderConfigForRuntime(row.runtime).api_key_masked : 'sk-...'"
                                  class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                                />
                                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                                  Leave blank to keep the saved key. The key is never shown after saving.
                                </p>
                              </div>
                            </div>
```

If the inserted block is nested inside the provider column rather than the two-column grid, adjust classes so it remains readable and does not break the existing layout.

- [ ] **Step 7: Update disabled state for Test connection**

Replace the Test connection disabled condition with:

```vue
:disabled="runtimeConnectionStatuses[row.runtime]?.status === 'testing' || !runtimeDefaultModels[row.runtime].provider || !runtimeDefaultModels[row.runtime].model || (runtimeDefaultModels[row.runtime].providerMode === CUSTOM_PROVIDER_VALUE && !customProviderConfigForRuntime(row.runtime).base_url && !customProviderConfigForRuntime(row.runtime).api_key_configured)"
```

- [ ] **Step 8: Run frontend build**

Run:

```bash
cd src/frontend && npm run build
```

Expected: build succeeds, existing chunk-size warnings are acceptable.

- [ ] **Step 9: Commit if authorized**

Run only if the user has approved committing:

```bash
git add src/frontend/src/views/Settings.vue
git commit -m "feat: add custom provider settings UI"
```

---

### Task 6: Regression verification and review

**Files:**
- No implementation files unless fixing verification failures.

- [ ] **Step 1: Run backend regression tests**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py tests/unit/test_provider_connection_test.py tests/unit/test_runtime_model_defaults.py tests/unit/test_opencode_backend_runtime_propagation.py tests/unit/test_opencode_backend_models.py tests/unit/test_opencode_runtime.py tests/unit/test_opencode_mcp_config.py tests/unit/test_opencode_terminal.py tests/unit/test_926_version_endpoint.py tests/unit/test_gemini_runtime_pipe_drop.py tests/unit/test_agent_server_hardening.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run frontend production build**

Run:

```bash
cd src/frontend && npm run build
```

Expected: build succeeds, existing chunk-size warnings are acceptable.

- [ ] **Step 3: Request separate code review**

Dispatch a new `@oracle` review agent with this prompt:

```text
Review the OpenAI-compatible custom provider implementation against:
- docs/superpowers/specs/2026-06-20-openai-compatible-custom-provider-design.md
- docs/superpowers/plans/2026-06-20-openai-compatible-custom-provider.md

Focus on security, secret leakage, API compatibility, validation gaps, test coverage, and regression risk. Do not modify files. Return APPROVED or REQUIRED CHANGES with exact file/line findings.
```

Expected: review returns `APPROVED` or actionable required changes.

- [ ] **Step 4: Fix review findings if any**

If review returns required changes, implement only those bounded changes, then rerun:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_custom_provider_configs.py tests/unit/test_provider_connection_test.py -q
cd src/frontend && npm run build
```

Expected: tests/build pass and review findings are addressed.

- [ ] **Step 5: Commit verification fixes if authorized**

Run only if changes were made and the user has approved committing:

```bash
git add src/backend/services/custom_provider_configs.py src/backend/services/settings_service.py src/backend/routers/settings.py src/backend/services/provider_connection_test_service.py tests/unit/test_custom_provider_configs.py tests/unit/test_provider_connection_test.py src/frontend/src/stores/settings.js src/frontend/src/utils/runtimeModelPresets.js src/frontend/src/views/Settings.vue
git commit -m "fix: harden custom provider settings"
```

---

## Deployment Plan

Deploy only after local verification and user approval.

- [ ] **Step 1: Apply local commits or patch to remote**

Remote Trinity path:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && git status --short'
```

Expected: existing known remote modifications may include Dockerfile proxy changes and `docker-compose.override.yml`; do not overwrite them.

- [ ] **Step 2: Build updated services with remote proxy**

Run on remote through SSH:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && HTTP_PROXY=http://172.17.0.1:7890 HTTPS_PROXY=http://172.17.0.1:7890 NO_PROXY=localhost,127.0.0.1 docker compose build --build-arg HTTP_PROXY=http://172.17.0.1:7890 --build-arg HTTPS_PROXY=http://172.17.0.1:7890 --build-arg http_proxy=http://172.17.0.1:7890 --build-arg https_proxy=http://172.17.0.1:7890 backend scheduler frontend mcp-server'
```

Expected: build succeeds.

- [ ] **Step 3: Restart updated services**

Run:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && docker compose up -d --force-recreate backend scheduler frontend mcp-server'
```

Expected: services start.

- [ ] **Step 4: Verify remote services**

Run:

```bash
ssh ubuntu-server 'curl -fsS http://127.0.0.1/ >/dev/null && echo FRONTEND_OK && curl -fsS http://127.0.0.1:8000/docs >/dev/null && echo BACKEND_DOCS_OK && curl -fsS http://127.0.0.1:8080/health'
```

Expected:

```text
FRONTEND_OK
BACKEND_DOCS_OK
✓ Ok
```

---

## Plan Review Notes

- Spec coverage: data model, endpoints, connection-test resolution order, OpenAI-compatible test sequence, validation, frontend behavior, secret masking, and rollout are covered by Tasks 1-6.
- Placeholder scan: no task relies on undefined “TODO” or “similar to” steps; each implementation step includes concrete code or commands.
- Type consistency: backend uses `custom_provider_configs`, `protocol`, `base_url`, `api_key`; frontend uses matching snake_case fields for API payloads and masked read state.
