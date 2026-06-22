# OpenCode Runtime Support Design

## Summary

Add OpenCode as a first-class Trinity agent runtime alongside the existing `claude-code` and `gemini-cli` runtimes. The first implementation will use OpenCode's one-shot headless CLI path (`opencode run --format json`) instead of a long-lived `opencode serve` process or ACP integration. This keeps the initial scope bounded while preserving the existing Trinity runtime adapter architecture.

The new runtime string will be:

```text
opencode
```

The feature should allow users to create and run OpenCode-backed agents without removing or weakening existing Claude Code and Gemini CLI support.

## Goals

- Add `opencode` as a selectable runtime for agents.
- Execute agent tasks through OpenCode in non-interactive/headless mode.
- Support OpenCode model names in `provider/model` format.
- Support configurable OpenCode permission profiles.
- Configure Trinity MCP access for OpenCode using OpenCode's `opencode.json` `mcp` format.
- Surface OpenCode correctly in backend APIs, frontend runtime badges, agent creation UI, and terminal mode.
- Keep the first implementation small enough to validate with end-to-end agent creation and chat/task execution.

## Non-Goals for the First Version

- Do not remove Claude Code support.
- Do not rename database/API fields such as `claude_session_id` in the first version; runtime-neutral cleanup can be a later migration.
- Do not implement `opencode serve` as a warm runtime in the first version.
- Do not implement ACP transport in the first version.
- Do not fully port Claude Code hook behavior to OpenCode hooks unless required for basic execution.
- Do not redesign Trinity's skill system around OpenCode conventions in the first version.

## Current Architecture Context

Trinity already has a runtime boundary inside the agent container:

- `docker/base-image/agent_server/services/runtime_adapter.py` defines the central `AgentRuntime` abstraction and factory.
- `docker/base-image/agent_server/services/claude_code.py` implements Claude Code execution.
- `docker/base-image/agent_server/services/gemini_runtime.py` implements Gemini CLI execution.
- `docker/base-image/agent_server/services/trinity_mcp.py` injects runtime-specific MCP configuration.
- `docker/base-image/agent_server/state.py` reads `AGENT_RUNTIME` and runtime model environment variables.
- Backend agent creation injects `AGENT_RUNTIME` and `AGENT_RUNTIME_MODEL` in `src/backend/services/agent_service/crud.py`.
- Frontend runtime-specific UI exists in components such as `RuntimeBadge.vue`, `AgentTerminal.vue`, and `CreateAgentModal.vue`.

OpenCode will fit into this existing pattern rather than bypass it.

## OpenCode Interfaces Used

The first version will rely on official OpenCode CLI behavior:

```bash
opencode run --format json --dir /workspace "Prompt"
```

Useful flags:

```bash
--model provider/model
--agent <agent-name>
--format json
--dir /workspace
--continue
--session <session-id>
--fork
--dangerously-skip-permissions
```

OpenCode config will be passed with environment variables where possible:

```text
OPENCODE_CONFIG_CONTENT
OPENCODE_PERMISSION
OPENCODE_DISABLE_AUTOUPDATE=1
OPENCODE_DISABLE_MODELS_FETCH=1
```

