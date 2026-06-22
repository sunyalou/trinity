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
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_ENV_STEM_RE = re.compile(r"[^A-Za-z0-9]")

PROTOCOL_AUTH_TYPES = {
    OPENAI_COMPATIBLE_PROTOCOL: {"api_key"},
    ANTHROPIC_MESSAGES_PROTOCOL: {"api_key"},
    GOOGLE_GEMINI_PROTOCOL: {"api_key"},
    GOOGLE_VERTEX_PROTOCOL: {"adc", "service_account"},
    GOOGLE_PROTOCOL_GATEWAY_PROTOCOL: {"api_key"},
}


def provider_env_var_name(provider_id: str) -> str:
    provider_id = normalize_provider_id(provider_id)
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
    if not _ALNUM_RE.search(normalized):
        raise ValueError("Provider id must contain at least one alphanumeric character")
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
    if auth_type not in PROTOCOL_AUTH_TYPES.get(protocol, set()):
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
            if not isinstance(raw_model["tool_call"], bool):
                raise ValueError(f"tool_call must be a boolean for {provider_id}/{model_id}")
            model["tool_call"] = raw_model["tool_call"]
        for key in ("context", "output"):
            if raw_model.get(key) not in (None, ""):
                try:
                    value = int(raw_model[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a positive integer for {provider_id}/{model_id}") from exc
                if value <= 0:
                    raise ValueError(f"{key} must be a positive integer for {provider_id}/{model_id}")
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
    return validate_provider_configs(parsed)


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
