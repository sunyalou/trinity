# Provider-Aware Runtime Default Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add provider-aware default model settings per runtime, manual provider/model connection tests, and runtime-aware execution fallback.

**Architecture:** Backend owns validation, default synthesis, persistence compatibility, and provider connectivity testing. Frontend owns Settings-page editing state, provider/model presets, resolved previews, manual test actions, and saving values without blocking on connectivity results. Execution keeps agent-specific runtime model overrides first, then falls back to the new runtime default map.

**Tech Stack:** FastAPI, Pydantic, pytest, httpx, SQLite-backed `system_settings`, Vue 3 Composition API, Pinia, axios, Tailwind CSS.

---

## File Structure

- Create `src/backend/services/runtime_model_defaults.py`
  - Constants for supported runtimes, built-in providers, defaults, setting key, validation, synthesis, serialization, runtime alias normalization, and runtime-compatible model resolution.
- Modify `src/backend/services/settings_service.py`
  - Add SettingsService methods for `runtime_default_models`, cache invalidation, OpenAI key lookup, and compatibility updates to `platform_default_model`.
- Create `src/backend/services/provider_connection_test_service.py`
  - Provider-specific test requests for Anthropic, OpenAI, and Google; normalized status responses; URL-safe model path handling; sanitized error handling. OpenAI credentials come from `system_settings.openai_api_key` or `OPENAI_API_KEY`; this feature intentionally does not add OpenAI credential-management UI.
- Modify `src/backend/routers/settings.py`
  - Add request/response models and endpoints before the `/{key}` catch-all route:
    - `GET /api/settings/runtime-default-models`
    - `PUT /api/settings/runtime-default-models`
    - `POST /api/settings/provider-connection-test`
  - Include `runtime_default_models` in feature flags for non-secret read paths.
- Modify `src/backend/services/task_execution_service.py`
  - Replace hardcoded runtime fallback with a helper that preserves priority: explicit model, `AGENT_RUNTIME_MODEL`, runtime defaults, then explicit legacy fallback. Preserve legacy `gemini` alias behavior.
- Create `tests/unit/test_runtime_model_defaults.py`
  - Unit tests for default synthesis, validation, legacy compatibility, persistence, and execution fallback helper behavior.
- Create `tests/unit/test_provider_connection_test.py`
  - Unit tests for request validation, missing credentials, unsupported provider, status normalization, model-specific provider calls, and secret sanitization.
- Create `src/frontend/src/utils/runtimeModelPresets.js`
  - Shared frontend constants/functions for runtimes, providers, preset models, default rows, resolved values, and status labels/classes.
- Modify `src/frontend/src/stores/settings.js`
  - Add actions to fetch/save runtime defaults and test provider connections.
- Modify `src/frontend/src/views/Settings.vue`
  - Replace Claude-only Default Model selector with provider-aware Default Models rows for Claude Code, Gemini CLI, and OpenCode.
  - Add per-row status and Test connection button.
  - Keep unrelated Settings behavior untouched.
- Optional modify `src/frontend/src/components/CreateAgentModal.vue`
  - Import shared preset constants only if a small reuse is safe; otherwise leave CreateAgentModal behavior unchanged for this feature.

## Task 1: Backend Runtime Defaults Module

**Files:**
- Create: `src/backend/services/runtime_model_defaults.py`
- Test: `tests/unit/test_runtime_model_defaults.py`

- [ ] **Step 1: Write failing tests for default synthesis and validation**

Create `tests/unit/test_runtime_model_defaults.py` with these initial tests:

```python
from __future__ import annotations

import json

import pytest


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
        ({"claude-code": {"provider": "anthropic", "model": ""}}, "Model is required"),
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'services.runtime_model_defaults'`.

- [ ] **Step 3: Implement runtime defaults module**

Create `src/backend/services/runtime_model_defaults.py`:

```python
from __future__ import annotations

import copy
import json
import re
from typing import Any

RUNTIME_DEFAULT_MODELS_KEY = "runtime_default_models"

SUPPORTED_RUNTIMES = ("claude-code", "gemini-cli", "opencode")
RUNTIME_ALIASES = {"gemini": "gemini-cli"}
BUILT_IN_PROVIDERS = ("anthropic", "openai", "google")

DEFAULT_RUNTIME_MODELS: dict[str, dict[str, str]] = {
    "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
    "opencode": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
}

_WHITESPACE_RE = re.compile(r"\s")


def _clone_defaults() -> dict[str, dict[str, str]]:
    return copy.deepcopy(DEFAULT_RUNTIME_MODELS)


def resolve_provider_model(entry: dict[str, str]) -> str:
    return f"{entry['provider']}/{entry['model']}"


def normalize_runtime(runtime: str | None) -> str:
    runtime = str(runtime or "claude-code").strip()
    return RUNTIME_ALIASES.get(runtime, runtime)


def runtime_compatible_model(runtime: str, entry: dict[str, str]) -> str:
    runtime = normalize_runtime(runtime)
    if runtime == "opencode":
        return resolve_provider_model(entry)
    return entry["model"]


def _validate_entry(runtime: str, entry: Any) -> dict[str, str]:
    runtime = normalize_runtime(runtime)
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")
    if not isinstance(entry, dict):
        raise ValueError(f"Default model for {runtime} must be an object")

    provider = str(entry.get("provider", "")).strip()
    model = str(entry.get("model", "")).strip()

    if not provider:
        raise ValueError(f"Provider is required for {runtime}")
    if "/" in provider:
        raise ValueError(f"Provider cannot contain '/' for {runtime}")
    if _WHITESPACE_RE.search(provider):
        raise ValueError(f"Provider cannot contain whitespace for {runtime}")
    if not model:
        raise ValueError(f"Model is required for {runtime}")
    if _WHITESPACE_RE.search(model):
        raise ValueError(f"Model cannot contain whitespace for {runtime}")

    return {"provider": provider, "model": model}


def validate_runtime_default_models(value: dict[str, Any]) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("runtime_default_models must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for runtime, entry in value.items():
        normalized_runtime = normalize_runtime(runtime)
        normalized[normalized_runtime] = _validate_entry(normalized_runtime, entry)
    missing = [runtime for runtime in SUPPORTED_RUNTIMES if runtime not in normalized]
    if missing:
        raise ValueError(f"Missing runtime default: {', '.join(missing)}")
    return normalized


def _parse_stored_models(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_default_runtime_models(
    raw_value: str | None,
    legacy_platform_default_model: str | None,
) -> dict[str, dict[str, str]]:
    defaults = _clone_defaults()
    if legacy_platform_default_model:
        defaults["claude-code"] = {
            "provider": "anthropic",
            "model": legacy_platform_default_model,
        }

    parsed = _parse_stored_models(raw_value)
    for runtime, entry in parsed.items():
        try:
            defaults[normalize_runtime(runtime)] = _validate_entry(runtime, entry)
        except ValueError:
            continue
    return defaults


def serialize_runtime_default_models(value: dict[str, Any]) -> str:
    return json.dumps(validate_runtime_default_models(value), sort_keys=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/backend/services/runtime_model_defaults.py tests/unit/test_runtime_model_defaults.py
git commit -m "feat: add runtime default model validation"
```

