# Runtime Provider Template Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Trinity's fragile generic provider/model behavior with runtime-aware provider settings and generated Claude Code, OpenCode, and Gemini CLI launch templates.

**Architecture:** Settings stores protocol-tagged provider definitions and runtime default selections. Backend runtime template builders validate `runtime + provider + model` and compile it into env/config/command metadata for agent creation. Frontend filters providers/models by runtime so invalid combinations cannot be selected.

**Tech Stack:** FastAPI/Python backend, SQLite-backed `system_settings`, pytest, Vue 3 + Pinia frontend, Node-based frontend utility tests.

---

## Scope

This plan implements the first shippable slice of the approved spec:

1. Provider Settings v2 data model.
2. Runtime default selections as `runtime -> provider_id/model_id`.
3. Runtime template builders for:
   - Claude Code + `anthropic-messages`
   - OpenCode + `openai-compatible`
   - Gemini CLI + `google-gemini`
   - Gemini CLI + `google-vertex` validation shape, without deep ADC preflight.
4. Agent creation injection through the template builders.
5. Runtime-aware Create Agent and model selector options.

Deferred to a later plan:

- Paid runtime preflight execution endpoints.
- Full provider deletion repair UX.
- Google protocol gateway enablement beyond disabled/pending representation.
- OpenCode Anthropic template.
- Full migration UI for every legacy schedule/chat override.

## File Structure

### Backend files to create

- `src/backend/services/provider_configs.py`  
  Protocol-tagged provider config validation, parsing, serialization, masking, and compatibility constants.

- `src/backend/services/runtime_provider_templates.py`  
  Runtime template builders. Converts `runtime + provider + model` into launch env/config metadata.

- `tests/unit/test_provider_configs.py`  
  Unit tests for provider validation, masking, protocol support, model validation, and delete-reference helper behavior.

- `tests/unit/test_runtime_provider_templates.py`  
  Unit tests for Claude/OpenCode/Gemini template generation and invalid combinations.

### Backend files to modify

- `src/backend/services/settings_service.py`  
  Add provider config v2 getters/setters and runtime default selection getters/setters while preserving legacy APIs.

- `src/backend/routers/settings.py`  
  Add provider config v2 endpoints and runtime option discovery endpoints.

- `src/backend/services/agent_service/crud.py`  
  Read `runtime_provider_id/runtime_model_id` when present and inject generated runtime template env/config at agent creation.

- `src/backend/models.py`  
  Extend `AgentConfig` with optional `runtime_provider_id` and `runtime_model_id`.

- `src/backend/services/provider_connection_test_service.py`  
  Rename semantics in API payloads where needed and reuse provider v2 validation without claiming runtime execution success.

### Frontend files to modify

- `src/frontend/src/stores/settings.js`  
  Add provider v2 fetch/save/discovery actions.

- `src/frontend/src/utils/runtimeModelPresets.js`  
  Add runtime compatibility/filter helpers for provider v2.

- `src/frontend/src/components/CreateAgentModal.vue`  
  Replace OpenCode-only provider selector with runtime-aware provider/model selection.

- `src/frontend/src/components/ModelSelector.vue`  
  Accept runtime context and show only options compatible with the current runtime.

- `src/frontend/scripts/test-runtime-model-presets.mjs`  
  Extend existing frontend utility tests for compatibility filtering.

---

## Task 1: Provider Config v2 service

**Files:**
- Create: `src/backend/services/provider_configs.py`
- Create: `tests/unit/test_provider_configs.py`

- [ ] **Step 1: Write failing tests for provider validation**

Create `tests/unit/test_provider_configs.py`:

```python
from __future__ import annotations

import json

import pytest

from services.provider_configs import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GOOGLE_GEMINI_PROTOCOL,
    GOOGLE_VERTEX_PROTOCOL,
    OPENAI_COMPATIBLE_PROTOCOL,
    PROVIDER_CONFIGS_KEY,
    mask_provider_configs,
    parse_provider_configs,
    provider_env_var_name,
    runtime_supports_provider,
    serialize_provider_configs,
    validate_provider_configs,
)


def test_constants():
    assert PROVIDER_CONFIGS_KEY == "provider_configs"
    assert OPENAI_COMPATIBLE_PROTOCOL == "openai-compatible"
    assert ANTHROPIC_MESSAGES_PROTOCOL == "anthropic-messages"
    assert GOOGLE_GEMINI_PROTOCOL == "google-gemini"
    assert GOOGLE_VERTEX_PROTOCOL == "google-vertex"


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


def test_provider_env_var_name_is_stable_and_sanitized():
    assert provider_env_var_name("deepseek-openai") == "TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"
    assert provider_env_var_name("foo.bar") == "TRINITY_PROVIDER_FOO_BAR_API_KEY"


@pytest.mark.parametrize(
    ("runtime", "protocol", "expected"),
    [
        ("claude-code", "anthropic-messages", True),
        ("claude-code", "openai-compatible", False),
        ("opencode", "openai-compatible", True),
        ("opencode", "anthropic-messages", False),
        ("gemini-cli", "google-gemini", True),
        ("gemini-cli", "google-vertex", True),
        ("gemini-cli", "openai-compatible", False),
    ],
)
def test_runtime_supports_provider(runtime, protocol, expected):
    assert runtime_supports_provider(runtime, protocol) is expected


def test_parse_invalid_json_returns_empty():
    assert parse_provider_configs("not-json") == {}


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
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'services.provider_configs'`.

