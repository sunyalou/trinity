from __future__ import annotations

import json
import re
from typing import Any

CUSTOM_PROVIDER_CONFIGS_KEY = "custom_provider_configs"
OPENAI_COMPATIBLE_PROTOCOL = "openai-compatible"

_WHITESPACE_RE = re.compile(r"\s")


def _existing_api_key(existing: dict[str, Any] | None, provider: str) -> str:
    if not isinstance(existing, dict):
        return ""
    entry = existing.get(provider)
    if not isinstance(entry, dict):
        return ""
    api_key = entry.get("api_key")
    return api_key if isinstance(api_key, str) else ""


def _validate_entry(provider: Any, entry: Any, existing: dict[str, Any] | None = None) -> dict[str, str]:
    if not isinstance(provider, str):
        raise ValueError("Provider name must be a string")
    provider_name = provider
    if not provider_name:
        raise ValueError("Provider name is required")
    if _WHITESPACE_RE.search(provider_name):
        raise ValueError("Provider name cannot contain whitespace")
    if "/" in provider_name:
        raise ValueError("Provider name cannot contain '/'")

    if not isinstance(entry, dict):
        raise ValueError(f"Custom provider config for {provider_name} must be an object")

    raw_protocol = entry.get("protocol")
    if not isinstance(raw_protocol, str) or not raw_protocol:
        raise ValueError(f"Protocol is required for {provider_name}")
    protocol = raw_protocol
    if protocol != OPENAI_COMPATIBLE_PROTOCOL:
        raise ValueError(f"Unsupported protocol for {provider_name}: {protocol}")

    raw_base_url = entry.get("base_url")
    if not isinstance(raw_base_url, str) or not raw_base_url:
        raise ValueError(f"Base URL is required for {provider_name}")
    base_url = raw_base_url
    if _WHITESPACE_RE.search(base_url):
        raise ValueError(f"Base URL cannot contain whitespace for {provider_name}")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(f"Base URL must start with http:// or https:// for {provider_name}")
    base_url = base_url.rstrip("/")

    raw_api_key = entry.get("api_key", "")
    if raw_api_key is None:
        raw_api_key = ""
    if not isinstance(raw_api_key, str):
        raise ValueError(f"API key must be a string for {provider_name}")
    api_key = raw_api_key.strip()
    if not api_key:
        api_key = _existing_api_key(existing, provider_name)
    if not api_key:
        raise ValueError(f"API key is required for {provider_name}")

    return {
        "protocol": protocol,
        "base_url": base_url,
        "api_key": api_key,
    }


def validate_custom_provider_configs(
    value: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("custom_provider_configs must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for provider, entry in value.items():
        normalized_entry = _validate_entry(provider, entry, existing=existing)
        normalized[provider] = normalized_entry
    return normalized


def parse_custom_provider_configs(raw: str | None) -> dict[str, dict[str, str]]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for provider, entry in parsed.items():
        try:
            normalized_entry = _validate_entry(provider, entry)
            normalized[provider] = normalized_entry
        except ValueError:
            continue
    return normalized


def serialize_custom_provider_configs(value: dict[str, Any]) -> str:
    return json.dumps(validate_custom_provider_configs(value), sort_keys=True)


def _mask_api_key(api_key: str) -> str | None:
    if not api_key:
        return None
    if len(api_key) < 8:
        return "****"
    return f"...{api_key[-4:]}"


def mask_custom_provider_configs(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    masked: dict[str, dict[str, Any]] = {}
    for provider, entry in value.items():
        if not isinstance(entry, dict):
            continue
        api_key = entry.get("api_key")
        if not isinstance(api_key, str):
            api_key = ""
        masked[provider] = {
            "protocol": entry.get("protocol"),
            "base_url": entry.get("base_url"),
            "api_key_configured": bool(api_key),
            "api_key_masked": _mask_api_key(api_key),
        }
    return masked