Expected: commit succeeds.

## Task 2: Settings Service Persistence and Compatibility

**Files:**
- Modify: `src/backend/services/settings_service.py`
- Test: `tests/unit/test_runtime_model_defaults.py`

- [ ] **Step 1: Add failing settings service tests**

Append to `tests/unit/test_runtime_model_defaults.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: FAIL with `AttributeError: 'SettingsService' object has no attribute 'get_runtime_default_models'`.

- [ ] **Step 3: Implement settings service methods**

Modify imports and constants in `src/backend/services/settings_service.py`:

```python
import copy

from services.runtime_model_defaults import (
    RUNTIME_DEFAULT_MODELS_KEY,
    get_default_runtime_models,
    normalize_runtime,
    runtime_compatible_model,
    serialize_runtime_default_models,
    validate_runtime_default_models,
)
```

Add near existing platform model cache globals:

```python
_runtime_models_cache: Optional[dict] = None
_runtime_models_cache_ts: float = 0.0
_RUNTIME_MODELS_CACHE_TTL = 60.0
```

Add a cache invalidation helper near these globals:

```python
def invalidate_runtime_model_caches() -> None:
    global _runtime_models_cache, _runtime_models_cache_ts, _platform_model_cache, _platform_model_cache_ts
    _runtime_models_cache = None
    _runtime_models_cache_ts = 0.0
    _platform_model_cache = None
    _platform_model_cache_ts = 0.0
```

Add `get_openai_api_key()` after `get_google_api_key()`:

```python
    def get_openai_api_key(self) -> str:
        """Get OpenAI API key from settings, fallback to env var."""
        key = self.get_setting('openai_api_key')
        if key:
            return key
        return os.getenv('OPENAI_API_KEY', '')
```

Add runtime defaults methods after `get_platform_default_model()`:

```python
    def get_runtime_default_models(self) -> dict:
        """Return provider-aware default models for each supported runtime."""
        global _runtime_models_cache, _runtime_models_cache_ts
        now = time.monotonic()
        if _runtime_models_cache is not None and (now - _runtime_models_cache_ts) < _RUNTIME_MODELS_CACHE_TTL:
            return copy.deepcopy(_runtime_models_cache)
        raw = self.get_setting(RUNTIME_DEFAULT_MODELS_KEY)
        defaults = get_default_runtime_models(raw, self.get_platform_default_model())
        _runtime_models_cache = copy.deepcopy(defaults)
        _runtime_models_cache_ts = now
        return copy.deepcopy(defaults)

    def set_runtime_default_models(self, value: dict) -> dict:
        """Persist provider-aware runtime defaults and keep legacy Claude setting in sync."""
        global _runtime_models_cache, _runtime_models_cache_ts, _platform_model_cache, _platform_model_cache_ts
        normalized = validate_runtime_default_models(value)
        db.set_setting(RUNTIME_DEFAULT_MODELS_KEY, serialize_runtime_default_models(normalized))
        db.set_setting(PLATFORM_DEFAULT_MODEL_KEY, normalized["claude-code"]["model"])
        _runtime_models_cache = copy.deepcopy(normalized)
        _runtime_models_cache_ts = time.monotonic()
        _platform_model_cache = normalized["claude-code"]["model"]
        _platform_model_cache_ts = time.monotonic()
        return copy.deepcopy(normalized)

    def resolve_model_for_runtime(self, runtime: str) -> str:
        """Return the runtime-compatible default model string for execution fallback."""
        defaults = self.get_runtime_default_models()
        normalized_runtime = normalize_runtime(runtime)
        entry = defaults.get(normalized_runtime)
        if entry:
            return runtime_compatible_model(normalized_runtime, entry)
        if normalized_runtime == "opencode":
            return "anthropic/claude-sonnet-4-5"
        if normalized_runtime == "gemini-cli":
            return "gemini-3-flash"
        return self.get_platform_default_model()
