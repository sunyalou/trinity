# System Agent Runtime Provider Design

Date: 2026-06-22

## Summary

`trinity-system` is automatically ensured during backend startup by `SystemAgentService.ensure_deployed()`. Today that path creates a Claude Code system agent with hand-written container environment, independent of the normal agent runtime provider template flow.

Trinity should let operators choose the system agent runtime through deployment environment variables. The first supported target is `opencode + deepseek-openai`, using the existing Provider Config v2 data and the existing `build_runtime_template()` OpenCode template builder.

Existing `agent-trinity-system` containers must not be silently replaced by default. If an existing system agent's runtime/provider/model does not match the configured target, backend startup should log/report drift and keep the current container unless an explicit migration flag is enabled.

## Goals

- Allow `trinity-system` to be auto-created as an OpenCode agent using a configured OpenAI-compatible provider such as `deepseek-openai`.
- Configure the system agent target through environment variables, not UI state.
- Reuse runtime provider template generation for OpenCode instead of duplicating OpenCode config construction.
- Preserve existing workspace data when migrating `agent-trinity-system`.
- Make existing-container migration explicit through a deployment flag.
- Keep backend startup resilient: invalid system-agent runtime config should produce a deployment warning/error result without crashing the platform.

## Non-Goals

- Do not redesign the full system agent lifecycle or route it through the normal user-facing agent creation flow.
- Do not add a UI for system-agent runtime selection in this change.
- Do not delete the `agent-trinity-system-workspace` volume during migration.
- Do not clean up old system-scoped MCP keys as part of this migration. Key cleanup can be a separate maintenance task.
- Do not make Claude Code use OpenAI-compatible DeepSeek models directly.

## Configuration

The backend startup path reads these environment variables:

```text
SYSTEM_AGENT_RUNTIME=opencode
SYSTEM_AGENT_RUNTIME_PROVIDER_ID=deepseek-openai
SYSTEM_AGENT_RUNTIME_MODEL_ID=deepseek-v4-flash
SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT=false
```

Behavior:

- If all target runtime/provider/model variables are unset, keep the current Claude Code default system-agent behavior.
- If `SYSTEM_AGENT_RUNTIME` is unset but provider/model are set, treat the configuration as invalid.
- If `SYSTEM_AGENT_RUNTIME=opencode`, both `SYSTEM_AGENT_RUNTIME_PROVIDER_ID` and `SYSTEM_AGENT_RUNTIME_MODEL_ID` are required.
- `SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT` defaults to `false`.
- Accepted truthy values for auto-recreate should follow existing Trinity conventions where possible, such as `1`, `true`, `yes`, and `on`.

No-target mode is compatibility mode for existing containers. If all target runtime/provider/model variables are unset, `ensure_deployed()` should create the legacy Claude Code system agent only when the container is missing. It should not treat an existing OpenCode system-agent container as drifted or automatically roll it back to Claude Code. Operators who want rollback by recreation must remove the container manually while preserving the workspace volume.

Target environment parsing and validation runs on every backend startup, before existing-container decisions. This ensures partial or invalid configuration is surfaced even if `agent-trinity-system` is already running.

The default recommended deployment for DeepSeek is:

```text
SYSTEM_AGENT_RUNTIME=opencode
SYSTEM_AGENT_RUNTIME_PROVIDER_ID=deepseek-openai
SYSTEM_AGENT_RUNTIME_MODEL_ID=deepseek-v4-flash
SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT=true
```

`deepseek-openai` must already exist in Provider Config v2 with protocol `openai-compatible`, a configured API key, and a model entry matching `deepseek-v4-flash`.

## Startup Flow

`SystemAgentService.ensure_deployed()` keeps its current role as the backend startup entry point.

When no existing `agent-trinity-system` container exists:

1. Resolve target system-agent runtime config from environment variables.
2. Build the base system-agent environment used today: name, type, SSH, agent server, template, TMPDIR, telemetry, and MCP credentials.
3. If the target runtime is `opencode`, load provider configs from `settings_service.get_provider_configs()` and call `build_runtime_template("opencode", provider, model_id)`.
4. Materialize the runtime template with the provider API key and merge the resulting env into the container environment.
5. Create the container with the same volume, template mount, full-capability, AppArmor, tmpfs, resource, and restart-policy behavior used today.
6. Add runtime labels to the container.

When an existing container exists:

1. Resolve and validate the target environment config.
2. Ensure the DB owner record is marked `is_system=True`, as today.
3. If no target runtime/provider/model is configured, keep current behavior: start the existing container if stopped, otherwise do nothing.
4. Compare the existing container's runtime/provider/model with the target config.
5. If no drift exists, keep current behavior: start it if stopped, otherwise do nothing.
6. If drift exists and auto-recreate is false, do not modify the container. Return a warning-style result that includes current and target runtime identity.
7. If drift exists and auto-recreate is true, run preflight validation for the replacement. Only after preflight succeeds should the old container be stopped and removed.
8. Create the replacement using the same creation path as a missing container.

Preflight validation must happen before stopping/removing the existing container. It should include target env validation, provider lookup, provider protocol check, model lookup, API key presence check, runtime template build/materialization, template path availability, workspace volume name calculation, and any Docker image/network checks already available without mutating the old container. Docker's fixed container name makes a full temporary-container swap impractical for v1, but the implementation should still avoid removing a working drifted system agent for configuration errors that can be detected ahead of time.

## Drift Detection

Drift should use labels first and environment variables as fallback.

Target identity:

- Runtime: `SYSTEM_AGENT_RUNTIME`, normalized so legacy `gemini` would compare as `gemini-cli` if supported later.
- Provider id: `SYSTEM_AGENT_RUNTIME_PROVIDER_ID`.
- Model id: `SYSTEM_AGENT_RUNTIME_MODEL_ID`.

Existing identity should read:

- Runtime from `trinity.agent-runtime` label, then `AGENT_RUNTIME` env, defaulting to `claude-code`.
- Provider id from `trinity.runtime-provider-id` label, then `TRINITY_RUNTIME_PROVIDER_ID` env.
- Model id from `trinity.runtime-model-id` label, then `TRINITY_RUNTIME_MODEL_ID` env.

If `TRINITY_RUNTIME_MODEL_ID` is missing and `AGENT_RUNTIME_MODEL` has `provider/model` shape, drift detection may parse `AGENT_RUNTIME_MODEL` as a compatibility fallback. If the parsed provider differs from the provider identity read from labels/env, the container is drifted. OpenCode containers without labels, `TRINITY_RUNTIME_PROVIDER_ID`, or a parseable `AGENT_RUNTIME_MODEL` are drifted when a provider/model target is configured.

For the legacy Claude Code system agent, provider and model id are expected to be empty. If the target is `opencode`, a legacy Claude Code container is drifted.

## Labels

The system-agent container should keep existing labels and add runtime identity labels:

```text
trinity.agent-runtime=opencode
trinity.runtime-provider-id=deepseek-openai
trinity.runtime-model-id=deepseek-v4-flash
```

If the default Claude Code path is used, `trinity.agent-runtime=claude-code` should still be written so future drift detection is explicit.

## Migration Semantics

Migration by auto-recreate affects only the container, not persistent workspace data.

The existing volume name remains:

```text
agent-trinity-system-workspace
```

The replacement container mounts that same volume at `/home/developer`. This preserves system-agent workspace files, command history stored in the home volume, and any template-generated state that lives under `/home/developer`.

Auto-recreate should not remove the template directory, Docker image, database owner record, or workspace volume.

Auto-recreate should preserve the old SSH host port when possible. If the old port is unavailable or cannot be safely reused, the replacement may receive a new port and must update `trinity.ssh-port`; consumers must rediscover the SSH port from the API or container labels.

## MCP Credentials

The current creation flow creates a system-scoped MCP API key for `trinity-system` and injects it into the container as `TRINITY_MCP_API_KEY`.

The replacement flow should keep that behavior. It may create a new key during recreation. Existing old keys should not be deleted by this change because a later cleanup task can safely reason about key age and active references.

## Error Handling

Invalid target configuration should be reported through the `ensure_deployed()` result and startup logs. It should not crash backend startup.

Error cases include:

- `SYSTEM_AGENT_RUNTIME=opencode` without provider or model id.
- Provider id not found in Provider Config v2.
- Provider protocol is not `openai-compatible` for OpenCode.
- Model id not found under the provider.
- Provider API key is not configured.
- Container removal or creation fails during auto-recreate.

Configuration and runtime-template errors must be caught during preflight, before the old container is removed. If an error occurs after the old container is removed, the workspace volume remains intact and the error result should make the failure clear. Operators can fix configuration and restart the backend to retry creation.

## Testing

Unit tests should cover:

- No env target keeps the Claude Code default creation environment and labels `trinity.agent-runtime=claude-code`.
- `opencode + deepseek-openai + deepseek-v4-flash` target creates OpenCode env, including `AGENT_RUNTIME=opencode`, `AGENT_RUNTIME_MODEL=deepseek-openai/deepseek-v4-flash`, `OPENCODE_CONFIG_CONTENT`, `TRINITY_RUNTIME_PROVIDER_ID`, and `TRINITY_RUNTIME_MODEL_ID`.
- Existing matching OpenCode system-agent container is not recreated.
- Existing legacy Claude Code system-agent container is considered drifted when target is OpenCode.
- No-target env with an existing OpenCode system-agent container does not roll back or recreate it.
- Partial target env config with an existing running container still returns an error/warning result.
- Drift with auto-recreate false does not stop, remove, or recreate the container.
- Drift with auto-recreate false does not start a stopped drifted container as if it matched the target.
- Drift with auto-recreate true stops/removes the old container and creates a replacement.
- Drift with auto-recreate true does not remove the old container when provider/model/protocol/API-key validation fails.
- Truthy and false/default parsing for `SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT`.
- Recreation preserves the `agent-trinity-system-workspace` bind mount exactly.
- Invalid provider/model/protocol configuration returns an error result and does not crash.

Remote smoke verification should include:

- Configure environment variables for `opencode + deepseek-openai`.
- Rebuild/restart backend with `SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT=true`.
- Verify `agent-trinity-system` reports `/api/model` as runtime `opencode` and model `deepseek-openai/deepseek-v4-flash`.
- Send a simple chat/task smoke and verify the agent no longer invokes Claude Code with a DeepSeek model.

## Rollback

To roll back to current Claude Code behavior:

1. Unset the `SYSTEM_AGENT_RUNTIME*` environment variables, or set `SYSTEM_AGENT_RUNTIME=claude-code` after support for explicit Claude target is added.
2. Leave `SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT=false` to avoid automatic replacement.
3. If a container was already migrated and Claude Code is required immediately, remove `agent-trinity-system` manually while preserving the workspace volume, then restart the backend with default configuration.

Rollback removal must not use `docker rm -v`, must not remove or prune `agent-trinity-system-workspace`, and should verify that volume exists before and after removing the container.

The existing workspace volume is preserved across both migration and rollback.
