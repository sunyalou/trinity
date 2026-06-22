# OpenAI-Compatible Custom Provider Settings Design

Date: 2026-06-20

## Goal

Trinity Settings should support testing and saving custom provider connection details for providers that expose an OpenAI-compatible API. This fixes the current behavior where a custom provider can be selected as a runtime default but `Test connection` returns `unsupported_provider` because Trinity only has `provider + model` and lacks base URL, credentials, and protocol information.

## Scope

This first version supports custom providers with an OpenAI-compatible protocol only.

Supported:

- Custom provider name, for example `my-provider`.
- Base URL, for example `https://api.example.com/v1`.
- Bearer API key.
- Model name.
- Manual connection testing from Settings.
- Saving custom provider connection config for reuse.

Not supported in this version:

- Anthropic-compatible custom providers.
- Google-compatible custom providers.
- Arbitrary authentication schemes.
- Per-runtime custom provider protocols.
- Automatic runtime injection into OpenCode provider config beyond preserving the selected `provider/model` runtime default.

## Current Behavior

- `runtime_default_models` stores only `{ provider, model }` per runtime.
- Settings can enter a custom provider string and model.
- `POST /api/settings/provider-connection-test` accepts only `runtime`, `provider`, and `model`.
- `provider_connection_test_service` returns `unsupported_provider` for non-built-in providers.
- There is no storage for custom provider `base_url`, `api_key`, or `protocol`.
- Generic Settings endpoints currently expose raw `system_settings` values, so storing custom provider API keys in `system_settings.custom_provider_configs` also requires a redaction/blocking guard for generic Settings routes.

## Desired Behavior

When a Settings runtime row uses a custom provider, the UI should allow configuring OpenAI-compatible connection details:

```text
Provider name: my-provider
Protocol:      OpenAI-compatible
Base URL:      https://api.example.com/v1
API Key:       sk-...
Model:         my-model
Status:        Not tested [Test connection]
```

Users should be able to:

1. Save the runtime default as `{ provider: "my-provider", model: "my-model" }`.
2. Save the custom provider config separately.
3. Test connection using unsaved form values.
4. Test connection using saved custom provider config when the current form does not include a new API key.

Connection test failures should not block saving runtime defaults or provider configs.

## Data Model

Keep the existing runtime default model format unchanged:

```json
runtime_default_models = {
  "opencode": {
    "provider": "my-provider",
    "model": "my-model"
  }
}
```

Add a new system setting:

```json
custom_provider_configs = {
  "my-provider": {
    "protocol": "openai-compatible",
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-secret"
  }
}
```

Rationale:

- Runtime defaults answer “which provider/model should this runtime use?”
- Custom provider configs answer “how does Trinity connect to this provider?”
- Separating them avoids putting secrets inside runtime defaults and preserves existing fallback behavior.

## Backend API

### Get Custom Provider Configs

Add an admin-only endpoint:

```text
GET /api/settings/custom-provider-configs
```

Response must not include raw API keys:

```json
{
  "custom_provider_configs": {
    "my-provider": {
      "protocol": "openai-compatible",
      "base_url": "https://api.example.com/v1",
      "api_key_configured": true,
      "api_key_masked": "...abcd"
    }
  }
}
```

### Save Custom Provider Configs

Add an admin-only endpoint:

```text
PUT /api/settings/custom-provider-configs
```

Request:

```json
{
  "custom_provider_configs": {
    "my-provider": {
      "protocol": "openai-compatible",
      "base_url": "https://api.example.com/v1",
      "api_key": "sk-secret"
    }
  }
}
```

Save behavior:

- Validate all provider names and config values before persistence.
- Store raw API keys in `system_settings.custom_provider_configs` using the existing settings storage pattern.
- Do not return raw API keys in the response.
- Treat the PUT request as a merge/update, not as full replacement. Omitted existing providers remain saved until a future explicit delete action is added.
- If an incoming provider config omits `api_key` or sends an empty API key while an existing key is already configured, preserve the existing key.
- If an incoming provider config sends a non-empty `api_key`, replace the stored key.
- Return the same masked shape as `GET /api/settings/custom-provider-configs`.

### Generic Settings Route Protection

Because `custom_provider_configs` contains secrets but is stored in `system_settings`, generic settings routes must be hardened:

- `GET /api/settings` must not return raw `custom_provider_configs`. It should either omit that key or return the masked JSON from `get_masked_custom_provider_configs()`.
- `GET /api/settings/custom_provider_configs` must not return raw API keys. It should return a `SystemSetting`-compatible response whose `value` is the masked JSON string, or reject with a clear message pointing callers to `GET /api/settings/custom-provider-configs`.
- Generic `PUT /api/settings/custom_provider_configs` must be blocked with HTTP 400 so callers cannot bypass custom provider validation, merge semantics, masking, and audit behavior. Callers must use `PUT /api/settings/custom-provider-configs`.
- Frontend `settingsStore.fetchSettings()` should defensively drop `custom_provider_configs` if a backend regression ever returns it.

### Provider Connection Test

Extend the existing admin-only endpoint:

```text
POST /api/settings/provider-connection-test
```

Request should accept optional custom provider details:

```json
{
  "runtime": "opencode",
  "provider": "my-provider",
  "model": "my-model",
  "custom_provider": {
    "protocol": "openai-compatible",
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-secret"
  }
}
```

Resolution order for custom provider test config:

1. Use `custom_provider` values from the request when present.
2. For omitted or empty `api_key`, fall back to saved `custom_provider_configs[provider].api_key`.
3. For omitted `base_url` or `protocol`, fall back to saved config.
4. If required config is still missing, return `missing_config`.