```

Add module-level convenience function near `get_google_api_key()`:

```python
def get_openai_api_key() -> str:
    """Get OpenAI API key from settings, fallback to env var."""
    return settings_service.get_openai_api_key()
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/backend/services/settings_service.py tests/unit/test_runtime_model_defaults.py
git commit -m "feat: persist runtime default models"
```

Expected: commit succeeds.

## Task 3: Settings API Endpoints

**Files:**
- Modify: `src/backend/routers/settings.py`
- Test: `tests/unit/test_runtime_model_defaults.py`

- [ ] **Step 1: Add focused route tests using FastAPI TestClient**

Append to `tests/unit/test_runtime_model_defaults.py`:

```python
def test_runtime_default_models_endpoint_returns_defaults(monkeypatch):
    from fastapi.testclient import TestClient
    import routers.settings as settings_router
    from dependencies import get_current_user
    from main import app
    from models import User

    monkeypatch.setattr(settings_router.settings_service, "get_runtime_default_models", lambda: {
        "claude-code": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "gemini-cli": {"provider": "google", "model": "gemini-3-flash"},
        "opencode": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
    })

    app.dependency_overrides[get_current_user] = lambda: User(username="admin", role="admin")
    try:
        client = TestClient(app)
        response = client.get("/api/settings/runtime-default-models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["runtime_default_models"]["opencode"]["provider"] == "anthropic"


def test_put_runtime_default_models_rejects_bad_payload(monkeypatch):
    from fastapi.testclient import TestClient
    from dependencies import get_current_user
    from main import app
    from models import User

    app.dependency_overrides[get_current_user] = lambda: User(username="admin", role="admin")
    try:
        client = TestClient(app)
        response = client.put("/api/settings/runtime-default-models", json={
            "runtime_default_models": {"bad-runtime": {"provider": "anthropic", "model": "claude-sonnet-4-6"}}
        })
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 400
    assert "Unsupported runtime" in response.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: endpoint tests fail with 404 or validation errors.

- [ ] **Step 3: Add endpoint models and routes**

Modify imports in `src/backend/routers/settings.py`:

```python
from services.runtime_model_defaults import resolve_provider_model, validate_runtime_default_models
```

Add Pydantic models after `OpsSettingsUpdate`:

```python
class RuntimeDefaultModelsUpdate(BaseModel):
    runtime_default_models: Dict[str, Dict[str, str]]


class ProviderConnectionTestRequest(BaseModel):
    runtime: str
    provider: str
    model: str
```

Add routes before `@router.get("/{key}")` catch-all:

```python
@router.get("/runtime-default-models")
async def get_runtime_default_models(current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    models = settings_service.get_runtime_default_models()
    return {
        "runtime_default_models": models,
        "resolved": {runtime: resolve_provider_model(entry) for runtime, entry in models.items()},
    }


@router.put("/runtime-default-models")
async def update_runtime_default_models(
    body: RuntimeDefaultModelsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    try:
        normalized = settings_service.set_runtime_default_models(body.runtime_default_models)
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
        details={"setting": "runtime_default_models", "action": "update"},
    )
    return {
        "success": True,
        "runtime_default_models": normalized,
        "resolved": {runtime: resolve_provider_model(entry) for runtime, entry in normalized.items()},
    }
```

Add `runtime_default_models` to `/feature-flags` response:

```python
"runtime_default_models": settings_service.get_runtime_default_models(),
```

- [ ] **Step 4: Run tests to verify route behavior**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_runtime_model_defaults.py -q
```

Expected: PASS. Keep `app.dependency_overrides` cleanup in `finally` blocks so global app state does not leak between tests.

- [ ] **Step 5: Preserve legacy platform_default_model writes**

In the existing generic `update_setting()` route, extend the `if key == "platform_default_model"` block so legacy writes invalidate the runtime defaults cache too and, if a runtime defaults map already exists, update its Claude Code entry:

```python
        if key == "platform_default_model":
            import services.settings_service as _ss
            _ss.invalidate_runtime_model_caches()
            current = settings_service.get_runtime_default_models()
            current["claude-code"] = {"provider": "anthropic", "model": body.value}
            settings_service.set_runtime_default_models(current)
```

Add a test in `tests/unit/test_runtime_model_defaults.py` that monkeypatches `db.set_setting`, calls the generic setting endpoint for `platform_default_model`, and asserts the stored `runtime_default_models` JSON has `claude-code.model == body.value`.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/backend/routers/settings.py tests/unit/test_runtime_model_defaults.py
git commit -m "feat: expose runtime default model settings"
```

Expected: commit succeeds.

## Task 4: Provider Connection Test Service and Endpoint

**Files:**
- Create: `src/backend/services/provider_connection_test_service.py`
- Modify: `src/backend/routers/settings.py`
- Test: `tests/unit/test_provider_connection_test.py`

- [ ] **Step 1: Write failing provider connection tests**

Create `tests/unit/test_provider_connection_test.py`:

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_non_builtin_provider_is_unsupported_without_network_call():
    from services.provider_connection_test_service import ProviderConnectionTestService

    result = await ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result["ok"] is False
    assert result["status"] == "unsupported_provider"
    assert result["resolved_model"] == "local-llm/my-model"


@pytest.mark.asyncio
async def test_missing_anthropic_credentials_maps_to_auth_failed(monkeypatch):
    import services.provider_connection_test_service as module

    monkeypatch.setattr(module.settings_service, "get_anthropic_api_key", lambda: "")

    result = await module.ProviderConnectionTestService().test_connection("claude-code", "anthropic", "claude-sonnet-4-6")

    assert result["ok"] is False
    assert result["status"] == "authentication_failed"
    assert "API key" in result["message"]


@pytest.mark.asyncio
async def test_anthropic_model_not_found_is_normalized(monkeypatch):
    import httpx
    import services.provider_connection_test_service as module

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def get(self, url, **kwargs):
            assert kwargs["headers"]["x-api-key"] == "sk-ant-secret"
            return httpx.Response(404, json={"error": {"message": "no such model sk-ant-secret"}})

    monkeypatch.setattr(module.settings_service, "get_anthropic_api_key", lambda: "sk-ant-secret")
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda timeout=10.0: FakeClient())

    result = await module.ProviderConnectionTestService().test_connection("opencode", "anthropic", "claude-missing")

    assert result["ok"] is False
    assert result["status"] == "model_not_found"
    assert "sk-ant-secret" not in result["message"]


@pytest.mark.asyncio
async def test_openai_success_uses_exact_model(monkeypatch):
    import httpx
    import services.provider_connection_test_service as module

    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(200, json={"id": "gpt-5-custom"})

    monkeypatch.setattr(module.settings_service, "get_openai_api_key", lambda: "sk-openai-secret")
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda timeout=10.0: FakeClient())

    result = await module.ProviderConnectionTestService().test_connection("opencode", "openai", "gpt-5-custom")

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert calls == ["https://api.openai.com/v1/models/gpt-5-custom"]


