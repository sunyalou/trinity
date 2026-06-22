# Runtime-Aware Provider and Template Design

Date: 2026-06-21

## Summary

Trinity should manage LLM providers and models in Settings, but compile those provider/model selections into runtime-specific launch templates when creating or rebuilding agents.

Runtime templates are the complete launch/config bundle for an agent tool. They cover provider/model configuration, command arguments, MCP wiring, permission mode, workspace/instruction settings, secret references, and redacted previews. They are not limited to model environment variables.

The current generic `runtime_default_models = { runtime: { provider, model } }` model is too weak. Claude Code, Gemini CLI, and OpenCode do not consume provider/model settings the same way:

- Claude Code needs Anthropic Messages-compatible environment variables and alias mapping.
- Gemini CLI needs Google Gemini, Vertex AI, or Google GenAI-compatible gateway settings.
- OpenCode needs generated OpenCode configuration, usually through `OPENCODE_CONFIG_CONTENT`, plus secret environment variables.

The product should expose a simple user model:

1. Create providers in Settings.
2. Add models under each provider.
3. Select runtime + provider + model when creating an agent.
4. Trinity validates compatibility and generates the correct runtime template automatically.

Users should not manually write Claude Code, Gemini CLI, or OpenCode config files for normal operation.

## Goals

- Make invalid runtime/provider combinations impossible in the UI and rejected by the backend.
- Let users configure custom base URLs, API keys, auth modes, and model lists once in Settings.
- Generate runtime-specific env/config templates automatically during agent creation and rebuild.
- Preserve secrets securely: never return raw API keys and never embed raw keys in generated config files when an env reference can be used.
- Support DeepSeek cleanly for both Claude Code via Anthropic Messages-compatible mode and OpenCode via OpenAI-compatible mode.
- Stop using backend-only connection tests as proof that an agent runtime can execute.

## Non-Goals

- Do not make Gemini CLI pretend to support OpenAI-compatible or Anthropic-compatible providers.
- Do not require users to manually edit `opencode.json`, Claude settings, Gemini settings, or env files.
- Do not create a universal provider abstraction that hides runtime differences.
- Do not store generated runtime configs as the primary source of truth.
- Do not persist raw generated launch environments that contain secrets. Persist provider/model selections and secret references; materialize raw secrets only at container launch/preflight time.

## Runtime Capability Boundaries

### Claude Code

Claude Code supports Anthropic Messages-compatible providers and gateways.

Compatible provider protocol:

- `anthropic-messages`

Unsupported provider protocols:

- `openai-compatible`
- `google-gemini`
- `google-vertex`

Claude Code runtime templates should use:

- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `ANTHROPIC_DEFAULT_FABLE_MODEL`
- `CLAUDE_CODE_SUBAGENT_MODEL`
- `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=0` by default

Claude model selection should prefer Claude Code aliases (`sonnet`, `opus`, `haiku`) mapped to provider-specific model IDs through `ANTHROPIC_DEFAULT_*_MODEL`.

### OpenCode

OpenCode supports generated provider configuration through `OPENCODE_CONFIG_CONTENT`.

Compatible provider protocols for v1:

- `openai-compatible`

Planned later, not v1:

- `anthropic-messages` through OpenCode's Anthropic provider configuration

OpenCode can technically represent Anthropic providers, but v1 will not expose this combination until Trinity has a tested template for OpenCode's Anthropic provider shape. This prevents a selectable but nonfunctional path.

OpenCode runtime templates should use:

- `OPENCODE_CONFIG_CONTENT`
- `OPENCODE_DISABLE_MODELS_FETCH=true`
- runtime-scoped secret env vars such as `TRINITY_PROVIDER_DEEPSEEK_API_KEY`

The generated OpenCode model string should be:

```text
provider_id/model_id
```

For OpenAI-compatible providers, generated config should use:

```json
"npm": "@ai-sdk/openai-compatible"
```

### Gemini CLI

Gemini CLI supports Google Gemini, Vertex AI, and Google GenAI-compatible gateways.

Compatible provider protocols:

- `google-gemini`
- `google-vertex`
- `google-protocol-gateway` only after runtime preflight verifies the configured endpoint speaks the Google GenAI protocol

Google protocol gateway providers should be visible as disabled/pending until both provider connectivity and Gemini runtime preflight pass. They must not be selectable for agent creation while unverified.

Unsupported provider protocols:

- `openai-compatible`
- `anthropic-messages`

Gemini runtime templates should use:

- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `GOOGLE_GENAI_USE_VERTEXAI`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GOOGLE_GEMINI_BASE_URL` only for Google GenAI-compatible gateways

Gemini CLI model and auth mapping verified for this design:

- Google AI Studio/API key mode: `GEMINI_API_KEY`, `GEMINI_MODEL`, and/or `--model`.
- Vertex mode: `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, optional `GOOGLE_APPLICATION_CREDENTIALS`, and `GEMINI_MODEL` and/or `--model`.
- Google protocol gateway mode: `GOOGLE_GEMINI_BASE_URL`, `GEMINI_API_KEY`, and `GEMINI_MODEL` and/or `--model`.
- CLI `--model` takes precedence over `GEMINI_MODEL`; Trinity may set both only when they agree.

`GOOGLE_GEMINI_BASE_URL` is not an OpenAI/Anthropic compatibility switch. It is valid only for gateways that implement the Google GenAI/Gemini API expected by Gemini CLI.

## Provider Settings Model

Settings should expose a Providers section.

Each provider has:

```json
{
  "id": "deepseek-anthropic",
  "name": "DeepSeek Anthropic",
  "protocol": "anthropic-messages",
  "base_url": "https://api.deepseek.com/anthropic",
  "auth": {
    "type": "api_key",
    "header_mode": "x-api-key",
    "secret_ref": "provider:deepseek-anthropic:api_key"
  },
  "models": [
    {
      "id": "deepseek-v4-pro",
      "label": "DeepSeek V4 Pro",
      "role": "balanced",
      "claude_alias": "sonnet",
      "tool_call": true,
      "context": 128000,
      "output": 8192
    },
    {
      "id": "deepseek-v4-flash",
      "label": "DeepSeek V4 Flash",
      "role": "fast",
      "claude_alias": "haiku",
      "tool_call": true
    }
  ]
}
```

Protocol-specific forms should be used instead of one generic form.

A provider record represents one protocol endpoint. A vendor can have multiple provider records. For example, DeepSeek may appear as both `deepseek-anthropic` for an Anthropic Messages-compatible endpoint and `deepseek-openai` for an OpenAI-compatible endpoint. This is intentional because the runtime templates and compatibility rules differ.

Provider lifecycle rules:

- Provider IDs are immutable after creation. Display names can be renamed.
- Provider IDs must be globally unique after normalization.
- V1 deletion rule: deleting a provider that is referenced by agents, runtime defaults, schedules, or saved task/chat overrides is blocked until the user first repairs or migrates those references.
- V1 deletion rule: deleting a model that is referenced by agents, runtime defaults, schedules, or saved task/chat overrides is blocked until the user first repairs or migrates those references.
- API key rotation updates the provider secret reference value; affected agents use the new key after rebuild/restart/preflight.
- Duplicate model IDs are rejected within one provider.
- Duplicate Claude alias slots are rejected within one Anthropic Messages provider unless the user explicitly marks only one model as the default for that alias.
- Base URLs are normalized by trimming whitespace and removing trailing slashes, but path prefixes such as `/anthropic` or `/v1` are preserved.
- Provider export/import must redact secrets and preserve only secret presence metadata.

### Anthropic Messages Provider Fields

- Name
- Base URL
- Auth mode: `x-api-key` or `bearer`
- API key/token
- Models
  - model ID
  - display label
  - optional Claude alias slot: `sonnet`, `opus`, `haiku`, or `fable`

Custom Claude model options are hidden in v1. They can be added later through explicit support for Claude Code's `ANTHROPIC_CUSTOM_MODEL_OPTION` variables.

### OpenAI-Compatible Provider Fields