The connection test endpoint must not persist request values.

## OpenAI-Compatible Test Logic

Normalize the base URL by trimming trailing slash.

Supported protocol value:

```text
openai-compatible
```

Test sequence:

1. Try model-specific metadata:

   ```text
   GET {base_url}/models/{url_encoded_model}
   Authorization: Bearer <api_key>
   ```

2. If the provider returns 404 for the model-specific endpoint, try model list:

   ```text
   GET {base_url}/models
   Authorization: Bearer <api_key>
   ```

   If the returned list contains the selected model id, return `connected`.

3. If model list is unavailable or does not contain the model, make a minimal chat completion request:

   ```text
   POST {base_url}/chat/completions
   Authorization: Bearer <api_key>
   Content-Type: application/json

   {
     "model": "my-model",
     "messages": [{"role": "user", "content": "ping"}],
     "max_tokens": 1
   }
   ```

Status mapping:

- HTTP 200 from model endpoint: `connected`
- HTTP 200 from model list containing model: `connected`
- HTTP 200 from chat completion: `connected`
- HTTP 401 or 403: `authentication_failed`
- HTTP 404 for all model checks or explicit model error from chat: `model_not_found`
- HTTP 408, 429, 500, 502, 503, 504: `provider_unreachable`
- timeout: `timed_out`
- missing base URL/API key/protocol: `missing_config`
- unsupported protocol: `unsupported_provider`
- other errors: `unknown_error`

The service should use a short timeout, matching the existing 10-second provider tests.

## Validation Rules

Provider name:

- Required.
- String.
- No `/`.
- No whitespace.

Model:

- Required.
- String.
- No whitespace.

Protocol:

- Required.
- Must be `openai-compatible`.

Base URL:

- Required.
- Must be `http://` or `https://`.
- Must not contain whitespace.
- Store without trailing slash.

API key:

- Required for first save and for connection tests.
- String.
- Must not be returned by GET endpoints or error messages.

## Frontend Settings UI

When `providerMode === __custom__`, each runtime row should show:

- Provider name input.
- Protocol display/select with only `OpenAI-compatible` in v1.
- Base URL input.
- API Key input.
- Model input.
- Resolved `provider/model` preview.
- Connection status and Test connection button.

Loading behavior:

- Load runtime defaults from `GET /api/settings/runtime-default-models`.
- Load custom provider configs from `GET /api/settings/custom-provider-configs`.
- When a runtime row has a custom provider with saved config, populate base URL/protocol and show masked API key state.
- Do not populate raw API key in the input.

Saving behavior:

- Save runtime defaults through `PUT /api/settings/runtime-default-models`.
- Save custom provider configs through `PUT /api/settings/custom-provider-configs`.
- The custom provider config PUT merges selected custom providers into the saved map and preserves omitted saved providers.
- If API key input is blank and a saved key exists, preserve the saved key.
- If API key input is non-empty, replace the saved key.
- Failed connection tests do not block save.

Status behavior:

- Changing provider, model, base URL, protocol, or API key resets row status to `Not tested`.
- Test connection sends current unsaved form values.
- In-flight stale responses must not overwrite status if values changed before the response returns.

## Security

- Never return raw API keys from GET endpoints.
- Never return raw API keys from generic settings endpoints.
- Block generic writes to `custom_provider_configs`; only the dedicated custom provider endpoint may persist this setting.
- Redact request API key and saved API key from all provider error messages.
- Do not log raw API keys.
- Do not include API key in frontend resolved preview.
- Avoid storing secrets in runtime defaults.

## Testing

Backend tests:

- Custom provider config validation rejects malformed provider names, protocols, base URLs, and missing first-save API keys.
- GET custom provider configs masks API keys.
- Generic `GET /api/settings` does not leak raw `custom_provider_configs`.
- Generic `GET /api/settings/custom_provider_configs` does not leak raw API keys or rejects access.
- Generic `PUT /api/settings/custom_provider_configs` is blocked.
- PUT custom provider configs preserves existing API key when blank is submitted.
- PUT custom provider configs replaces API key when non-empty key is submitted.
- PUT custom provider configs preserves omitted saved providers.
- PUT custom provider configs returns masked configs and does not echo submitted API keys.
- Provider connection test with request-provided custom config uses those unsaved values and does not persist them.
- Provider connection test falls back to saved custom provider config.
- Missing custom config returns `missing_config`.
- OpenAI-compatible model endpoint success returns `connected`.
- Model list fallback returns `connected` when model id is present.
- Chat completion fallback returns `connected` when model/list endpoints cannot confirm the model.
- Authentication failure, model not found, provider unreachable, timeout, unsupported protocol, and unknown errors map to stable statuses.
- Error messages do not leak request or saved API keys.
- PUT validation errors and unknown provider-test exceptions do not leak request or saved API keys.

Frontend verification:

- Custom mode renders provider/base URL/API key/model fields.
- Saved custom provider config loads masked API key state and base URL.
- Blank API key preserves saved key on save.
- Non-empty API key replaces saved key on save.
- Test connection sends unsaved base URL/API key/model values.
- Changing custom fields resets status to `Not tested`.
- Production build succeeds.

## Rollout

1. Add backend custom provider config service/helpers and tests.
2. Add custom provider config Settings endpoints and tests.
3. Extend provider connection test service for OpenAI-compatible custom providers and tests.
4. Extend frontend Settings UI/store/preset utilities.
5. Run backend regression and frontend build.
6. Deploy to `ubuntu-server` and verify Settings custom provider test from the browser.