@pytest.mark.asyncio
async def test_timeout_maps_to_timed_out(monkeypatch):
    import httpx
    import services.provider_connection_test_service as module

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def get(self, url, **kwargs):
            raise httpx.TimeoutException("secret timeout")

    monkeypatch.setattr(module.settings_service, "get_openai_api_key", lambda: "sk-openai-secret")
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda timeout=10.0: FakeClient())

    result = await module.ProviderConnectionTestService().test_connection("opencode", "openai", "gpt-5")

    assert result["ok"] is False
    assert result["status"] == "timed_out"


def test_provider_test_validation_rejects_bad_provider():
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Provider cannot contain"):
        validate_provider_test_request("opencode", "bad/provider", "gpt-5")


@pytest.mark.parametrize("bad_model", ["gpt/5", "gpt?x=1", "gpt#frag"])
def test_provider_test_validation_rejects_url_path_delimiters_for_model(bad_model):
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Model cannot contain URL delimiters"):
        validate_provider_test_request("opencode", "openai", bad_model)


def test_provider_connection_endpoint_delegates_and_does_not_persist(monkeypatch):
    from fastapi.testclient import TestClient
    from dependencies import get_current_user
    from main import app
    from models import User
    import routers.settings as settings_router

    persisted = []

    async def fake_test(runtime, provider, model):
        assert (runtime, provider, model) == ("opencode", "openai", "gpt-5")
        return {
            "ok": True,
            "status": "connected",
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "resolved_model": "openai/gpt-5",
            "message": "Connection verified.",
        }

    monkeypatch.setattr(settings_router.provider_connection_test_service, "test_connection", fake_test)
    monkeypatch.setattr(settings_router.db, "set_setting", lambda key, value: persisted.append((key, value)))

    app.dependency_overrides[get_current_user] = lambda: User(username="admin", role="admin")
    try:
        client = TestClient(app)
        response = client.post("/api/settings/provider-connection-test", json={
            "runtime": "opencode",
            "provider": "openai",
            "model": "gpt-5",
        })
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert persisted == []


def test_provider_connection_endpoint_rejects_invalid_payload():
    from fastapi.testclient import TestClient
    from dependencies import get_current_user
    from main import app
    from models import User

    app.dependency_overrides[get_current_user] = lambda: User(username="admin", role="admin")
    try:
        client = TestClient(app)
        response = client.post("/api/settings/provider-connection-test", json={
            "runtime": "bad-runtime",
            "provider": "openai",
            "model": "gpt-5",
        })
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 400
    assert "Unsupported runtime" in response.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_provider_connection_test.py -q
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement provider connection service**

Create `src/backend/services/provider_connection_test_service.py`:

```python
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from services.settings_service import settings_service
from services.runtime_model_defaults import SUPPORTED_RUNTIMES, resolve_provider_model

_WHITESPACE_RE = re.compile(r"\s")


def validate_provider_test_request(runtime: str, provider: str, model: str) -> tuple[str, str, str]:
    runtime = str(runtime or "").strip()
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")
    if not provider:
        raise ValueError("Provider is required")
    if "/" in provider:
        raise ValueError("Provider cannot contain '/'")
    if _WHITESPACE_RE.search(provider):
        raise ValueError("Provider cannot contain whitespace")
    if not model:
        raise ValueError("Model is required")
    if _WHITESPACE_RE.search(model):
        raise ValueError("Model cannot contain whitespace")
    if provider in {"anthropic", "openai", "google"} and any(ch in model for ch in "/?#"):
        raise ValueError("Model cannot contain URL delimiters for built-in provider tests")
    return runtime, provider, model


def _sanitize_message(message: str, secrets: list[str]) -> str:
    sanitized = str(message or "")
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized[:300]


class ProviderConnectionTestService:
    def _result(self, ok: bool, status: str, runtime: str, provider: str, model: str, message: str) -> dict[str, Any]:
        return {
            "ok": ok,
            "status": status,
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "resolved_model": resolve_provider_model({"provider": provider, "model": model}),
            "message": message,
        }

    async def test_connection(self, runtime: str, provider: str, model: str) -> dict[str, Any]:
        runtime, provider, model = validate_provider_test_request(runtime, provider, model)
        if provider == "anthropic":
            return await self._test_anthropic(runtime, provider, model)
        if provider == "openai":
            return await self._test_openai(runtime, provider, model)
        if provider == "google":
            return await self._test_google(runtime, provider, model)
        return self._result(False, "unsupported_provider", runtime, provider, model, "Custom providers are not testable without base URL, auth scheme, and protocol settings.")

    async def _test_anthropic(self, runtime: str, provider: str, model: str) -> dict[str, Any]:
        key = settings_service.get_anthropic_api_key()
        if not key:
            return self._result(False, "authentication_failed", runtime, provider, model, "Anthropic API key is not configured.")
        try:
            model_path = quote(model, safe="")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"https://api.anthropic.com/v1/models/{model_path}",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
            return self._map_response(response, runtime, provider, model, [key])
        except httpx.TimeoutException:
            return self._result(False, "timed_out", runtime, provider, model, "Provider request timed out.")
        except httpx.ConnectError:
            return self._result(False, "provider_unreachable", runtime, provider, model, "Provider is unreachable.")
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, _sanitize_message(str(e), [key]))

    async def _test_openai(self, runtime: str, provider: str, model: str) -> dict[str, Any]:
        key = settings_service.get_openai_api_key()
        if not key:
            return self._result(False, "authentication_failed", runtime, provider, model, "OpenAI API key is not configured.")
        try:
            model_path = quote(model, safe="")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"https://api.openai.com/v1/models/{model_path}",
                    headers={"Authorization": f"Bearer {key}"},
                )
            return self._map_response(response, runtime, provider, model, [key])
        except httpx.TimeoutException:
            return self._result(False, "timed_out", runtime, provider, model, "Provider request timed out.")
        except httpx.ConnectError:
            return self._result(False, "provider_unreachable", runtime, provider, model, "Provider is unreachable.")
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, _sanitize_message(str(e), [key]))

    async def _test_google(self, runtime: str, provider: str, model: str) -> dict[str, Any]:
        key = settings_service.get_google_api_key()
        if not key:
            return self._result(False, "authentication_failed", runtime, provider, model, "Google API key is not configured.")
        try:
            model_path = quote(model, safe="")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}",
                    params={"key": key},
                )
            return self._map_response(response, runtime, provider, model, [key])
        except httpx.TimeoutException:
            return self._result(False, "timed_out", runtime, provider, model, "Provider request timed out.")
        except httpx.ConnectError:
            return self._result(False, "provider_unreachable", runtime, provider, model, "Provider is unreachable.")
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, _sanitize_message(str(e), [key]))

    def _map_response(self, response: httpx.Response, runtime: str, provider: str, model: str, secrets: list[str]) -> dict[str, Any]:
        if response.status_code == 200:
            return self._result(True, "connected", runtime, provider, model, "Connection verified.")
        if response.status_code in {401, 403}:
            return self._result(False, "authentication_failed", runtime, provider, model, "Authentication failed or model access is denied.")
        if response.status_code == 404:
            return self._result(False, "model_not_found", runtime, provider, model, "Model not found or unavailable.")
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            return self._result(False, "provider_unreachable", runtime, provider, model, f"Provider returned HTTP {response.status_code}.")
        return self._result(False, "unknown_error", runtime, provider, model, _sanitize_message(f"Provider returned HTTP {response.status_code}.", secrets))


provider_connection_test_service = ProviderConnectionTestService()
```

