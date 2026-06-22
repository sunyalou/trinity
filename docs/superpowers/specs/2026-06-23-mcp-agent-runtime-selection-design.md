# MCP Agent Runtime Selection Design

## Problem

Trinity supports multiple agent runtimes in the backend (`claude-code`, `gemini-cli`, and `opencode`), including runtime-specific model/provider fields for OpenCode. The MCP server's `create_agent` tool does not expose those fields, so agents created through MCP default to Claude Code. The MCP `deploy_local_agent` tool also cannot override runtime settings for uploaded archives; it only sends `archive` and `name`.

This prevents MCP clients and existing agents from creating OpenCode or Gemini agents directly.

## Goals

- Let MCP clients create agents with `claude-code`, `gemini-cli`, or `opencode`.
- Support runtime model/provider configuration through MCP for parity with backend and UI capabilities.
- Let MCP `deploy_local_agent` override archive `template.yaml` runtime values when requested.
- Preserve current behavior when the new parameters are omitted.
- Keep business validation authoritative in the backend; MCP should expose schema and pass values through.

## Non-goals

- Add a new runtime.
- Change UI behavior.
- Change provider configuration storage or provider connection testing.
- Make deploy-local asynchronous.
- Change existing defaults for agents created without runtime parameters.

## Public MCP API Changes

### `create_agent`

Add optional parameters:

- `runtime`: enum of `claude-code`, `gemini-cli`, `opencode`
- `runtime_model`: string, optional runtime-specific model override
- `runtime_provider_id`: string, optional provider identifier for template-based runtime provider injection
- `runtime_model_id`: string, optional provider model identifier for template-based runtime provider injection
- `runtime_permission`: enum of `restricted`, `standard`, `dangerous`; mostly relevant to OpenCode

The tool forwards these fields to `POST /api/agents` when present.

Example:

```json
{
  "name": "deepseek-worker",
  "template": "github:sunyalou/some-agent",
  "runtime": "opencode",
  "runtime_provider_id": "deepseek-openai",
  "runtime_model_id": "deepseek-v4-flash",
  "runtime_permission": "restricted"
}
```

### `deploy_local_agent`

Add the same optional parameters:

- `runtime`
- `runtime_model`
- `runtime_provider_id`
- `runtime_model_id`
- `runtime_permission`

The tool forwards these fields to `POST /api/agents/deploy-local` when present.

Example:

```json
{
  "archive": "<base64-tar-gz>",
  "name": "local-opencode-agent",
  "runtime": "opencode",
  "runtime_provider_id": "deepseek-openai",
  "runtime_model_id": "deepseek-v4-flash",
  "runtime_permission": "standard"
}
```

## Backend API Changes

Extend `DeployLocalRequest` with optional fields matching `AgentConfig`:

- `runtime`
- `runtime_model`
- `runtime_provider_id`
- `runtime_model_id`
- `runtime_permission`

Use existing validators for `runtime` and `runtime_permission` so deploy-local accepts the same values as normal agent creation.

## Deploy-local Runtime Resolution

Deploy-local currently reads runtime settings from `template.yaml`:

```yaml
runtime:
  type: opencode
  model: deepseek-openai/deepseek-v4-flash
  permission: restricted
```

After this change, runtime resolution order is:

1. Explicit deploy-local request fields
2. `template.yaml` runtime fields
3. Existing backend defaults (`claude-code` when omitted)

This means MCP callers can override an archive's runtime without modifying the archive.

Provider/model pair handling:

- If either `runtime_provider_id` or `runtime_model_id` is provided, both must be provided.
- The backend remains responsible for rejecting invalid provider/model combinations.
- MCP does not duplicate backend provider registry logic.

## Validation and Error Handling

- MCP schema restricts `runtime` and `runtime_permission` to known values.
- Backend validates the same fields again for API safety.
- Backend returns existing structured validation errors for unsupported runtimes or incomplete provider/model pairs.
- No secrets are logged by MCP or backend when forwarding provider identifiers.

## Compatibility

- Existing MCP calls without new fields continue to create Claude Code agents.
- Existing archive templates continue to work.
- Existing `template.yaml` runtime settings continue to work.
- Old MCP clients can ignore the new schema fields.

## Testing Plan

Backend unit tests:

- `DeployLocalRequest` accepts runtime fields.
- deploy-local request fields override `template.yaml` runtime fields.
- deploy-local preserves `template.yaml` runtime when no request override is provided.
- incomplete `runtime_provider_id` / `runtime_model_id` pair is rejected by existing backend validation.

MCP server tests or script-level tests:

- `create_agent` schema exposes runtime fields.
- `create_agent` forwards runtime fields to the backend client.
- `deploy_local_agent` schema exposes runtime fields.
- `deploy_local_agent` forwards runtime fields to `/api/agents/deploy-local`.

Deployment verification:

- Create an OpenCode agent via MCP with `runtime_provider_id=deepseek-openai` and `runtime_model_id=deepseek-v4-flash`.
- Verify the created container labels/env show runtime `opencode` and selected provider/model.
- Create a Gemini agent via MCP with `runtime=gemini-cli`.
- Verify normal Claude Code creation still works without runtime parameters.

## Implementation Boundaries

- MCP changes live in `src/mcp-server/src/tools/agents.ts` and type definitions if needed.
- Backend request model changes live in `src/backend/models.py`.
- deploy-local resolution changes live in `src/backend/services/agent_service/deploy.py`.
- Do not refactor unrelated agent creation code.
- Do not change UI Settings or model selector behavior in this task.
