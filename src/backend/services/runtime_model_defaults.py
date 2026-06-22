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

    raw_provider = entry.get("provider")
    raw_model = entry.get("model")

    if not isinstance(raw_provider, str):
        raise ValueError(f"Provider must be a string for {runtime}")
    if not isinstance(raw_model, str):
        raise ValueError(f"Model must be a string for {runtime}")

    provider = raw_provider.strip()
    model = raw_model.strip()

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
    legacy_entry = {"provider": "anthropic", "model": legacy_platform_default_model}
    try:
        defaults["claude-code"] = _validate_entry("claude-code", legacy_entry)
    except ValueError:
        pass

    parsed = _parse_stored_models(raw_value)
    for runtime, entry in parsed.items():
        try:
            defaults[normalize_runtime(runtime)] = _validate_entry(runtime, entry)
        except ValueError:
            continue
    return defaults


def serialize_runtime_default_models(value: dict[str, Any]) -> str:
    return json.dumps(validate_runtime_default_models(value), sort_keys=True)