- [ ] **Step 4: Add API endpoint**

In `src/backend/routers/settings.py`, import service:

```python
from services.provider_connection_test_service import provider_connection_test_service
```

Add route before `/{key}` catch-all:

```python
@router.post("/provider-connection-test")
async def test_provider_connection(
    body: ProviderConnectionTestRequest,
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    try:
        return await provider_connection_test_service.test_connection(body.runtime, body.provider, body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 5: Run provider tests**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_provider_connection_test.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/backend/services/provider_connection_test_service.py src/backend/routers/settings.py tests/unit/test_provider_connection_test.py
git commit -m "feat: add provider connection tests"
```

Expected: commit succeeds.

## Task 5: Runtime-Aware Execution Fallback

**Files:**
- Modify: `src/backend/services/task_execution_service.py`
- Test: `tests/unit/test_opencode_backend_runtime_propagation.py`

- [ ] **Step 1: Add failing fallback helper tests**

Append to `tests/unit/test_opencode_backend_runtime_propagation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_opencode_backend_runtime_propagation.py -q
```

Expected: FAIL with missing `_resolve_execution_model`.

- [ ] **Step 3: Implement execution fallback helper and use it**

In `src/backend/services/task_execution_service.py`, add after `_get_agent_runtime_defaults`:

```python
def _legacy_model_fallback_for_runtime(runtime: str) -> str:
    if runtime == "opencode":
        return "anthropic/claude-sonnet-4-5"
    if runtime in {"gemini-cli", "gemini"}:
        return "gemini-3-flash"
    return settings_service.get_platform_default_model()


def _resolve_execution_model(agent_name: str, explicit_model: Optional[str]) -> str:
    if explicit_model is not None:
        return explicit_model
    agent_runtime, agent_runtime_model = _get_agent_runtime_defaults(agent_name)
    if agent_runtime_model:
        return agent_runtime_model
    normalized_runtime = "gemini-cli" if agent_runtime == "gemini" else agent_runtime
    try:
        return settings_service.resolve_model_for_runtime(normalized_runtime)
    except Exception:
        return _legacy_model_fallback_for_runtime(agent_runtime)
```

Replace the current block around lines 523-532:

```python
        if model is None:
            agent_runtime, agent_runtime_model = _get_agent_runtime_defaults(agent_name)
            if agent_runtime == "opencode":
                model = agent_runtime_model or "anthropic/claude-sonnet-4-5"
            elif agent_runtime in {"gemini-cli", "gemini"}:
                model = agent_runtime_model or "gemini-3-flash"
            else:
                model = settings_service.get_platform_default_model()
```

with:

```python
        model = _resolve_execution_model(agent_name, model)
```

- [ ] **Step 4: Run runtime propagation tests**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest tests/unit/test_opencode_backend_runtime_propagation.py tests/unit/test_runtime_model_defaults.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/backend/services/task_execution_service.py tests/unit/test_opencode_backend_runtime_propagation.py
git commit -m "feat: use runtime defaults for execution fallback"
```

Expected: commit succeeds.

## Task 6: Frontend Presets, Store Actions, and Settings UI

**Files:**
- Create: `src/frontend/src/utils/runtimeModelPresets.js`
- Modify: `src/frontend/src/stores/settings.js`
- Modify: `src/frontend/src/views/Settings.vue`

- [ ] **Step 1: Add shared preset utility**

Create `src/frontend/src/utils/runtimeModelPresets.js`:

```javascript
export const RUNTIME_MODEL_ROWS = [
  { key: 'claude-code', label: 'Claude Code', description: 'Default for Claude Code agents and legacy platform paths.' },
  { key: 'gemini-cli', label: 'Gemini CLI', description: 'Default for Gemini CLI agents.' },
  { key: 'opencode', label: 'OpenCode', description: 'Default for OpenCode agents; passed as provider/model.' }
]

export const PROVIDER_OPTIONS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'google', label: 'Google' },
  { value: '__custom__', label: 'Custom' }
]

export const MODEL_PRESETS = {
  anthropic: ['claude-sonnet-4-6', 'claude-sonnet-4-5', 'claude-opus-4-8'],
  openai: ['gpt-5', 'gpt-5-mini', 'gpt-4.1'],
  google: ['gemini-3-flash', 'gemini-2.5-flash', 'gemini-2.5-pro']
}

export const DEFAULT_RUNTIME_MODELS = {
  'claude-code': { provider: 'anthropic', model: 'claude-sonnet-4-6', providerMode: 'anthropic' },
  'gemini-cli': { provider: 'google', model: 'gemini-3-flash', providerMode: 'google' },
  opencode: { provider: 'anthropic', model: 'claude-sonnet-4-5', providerMode: 'anthropic' }
}