MCP configuration will use OpenCode's `opencode.json` shape:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "anthropic/claude-sonnet-4-5",
  "mcp": {
    "trinity": {
      "type": "remote",
      "url": "http://.../mcp",
      "headers": {
        "Authorization": "Bearer {env:TRINITY_MCP_API_KEY}"
      },
      "enabled": true
    }
  }
}
```

## Runtime Adapter Design

Add:

```text
docker/base-image/agent_server/services/opencode_runtime.py
```

The new `OpenCodeRuntime` should implement the existing `AgentRuntime` interface:

- `runtime_name`: `opencode`
- availability check: `opencode --version`
- default model: a configurable provider/model string, initially `anthropic/claude-sonnet-4-5` unless overridden
- execution command: `opencode run --format json --dir <workspace>`
- model override: `--model <provider/model>`
- session continuation: map Trinity's existing resume/session inputs to OpenCode `--session` or `--continue`
- permission handling: add `--dangerously-skip-permissions` only for the dangerous profile

The runtime should parse OpenCode JSON events into Trinity's existing response shape. If OpenCode event details differ from Claude's stream-json, the first version can extract only stable fields needed by Trinity:

- final text/result
- session ID, if present
- basic usage/cost/model metadata, if present
- error messages and non-zero exit status

Tool timeline parity with Claude Code can be incremental. Missing OpenCode-specific event fields should degrade gracefully rather than failing execution.

## Permission Profiles

Add a runtime permission concept for OpenCode agents:

```text
restricted | standard | dangerous
```

Initial behavior:

- `restricted`: read and web fetch allowed; edit and bash denied or ask-based depending on OpenCode non-interactive behavior.
- `standard`: common development operations allowed with dangerous operations denied.
- `dangerous`: pass `--dangerously-skip-permissions` and rely on explicit OpenCode deny rules where configured.

The exact profile JSON should be generated by Trinity and passed through `OPENCODE_PERMISSION` or embedded in `OPENCODE_CONFIG_CONTENT`.

The default should be `restricted` for newly created OpenCode agents unless the user chooses otherwise.

## MCP Configuration

Extend `trinity_mcp.py` to configure OpenCode separately from Claude and Gemini:

- Claude Code: keep writing `~/.mcp.json`.
- Gemini CLI: keep writing Gemini settings.
- OpenCode: write or provide `opencode.json` / `OPENCODE_CONFIG_CONTENT` containing an `mcp` block.

The OpenCode MCP config should use environment variable interpolation for secrets, for example `{env:TRINITY_MCP_API_KEY}`, instead of writing raw secrets into long-lived files when avoidable.

## Base Image Changes

Update the agent base image to install OpenCode:

```bash
npm i -g opencode-ai@latest
```

The image should also set safe defaults:

```text
OPENCODE_DISABLE_AUTOUPDATE=1
OPENCODE_DISABLE_MODELS_FETCH=1
```

The availability endpoint should report `opencode_available` or a runtime-neutral equivalent while preserving backward compatibility for `claude_available`.

## Backend Changes

Backend changes should propagate the new runtime without changing existing behavior:

- Accept `runtime="opencode"` in agent config models.
- Inject `AGENT_RUNTIME=opencode` and `AGENT_RUNTIME_MODEL=<provider/model>` into agent containers.
- Add an OpenCode permission field or runtime option for new agents.
- Update runtime labels/discovery in Docker service code.
- Include `opencode` in the `/api/version` supported runtimes list.
- Update terminal mode mapping so browser terminal can launch `opencode`.
- Avoid injecting Claude subscription token behavior for OpenCode unless a specific provider mode needs it.

Task model fallback should avoid assuming Claude defaults for OpenCode. If no task-level model is provided, the backend should prefer the agent's `AGENT_RUNTIME_MODEL` or an OpenCode runtime default.

## Frontend Changes

Frontend updates should make OpenCode visible and selectable:

- `RuntimeBadge.vue`: add OpenCode label/icon/color.
- `CreateAgentModal.vue`: allow choosing OpenCode for blank agents.
- `AgentDetail.vue`: set OpenCode default model as `provider/model`, not a Claude short model name.
- `AgentTerminal.vue`: add `opencode` terminal mode and label.
- Any runtime-specific helper should treat unknown runtime strings defensively.

OpenCode model input should accept arbitrary provider/model strings rather than forcing a small hard-coded list.

## Session and Metadata Handling

OpenCode supports:

```bash
opencode run --continue
opencode run --session <session-id>
opencode run --fork
```

The first version can reuse Trinity's existing session plumbing even where field names still say `claude_session_id`. Internally, the value should be treated as a runtime session ID. A later migration can rename these fields to `runtime_session_id`.

Session cleanup for Claude JSONL files should remain Claude-specific and should not assume OpenCode stores sessions under `~/.claude`.

## Error Handling

OpenCode runtime errors should map into the existing agent-server error response pattern:

- missing CLI: runtime unavailable with actionable message
- invalid model/provider: return command stderr/stdout summary
- permission denial: report permission profile and OpenCode message
- JSON parse failure: include sanitized first malformed event and raw-output length
- timeout/stall: reuse existing timeout classification where possible

Secrets in config, environment, and command output must be sanitized before returning errors.

## Testing Strategy

Unit tests:

- runtime factory returns `OpenCodeRuntime` for `opencode`
- OpenCode command construction for model, session, workspace, and permission profiles
- OpenCode JSON event parser extracts final text/session metadata
- MCP config generation produces OpenCode `mcp` format
- backend model accepts `opencode` and rejects invalid runtime strings
- frontend runtime badge and terminal mode display OpenCode

Integration tests:

- container image has `opencode --version`
- create an OpenCode agent with restricted permission profile
- run a simple chat/task and receive a final answer
- verify existing Claude Code and Gemini CLI runtime tests still pass

Manual smoke test:

```bash
opencode run --format json --dir /workspace --model anthropic/claude-sonnet-4-5 "Say hello from Trinity"
```

Then create an OpenCode agent through the UI and send a simple prompt.

## Future Work

- Add `opencode serve` warm runtime mode for reduced cold-start overhead.
- Add ACP integration if Trinity wants a protocol-level runtime instead of shelling out.
- Rename `claude_session_id` and related fields to runtime-neutral names.
- Add richer OpenCode event parsing for tool timelines and usage metrics.
- Map OpenCode-specific hooks and guardrails if needed.
- Add OpenCode-native template conventions such as `opencode.json` and `.opencode/` state preservation.

## First-Version Decisions

- Default OpenCode model: `anthropic/claude-sonnet-4-5`. This matches OpenCode's provider/model style and gives users a known high-quality default while allowing override at agent creation time.
- `standard` permissions: allow reads, edits, web fetches, and common package/git commands; deny or ask for destructive shell patterns such as broad `rm`, credential file reads, and external directory writes. The exact JSON profile should be centralized in the OpenCode runtime module so tests can lock it down.
- Credentials: use environment variables and `OPENCODE_CONFIG_CONTENT` in the first version. Do not persist `~/.local/share/opencode/auth.json` as part of Trinity agent state yet, because persistent provider auth files introduce secret lifecycle and rotation concerns that are separate from runtime enablement.
