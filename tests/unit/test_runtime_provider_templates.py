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
        "vertex-service-account": {
            "name": "Vertex Service Account",
            "protocol": "google-vertex",
            "project": "project-id",
            "location": "us-central1",
            "auth": {"type": "service_account", "credential_ref": "secret://vertex-sa"},
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


def test_claude_template_with_haiku_alias_sets_subagent_model():
    template = build_runtime_template("claude-code", _providers()["deepseek-anthropic"], "deepseek-v4-pro")
    assert template.env["CLAUDE_CODE_SUBAGENT_MODEL"].value == "haiku"


def test_claude_template_without_haiku_alias_omits_subagent_model():
    template = build_runtime_template("claude-code", _providers()["openrouter-anthropic"], "anthropic/claude-sonnet-4.5")
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in template.env


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


def test_opencode_redacted_dict_and_config_preview_do_not_contain_raw_secret():
    template = build_runtime_template("opencode", _providers()["deepseek-openai"], "deepseek-v4-pro")
    redacted = json.dumps(template.redacted_dict())
    preview = json.dumps(template.config_preview)
    assert "sk-openai-secret" not in redacted
    assert "sk-openai-secret" not in preview
    assert "{env:TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY}" in redacted


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


def test_gemini_template_rejects_vertex_service_account_until_materialization_exists():
    with pytest.raises(ValueError, match="Vertex service_account auth is not supported"):
        build_runtime_template("gemini-cli", _providers()["vertex-service-account"], "gemini-2.5-pro")


def test_invalid_runtime_provider_combination_rejected():
    with pytest.raises(ValueError, match="Gemini CLI cannot use"):
        build_runtime_template("gemini-cli", _providers()["deepseek-openai"], "deepseek-v4-pro")


def test_runtime_template_materialize_env_resolves_secret_refs():
    template = build_runtime_template("opencode", _providers()["deepseek-openai"], "deepseek-v4-pro")
    env = template.materialize_env({"provider:deepseek-openai:api_key": "sk-openai-secret"})
    assert env["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"] == "sk-openai-secret"
    assert "sk-openai-secret" not in env["OPENCODE_CONFIG_CONTENT"]


def test_runtime_template_materialize_env_missing_secret_ref_raises_clear_error():
    template = build_runtime_template("opencode", _providers()["deepseek-openai"], "deepseek-v4-pro")

    with pytest.raises(ValueError, match="Missing secret for env var TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY.*provider:deepseek-openai:api_key"):
        template.materialize_env({})


@pytest.mark.parametrize(
    ("runtime", "provider_id", "model_id", "raw_secret"),
    [
        ("claude-code", "deepseek-anthropic", "deepseek-v4-pro", "sk-anthropic-secret"),
        ("opencode", "deepseek-openai", "deepseek-v4-pro", "sk-openai-secret"),
        ("gemini-cli", "google-ai-studio", "gemini-2.5-pro", "google-secret"),
    ],
)
def test_redacted_dict_never_contains_known_raw_fake_secrets(runtime, provider_id, model_id, raw_secret):
    template = build_runtime_template(runtime, _providers()[provider_id], model_id)
    assert raw_secret not in json.dumps(template.redacted_dict())