- Name
- Base URL
- API key
- Optional headers later, not required for the first version
- Models
  - model ID
  - display label
  - tool-call support
  - context limit
  - output limit

### Google Gemini Provider Fields

- Name
- API key
- Models
  - model ID
  - display label

### Google Vertex Provider Fields

- Name
- project
- location
- auth mode: ADC, service account file, or API key if supported by deployment
- Models
  - model ID
  - display label

### Google GenAI-Compatible Gateway Provider Fields

- Name
- Base URL
- API key or gateway token
- Models
  - model ID
  - display label

This protocol is accepted only if runtime preflight verifies the gateway works with Gemini CLI's Google GenAI request shape. It must not be used for OpenAI-compatible or Anthropic Messages-compatible endpoints.

## Agent Model Selection

Agent creation should ask for:

```json
{
  "runtime": "claude-code",
  "provider_id": "deepseek-anthropic",
  "model_id": "deepseek-v4-pro"
}
```

The UI must filter providers and models by runtime:

- Claude Code shows only `anthropic-messages` providers.
- OpenCode v1 shows `openai-compatible` providers. Anthropic Messages providers remain hidden until a tested OpenCode Anthropic template is added.
- Gemini CLI shows only Google-compatible providers.

The backend must enforce the same compatibility checks.

The selected runtime/provider/model tuple should be persisted on the agent. Generated runtime config should not be the source of truth; it should be rebuilt from provider settings and secrets when creating or rebuilding containers.

The persisted source of truth is always:

```json
{
  "runtime": "claude-code",
  "provider_id": "deepseek-anthropic",
  "model_id": "deepseek-v4-pro"
}
```

Runtime-specific launch tokens are computed metadata, not user-authored source of truth. They may be returned by APIs for display or debugging, but they should be derived from the persisted tuple and provider metadata.

## V1 Support Matrix

| Runtime | Provider protocol | V1 status |
|---|---|---|
| Claude Code | `anthropic-messages` | Supported |
| Claude Code | `openai-compatible` | Unsupported |
| Claude Code | `google-gemini` / `google-vertex` | Unsupported |
| OpenCode | `openai-compatible` | Supported |
| OpenCode | `anthropic-messages` | Not exposed in v1; add only after template support lands |
| Gemini CLI | `google-gemini` | Supported |
| Gemini CLI | `google-vertex` | Supported after exact env/auth preflight succeeds |
| Gemini CLI | `google-protocol-gateway` | Supported only after exact Google GenAI gateway preflight succeeds |
| Gemini CLI | `openai-compatible` / `anthropic-messages` | Unsupported |

## Runtime Templates

Runtime templates produce a launch contract, not a persisted raw env blob.

```json
{
  "runtime": "opencode",
  "command": ["opencode", "run", "--model", "deepseek-openai/deepseek-v4-pro"],
  "env": {
    "OPENCODE_CONFIG_CONTENT": {
      "kind": "generated_config",
      "redacted_preview": "{...}"
    },
    "TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY": {
      "kind": "secret_ref",
      "ref": "provider:deepseek-openai:api_key"
    }
  },
  "files": [],
  "mcp": {
    "kind": "runtime_specific",
    "included": true
  },
  "permissions": {
    "profile": "restricted"
  },
  "workspace": {
    "dir": "/workspace"
  },
  "preview": {
    "redacted": true
  }
}
```

The raw secret value is resolved only when the backend creates a container or runs a preflight command. Template records, previews, logs, and API responses contain `secret_ref`s or redacted values only.

Template builders own the full agent-tool launch surface:

- provider/model env or generated config
- CLI command and model argument
- MCP config representation for the runtime
- permission profile/config
- workspace directory and instruction paths
- runtime-specific settings files if needed later
- redacted preview

### Claude Code Template

Input:

```json
{
  "runtime": "claude-code",
  "provider_id": "deepseek-anthropic",
  "model_id": "deepseek-v4-pro"
}
```

Generated environment for x-api-key auth:

```env
AGENT_RUNTIME=claude-code
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=<secret_ref:provider:deepseek-anthropic:api_key resolved at launch>
ANTHROPIC_MODEL=sonnet
ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro
ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
CLAUDE_CODE_SUBAGENT_MODEL=haiku
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=0
```

For bearer auth:

```env
ANTHROPIC_AUTH_TOKEN=<secret_ref:provider:deepseek-anthropic:api_key resolved at launch>
```

Unused inherited Anthropic auth variables should be explicitly cleared only when required to avoid a known conflict, such as bearer-token gateways where `ANTHROPIC_API_KEY` would otherwise override or confuse auth. Otherwise the template should omit unused auth env vars from the generated container env.

The command should use the alias:

```bash
claude --model sonnet
```

not the provider-specific full model ID, unless the selection is explicitly a custom direct model token.

Claude alias handling rules:

- `sonnet`, `opus`, `haiku`, and `fable` are supported alias slots.
- Alias slots map to `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, and `ANTHROPIC_DEFAULT_FABLE_MODEL`.
- If a selected model has no alias slot, v1 marks it unsupported for Claude Code unless the user enables a later custom-direct-model mode.
- `ANTHROPIC_CUSTOM_MODEL_OPTION` and its display/capability companion vars are out of scope for v1 and should not appear in the UI until implemented.
- If no `haiku` model is configured, `CLAUDE_CODE_SUBAGENT_MODEL` should fall back to the selected alias or be omitted. It must not point at an unconfigured alias.
- Trinity should choose either command flag or env as source of model truth. For v1, command flag wins: `claude --model <alias>`. `ANTHROPIC_MODEL` may be omitted unless direct agent startup paths require it.

Claude template also owns:

- MCP config file or command flag generation for Claude Code.
- Runtime permission mode.
- Workspace directory.
- System/instruction prompt injection strategy.

### OpenCode Template

Input:

```json
{
  "runtime": "opencode",
  "provider_id": "deepseek-openai",
  "model_id": "deepseek-v4-pro"
}
```

Generated environment:

```env
AGENT_RUNTIME=opencode
TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY=<secret_ref:provider:deepseek-openai:api_key resolved at launch>
OPENCODE_DISABLE_MODELS_FETCH=true
OPENCODE_CONFIG_CONTENT=<generated json>
```

Generated `OPENCODE_CONFIG_CONTENT` for an OpenAI-compatible provider:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["deepseek-openai"],
  "model": "deepseek-openai/deepseek-v4-pro",
  "small_model": "deepseek-openai/deepseek-v4-flash",
  "provider": {
    "deepseek-openai": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek",
      "options": {
        "baseURL": "https://api.deepseek.com/v1",
        "apiKey": "{env:TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY}"
      },
      "models": {
        "deepseek-v4-pro": {
          "name": "DeepSeek V4 Pro",
          "tool_call": true,
          "limit": {
            "context": 128000,
            "output": 8192
          }
        },
        "deepseek-v4-flash": {
          "name": "DeepSeek V4 Flash",
          "tool_call": true
        }
      }
    }
  },
  "permission": {
    "*": "ask",
    "read": "allow",
    "edit": "ask",
    "bash": {
      "*": "ask",
      "git status*": "allow",
      "git diff*": "allow",
      "rm *": "deny",
      "sudo *": "deny"
    }
  },
  "autoupdate": false
}
```

The command should use:

```bash
opencode run --model deepseek-openai/deepseek-v4-pro
```

OpenCode permissions are not provider settings. The template builder composes provider/model config with the agent's selected OpenCode permission profile (`restricted`, `standard`, or `dangerous`). Provider records must not own the permission policy.

OpenCode template also owns:

- `OPENCODE_CONFIG_CONTENT` generation.
- MCP block generation inside the OpenCode config.
- Permission config generation from the selected permission profile.
- Workspace directory and command flags.

### Gemini CLI Template

Input:

```json
{
  "runtime": "gemini-cli",
  "provider_id": "google-ai-studio",
  "model_id": "gemini-2.5-pro"
}
```

Generated environment:

```env
AGENT_RUNTIME=gemini-cli
GEMINI_API_KEY=<secret_ref:provider:google-ai-studio:api_key resolved at launch>
GEMINI_MODEL=gemini-2.5-pro
```

For Vertex:

```env
AGENT_RUNTIME=gemini-cli
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=my-project
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=<secret_ref or mounted credential file path resolved at launch>
GEMINI_MODEL=gemini-2.5-pro
```

For a Google GenAI-compatible gateway:

```env
AGENT_RUNTIME=gemini-cli
GOOGLE_GEMINI_BASE_URL=https://gateway.example.com
GEMINI_API_KEY=<secret_ref:provider:gateway:api_key resolved at launch>
GEMINI_MODEL=gemini-2.5-pro
```

Gemini template also owns:

- Gemini CLI command flags and `--model` precedence.
- Gemini settings file generation if needed later.
- MCP wiring strategy supported by Trinity's Gemini runtime.
- Workspace directory and non-interactive prompt mode.

## Runtime Template Builder

Runtime template generation should be centralized.

```python
def build_runtime_template(runtime, provider, model):
    if runtime == "claude-code":
        return ClaudeCodeTemplate.build(provider, model)
    if runtime == "opencode":
        return OpenCodeTemplate.build(provider, model)
    if runtime == "gemini-cli":
        return GeminiCliTemplate.build(provider, model)
    raise UnsupportedRuntimeError(runtime)
```

Each template owns its compatibility checks:

```python
ClaudeCodeTemplate.supports(provider):
    return provider.protocol == "anthropic-messages"

OpenCodeTemplate.supports(provider):
    return provider.protocol == "openai-compatible"  # v1

GeminiCliTemplate.supports(provider):
    return provider.protocol in ["google-gemini", "google-vertex", "google-protocol-gateway"]
```

Unsupported combinations should produce explicit errors, for example:

```text
Claude Code cannot use OpenAI-compatible providers. Use an Anthropic Messages-compatible provider or switch the agent runtime to OpenCode.
```

Runtime template builders must also validate provider completeness before launch:

- required secret exists
- selected model exists under the provider
- auth mode is valid for the protocol
- base URL is present where required
- runtime permission profile is valid
- provider env var names are deterministic and collision-free

Provider env var names use sanitized provider IDs:

```text
TRINITY_PROVIDER_<SANITIZED_PROVIDER_ID>_API_KEY
```

Sanitization uppercases ASCII letters and replaces every non-alphanumeric character with `_`. If two provider IDs normalize to the same env var stem, the backend must reject the second provider ID or append a stable short hash; silent collisions are forbidden.

## Model Selection in Chat, Tasks, and Schedules

Model selectors must be runtime-aware.

They should not return a generic string. The primary API input should be the provider/model tuple:

```json
{
  "provider_id": "deepseek-anthropic",
  "model_id": "deepseek-v4-pro"
}
```

The backend derives runtime-specific launch tokens from the current agent runtime, provider protocol, and model metadata. Derived tokens are response/debug metadata, not the persisted source of truth.

For Claude Code:

```json
{
  "provider_id": "deepseek-anthropic",
  "model_id": "deepseek-v4-pro",
  "token": {
    "kind": "claude-alias",
    "alias": "sonnet"
  }
}
```

The UI may display the derived token so users can understand that `DeepSeek V4 Pro` will run through the Claude `sonnet` alias, but the persisted value remains `provider_id + model_id`.

For OpenCode:

```json
{
  "provider_id": "deepseek-openai",
  "model_id": "deepseek-v4-pro",
  "token": {
    "kind": "opencode-provider-model",
    "value": "deepseek-openai/deepseek-v4-pro"
  }
}
```

For Gemini CLI:

```json
{
  "provider_id": "google-ai-studio",
  "model_id": "gemini-2.5-pro",
  "token": {
    "kind": "gemini-model",
    "model": "gemini-2.5-pro"
  }
}
```

## Validation and Testing UX

Connection testing should have two layers.

### Provider Connectivity Test

Tests whether the external provider endpoint, auth, and selected model are reachable.

