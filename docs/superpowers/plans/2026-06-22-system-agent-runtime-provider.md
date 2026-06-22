# System Agent Runtime Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the auto-created `trinity-system` agent configurable through environment variables so it can run as `opencode + deepseek-openai`, with explicit and safe migration of existing system-agent containers.

**Architecture:** Keep `SystemAgentService.ensure_deployed()` as the startup entry point. Split target parsing, launch-plan construction, runtime identity extraction, drift detection, preflight, and recreation into focused helpers inside `src/backend/services/system_agent_service.py`. Reuse `services.runtime_provider_templates.build_runtime_template()` for OpenCode provider configuration.

**Tech Stack:** Python 3.13, pytest, Docker SDK wrappers in `services/docker_utils.py`, Trinity Provider Config v2, OpenCode runtime templates.

---

## File Structure

- Modify `src/backend/services/system_agent_service.py`.
- Create `tests/unit/test_system_agent_runtime_provider.py`.
- Do not modify frontend files, Provider Config v2 schema, normal agent creation flow, or `config/agent-templates/trinity-system/`.

---

## Task 1: Parse target runtime config

**Files:**
- Modify: `src/backend/services/system_agent_service.py`
- Create: `tests/unit/test_system_agent_runtime_provider.py`

- [ ] Write failing tests for `_resolve_system_agent_target()` covering no env, truthy auto-recreate values, false/default auto-recreate values, provider/model without runtime, and `opencode` without both provider/model.
- [ ] Run `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q` and verify RED because `_resolve_system_agent_target()` does not exist.
- [ ] Add `SystemAgentRuntimeTarget`, `_env_flag()`, `_normalize_runtime()`, and `_resolve_system_agent_target()` in `system_agent_service.py`.
- [ ] Verify GREEN with `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q`.
- [ ] Commit with message `feat: parse system agent runtime target config`.

Acceptance details:
- No env means `configured=False`, runtime `claude-code`, no provider/model, no error.
- Truthy values are `1`, `true`, `yes`, and `on`, case-insensitive.
- Partial env error text: `SYSTEM_AGENT_RUNTIME is required when provider or model is configured`.
- OpenCode missing provider/model error text: `SYSTEM_AGENT_RUNTIME_PROVIDER_ID and SYSTEM_AGENT_RUNTIME_MODEL_ID are required for opencode`.
- Unsupported runtime error text starts with `Unsupported SYSTEM_AGENT_RUNTIME:`.

---

## Task 2: Build launch plans

**Files:**
- Modify: `src/backend/services/system_agent_service.py`
- Modify: `tests/unit/test_system_agent_runtime_provider.py`

- [ ] Add failing tests for `SystemAgentService()._build_launch_plan(target, ssh_port=2222, agent_mcp_key=SimpleNamespace(api_key="mcp-key"))`.
- [ ] Test default/no-env launch plan includes `ANTHROPIC_API_KEY`, `TRINITY_MCP_API_KEY`, `trinity.agent-runtime=claude-code`, `trinity.is-system=true`, and `ssh_port=2222`.
- [ ] Test OpenCode launch plan with mocked `settings_service.get_provider_configs()` includes `AGENT_RUNTIME=opencode`, `AGENT_RUNTIME_MODEL=deepseek-openai/deepseek-v4-flash`, `TRINITY_RUNTIME_PROVIDER_ID=deepseek-openai`, `TRINITY_RUNTIME_MODEL_ID=deepseek-v4-flash`, `OPENCODE_CONFIG_CONTENT`, provider secret env `TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY`, and runtime labels.
- [ ] Run `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q` and verify RED because `_build_launch_plan()` does not exist.
- [ ] Import `settings_service` and `build_runtime_template`; add `SystemAgentLaunchPlan(env, labels, volumes, resources, ssh_port)`.
- [ ] Implement `_build_launch_plan()` by loading `template.yaml`, preserving existing base env/telemetry/MCP/template mount behavior, adding runtime labels, and materializing OpenCode template env when configured.
- [ ] Verify GREEN with `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q`.
- [ ] Commit with message `feat: build system agent runtime launch plan`.

---

## Task 3: Detect runtime drift

**Files:**
- Modify: `src/backend/services/system_agent_service.py`
- Modify: `tests/unit/test_system_agent_runtime_provider.py`

- [ ] Add failing tests for runtime identity and drift detection.
- [ ] Cover labels-first/env-second identity, `AGENT_RUNTIME_MODEL=provider/model` parsing, legacy Claude container drift under OpenCode target, matching OpenCode not drifted, and no-target mode not drifting an existing OpenCode container.
- [ ] Run `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q` and verify RED because identity/drift helpers do not exist.
- [ ] Add `SystemAgentRuntimeIdentity`, `_container_env_map()`, `_system_agent_identity_from_container()`, `_system_agent_target_identity()`, and `_system_agent_is_drifted()`.
- [ ] Verify GREEN with `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q`.
- [ ] Commit with message `feat: detect system agent runtime drift`.

Acceptance details:
- Identity reads `trinity.agent-runtime`, `trinity.runtime-provider-id`, and `trinity.runtime-model-id` labels first.
- Fallbacks are `AGENT_RUNTIME`, `TRINITY_RUNTIME_PROVIDER_ID`, `TRINITY_RUNTIME_MODEL_ID`, and parseable `AGENT_RUNTIME_MODEL`.
- No-target mode always returns not drifted.

