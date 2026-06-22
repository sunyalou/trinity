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

    def materialize_env(self, secret_map: dict[str, str]) -> dict[str, str]:
        materialized: dict[str, str] = {}
        for key, item in self.env.items():
            if item.secret_ref:
                if item.secret_ref not in secret_map:
                    raise ValueError(f"Missing secret for env var {key}: {item.secret_ref}")
                materialized[key] = secret_map[item.secret_ref]
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

    alias_env_names = {
        "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    }
    for model in provider.get("models", []):
        model_alias = model.get("claude_alias")
        if model_alias in alias_env_names:
            env[alias_env_names[model_alias]] = TemplateValue(value=model["id"])
    if "ANTHROPIC_DEFAULT_HAIKU_MODEL" in env:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = TemplateValue(value="haiku")

    return RuntimeTemplate(
        runtime="claude-code",
        model_arg=alias,
        env=env,
        config_preview={"provider_id": provider["id"], "model_id": model_id, "alias": alias},
    )


def _build_opencode(provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    if provider["protocol"] != OPENAI_COMPATIBLE_PROTOCOL:
        raise ValueError("OpenCode v1 supports openai-compatible providers only")

    selected = find_provider_model(provider, model_id)
    env_name = provider_env_var_name(provider["id"])
    model_arg = f"{provider['id']}/{model_id}"

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
        "model": model_arg,
        "provider": {
            provider["id"]: {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider.get("name") or provider["id"],
                "options": {
                    "baseURL": provider["base_url"],
                    "apiKey": f"{{env:{env_name}}}",
                },
                "models": models,
            }
        },
        "autoupdate": False,
    }
    if small_model:
        config["small_model"] = small_model

    env = {
        "AGENT_RUNTIME": TemplateValue(value="opencode"),
        "AGENT_RUNTIME_MODEL": TemplateValue(value=model_arg),
        "OPENCODE_DISABLE_MODELS_FETCH": TemplateValue(value="true"),
        "OPENCODE_CONFIG_CONTENT": TemplateValue(value=json.dumps(config, sort_keys=True)),
        env_name: TemplateValue(secret_ref=_secret_ref(provider)),
    }
    return RuntimeTemplate(
        runtime="opencode",
        model_arg=model_arg,
        env=env,
        config_preview={"provider_id": provider["id"], "model_id": selected["id"], "config": config},
    )


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
        if provider.get("auth", {}).get("type") == "service_account":
            raise ValueError("Vertex service_account auth is not supported until credential file materialization is implemented")
        env["GOOGLE_GENAI_USE_VERTEXAI"] = TemplateValue(value="true")
        env["GOOGLE_CLOUD_PROJECT"] = TemplateValue(value=provider["project"])
        env["GOOGLE_CLOUD_LOCATION"] = TemplateValue(value=provider["location"])

    return RuntimeTemplate(
        runtime="gemini-cli",
        model_arg=model_id,
        env=env,
        config_preview={"provider_id": provider["id"], "model_id": model_id},
    )


def build_runtime_template(runtime: str, provider: dict[str, Any], model_id: str) -> RuntimeTemplate:
    normalized_runtime = _normalize_runtime(runtime)
    if normalized_runtime == "claude-code":
        return _build_claude(provider, model_id)
    if normalized_runtime == "opencode":
        return _build_opencode(provider, model_id)
    if normalized_runtime == "gemini-cli":
        return _build_gemini(provider, model_id)
    raise ValueError(f"Unsupported runtime: {runtime}")