This does not prove that a runtime can execute with the provider.

Protocol-specific tests:

- `anthropic-messages`: test a minimal `/v1/messages` request where possible; optionally test `/v1/models` or `/v1/messages/count_tokens` only if the provider supports them.
- `openai-compatible`: test `/models/{model}`, then `/models`, then a minimal `/chat/completions` request.
- `google-gemini`: test a minimal Gemini API call with the configured model.
- `google-vertex`: validate project/location/auth and run a minimal Vertex-backed Gemini request when credentials permit.
- `google-protocol-gateway`: run the same minimal Google GenAI request against the custom base URL.

Unsupported or unimplemented protocol tests must return `unsupported` rather than a false “connected” state.

### Runtime Template Preflight

Tests whether Trinity can compile and launch the runtime with the selected provider/model.

Examples:

```bash
claude --model sonnet --print "Reply OK"
opencode run --model deepseek-openai/deepseek-v4-pro "Reply OK"
gemini --model gemini-2.5-pro -p "Reply OK"
```

Preflight guardrails:

- Use a strict timeout.
- Use a minimal safe prompt.
- Run in an isolated temporary workspace, not the user's agent workspace.
- Disable tools or use read-only/no-op permissions where the runtime supports it.
- Do not trigger scheduled tasks, chat sessions, or agent state mutation.
- Sanitize stdout/stderr before storing or returning results.
- Rate-limit preflight calls to avoid accidental cost spikes.
- Show a cost warning when a preflight may call a paid provider.
- For OpenCode, avoid an interactive `ask` permission profile during preflight; use a non-interactive safe profile that cannot hang waiting for approval.

The UI should show both statuses separately:

```text
Provider connectivity: Connected
Claude Code preflight: Passed
OpenCode preflight: Unsupported / Passed / Failed
Gemini CLI preflight: Unsupported
```

## Secrets

- Raw secrets must never be returned by API responses.
- Provider GET responses should return only `api_key_configured` and a masked value.
- Template generation should use raw secrets only at launch time through a secret resolver.
- Persisted templates and provider records store `secret_ref`s, not raw secret values.
- Docker/container env may contain raw secrets at runtime because the CLI requires them, but those values must be redacted from API responses, logs, previews, audit payloads, and error messages.
- OpenCode generated config should reference secrets through env interpolation such as `{env:TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY}`.
- Logs and previews must use redacted env/config.

## Generated Config Preview

An advanced preview can show the generated runtime template with secrets redacted.