- [ ] **Step 3: Implement provider config service**

Create `src/backend/services/provider_configs.py`:

```python
from __future__ import annotations

import json
import re
from typing import Any

PROVIDER_CONFIGS_KEY = "provider_configs"

OPENAI_COMPATIBLE_PROTOCOL = "openai-compatible"
ANTHROPIC_MESSAGES_PROTOCOL = "anthropic-messages"
GOOGLE_GEMINI_PROTOCOL = "google-gemini"
GOOGLE_VERTEX_PROTOCOL = "google-vertex"
GOOGLE_PROTOCOL_GATEWAY_PROTOCOL = "google-protocol-gateway"

SUPPORTED_PROTOCOLS = {
    OPENAI_COMPATIBLE_PROTOCOL,
    ANTHROPIC_MESSAGES_PROTOCOL,
    GOOGLE_GEMINI_PROTOCOL,
    GOOGLE_VERTEX_PROTOCOL,
    GOOGLE_PROTOCOL_GATEWAY_PROTOCOL,
}

CLAUDE_ALIASES = {"sonnet", "opus", "haiku", "fable"}
_BAD_ID_RE = re.compile(r"\s|/")
_ENV_STEM_RE = re.compile(r"[^A-Za-z0-9]")


def provider_env_var_name(provider_id: str) -> str:
    stem = _ENV_STEM_RE.sub("_", provider_id).upper().strip("_")
    return f"TRINITY_PROVIDER_{stem}_API_KEY"


def normalize_provider_id(provider_id: Any) -> str:
    if not isinstance(provider_id, str):
        raise ValueError("Provider id must be a string")
    normalized = provider_id.strip()
    if not normalized:
        raise ValueError("Provider id is required")
    if _BAD_ID_RE.search(normalized):
        raise ValueError("Provider id cannot contain whitespace or '/'")
    return normalized


def _mask_secret(secret: str) -> str | None:
    if not secret:
        return None
    if len(secret) < 8:
        return "****"
    return f"...{secret[-4:]}"


def _normalize_base_url(value: Any, provider_id: str, required: bool = True) -> str:
    if value in (None, ""):
        if required:
            raise ValueError(f"base_url is required for {provider_id}")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"base_url must be a string for {provider_id}")
    base_url = value.strip().rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(f"base_url must start with http:// or https:// for {provider_id}")
    if re.search(r"\s", base_url):
        raise ValueError(f"base_url cannot contain whitespace for {provider_id}")
    return base_url


def _normalize_auth(raw_auth: Any, protocol: str, provider_id: str, existing: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_auth, dict):
        raise ValueError(f"auth is required for {provider_id}")
    auth_type = str(raw_auth.get("type") or "api_key").strip()
    if auth_type not in {"api_key", "adc", "service_account"}:
        raise ValueError(f"Unsupported auth type for {provider_id}: {auth_type}")

    result: dict[str, Any] = {"type": auth_type}
    if auth_type == "api_key":
        api_key = raw_auth.get("api_key")
        if api_key is None or str(api_key).strip() == "":
            api_key = (((existing or {}).get(provider_id) or {}).get("auth") or {}).get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(f"api_key is required for {provider_id}")
        result["api_key"] = api_key.strip()
        if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
            header_mode = str(raw_auth.get("header_mode") or "x-api-key").strip()
            if header_mode not in {"x-api-key", "bearer"}:
                raise ValueError(f"Unsupported header_mode for {provider_id}: {header_mode}")
            result["header_mode"] = header_mode
    elif auth_type == "service_account":
        credential_ref = raw_auth.get("credential_ref")
        if not isinstance(credential_ref, str) or not credential_ref.strip():
            raise ValueError(f"credential_ref is required for {provider_id}")
        result["credential_ref"] = credential_ref.strip()
    return result


def _normalize_models(raw_models: Any, protocol: str, provider_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError(f"models are required for {provider_id}")
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    models: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ValueError(f"model entries must be objects for {provider_id}")
        model_id = str(raw_model.get("id") or "").strip()
        if not model_id:
            raise ValueError(f"model id is required for {provider_id}")
        if re.search(r"\s", model_id):
            raise ValueError(f"model id cannot contain whitespace for {provider_id}")
        if model_id in seen_ids:
            raise ValueError(f"Duplicate model id for {provider_id}: {model_id}")
        seen_ids.add(model_id)
        model: dict[str, Any] = {
            "id": model_id,
            "label": str(raw_model.get("label") or model_id).strip() or model_id,
        }
        if protocol == ANTHROPIC_MESSAGES_PROTOCOL and raw_model.get("claude_alias"):
            alias = str(raw_model.get("claude_alias")).strip()
            if alias not in CLAUDE_ALIASES:
                raise ValueError(f"Unsupported Claude alias for {provider_id}: {alias}")
            if alias in seen_aliases:
                raise ValueError(f"Duplicate Claude alias for {provider_id}: {alias}")
            seen_aliases.add(alias)
            model["claude_alias"] = alias
        if "tool_call" in raw_model:
            model["tool_call"] = bool(raw_model["tool_call"])
        for key in ("context", "output"):
            if raw_model.get(key) not in (None, ""):
                value = int(raw_model[key])
                if value <= 0:
                    raise ValueError(f"{key} must be positive for {provider_id}")
                model[key] = value
        models.append(model)
    return models


def _validate_entry(provider_id: Any, entry: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    provider_id = normalize_provider_id(provider_id)
    if not isinstance(entry, dict):
        raise ValueError(f"Provider config for {provider_id} must be an object")
    protocol = str(entry.get("protocol") or "").strip()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Unsupported protocol for {provider_id}: {protocol}")
    result: dict[str, Any] = {
        "id": provider_id,
        "name": str(entry.get("name") or provider_id).strip() or provider_id,
        "protocol": protocol,
        "auth": _normalize_auth(entry.get("auth"), protocol, provider_id, existing),
        "models": _normalize_models(entry.get("models"), protocol, provider_id),
    }
    if protocol in {OPENAI_COMPATIBLE_PROTOCOL, ANTHROPIC_MESSAGES_PROTOCOL, GOOGLE_PROTOCOL_GATEWAY_PROTOCOL}:
        result["base_url"] = _normalize_base_url(entry.get("base_url"), provider_id)
    if protocol == GOOGLE_VERTEX_PROTOCOL:
        project = str(entry.get("project") or "").strip()
        location = str(entry.get("location") or "").strip()
        if not project:
            raise ValueError(f"project is required for {provider_id}")
        if not location:
            raise ValueError(f"location is required for {provider_id}")
        result["project"] = project
        result["location"] = location
    return result


def validate_provider_configs(value: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("provider_configs must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    env_names: dict[str, str] = {}
    for provider_id, entry in value.items():
        normalized_id = normalize_provider_id(provider_id)
        env_name = provider_env_var_name(normalized_id)
        if env_name in env_names:
            raise ValueError(f"Provider env var collision: {env_names[env_name]} and {normalized_id}")
        env_names[env_name] = normalized_id
        normalized[normalized_id] = _validate_entry(normalized_id, entry, existing=existing)
    return normalized


def parse_provider_configs(raw: str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    try:
        return validate_provider_configs(parsed)
    except ValueError:
        return {}


def serialize_provider_configs(value: dict[str, Any]) -> str:
    return json.dumps(validate_provider_configs(value), sort_keys=True)


def mask_provider_configs(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    masked: dict[str, dict[str, Any]] = {}
    for provider_id, entry in validate_provider_configs(value).items():
        auth = dict(entry.get("auth") or {})
        api_key = auth.pop("api_key", "") if isinstance(auth.get("api_key"), str) else ""
        auth["api_key_configured"] = bool(api_key)
        auth["api_key_masked"] = _mask_secret(api_key)
        masked_entry = {key: val for key, val in entry.items() if key != "auth"}
        masked_entry["auth"] = auth
        masked[provider_id] = masked_entry
    return masked


def find_provider_model(provider: dict[str, Any], model_id: str) -> dict[str, Any]:
    for model in provider.get("models", []):
        if model.get("id") == model_id:
            return model
    raise ValueError(f"Model not found for provider {provider.get('id')}: {model_id}")


def runtime_supports_provider(runtime: str, protocol: str) -> bool:
    normalized_runtime = "gemini-cli" if runtime == "gemini" else runtime
    if normalized_runtime == "claude-code":
        return protocol == ANTHROPIC_MESSAGES_PROTOCOL
    if normalized_runtime == "opencode":
        return protocol == OPENAI_COMPATIBLE_PROTOCOL
    if normalized_runtime == "gemini-cli":
        return protocol in {GOOGLE_GEMINI_PROTOCOL, GOOGLE_VERTEX_PROTOCOL}
    return False
```

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/provider_configs.py tests/unit/test_provider_configs.py
git commit -m "feat: add provider config model"
```

---

## Task 2: Runtime template builders

**Files:**
- Create: `src/backend/services/runtime_provider_templates.py`
- Create: `tests/unit/test_runtime_provider_templates.py`

- [ ] **Step 1: Write failing template tests**

Create `tests/unit/test_runtime_provider_templates.py`:

```python
from __future__ import annotations