export const CONNECTION_STATUS_LABELS = {
  not_tested: 'Not tested',
  testing: 'Testing...',
  connected: 'Connected',
  authentication_failed: 'Authentication failed',
  model_not_found: 'Model not found or unavailable',
  provider_unreachable: 'Provider unreachable',
  timed_out: 'Timed out',
  unsupported_provider: 'Unsupported provider',
  unknown_error: 'Unknown error'
}

export function providerModelOptions(provider) {
  return MODEL_PRESETS[provider] || []
}

export function resolveRuntimeModel(entry) {
  const provider = (entry?.provider || '').trim()
  const model = (entry?.model || '').trim()
  return provider && model ? `${provider}/${model}` : ''
}

export function cloneRuntimeDefaults(value = DEFAULT_RUNTIME_MODELS) {
  const cloned = JSON.parse(JSON.stringify(value))
  for (const entry of Object.values(cloned)) {
    entry.providerMode = MODEL_PRESETS[entry.provider] ? entry.provider : '__custom__'
  }
  return cloned
}

export function payloadRuntimeDefaults(value) {
  const payload = {}
  for (const [runtime, entry] of Object.entries(value)) {
    payload[runtime] = {
      provider: (entry.provider || '').trim(),
      model: (entry.model || '').trim()
    }
  }
  return payload
}
```

- [ ] **Step 2: Add store actions**

Modify `src/frontend/src/stores/settings.js` actions before `clearError()`:

```javascript
    async fetchRuntimeDefaultModels() {
      const response = await axios.get('/api/settings/runtime-default-models')
      this.settings.runtime_default_models = response.data.runtime_default_models
      return response.data
    },

    async updateRuntimeDefaultModels(runtimeDefaultModels) {
      this.saving = true
      this.error = null
      try {
        const response = await axios.put('/api/settings/runtime-default-models', {
          runtime_default_models: runtimeDefaultModels
        })
        this.settings.runtime_default_models = response.data.runtime_default_models
        if (response.data.runtime_default_models?.['claude-code']?.model) {
          this.settings.platform_default_model = response.data.runtime_default_models['claude-code'].model
        }
        return response.data
      } catch (error) {
        console.error('Failed to update runtime default models:', error)
        this.error = error.response?.data?.detail || 'Failed to update runtime default models'
        throw error
      } finally {
        this.saving = false
      }
    },

    async testProviderConnection(payload) {
      const response = await axios.post('/api/settings/provider-connection-test', payload)
      return response.data
    },
