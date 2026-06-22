# Provider-Aware Runtime Default Models Design

Date: 2026-06-20

## Goal

Trinity should let users configure default models per runtime, including the provider that owns each model. This fixes the current Settings behavior where model choices are Claude-only even though agents can now run with Claude Code, Gemini CLI, or OpenCode.

The first implementation should cover Settings defaults and execution fallback. It should not attempt to discover live model lists from provider APIs.

## Current Behavior

- `Settings.vue` shows a hardcoded Claude-only platform default model selector.
- `ModelSelector.vue` has a hardcoded Claude preset list and is not runtime-aware.
- Backend settings expose and store `platform_default_model`, but not per-runtime model defaults.
- Backend execution already has some runtime-specific fallback logic, but it cannot read a provider-aware default model map from settings.
- OpenCode create-agent UI accepts a free-text `provider/model` value, but Settings does not expose equivalent defaults.

## Desired Behavior

Settings should expose default model configuration for all supported runtimes:

1. Claude Code
2. Gemini CLI
3. OpenCode

Each runtime default should include:

- `provider`
- `model`
- a visible resolved value, formatted as `provider/model`

Users should be able to choose common provider/model presets or enter custom values. All three runtimes should support provider selection in the UI, even if their underlying CLIs may interpret provider-aware strings differently.

## Settings UI

Add or replace the existing model setting section with a `Default Models` section.

For each runtime, render:

```text
<Runtime Label>
Provider: [provider select]
Model:    [model select/input]
Resolved: provider/model
Status:   [Not tested] [Test connection]
```

Provider choices:

- Anthropic (`anthropic`)
- OpenAI (`openai`)
- Google (`google`)
- Custom

Preset model examples:

- Anthropic:
  - `claude-sonnet-4-6`
  - `claude-sonnet-4-5`
  - `claude-opus-4-8`
- OpenAI:
  - `gpt-5`
  - `gpt-5-mini`
  - `gpt-4.1`
- Google:
  - `gemini-3-flash`
  - `gemini-2.5-flash`
  - `gemini-2.5-pro`

For `Custom`, show free-text provider and model inputs.

OpenCode should continue to use `provider/model` directly. Claude Code and Gemini CLI should also save provider/model metadata, but adapter behavior can decide whether to pass the full resolved value or a runtime-compatible model value.

## Provider Connectivity Testing

Settings should support manual provider connectivity checks. Connectivity checks must only run when the user clicks `Test connection`; they should not run automatically on page load.

Each runtime default row should include a `Test connection` action. The action sends the selected runtime, provider, and model to the backend. The backend performs a lightweight provider-specific request using the configured credentials and returns a normalized result. The check validates the selected provider credentials and the selected model value together; it is not merely a provider-level ping.

UI states:

- `Not tested`
- `Testing...`
- `Connected`
- `Authentication failed`
- `Model not found or unavailable`
- `Provider unreachable`
- `Timed out`
- `Unsupported provider`
- `Unknown error`

The UI should show the last result for the current row. Changing provider or model should reset that row to `Not tested` so users do not trust stale results.

Connectivity tests should not save Settings values by themselves. They test the currently selected form values, even if the user has not clicked Save yet.

### Backend Connectivity API

Add an authenticated endpoint, for example:

```text
POST /api/settings/provider-connection-test
```

Request:

```json
{
  "runtime": "opencode",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5"
}
```

Response:

```json
{
  "ok": true,
  "status": "connected",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "resolved_model": "anthropic/claude-sonnet-4-5",
  "message": "Connection verified"
}
```

Failure responses should use the same shape with `ok: false` and a stable `status` code, plus a human-readable `message`. Secrets must never be returned in messages or logs.

### Provider Test Implementations

Provider tests should use the project's existing credential sources. They should fail clearly when required credentials are missing.

Initial implementation should support:

- Anthropic
- OpenAI
- Google

Known providers must test the exact model value selected or typed by the user, including custom model names that are not in Trinity's preset lists. For example, `provider=openai` and `model=gpt-5-custom` should test whether the configured OpenAI credentials can access `gpt-5-custom`.

Custom providers should return `unsupported_provider` unless a future provider adapter is added. A custom provider cannot be tested from provider/model alone because Trinity does not know its base URL, authentication scheme, or protocol. Future support may add custom-provider fields such as `base_url`, `api_key_env`, and `protocol`.

Testing strategy per provider:

- Prefer a low-cost model metadata/list request when the provider SDK/API supports one.
- Otherwise use the smallest safe request that validates credentials and model access.
- Use a short timeout, for example 10 seconds.
- Normalize provider exceptions into the UI states above.

Connectivity checks are best-effort diagnostics. Passing a connection test does not guarantee every future generation request will succeed, and failing a test should not prevent saving Settings.