import json

import pytest

from services.provider_configs import validate_provider_configs
from services.runtime_provider_templates import build_runtime_template


def _providers():
    return validate_provider_configs({
        "deepseek-anthropic": {
            "name": "DeepSeek Anthropic",
            "protocol": "anthropic-messages",
            "base_url": "https://api.deepseek.com/anthropic",
            "auth": {"type": "api_key", "header_mode": "x-api-key", "api_key": "sk-anthropic-secret"},
            "models": [
                {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "claude_alias": "sonnet"},
                {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "claude_alias": "haiku"},
            ],
        },
        "openrouter-anthropic": {
            "name": "OpenRouter Anthropic",
            "protocol": "anthropic-messages",
            "base_url": "https://openrouter.ai/api",
            "auth": {"type": "api_key", "header_mode": "bearer", "api_key": "sk-openrouter-secret"},
            "models": [{"id": "anthropic/claude-sonnet-4.5", "claude_alias": "sonnet"}],
        },
        "deepseek-openai": {
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"type": "api_key", "api_key": "sk-openai-secret"},
            "models": [
                {"id": "deepseek-v4-pro", "tool_call": True, "context": 128000, "output": 8192},
                {"id": "deepseek-v4-flash", "tool_call": True},
            ],
        },
        "google-ai-studio": {
            "name": "Google AI Studio",
            "protocol": "google-gemini",
            "auth": {"type": "api_key", "api_key": "google-secret"},
            "models": [{"id": "gemini-2.5-pro"}],
        },
        "vertex-prod": {
            "name": "Vertex Prod",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "adc"},
            "models": [{"id": "gemini-2.5-pro"}],
        },
    })