```

- [ ] **Step 3: Replace Default Model UI block**

In `src/frontend/src/views/Settings.vue`, replace the `<!-- Platform Default Model (#831) -->` block at lines 113-151 with:

```vue
                <!-- Runtime Default Models -->
                <div v-if="isAdmin" class="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700">
                  <div class="flex items-start justify-between gap-4">
                    <div>
                      <h3 class="text-sm font-medium text-gray-900 dark:text-white">Default Models</h3>
                      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                        Choose the provider and model used when a runtime has no explicit model selected.
                      </p>
                    </div>
                    <button
                      @click="saveRuntimeDefaultModels"
                      :disabled="savingRuntimeDefaultModels"
                      class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg v-if="savingRuntimeDefaultModels" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Save defaults
                    </button>
                  </div>

                  <div class="mt-4 space-y-4">
                    <div
                      v-for="runtime in runtimeModelRows"
                      :key="runtime.key"
                      class="rounded-lg border border-gray-200 dark:border-gray-700 p-4 bg-gray-50 dark:bg-gray-900/40"
                    >
                      <div class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                        <div class="min-w-0">
                          <h4 class="text-sm font-semibold text-gray-900 dark:text-white">{{ runtime.label }}</h4>
                          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ runtime.description }}</p>
                        </div>
                        <div class="text-xs font-mono text-gray-600 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded px-2 py-1">
                          {{ resolvedRuntimeModel(runtime.key) || 'provider/model' }}
                        </div>
                      </div>

                      <div class="mt-3 grid gap-3 md:grid-cols-2">
                        <div>
                          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">Provider</label>
                          <select
                            v-model="runtimeDefaultModels[runtime.key].providerMode"
                            @change="onRuntimeProviderModeChanged(runtime.key)"
                            class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                          >
                            <option v-for="provider in providerOptions" :key="provider.value" :value="provider.value">
                              {{ provider.label }}
                            </option>
                          </select>
                          <input
                            v-if="runtimeDefaultModels[runtime.key].providerMode === '__custom__'"
                            v-model="runtimeDefaultModels[runtime.key].provider"
                            @input="resetRuntimeConnectionStatus(runtime.key)"
                            type="text"
                            placeholder="provider-name"
                            class="mt-2 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                          />
                        </div>
                        <div>
                          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">Model</label>
                          <input
                            v-if="runtimeDefaultModels[runtime.key].providerMode === '__custom__'"
                            v-model="runtimeDefaultModels[runtime.key].model"
                            @input="resetRuntimeConnectionStatus(runtime.key)"
                            type="text"
                            placeholder="model-name"
                            class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                          />
                          <input
                            v-else
                            v-model="runtimeDefaultModels[runtime.key].model"
                            @input="resetRuntimeConnectionStatus(runtime.key)"
                            :list="`runtime-model-options-${runtime.key}`"
                            type="text"
                            class="mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 dark:bg-gray-700 dark:text-white text-sm"
                          />
                          <datalist :id="`runtime-model-options-${runtime.key}`">
                            <option v-for="model in providerModelOptions(runtimeDefaultModels[runtime.key].provider)" :key="model" :value="model" />
                          </datalist>
                        </div>
                      </div>

                      <div class="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                        <div class="text-sm" :class="runtimeConnectionStatusClass(runtime.key)">
                          {{ runtimeConnectionStatusLabel(runtime.key) }}
                          <span v-if="runtimeConnectionStatuses[runtime.key]?.message" class="text-gray-500 dark:text-gray-400">
                            — {{ runtimeConnectionStatuses[runtime.key].message }}
                          </span>
                        </div>
                        <button
                          type="button"
                          @click="testRuntimeProviderConnection(runtime.key)"
                          :disabled="runtimeConnectionStatuses[runtime.key]?.status === 'testing'"
                          class="inline-flex items-center justify-center px-3 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <svg v-if="runtimeConnectionStatuses[runtime.key]?.status === 'testing'" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
                            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                          </svg>
                          Test connection
                        </button>
                      </div>
                    </div>
                  </div>

                  <div v-if="runtimeDefaultModelsSaveSuccess" class="mt-3 flex items-center text-sm text-status-success-600 dark:text-status-success-400">
                    <svg class="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                    Saved
                  </div>
                </div>
```

For both spinners in this block, copy the existing spinner SVG path from nearby Settings buttons exactly; do not hand-type or modify the path data.

- [ ] **Step 4: Add imports and state**

At the top of the `<script setup>` import block in `Settings.vue`, add:

```javascript
import {
  CONNECTION_STATUS_LABELS,
  DEFAULT_RUNTIME_MODELS,
  PROVIDER_OPTIONS,
  RUNTIME_MODEL_ROWS,
  cloneRuntimeDefaults,
  payloadRuntimeDefaults,
  providerModelOptions,
  resolveRuntimeModel
} from '../utils/runtimeModelPresets'
```

Replace old platform default model state:

```javascript
const platformDefaultModelValue = ref('claude-sonnet-4-6')
const savingPlatformDefaultModel = ref(false)
const platformDefaultModelSaveSuccess = ref(false)
```

with:

```javascript
const runtimeModelRows = RUNTIME_MODEL_ROWS
const providerOptions = PROVIDER_OPTIONS
const runtimeDefaultModels = ref(cloneRuntimeDefaults())
const savingRuntimeDefaultModels = ref(false)
const runtimeDefaultModelsSaveSuccess = ref(false)
const runtimeConnectionStatuses = ref({
  'claude-code': { status: 'not_tested', message: '' },
  'gemini-cli': { status: 'not_tested', message: '' },
  opencode: { status: 'not_tested', message: '' }
})
```

Update `loadSettings()` Promise list by replacing `loadPlatformDefaultModel()` with `loadRuntimeDefaultModels()`.

- [ ] **Step 5: Add frontend methods and remove old methods**

Remove old functions `loadPlatformDefaultModel` and `savePlatformDefaultModel` if present. Add these functions near the old platform default model helpers:

```javascript
async function loadRuntimeDefaultModels() {
  try {
    const response = await settingsStore.fetchRuntimeDefaultModels()
    runtimeDefaultModels.value = cloneRuntimeDefaults(response.runtime_default_models || DEFAULT_RUNTIME_MODELS)
  } catch (e) {
    console.error('Failed to load runtime default models:', e)
    runtimeDefaultModels.value = cloneRuntimeDefaults(DEFAULT_RUNTIME_MODELS)
  }
}

function resolvedRuntimeModel(runtimeKey) {
  return resolveRuntimeModel(runtimeDefaultModels.value[runtimeKey])
}

function resetRuntimeConnectionStatus(runtimeKey) {
  runtimeConnectionStatuses.value[runtimeKey] = { status: 'not_tested', message: '' }
}

function onRuntimeProviderModeChanged(runtimeKey) {
  const entry = runtimeDefaultModels.value[runtimeKey]
  if (entry.providerMode !== '__custom__') {
    entry.provider = entry.providerMode
  } else if (['anthropic', 'openai', 'google'].includes(entry.provider)) {
    entry.provider = ''
  }
  const presets = providerModelOptions(entry.provider)
  if (presets.length > 0 && !presets.includes(entry.model)) {
    entry.model = presets[0]
  }
  resetRuntimeConnectionStatus(runtimeKey)
}

function runtimeConnectionStatusLabel(runtimeKey) {
  const status = runtimeConnectionStatuses.value[runtimeKey]?.status || 'not_tested'
  return CONNECTION_STATUS_LABELS[status] || CONNECTION_STATUS_LABELS.unknown_error
}

function runtimeConnectionStatusClass(runtimeKey) {
  const status = runtimeConnectionStatuses.value[runtimeKey]?.status || 'not_tested'
  if (status === 'connected') return 'text-status-success-600 dark:text-status-success-400'
  if (status === 'testing') return 'text-action-primary-600 dark:text-action-primary-400'
  if (status === 'not_tested') return 'text-gray-500 dark:text-gray-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
}

async function testRuntimeProviderConnection(runtimeKey) {
  const entry = runtimeDefaultModels.value[runtimeKey]
  runtimeConnectionStatuses.value[runtimeKey] = { status: 'testing', message: '' }
  try {
    const response = await settingsStore.testProviderConnection({
      runtime: runtimeKey,
      provider: (entry.provider || '').trim(),
      model: entry.model
    })
    runtimeConnectionStatuses.value[runtimeKey] = {
      status: response.status || 'unknown_error',
      message: response.message || ''
    }
  } catch (e) {
    runtimeConnectionStatuses.value[runtimeKey] = {
      status: 'unknown_error',
      message: e.response?.data?.detail || 'Connection test failed'
    }
  }
}

async function saveRuntimeDefaultModels() {
  savingRuntimeDefaultModels.value = true
  runtimeDefaultModelsSaveSuccess.value = false
  error.value = null
  try {
    const response = await settingsStore.updateRuntimeDefaultModels(payloadRuntimeDefaults(runtimeDefaultModels.value))
    runtimeDefaultModels.value = cloneRuntimeDefaults(response.runtime_default_models)
    runtimeDefaultModelsSaveSuccess.value = true
    setTimeout(() => { runtimeDefaultModelsSaveSuccess.value = false }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save runtime default models'
  } finally {
    savingRuntimeDefaultModels.value = false
  }
}
```

- [ ] **Step 6: Run frontend build**

Before building, run this local manual verification checklist in the browser or Vue dev server if available:

```text
Settings → General → Default Models:
- Three rows render: Claude Code, Gemini CLI, OpenCode.
- Changing a built-in provider updates the datalist presets and resolved provider/model value.
- Choosing Custom reveals a free-text provider input and free-text model input.
- Custom provider and model values are preserved in the save payload.
- Editing provider or model resets only that row to Not tested.
- Test connection sends the current unsaved runtime/provider/model values.
- Test connection updates only row status and does not save settings.
- Save payload includes runtime_default_models and does not remove unrelated settings.
```

Run:

```bash
cd src/frontend && npm run build
```

Expected: build passes. If Vue reports a template syntax error, fix the exact reported line; do not redesign the UI.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/frontend/src/utils/runtimeModelPresets.js src/frontend/src/stores/settings.js src/frontend/src/views/Settings.vue
git commit -m "feat: add provider-aware settings UI"
```

Expected: commit succeeds.

## Task 7: Full Verification and Separate Review

**Files:**
- No code changes expected unless verification or review finds issues.

- [ ] **Step 1: Run backend regression tests**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest \
  tests/unit/test_runtime_model_defaults.py \
  tests/unit/test_provider_connection_test.py \
  tests/unit/test_opencode_backend_runtime_propagation.py \
  tests/unit/test_opencode_backend_models.py \
  tests/unit/test_opencode_runtime.py \
  tests/unit/test_opencode_mcp_config.py \
  tests/unit/test_opencode_terminal.py \
  tests/unit/test_926_version_endpoint.py \
  tests/unit/test_gemini_runtime_pipe_drop.py \
  tests/unit/test_agent_server_hardening.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run frontend production build**

Run:

```bash
cd src/frontend && npm run build
```

Expected: build passes; existing large chunk warnings are acceptable.

- [ ] **Step 3: Request separate code review**

Dispatch a fresh `@oracle` review, not the implementation worker, with this prompt:

```text
Review provider-aware runtime default models implementation in /Users/yalou/.config/superpowers/worktrees/trinity/opencode-runtime.

Focus on:
- spec compliance with docs/superpowers/specs/2026-06-20-provider-aware-runtime-default-models-design.md
- backwards compatibility with platform_default_model
- provider connection test safety and secret sanitization
- execution fallback priority: explicit model, AGENT_RUNTIME_MODEL, runtime defaults, legacy fallback
- Settings UI correctness and no accidental secret exposure

Return APPROVED or CHANGES_REQUESTED with concrete file/line issues.
Do not edit files.
```

Expected: oracle returns `APPROVED` or actionable changes.

- [ ] **Step 4: Fix review findings if needed**

If review returns `CHANGES_REQUESTED`, implement only the requested fixes, then rerun Step 1 and Step 2 and request re-review.

- [ ] **Step 5: Commit any verification fixes**

If changes were made after the Task 6 commit, run:

```bash
git add src/backend src/frontend tests
git commit -m "fix: harden runtime default model settings"
```

Expected: commit succeeds, or no commit is needed if there were no changes.

## Task 8: Deploy to ubuntu-server After Approval

Do not start this task without explicit user confirmation after local verification and separate code review approval. Deployment mutates the official remote stack.

**Files:**
- Remote repo: `/home/sun/trinity`
- Local worktree: `/Users/yalou/.config/superpowers/worktrees/trinity/opencode-runtime`

- [ ] **Step 1: Confirm local status and commits**

Run:

```bash
git status --short && git log --oneline -10
```

Expected: clean working tree except intentionally uncommitted files; provider-aware commits visible.

- [ ] **Step 2: Apply commits to remote official repo**

First determine the actual base commit shared by the local worktree and remote official repo:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && git rev-parse HEAD'
git merge-base HEAD origin/main
```

Confirm the patch range includes only provider-aware runtime defaults commits. If the remote HEAD is not an ancestor of the local branch or the included commits are not exactly the intended provider-aware commits, stop and ask for guidance.

Run from the local worktree:

```bash
git format-patch <confirmed-remote-head>..HEAD --stdout > /var/folders/bb/3xpl_11n1x39cftnht788rsc0000gn/T/opencode/provider-runtime-defaults.patch
scp /var/folders/bb/3xpl_11n1x39cftnht788rsc0000gn/T/opencode/provider-runtime-defaults.patch ubuntu-server:/tmp/provider-runtime-defaults.patch
ssh ubuntu-server 'cd /home/sun/trinity && git am /tmp/provider-runtime-defaults.patch'
```

Expected: patch applies cleanly.

- [ ] **Step 3: Rebuild and restart services on remote**

Run:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && HTTP_PROXY=http://172.17.0.1:7890 HTTPS_PROXY=http://172.17.0.1:7890 NO_PROXY=localhost,127.0.0.1 docker compose build --build-arg HTTP_PROXY=http://172.17.0.1:7890 --build-arg HTTPS_PROXY=http://172.17.0.1:7890 --build-arg http_proxy=http://172.17.0.1:7890 --build-arg https_proxy=http://172.17.0.1:7890 backend scheduler frontend mcp-server && docker compose up -d --force-recreate backend scheduler frontend mcp-server'
```

Expected: build succeeds and services recreate.

- [ ] **Step 4: Verify remote health and Settings endpoints**

Run:

```bash
ssh ubuntu-server 'curl -fsS http://127.0.0.1/ >/dev/null && echo FRONTEND_OK && curl -fsS http://127.0.0.1:8000/docs >/dev/null && echo BACKEND_DOCS_OK && docker compose ps'
```

Expected: `FRONTEND_OK`, `BACKEND_DOCS_OK`, and services running/healthy.

- [ ] **Step 5: Manual browser verification**

Open:

```text
http://192.168.68.89/
```

Expected: Settings → General shows Default Models rows for Claude Code, Gemini CLI, and OpenCode. Changing provider/model resets a row to Not tested. Test connection sends current unsaved values and displays a normalized status. Saving persists values.

---

## Self-Review

- Spec coverage: backend storage, compatibility, provider tests, frontend rows/statuses, manual-only testing, execution fallback, and deployment are each mapped to tasks.
- Placeholder scan: no `TBD`, no bare `TODO`, and no "write tests for above" without code. One UI paste warning is explicit remediation for a known SVG typo risk, not a placeholder.
- Type consistency: backend setting key is consistently `runtime_default_models`; runtime keys are `claude-code`, `gemini-cli`, `opencode`; provider statuses match the design's normalized states.