Example Claude preview:

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=***
ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro
```

Example OpenCode preview:

```json
{
  "model": "deepseek-openai/deepseek-v4-pro",
  "provider": {
    "deepseek-openai": {
      "options": {
        "apiKey": "{env:TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY}"
      }
    }
  }
}
```

Generated previews are produced on demand from provider/model selections and runtime options. They are never saved as authoritative config blobs.

## Repair-State UX

Any migrated, deleted, or invalid provider/model reference should produce an explicit repair state.

Examples:

```json
{
  "status": "model_configuration_needs_repair",
  "runtime": "claude-code",
  "previous_provider": "openrouter",
  "previous_model": "anthropic/claude-sonnet-4.5",
  "reason": "Claude Code cannot use an OpenAI-compatible provider. Choose an Anthropic Messages-compatible provider or switch runtime to OpenCode."
}
```

UI requirements:

- Agent cards and detail pages must show a clear repair badge.
- Agent rebuild should be blocked until the selection is repaired.
- Chat/task/schedule execution should fall back only when safe and explicit; otherwise it should fail with a repair message.
- Repair forms should prefill previous runtime/provider/model values where possible.

## Migration

Existing settings should migrate conservatively.

### Existing `custom_provider_configs`

Current OpenAI-compatible custom providers migrate to protocol `openai-compatible` and become usable by OpenCode only.

If such a provider is currently selected for Claude Code or Gemini CLI, migration should mark the selection incompatible and require user repair.

### Existing `runtime_default_models`

Migration examples:

- Claude Code + Anthropic model -> Claude default selection using an alias where possible.
- OpenCode + `provider/model` -> OpenCode provider/model selection if provider exists.
- Gemini CLI + Google model -> Gemini provider/model selection.
- Any unsupported combination -> invalid selection with a clear repair prompt.

Do not guess ambiguous custom mappings.

### Existing Agents

Existing agents must also be migrated or marked for repair.

Rules:

- If an existing agent has a runtime and an `AGENT_RUNTIME_MODEL` that maps cleanly to a known provider/model, store the new `runtime/provider_id/model_id` selection.
- If the provider cannot be identified, preserve the existing runtime/model as legacy metadata and mark the agent as `model_configuration_needs_repair`.
- Existing running containers are not rebuilt automatically during migration. New settings apply on explicit rebuild/recreate.
- Agent detail and create flows must surface repair state with the previous runtime/model visible.
- Schedules, task overrides, and chat model overrides that use legacy bare strings should be migrated only when unambiguous; otherwise mark the override as needing repair. Scheduled execution should block or skip the invalid override with a repair error unless the user explicitly accepts fallback to the agent default.
- Migration must never silently switch an agent from one provider protocol to another.

## Implementation Phases

### Phase 0: Verify Runtime Knobs

- Verify the exact Claude Code version, env vars, alias variables, and command flag behavior used by Trinity's base image.
- Verify the exact Gemini CLI version, `GEMINI_API_KEY`, `GEMINI_MODEL`, Vertex envs, and `GOOGLE_GEMINI_BASE_URL` behavior used by Trinity's base image.
- Verify the exact OpenCode version and generated config schema fields, including `tool_call`, `limit.context`, `limit.output`, `enabled_providers`, and `OPENCODE_CONFIG_CONTENT` behavior.
- Keep `google-protocol-gateway` providers disabled until this verification and runtime preflight pass.

### Phase 1: Stop Invalid Combinations

- Add runtime-aware provider/model filtering.
- Gemini CLI only shows Google-compatible providers. Unverified Google protocol gateway providers appear disabled/pending.
- Claude Code only shows Anthropic Messages-compatible providers.
- OpenCode shows OpenAI-compatible providers. Anthropic Messages providers remain hidden until a tested OpenCode Anthropic template is added.
- Rename current connection test to provider connectivity.

### Phase 2: Provider Settings v2 and Template Builders

- Add protocol-tagged provider configs.
- Add provider models under each provider.
- Add template builders for Claude Code, OpenCode, and Gemini CLI.
- Agent creation persists runtime/provider/model selection.
- Agent creation injects generated env/config from template builders.

### Phase 3: Runtime Preflight

- Add runtime template preflight endpoints.
- Add redacted generated template preview.
- Show provider connectivity and runtime preflight separately.

### Phase 4: Deprecate Old Abstractions

- Deprecate generic `runtime_default_models` writes.
- Deprecate OpenAI-only `custom_provider_configs`.
- Remove generic string-only ModelSelector behavior after migration.

## Acceptance Criteria

- Claude Code cannot select OpenAI-compatible providers.
- Gemini CLI cannot select DeepSeek/OpenAI/Anthropic-compatible providers unless they are explicitly configured as Google GenAI-compatible gateways.
- OpenCode can select OpenAI-compatible providers and receives valid generated `OPENCODE_CONFIG_CONTENT`.
- OpenCode does not show Anthropic Messages providers in v1 unless a tested Anthropic OpenCode template is implemented.
- DeepSeek Anthropic-compatible provider can be used by Claude Code through alias mapping.
- DeepSeek OpenAI-compatible provider can be used by OpenCode through `provider/model`.
- Settings provider test no longer implies runtime execution success.
- Runtime preflight proves that generated templates can launch the selected runtime.
- Raw API keys never appear in API responses, logs, persisted generated config, or previews.
- Runtime templates include the complete agent-tool launch bundle for each runtime: provider/model config, command arguments, MCP config, permission profile, workspace/instruction settings, secret refs, and redacted preview.
- Provider deletion or model deletion cannot silently break existing agents; references are blocked or marked for explicit repair.