def test_claude_template_uses_alias_and_secret_ref():
    template = build_runtime_template("claude-code", _providers()["deepseek-anthropic"], "deepseek-v4-pro")
    assert template.runtime == "claude-code"
    assert template.model_arg == "sonnet"
    assert template.env["ANTHROPIC_BASE_URL"].value == "https://api.deepseek.com/anthropic"
    assert template.env["ANTHROPIC_API_KEY"].secret_ref == "provider:deepseek-anthropic:api_key"
    assert template.env["ANTHROPIC_DEFAULT_SONNET_MODEL"].value == "deepseek-v4-pro"
    assert template.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"].value == "deepseek-v4-flash"
    assert "sk-anthropic-secret" not in json.dumps(template.redacted_dict())


def test_claude_template_bearer_uses_auth_token_and_clears_api_key():
    template = build_runtime_template("claude-code", _providers()["openrouter-anthropic"], "anthropic/claude-sonnet-4.5")
    assert template.env["ANTHROPIC_AUTH_TOKEN"].secret_ref == "provider:openrouter-anthropic:api_key"
    assert template.env["ANTHROPIC_API_KEY"].value == ""


def test_claude_template_rejects_model_without_alias():
    providers = validate_provider_configs({
        "plain-anthropic": {
            "name": "Plain",
            "protocol": "anthropic-messages",
            "base_url": "https://example.test/anthropic",
            "auth": {"type": "api_key", "api_key": "secret"},
            "models": [{"id": "plain-model"}],
        }
    })
    with pytest.raises(ValueError, match="Claude Code v1 requires a Claude alias"):
        build_runtime_template("claude-code", providers["plain-anthropic"], "plain-model")


def test_opencode_template_generates_config_without_raw_secret():
    template = build_runtime_template("opencode", _providers()["deepseek-openai"], "deepseek-v4-pro")
    assert template.runtime == "opencode"
    assert template.model_arg == "deepseek-openai/deepseek-v4-pro"
    assert template.env["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"].secret_ref == "provider:deepseek-openai:api_key"
    content = template.env["OPENCODE_CONFIG_CONTENT"].value
    data = json.loads(content)
    assert data["enabled_providers"] == ["deepseek-openai"]
    assert data["model"] == "deepseek-openai/deepseek-v4-pro"
    assert data["provider"]["deepseek-openai"]["npm"] == "@ai-sdk/openai-compatible"
    assert data["provider"]["deepseek-openai"]["options"]["apiKey"] == "{env:TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY}"
    assert "sk-openai-secret" not in content


def test_opencode_rejects_anthropic_messages_in_v1():
    with pytest.raises(ValueError, match="OpenCode v1 supports openai-compatible"):
        build_runtime_template("opencode", _providers()["deepseek-anthropic"], "deepseek-v4-pro")


def test_gemini_template_api_key_mode():
    template = build_runtime_template("gemini-cli", _providers()["google-ai-studio"], "gemini-2.5-pro")
    assert template.model_arg == "gemini-2.5-pro"
    assert template.env["GEMINI_API_KEY"].secret_ref == "provider:google-ai-studio:api_key"
    assert template.env["GEMINI_MODEL"].value == "gemini-2.5-pro"


def test_gemini_template_vertex_mode():
    template = build_runtime_template("gemini-cli", _providers()["vertex-prod"], "gemini-2.5-pro")
    assert template.env["GOOGLE_GENAI_USE_VERTEXAI"].value == "true"
    assert template.env["GOOGLE_CLOUD_PROJECT"].value == "project-id"
    assert template.env["GOOGLE_CLOUD_LOCATION"].value == "us-central1"


def test_invalid_runtime_provider_combination_rejected():
    with pytest.raises(ValueError, match="Gemini CLI cannot use"):
        build_runtime_template("gemini-cli", _providers()["deepseek-openai"], "deepseek-v4-pro")
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/unit/test_runtime_provider_templates.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'services.runtime_provider_templates'`.

- [ ] **Step 3: Implement runtime template builders**

Create `src/backend/services/runtime_provider_templates.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.provider_configs import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GOOGLE_GEMINI_PROTOCOL,
    GOOGLE_VERTEX_PROTOCOL,
    OPENAI_COMPATIBLE_PROTOCOL,
    find_provider_model,
    provider_env_var_name,
)