---

## Task 4: Refactor creation to use launch plans

**Files:**
- Modify: `src/backend/services/system_agent_service.py`
- Modify: `tests/unit/test_system_agent_runtime_provider.py`

- [ ] Add an async failing test that patches `containers_run`, DB owner/key functions, provider configs, and `get_next_available_port`, then calls `await SystemAgentService()._create_system_agent()` with OpenCode env set.
- [ ] Assert `containers_run` receives OpenCode env, runtime labels, `ports={"22/tcp": 2222}`, and result `ssh_port=2222`.
- [ ] Run the single new test and verify RED because `_create_system_agent()` still builds inline Claude env.
- [ ] Change `_create_system_agent()` signature to accept optional `target` and `ssh_port`.
- [ ] Resolve target, reject `target.error`, allocate port, build launch plan, and pass launch plan env/labels/volumes/resources/port to `containers_run()`.
- [ ] Remove duplicated inline env/label/volume construction.
- [ ] Verify GREEN with `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q`.
- [ ] Commit with message `feat: create system agent from runtime launch plan`.

---

## Task 5: Safely handle drift and optional auto-recreate

**Files:**
- Modify: `src/backend/services/system_agent_service.py`
- Modify: `tests/unit/test_system_agent_runtime_provider.py`

- [ ] Add async failing tests for existing-container behavior.
- [ ] Cover partial env returning `action=config_error` and `status=error` even when container exists.
- [ ] Cover drift plus auto-recreate false returning `action=drift_detected` and `status=drifted` without start/stop/remove.
- [ ] Cover drift plus auto-recreate true plus invalid provider returning `action=preflight_failed` without stop/remove.
- [ ] Cover drift plus auto-recreate true plus valid provider calling stop, remove, then `_create_system_agent(target, ssh_port=1234)` and returning `action=recreated`, `status=running`.
- [ ] Run `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q` and verify RED because the drift branch is not implemented.
- [ ] Import `container_stop` and `container_remove`.
- [ ] Add `_existing_ssh_port(container)` and `_preflight_replacement(target, ssh_port)`.
- [ ] Parse target at the start of `ensure_deployed()` and return `config_error` for target errors.
- [ ] In existing-container branch, register owner, detect drift, avoid mutation when auto-recreate is false, preflight before mutation when true, then stop/remove/create with preserved SSH port.
- [ ] Verify GREEN with `python -m pytest tests/unit/test_system_agent_runtime_provider.py -q`.
- [ ] Commit with message `feat: safely recreate drifted system agent runtime`.

---

## Task 6: Verify and deploy

**Files:**
- Modify only if verification reveals failures: `src/backend/services/system_agent_service.py`, `tests/unit/test_system_agent_runtime_provider.py`.

- [ ] Run `python -m pytest tests/unit/test_system_agent_runtime_provider.py tests/unit/test_opencode_backend_runtime_propagation.py -q`.
- [ ] If pre-existing frontend model-selector changes remain in the worktree, run `node src/frontend/scripts/test-runtime-model-presets.mjs` and `npm run build --prefix src/frontend`.
- [ ] Inspect `git status --short`, `git diff --stat`, and `git diff -- src/backend/services/system_agent_service.py tests/unit/test_system_agent_runtime_provider.py`.
- [ ] Deploy backend files with `rsync -avR src/backend/services/system_agent_service.py tests/unit/test_system_agent_runtime_provider.py ubuntu-server:/home/sun/trinity/`.
- [ ] Add non-secret remote env lines to `/home/sun/trinity/.env` without printing the full file: `SYSTEM_AGENT_RUNTIME=opencode`, `SYSTEM_AGENT_RUNTIME_PROVIDER_ID=deepseek-openai`, `SYSTEM_AGENT_RUNTIME_MODEL_ID=deepseek-v4-flash`, `SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT=true`.
- [ ] Rebuild backend with `ssh ubuntu-server 'cd /home/sun/trinity && docker compose up -d --build backend'`.
- [ ] Health check backend docs and MCP health.
- [ ] Verify `docker exec agent-trinity-system curl -fsS http://127.0.0.1:8000/api/model` returns runtime `opencode` and model `deepseek-openai/deepseek-v4-flash`.
- [ ] Submit a minimal remote smoke task and verify success, non-empty response, and no recent logs containing `Using model: deepseek/deepseek-v4-flash` or Claude Code API 400 for DeepSeek.
- [ ] Commit final implementation with message `feat: configure system agent runtime provider`.

---

## Self-Review Notes

- Spec coverage: tasks cover env parsing, OpenCode launch template reuse, drift detection, no-target compatibility, preflight-before-remove, optional auto-recreate, workspace preservation through unchanged volume name, remote env, deployment, and smoke verification.
- Placeholder scan: no TBD/TODO placeholders; each task has explicit files, commands, and expected behavior.
- Type consistency: helper names are consistent across tasks: `SystemAgentRuntimeTarget`, `SystemAgentRuntimeIdentity`, `SystemAgentLaunchPlan`, `_resolve_system_agent_target()`, `_build_launch_plan()`, `_system_agent_identity_from_container()`, `_system_agent_is_drifted()`.