## Settings Data Model

Introduce a unified setting:

```json
runtime_default_models = {
  "claude-code": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6"
  },
  "gemini-cli": {
    "provider": "google",
    "model": "gemini-3-flash"
  },
  "opencode": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-5"
  }
}
```

Default values:

- Claude Code: `anthropic/claude-sonnet-4-6`
- Gemini CLI: `google/gemini-3-flash`
- OpenCode: `anthropic/claude-sonnet-4-5`

Retain `platform_default_model` for backward compatibility. Treat it as the legacy Claude Code default model.

Compatibility rules:

- If `runtime_default_models.claude-code` is missing, use `platform_default_model` as Claude Code's model with provider `anthropic`.
- If `runtime_default_models` is missing entirely, synthesize all defaults from the values above.
- When saving runtime defaults, also update `platform_default_model` to the Claude Code model or resolved value expected by existing code paths.

## Execution Fallback

When a task/chat/session has no explicit model:

1. Use the agent's `AGENT_RUNTIME_MODEL` if present.
2. Otherwise, use `runtime_default_models[runtime]`.
3. Otherwise, use the legacy fallback.

Resolved runtime defaults are formatted as:

```text
provider/model
```

OpenCode should receive the resolved value directly. Existing Claude Code and Gemini CLI adapter behavior must not regress; if a runtime expects a plain model name, the adapter or fallback function may use the `model` part while preserving the provider-aware setting in storage.

## Components and Boundaries

### Frontend Settings

- Owns the Settings page UI for editing all runtime defaults.
- Loads and saves `runtime_default_models` through the settings store.
- Displays a resolved `provider/model` preview for each runtime.

### Model Preset Utility

- Provide a small frontend utility or constants module for provider/model presets.
- Avoid scattering provider/model lists across `Settings.vue`, `CreateAgentModal.vue`, and `ModelSelector.vue`.

### Backend Settings Service

- Owns default value synthesis and validation for `runtime_default_models`.
- Maintains legacy compatibility with `platform_default_model`.

### Provider Connection Test Service

- Owns provider-specific connectivity checks.
- Normalizes provider errors into stable statuses.
- Sanitizes all provider errors before returning them to the frontend.
- Does not persist Settings values.

### Execution Service

- Resolves default model by runtime when model is omitted.
- Preserves agent-specific `AGENT_RUNTIME_MODEL` priority.

## Validation Rules

- Runtime keys must be supported runtime names: `claude-code`, `gemini-cli`, `opencode`.
- Provider must be non-empty and contain no whitespace or slash.
- Model must be non-empty and contain no whitespace.
- Resolved value must be exactly `provider/model`.
- Invalid or missing entries should fall back to safe defaults rather than breaking Settings load.
- Settings save should reject malformed entries with a clear 400 error.
- Connectivity test requests should use the same provider/model validation before contacting external services.

## Testing

Backend tests:

- Settings service returns synthesized defaults when no setting exists.
- Legacy `platform_default_model` populates Claude Code default when runtime map is missing.
- Invalid runtime default entries are rejected on save.
- Task execution fallback picks runtime defaults for Claude Code, Gemini CLI, and OpenCode.
- Agent `AGENT_RUNTIME_MODEL` still overrides runtime defaults.
- Provider connection test endpoint validates request shape.
- Provider connection test endpoint maps missing credentials, auth failures, unavailable models, timeouts, and unsupported providers to stable statuses.
- Provider connection test responses do not leak API keys or raw credential values.

Frontend verification:

- Settings renders three runtime default model rows.
- Provider selection updates available presets and resolved value.
- Custom provider/model values are preserved.
- Changing provider/model resets the row's connectivity status to `Not tested`.
- Clicking `Test connection` calls the backend with current unsaved form values and renders the returned status.
- Save payload writes `runtime_default_models` and does not remove existing unrelated settings.
- Production build succeeds.

## Non-Goals

- Live provider model discovery.
- Provider credential management in Settings.
- Changing OpenCode's provider authentication behavior.
- Full rewrite of Chat/Tasks/Schedules model picker. These can later reuse the provider-aware presets, but this feature targets Settings defaults and execution fallback first.
- Automatically testing providers on page load.
- Blocking Settings saves when connectivity tests fail.

## Rollout

1. Add backend settings support and tests.
2. Add backend provider connectivity test endpoint and tests.
3. Add frontend provider-aware Settings UI with manual `Test connection` actions.
4. Wire execution fallback to the new runtime defaults.
5. Preserve existing `platform_default_model` behavior for compatibility.
6. Deploy to `ubuntu-server` and verify Settings plus an OpenCode runtime smoke check.