@dataclass(frozen=True)
class TemplateValue:
    value: str | None = None
    secret_ref: str | None = None

    def redacted(self) -> str:
        if self.secret_ref:
            return "***"
        return self.value or ""


@dataclass(frozen=True)
class RuntimeTemplate:
    runtime: str
    model_arg: str
    env: dict[str, TemplateValue]
    config_preview: dict[str, Any]

    def materialize_env(self, secrets: dict[str, str]) -> dict[str, str]:
        materialized: dict[str, str] = {}
        for key, item in self.env.items():
            if item.secret_ref:
                materialized[key] = secrets[item.secret_ref]
            else:
                materialized[key] = item.value or ""
        return materialized

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "model_arg": self.model_arg,
            "env": {key: item.redacted() for key, item in self.env.items()},
            "config_preview": self.config_preview,
        }


def _secret_ref(provider: dict[str, Any]) -> str:
    return f"provider:{provider['id']}:api_key"


def _normalize_runtime(runtime: str) -> str:
    return "gemini-cli" if runtime == "gemini" else runtime


def _build_claude(provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    if provider["protocol"] != ANTHROPIC_MESSAGES_PROTOCOL:
        raise ValueError("Claude Code cannot use non-Anthropic Messages providers")
    selected = find_provider_model(provider, model_id)
    alias = selected.get("claude_alias")
    if not alias:
        raise ValueError("Claude Code v1 requires a Claude alias for the selected model")

    env: dict[str, TemplateValue] = {
        "AGENT_RUNTIME": TemplateValue(value="claude-code"),
        "ANTHROPIC_BASE_URL": TemplateValue(value=provider["base_url"]),
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": TemplateValue(value="0"),
    }
    auth = provider.get("auth", {})
    if auth.get("header_mode") == "bearer":
        env["ANTHROPIC_AUTH_TOKEN"] = TemplateValue(secret_ref=_secret_ref(provider))
        env["ANTHROPIC_API_KEY"] = TemplateValue(value="")
    else:
        env["ANTHROPIC_API_KEY"] = TemplateValue(secret_ref=_secret_ref(provider))

    alias_env = {
        "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    }
    for model in provider.get("models", []):
        model_alias = model.get("claude_alias")
        if model_alias in alias_env:
            env[alias_env[model_alias]] = TemplateValue(value=model["id"])
    if "ANTHROPIC_DEFAULT_HAIKU_MODEL" in env:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = TemplateValue(value="haiku")
    return RuntimeTemplate("claude-code", alias, env, {"provider_id": provider["id"], "model_id": model_id, "alias": alias})


def _build_opencode(provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    if provider["protocol"] != OPENAI_COMPATIBLE_PROTOCOL:
        raise ValueError("OpenCode v1 supports openai-compatible providers only")
    selected = find_provider_model(provider, model_id)
    env_name = provider_env_var_name(provider["id"])
    model_value = f"{provider['id']}/{model_id}"
    models: dict[str, Any] = {}
    for model in provider.get("models", []):
        metadata: dict[str, Any] = {"name": model.get("label") or model["id"]}
        if "tool_call" in model:
            metadata["tool_call"] = bool(model["tool_call"])
        limit: dict[str, int] = {}
        if model.get("context"):
            limit["context"] = int(model["context"])
        if model.get("output"):
            limit["output"] = int(model["output"])
        if limit:
            metadata["limit"] = limit
        models[model["id"]] = metadata
    small_model = None
    for model in provider.get("models", []):
        if model["id"] != model_id:
            small_model = f"{provider['id']}/{model['id']}"
            break
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": [provider["id"]],
        "model": model_value,
        "provider": {
            provider["id"]: {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider.get("name") or provider["id"],
                "options": {"baseURL": provider["base_url"], "apiKey": f"{{env:{env_name}}}"},
                "models": models,
            }
        },
        "autoupdate": False,
    }
    if small_model:
        config["small_model"] = small_model
    env = {
        "AGENT_RUNTIME": TemplateValue(value="opencode"),
        "AGENT_RUNTIME_MODEL": TemplateValue(value=model_value),
        "OPENCODE_DISABLE_MODELS_FETCH": TemplateValue(value="true"),
        "OPENCODE_CONFIG_CONTENT": TemplateValue(value=json.dumps(config, sort_keys=True)),
        env_name: TemplateValue(secret_ref=_secret_ref(provider)),
    }
    return RuntimeTemplate("opencode", model_value, env, {"provider_id": provider["id"], "model_id": selected["id"], "config": config})


def _build_gemini(provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    if provider["protocol"] not in {GOOGLE_GEMINI_PROTOCOL, GOOGLE_VERTEX_PROTOCOL}:
        raise ValueError("Gemini CLI cannot use this provider protocol")
    find_provider_model(provider, model_id)
    env: dict[str, TemplateValue] = {
        "AGENT_RUNTIME": TemplateValue(value="gemini-cli"),
        "GEMINI_MODEL": TemplateValue(value=model_id),
    }
    if provider["protocol"] == GOOGLE_GEMINI_PROTOCOL:
        env["GEMINI_API_KEY"] = TemplateValue(secret_ref=_secret_ref(provider))
    else:
        env["GOOGLE_GENAI_USE_VERTEXAI"] = TemplateValue(value="true")
        env["GOOGLE_CLOUD_PROJECT"] = TemplateValue(value=provider["project"])
        env["GOOGLE_CLOUD_LOCATION"] = TemplateValue(value=provider["location"])
    return RuntimeTemplate("gemini-cli", model_id, env, {"provider_id": provider["id"], "model_id": model_id})


def build_runtime_template(runtime: str, provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    normalized = _normalize_runtime(runtime)
    if normalized == "claude-code":
        return _build_claude(provider, model_id)
    if normalized == "opencode":
        return _build_opencode(provider, model_id)
    if normalized == "gemini-cli":
        return _build_gemini(provider, model_id)
    raise ValueError(f"Unsupported runtime: {runtime}")
```

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py tests/unit/test_runtime_provider_templates.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/runtime_provider_templates.py tests/unit/test_runtime_provider_templates.py
git commit -m "feat: add runtime provider templates"
```

---

## Task 3: Settings service and API endpoints

**Files:**
- Modify: `src/backend/services/settings_service.py`
- Modify: `src/backend/routers/settings.py`
- Test: extend `tests/unit/test_provider_configs.py`

- [ ] **Step 1: Add failing API tests**

Append to `tests/unit/test_provider_configs.py`:

```python

def test_settings_service_roundtrips_provider_configs(monkeypatch):
    from services import settings_service

    stored = {}
    monkeypatch.setattr(settings_service.db, "get_setting_value", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_service.db, "set_setting_value", lambda key, value, updated_by="system": stored.__setitem__(key, value) or True)

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
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py::test_settings_service_roundtrips_provider_configs -v
```

Expected: FAIL with `AttributeError: module 'services.settings_service' has no attribute 'set_provider_configs'`.

- [ ] **Step 3: Add settings_service helpers**

Modify `src/backend/services/settings_service.py` to import provider config helpers and add module-level functions near existing custom-provider helpers:

```python
from services.provider_configs import (
    PROVIDER_CONFIGS_KEY,
    mask_provider_configs,
    parse_provider_configs,
    serialize_provider_configs,
    validate_provider_configs,
)


def get_provider_configs() -> dict:
    return parse_provider_configs(db.get_setting_value(PROVIDER_CONFIGS_KEY, "{}"))


def get_masked_provider_configs() -> dict:
    return mask_provider_configs(get_provider_configs())


def set_provider_configs(value: dict, updated_by: str = "system") -> dict:
    existing = get_provider_configs()
    normalized = validate_provider_configs(value, existing=existing)
    db.set_setting_value(PROVIDER_CONFIGS_KEY, serialize_provider_configs(normalized), updated_by=updated_by)
    return normalized
```

If `settings_service.py` uses a class-based `SettingsService`, add matching methods to the class and module wrappers consistent with existing patterns.

- [ ] **Step 4: Add settings routes**

Modify `src/backend/routers/settings.py` near the existing custom-provider routes:

```python
@router.get("/provider-configs")
async def get_provider_configs_endpoint(current_user: User = Depends(get_current_user)):
    return {"providers": settings_service.get_masked_provider_configs()}


@router.put("/provider-configs")
async def update_provider_configs_endpoint(
    payload: dict,
    current_user: User = Depends(require_role("admin")),
):
    providers = payload.get("providers", payload)
    normalized = settings_service.set_provider_configs(providers, updated_by=current_user.username)
    return {"providers": mask_provider_configs(normalized)}
```

Also protect generic settings routes so `provider_configs` is not returned, written, or deleted through broad `/settings/{key}` routes.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/backend/services/settings_service.py src/backend/routers/settings.py tests/unit/test_provider_configs.py
git commit -m "feat: expose provider config settings"
```

---

## Task 4: Agent creation runtime/provider/model injection

**Files:**
- Modify: `src/backend/models.py`
- Modify: `src/backend/services/agent_service/crud.py`
- Test: extend `tests/unit/test_opencode_backend_runtime_propagation.py`

- [ ] **Step 1: Add failing agent creation tests**

Append to `tests/unit/test_opencode_backend_runtime_propagation.py`:

```python

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
    monkeypatch.setattr(crud.db, "create_agent_mcp_api_key", lambda **kwargs: SimpleNamespace(key_prefix="trinity_mcp_test", api_key="trinity_mcp_secret"))

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
    assert "sk-secret" not in env_vars["OPENCODE_CONFIG_CONTENT"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/unit/test_opencode_backend_runtime_propagation.py::test_create_agent_internal_uses_runtime_provider_model_selection -v
```

Expected: FAIL because `AgentConfig` has no `runtime_provider_id`/`runtime_model_id` or creation does not inject template env.

- [ ] **Step 3: Extend AgentConfig**

Modify `src/backend/models.py` `AgentConfig`:

```python
runtime_provider_id: Optional[str] = None
runtime_model_id: Optional[str] = None
```

Keep `runtime_model` for backwards compatibility.

- [ ] **Step 4: Add runtime template injection helper**

Modify `src/backend/services/agent_service/crud.py` imports:

```python
from services.runtime_provider_templates import build_runtime_template
```

Add helper near existing runtime helper functions:

```python
def _apply_provider_runtime_template(env_vars: dict, runtime: str, provider_id: str | None, model_id: str | None) -> bool:
    if not provider_id or not model_id:
        return False
    providers = settings_service.get_provider_configs()
    provider = providers.get(provider_id)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Provider '{provider_id}' not found")
    template = build_runtime_template(runtime, provider, model_id)
    secrets = {f"provider:{provider_id}:api_key": provider.get("auth", {}).get("api_key", "")}
    env_vars.update(template.materialize_env(secrets))
    env_vars["AGENT_RUNTIME_MODEL"] = template.model_arg
    env_vars["TRINITY_RUNTIME_PROVIDER_ID"] = provider_id
    env_vars["TRINITY_RUNTIME_MODEL_ID"] = model_id
    return True
```

Call this after initial `env_vars` creation and before legacy OpenCode custom-provider injection:

```python
provider_template_applied = _apply_provider_runtime_template(
    env_vars,
    normalized_runtime,
    config.runtime_provider_id,
    config.runtime_model_id,
)
if provider_template_applied:
    config.runtime_model = env_vars.get("AGENT_RUNTIME_MODEL") or config.runtime_model
```

Guard legacy custom-provider injection:

```python
if not provider_template_applied and normalized_runtime == "opencode":
    ... existing custom provider injection ...
```

- [ ] **Step 5: Run backend tests**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py tests/unit/test_runtime_provider_templates.py tests/unit/test_opencode_backend_runtime_propagation.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/backend/models.py src/backend/services/agent_service/crud.py tests/unit/test_opencode_backend_runtime_propagation.py
git commit -m "feat: inject runtime provider templates"
```

---

## Task 5: Runtime-aware frontend provider/model selection

**Files:**
- Modify: `src/frontend/src/utils/runtimeModelPresets.js`
- Modify: `src/frontend/src/stores/settings.js`
- Modify: `src/frontend/src/components/CreateAgentModal.vue`
- Modify: `src/frontend/scripts/test-runtime-model-presets.mjs`

- [ ] **Step 1: Add failing frontend utility tests**

Append to `src/frontend/scripts/test-runtime-model-presets.mjs`:

```javascript
import { buildRuntimeProviderModelOptions } from '../src/utils/runtimeModelPresets.js'

const providerConfigs = {
  'deepseek-anthropic': {
    id: 'deepseek-anthropic',
    name: 'DeepSeek Anthropic',
    protocol: 'anthropic-messages',
    models: [{ id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro', claude_alias: 'sonnet' }],
    auth: { api_key_configured: true },
  },
  'deepseek-openai': {
    id: 'deepseek-openai',
    name: 'DeepSeek OpenAI',
    protocol: 'openai-compatible',
    models: [{ id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro' }],
    auth: { api_key_configured: true },
  },
  google: {
    id: 'google',
    name: 'Google',
    protocol: 'google-gemini',
    models: [{ id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' }],
    auth: { api_key_configured: true },
  },
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const claudeOptions = buildRuntimeProviderModelOptions('claude-code', providerConfigs)
assert(claudeOptions.some((option) => option.providerId === 'deepseek-anthropic'), 'Claude should show anthropic-messages provider')
assert(!claudeOptions.some((option) => option.providerId === 'deepseek-openai'), 'Claude should hide openai-compatible provider')

const opencodeOptions = buildRuntimeProviderModelOptions('opencode', providerConfigs)
assert(opencodeOptions.some((option) => option.value === 'deepseek-openai/deepseek-v4-pro'), 'OpenCode should show OpenAI-compatible provider/model')
assert(!opencodeOptions.some((option) => option.providerId === 'deepseek-anthropic'), 'OpenCode v1 should hide anthropic-messages provider')

const geminiOptions = buildRuntimeProviderModelOptions('gemini-cli', providerConfigs)
assert(geminiOptions.some((option) => option.value === 'google/gemini-2.5-pro'), 'Gemini should show Google model')
assert(!geminiOptions.some((option) => option.providerId === 'deepseek-openai'), 'Gemini should hide OpenAI-compatible provider')
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
node src/frontend/scripts/test-runtime-model-presets.mjs
```

Expected: FAIL because `buildRuntimeProviderModelOptions` is not exported.

- [ ] **Step 3: Implement frontend helper**

Modify `src/frontend/src/utils/runtimeModelPresets.js`:

```javascript
export function runtimeSupportsProtocol(runtime, protocol) {
  const normalizedRuntime = runtime === 'gemini' ? 'gemini-cli' : runtime
  if (normalizedRuntime === 'claude-code') return protocol === 'anthropic-messages'
  if (normalizedRuntime === 'opencode') return protocol === 'openai-compatible'
  if (normalizedRuntime === 'gemini-cli') return protocol === 'google-gemini' || protocol === 'google-vertex'
  return false
}

export function buildRuntimeProviderModelOptions(runtime, providerConfigs = {}) {
  const options = []
  for (const [providerId, provider] of Object.entries(providerConfigs || {})) {
    if (!provider || !runtimeSupportsProtocol(runtime, provider.protocol)) continue
    if (provider.auth && provider.auth.api_key_configured === false) continue
    const models = Array.isArray(provider.models) ? provider.models : []
    for (const model of models) {
      const modelId = String(model?.id || '').trim()
      if (!modelId) continue
      options.push({
        providerId,
        modelId,
        value: `${providerId}/${modelId}`,
        label: `${provider.name || providerId}: ${model.label || modelId}`,
        protocol: provider.protocol,
        claudeAlias: model.claude_alias || null,
      })
    }
  }
  return options
}
```

- [ ] **Step 4: Add settings store actions**

Modify `src/frontend/src/stores/settings.js`:

```javascript
async fetchProviderConfigs() {
  const response = await api.get('/settings/provider-configs')
  return response.data.providers || {}
},
async updateProviderConfigs(providers) {
  const response = await api.put('/settings/provider-configs', { providers })
  return response.data.providers || {}
},
```

Adapt to the store's existing HTTP helper style if it uses `fetch` instead of `api`.

- [ ] **Step 5: Update CreateAgentModal payload**

Modify `src/frontend/src/components/CreateAgentModal.vue` so the form stores:

```javascript
runtime_provider_id: '',
runtime_model_id: '',
```

When runtime changes, load provider configs and compute:

```javascript
const runtimeModelOptions = computed(() => buildRuntimeProviderModelOptions(form.runtime, providerConfigs.value))
```

When an option is selected:

```javascript
const [providerId, ...modelParts] = selectedValue.split('/')
form.runtime_provider_id = providerId
form.runtime_model_id = modelParts.join('/')
```

Submit both fields for every runtime:

```javascript
runtime_provider_id: form.runtime_provider_id || null,
runtime_model_id: form.runtime_model_id || null,
runtime_model: form.runtime === 'opencode' && form.runtime_provider_id && form.runtime_model_id
  ? `${form.runtime_provider_id}/${form.runtime_model_id}`
  : form.runtime_model,
```

- [ ] **Step 6: Run frontend utility tests and build**

Run:

```bash
node src/frontend/scripts/test-runtime-model-presets.mjs
npm run build --prefix src/frontend
```

Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add src/frontend/src/utils/runtimeModelPresets.js src/frontend/src/stores/settings.js src/frontend/src/components/CreateAgentModal.vue src/frontend/scripts/test-runtime-model-presets.mjs
git commit -m "feat: add runtime-aware provider model selection"
```

---

## Task 6: Verification and review

**Files:**
- No new files unless fixes are required.

- [ ] **Step 1: Run backend focused tests**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py tests/unit/test_runtime_provider_templates.py tests/unit/test_opencode_backend_runtime_propagation.py -v
```

Expected: PASS.

- [ ] **Step 2: Run existing related tests**

Run:

```bash
python -m pytest tests/unit/test_custom_provider_configs.py tests/unit/test_provider_connection_test.py tests/unit/test_runtime_model_defaults.py -v
```

Expected: PASS or only expected unrelated warnings.

- [ ] **Step 3: Run frontend tests/build**

Run:

```bash
node src/frontend/scripts/test-runtime-model-presets.mjs
npm run build --prefix src/frontend
```

Expected: PASS.

- [ ] **Step 4: Request code review**

Dispatch reviewer with:

```text
Review provider config v2 + runtime template builder implementation.
Check: invalid runtime/provider combinations, raw secret exposure, OpenCode config correctness, Claude alias mapping, Gemini protocol restrictions, frontend filtering, and legacy compatibility.
```

- [ ] **Step 5: Fix review feedback**

For each Critical or Important reviewer finding, add or update a failing test, implement the fix, and rerun the focused test command from the failing area.

- [ ] **Step 6: Final verification before deploy**

Run:

```bash
python -m pytest tests/unit/test_provider_configs.py tests/unit/test_runtime_provider_templates.py tests/unit/test_opencode_backend_runtime_propagation.py tests/unit/test_custom_provider_configs.py tests/unit/test_provider_connection_test.py tests/unit/test_runtime_model_defaults.py -v
node src/frontend/scripts/test-runtime-model-presets.mjs
npm run build --prefix src/frontend
```

Expected: all PASS.

- [ ] **Step 7: Commit final fixes**

```bash
git status --short
git diff
git add <only intended files>
git commit -m "feat: add runtime-aware provider templates"
```

---

## Self-Review Notes

- Spec coverage: This plan implements provider settings, runtime compatibility filtering, runtime template builders, secret-ref launch materialization, and agent creation injection. Runtime preflight endpoints and full repair UX are explicitly deferred.
- Placeholder scan: No `TBD`/`TODO` placeholders are required for implementation steps. Deferred work is named in Scope and not part of this milestone.
- Type consistency: Provider source of truth is consistently `runtime + provider_id + model_id`; runtime-specific `model_arg` is derived by `build_runtime_template`.
