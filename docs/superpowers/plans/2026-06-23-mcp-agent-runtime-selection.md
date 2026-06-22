# MCP Agent Runtime Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Trinity MCP tools to create/deploy agents with `claude-code`, `gemini-cli`, or `opencode` runtime settings, including OpenCode provider/model selection.

**Architecture:** Extend the existing backend models and deploy-local resolution to accept runtime override fields, then expose and forward the same fields from MCP `create_agent` and `deploy_local_agent`. Backend remains authoritative for business validation; MCP handles schema shape and pass-through only.

**Tech Stack:** Python/FastAPI/Pydantic backend, TypeScript/FastMCP/Zod MCP server, pytest, Node built-in test runner with `tsx`.

---

## File Structure

- Modify `src/backend/models.py`: add deploy-local request fields and validators matching `AgentConfig`.
- Modify `src/backend/services/agent_service/deploy.py`: merge request runtime overrides with `template.yaml` runtime config before constructing `AgentConfig`.
- Modify `tests/test_deploy_local.py`: add unit/integration-style tests for deploy-local runtime field validation and override behavior using mocks.
- Modify `src/mcp-server/src/types.ts`: extend `AgentConfig` with runtime fields.
- Modify `src/mcp-server/src/tools/agents.ts`: add Zod schema fields and forward values for `create_agent` and `deploy_local_agent`.
- Add `src/mcp-server/src/agents_runtime.test.ts`: test MCP helper behavior using exported helpers from `agents.ts`.

## Task 1: Backend deploy-local request accepts runtime fields

**Files:**
- Modify: `src/backend/models.py`
- Test: `tests/test_deploy_local.py`

- [ ] **Step 1: Write failing model validation test**

Add this near the other deploy-local validation tests in `tests/test_deploy_local.py`:

```python
from pydantic import ValidationError
from models import DeployLocalRequest


def test_deploy_local_request_accepts_runtime_fields():
    req = DeployLocalRequest(
        archive="dGVzdA==",
        name="runtime-agent",
        runtime="opencode",
        runtime_model="deepseek-openai/deepseek-v4-flash",
        runtime_provider_id="deepseek-openai",
        runtime_model_id="deepseek-v4-flash",
        runtime_permission="standard",
    )

    assert req.runtime == "opencode"
    assert req.runtime_model == "deepseek-openai/deepseek-v4-flash"
    assert req.runtime_provider_id == "deepseek-openai"
    assert req.runtime_model_id == "deepseek-v4-flash"
    assert req.runtime_permission == "standard"


def test_deploy_local_request_rejects_unsupported_runtime():
    try:
        DeployLocalRequest(archive="dGVzdA==", runtime="bad-runtime")
    except ValidationError as exc:
        assert "Unsupported runtime" in str(exc)
    else:
        raise AssertionError("DeployLocalRequest accepted unsupported runtime")


def test_deploy_local_request_rejects_unsupported_runtime_permission():
    try:
        DeployLocalRequest(archive="dGVzdA==", runtime_permission="root")
    except ValidationError as exc:
        assert "Unsupported runtime_permission" in str(exc)
    else:
        raise AssertionError("DeployLocalRequest accepted unsupported runtime_permission")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_deploy_local.py::test_deploy_local_request_accepts_runtime_fields tests/test_deploy_local.py::test_deploy_local_request_rejects_unsupported_runtime tests/test_deploy_local.py::test_deploy_local_request_rejects_unsupported_runtime_permission -q
```

Expected: first test fails because `DeployLocalRequest` has no runtime fields; validator tests may also fail because fields are ignored.

- [ ] **Step 3: Implement minimal model fields**

In `src/backend/models.py`, update `DeployLocalRequest`:

```python
class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz
    name: Optional[str] = None  # Override name from template.yaml
    credentials: Optional[Dict[str, str]] = None  # Optional credentials to inject {KEY: value}
    runtime: Optional[str] = None
    runtime_model: Optional[str] = None
    runtime_provider_id: Optional[str] = None
    runtime_model_id: Optional[str] = None
    runtime_permission: Optional[str] = None

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: Optional[str]) -> Optional[str]:
        return validate_agent_runtime(value)

    @field_validator("runtime_permission")
    @classmethod
    def validate_runtime_permission(cls, value: Optional[str]) -> Optional[str]:
        return validate_agent_runtime_permission(value)
```

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command from Step 2.

Expected: all three tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/backend/models.py tests/test_deploy_local.py
git commit -m "feat: accept runtime fields for local deploy requests"
```

## Task 2: Backend deploy-local request fields override template runtime

**Files:**
- Modify: `src/backend/services/agent_service/deploy.py`
- Test: `tests/test_deploy_local.py`

- [ ] **Step 1: Write failing behavior test**

Add a test that patches agent creation and verifies the `AgentConfig` built by `deploy_local_agent` uses request runtime fields over template fields. Use existing helper `create_test_archive` from `tests/test_deploy_local.py`.

```python
@pytest.mark.asyncio
async def test_deploy_local_request_runtime_overrides_template_runtime(tmp_path, monkeypatch):
    from models import DeployLocalRequest
    import services.agent_service.deploy as deploy_mod

    captured = {}

    template_content = """
name: runtime-override
display_name: Runtime Override
resources:
  cpu: "1"
  memory: "2g"
runtime:
  type: claude-code
  model: sonnet
  permission: restricted
"""
    archive = create_test_archive(template_content)

    async def fake_create_agent_internal(config, current_user, request, skip_name_sanitization=False):
        captured["config"] = config
        from datetime import datetime, timezone
        from models import AgentStatus

        return AgentStatus(
            name=config.name,
            type=config.type,
            status="running",
            port=2222,
            created=datetime.now(timezone.utc),
            resources=config.resources,
            template=config.template,
            runtime=config.runtime,
        )

    monkeypatch.setattr(deploy_mod, "DEPLOYED_TEMPLATES_DIR_IN_BACKEND", str(tmp_path / "templates"))
    monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda base_name: [])
    monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda base_name: base_name)
    monkeypatch.setattr(deploy_mod, "get_latest_version", lambda base_name: None)
    monkeypatch.setattr(deploy_mod, "_prepopulate_workspace_from_template", lambda *args, **kwargs: None)

    body = DeployLocalRequest(
        archive=archive,
        runtime="opencode",
        runtime_model="deepseek-openai/deepseek-v4-flash",
        runtime_provider_id="deepseek-openai",
        runtime_model_id="deepseek-v4-flash",
        runtime_permission="standard",
    )

    result = await deploy_mod.deploy_local_agent_logic(
        body=body,
        current_user=type("U", (), {"username": "admin", "role": "admin"})(),
        request=None,
        create_agent_fn=fake_create_agent_internal,
    )

    assert result.status == "success"
    assert captured["config"].runtime == "opencode"
    assert captured["config"].runtime_model == "deepseek-openai/deepseek-v4-flash"
    assert captured["config"].runtime_provider_id == "deepseek-openai"
    assert captured["config"].runtime_model_id == "deepseek-v4-flash"
    assert captured["config"].runtime_permission == "standard"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_deploy_local.py::test_deploy_local_request_runtime_overrides_template_runtime -q
```

Expected: fails because request runtime fields are not applied.

- [ ] **Step 3: Implement runtime resolution**

In `src/backend/services/agent_service/deploy.py`, replace the runtime extraction block with explicit request-over-template fallback:

```python
# Extract runtime config from template, then apply request overrides.
runtime_config = template_data.get("runtime", {})
template_runtime_type = None
template_runtime_model = None
template_runtime_permission = "restricted"
if isinstance(runtime_config, dict):
    template_runtime_type = runtime_config.get("type")
    template_runtime_model = runtime_config.get("model")
    template_runtime_permission = runtime_config.get("permission", "restricted")
elif isinstance(runtime_config, str):
    template_runtime_type = runtime_config

runtime_type = body.runtime if body.runtime is not None else template_runtime_type
runtime_model = body.runtime_model if body.runtime_model is not None else template_runtime_model
runtime_provider_id = body.runtime_provider_id
runtime_model_id = body.runtime_model_id
runtime_permission = (
    body.runtime_permission if body.runtime_permission is not None else template_runtime_permission
)
```

Then include provider/model fields in `AgentConfig`:

```python
agent_config = AgentConfig(
    name=version_name,
    template=f"local:{version_name}",
    type=template_data.get("type", "business-assistant"),
    resources=template_data.get("resources", {"cpu": "2", "memory": "4g"}),
    runtime=runtime_type,
    runtime_model=runtime_model,
    runtime_provider_id=runtime_provider_id,
    runtime_model_id=runtime_model_id,
    runtime_permission=runtime_permission,
)
```

- [ ] **Step 4: Run backend deploy-local tests**

Run:

```bash
python -m pytest tests/test_deploy_local.py::test_deploy_local_request_runtime_overrides_template_runtime tests/test_deploy_local.py::test_deploy_local_request_accepts_runtime_fields tests/test_deploy_local.py::test_deploy_local_request_rejects_unsupported_runtime tests/test_deploy_local.py::test_deploy_local_request_rejects_unsupported_runtime_permission -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/agent_service/deploy.py tests/test_deploy_local.py
git commit -m "feat: let local deploy override agent runtime"
```

## Task 3: MCP create_agent exposes and forwards runtime fields

**Files:**
- Modify: `src/mcp-server/src/types.ts`
- Modify: `src/mcp-server/src/tools/agents.ts`
- Add: `src/mcp-server/src/agents_runtime.test.ts`

- [ ] **Step 1: Extract a helper and write failing tests**

Add `src/mcp-server/src/agents_runtime.test.ts`:

```ts
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { buildCreateAgentConfig, buildDeployLocalAgentPayload } from "./tools/agents.js";

describe("MCP agent runtime fields", () => {
  it("forwards runtime fields for create_agent", () => {
    const config = buildCreateAgentConfig({
      name: "worker",
      template: "github:sunyalou/agent",
      runtime: "opencode",
      runtime_model: "deepseek-openai/deepseek-v4-flash",
      runtime_provider_id: "deepseek-openai",
      runtime_model_id: "deepseek-v4-flash",
      runtime_permission: "standard",
    });

    assert.equal(config.runtime, "opencode");
    assert.equal(config.runtime_model, "deepseek-openai/deepseek-v4-flash");
    assert.equal(config.runtime_provider_id, "deepseek-openai");
    assert.equal(config.runtime_model_id, "deepseek-v4-flash");
    assert.equal(config.runtime_permission, "standard");
  });

  it("omits runtime fields when create_agent args omit them", () => {
    const config = buildCreateAgentConfig({ name: "default-worker" });

    assert.equal(Object.prototype.hasOwnProperty.call(config, "runtime"), false);
    assert.equal(Object.prototype.hasOwnProperty.call(config, "runtime_provider_id"), false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
npm --prefix src/mcp-server test -- src/agents_runtime.test.ts
```

Expected: fails because `buildCreateAgentConfig` is not exported.

- [ ] **Step 3: Implement helper, type fields, and schema fields**

In `src/mcp-server/src/types.ts`, extend `AgentConfig`:

```ts
  runtime?: "claude-code" | "gemini-cli" | "opencode";
  runtime_model?: string;
  runtime_provider_id?: string;
  runtime_model_id?: string;
  runtime_permission?: "restricted" | "standard" | "dangerous";
```

In `src/mcp-server/src/tools/agents.ts`, add exported arg type and helper near imports:

```ts
type AgentRuntime = "claude-code" | "gemini-cli" | "opencode";
type RuntimePermission = "restricted" | "standard" | "dangerous";

export type CreateAgentToolArgs = {
  name: string;
  type?: string;
  template?: string;
  resources?: { cpu?: string; memory?: string };
  tools?: string[];
  mcp_servers?: string[];
  custom_instructions?: string;
  source_branch?: string;
  runtime?: AgentRuntime;
  runtime_model?: string;
  runtime_provider_id?: string;
  runtime_model_id?: string;
  runtime_permission?: RuntimePermission;
};

export function buildCreateAgentConfig(args: CreateAgentToolArgs) {
  return {
    name: args.name,
    type: args.type,
    template: args.template,
    resources: args.resources
      ? {
          cpu: args.resources.cpu || "2",
          memory: args.resources.memory || "4g",
        }
      : undefined,
    tools: args.tools,
    mcp_servers: args.mcp_servers,
    custom_instructions: args.custom_instructions,
    source_branch: args.source_branch,
    ...(args.runtime !== undefined ? { runtime: args.runtime } : {}),
    ...(args.runtime_model !== undefined ? { runtime_model: args.runtime_model } : {}),
    ...(args.runtime_provider_id !== undefined ? { runtime_provider_id: args.runtime_provider_id } : {}),
    ...(args.runtime_model_id !== undefined ? { runtime_model_id: args.runtime_model_id } : {}),
    ...(args.runtime_permission !== undefined ? { runtime_permission: args.runtime_permission } : {}),
  };
}
```

Add Zod fields to `createAgent.parameters`:

```ts
        runtime: z.enum(["claude-code", "gemini-cli", "opencode"]).optional().describe(
          "Agent runtime. Default is backend default 'claude-code'. Use 'opencode' for OpenCode or 'gemini-cli' for Gemini CLI."
        ),
        runtime_model: z.string().optional().describe("Runtime-specific model override."),
        runtime_provider_id: z.string().optional().describe("Runtime provider id, for example 'deepseek-openai'. Must be paired with runtime_model_id."),
        runtime_model_id: z.string().optional().describe("Runtime provider model id, for example 'deepseek-v4-flash'. Must be paired with runtime_provider_id."),
        runtime_permission: z.enum(["restricted", "standard", "dangerous"]).optional().describe("Runtime permission profile, mainly for OpenCode."),
```

Replace the inline config object in `execute` with:

```ts
        const config = buildCreateAgentConfig(args);
```

Change the execute args type to `CreateAgentToolArgs`.

- [ ] **Step 4: Run MCP test**

Run:

```bash
npm --prefix src/mcp-server test -- src/agents_runtime.test.ts
```

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp-server/src/types.ts src/mcp-server/src/tools/agents.ts src/mcp-server/src/agents_runtime.test.ts
git commit -m "feat(mcp): pass runtime fields when creating agents"
```

## Task 4: MCP deploy_local_agent exposes and forwards runtime fields

**Files:**
- Modify: `src/mcp-server/src/tools/agents.ts`
- Modify: `src/mcp-server/src/agents_runtime.test.ts`

- [ ] **Step 1: Write failing deploy-local helper test**

Append this block to `src/mcp-server/src/agents_runtime.test.ts`. Do not add another import; `buildDeployLocalAgentPayload` was imported in Task 3.

```ts
describe("MCP deploy_local_agent runtime fields", () => {
  it("forwards runtime fields for deploy_local_agent", () => {
    const payload = buildDeployLocalAgentPayload({
      archive: "dGVzdA==",
      name: "local-worker",
      runtime: "opencode",
      runtime_model: "deepseek-openai/deepseek-v4-flash",
      runtime_provider_id: "deepseek-openai",
      runtime_model_id: "deepseek-v4-flash",
      runtime_permission: "dangerous",
    });

    assert.equal(payload.archive, "dGVzdA==");
    assert.equal(payload.name, "local-worker");
    assert.equal(payload.runtime, "opencode");
    assert.equal(payload.runtime_model, "deepseek-openai/deepseek-v4-flash");
    assert.equal(payload.runtime_provider_id, "deepseek-openai");
    assert.equal(payload.runtime_model_id, "deepseek-v4-flash");
    assert.equal(payload.runtime_permission, "dangerous");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
npm --prefix src/mcp-server test -- src/agents_runtime.test.ts
```

Expected: fails because `buildDeployLocalAgentPayload` is not exported.

- [ ] **Step 3: Implement deploy-local helper and schema fields**

In `src/mcp-server/src/tools/agents.ts`, add:

```ts
export type DeployLocalAgentToolArgs = {
  archive: string;
  name?: string;
  runtime?: AgentRuntime;
  runtime_model?: string;
  runtime_provider_id?: string;
  runtime_model_id?: string;
  runtime_permission?: RuntimePermission;
};

export function buildDeployLocalAgentPayload(args: DeployLocalAgentToolArgs) {
  return {
    archive: args.archive,
    name: args.name,
    ...(args.runtime !== undefined ? { runtime: args.runtime } : {}),
    ...(args.runtime_model !== undefined ? { runtime_model: args.runtime_model } : {}),
    ...(args.runtime_provider_id !== undefined ? { runtime_provider_id: args.runtime_provider_id } : {}),
    ...(args.runtime_model_id !== undefined ? { runtime_model_id: args.runtime_model_id } : {}),
    ...(args.runtime_permission !== undefined ? { runtime_permission: args.runtime_permission } : {}),
  };
}
```

Add the same Zod runtime fields to `deployLocalAgent.parameters`.

Replace the request body in `deployLocalAgent.execute` with:

```ts
          buildDeployLocalAgentPayload(args)
```

Change the execute args type to `DeployLocalAgentToolArgs`.

- [ ] **Step 4: Run MCP runtime test**

Run:

```bash
npm --prefix src/mcp-server test -- src/agents_runtime.test.ts
```

Expected: all MCP runtime tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp-server/src/tools/agents.ts src/mcp-server/src/agents_runtime.test.ts
git commit -m "feat(mcp): pass runtime fields for local deploy"
```

## Task 5: Final verification and deployment

**Files:**
- No new source changes expected.

- [ ] **Step 1: Run backend targeted tests**

Run:

```bash
python -m pytest tests/test_deploy_local.py tests/unit/test_opencode_backend_runtime_propagation.py -q
```

Expected: tests pass.

- [ ] **Step 2: Run MCP tests and build**

Run:

```bash
npm --prefix src/mcp-server test && npm --prefix src/mcp-server run build
```

Expected: tests pass and TypeScript build exits 0.

- [ ] **Step 3: Inspect git state**

Run:

```bash
git status --short
git log --oneline -10
```

Expected: no unintended files remain unstaged. Recent commits include the spec and runtime support commits.

- [ ] **Step 4: Push main using local proxy if needed**

Run:

```bash
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 git push origin main
```

Expected: push succeeds.

- [ ] **Step 5: Deploy remote using remote proxy**

Run:

```bash
ssh ubuntu-server 'set -e; cd /home/sun/trinity; HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 git fetch https://github.com/sunyalou/trinity.git main; git reset --hard FETCH_HEAD; python3 - <<'"'"'PY'"'"'
from pathlib import Path
path = Path(".env")
keys = {"VERSION", "GIT_COMMIT", "GIT_COMMIT_SUBJECT", "GIT_COMMIT_TIMESTAMP", "GIT_BRANCH", "BUILD_DATE"}
lines = path.read_text().splitlines()
kept = [line for line in lines if not any(line.startswith(f"{key}=") for key in keys)]
path.write_text("\n".join(kept) + "\n")
print("removed_provenance_keys", len(lines) - len(kept))
PY
HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 ./scripts/deploy/start.sh'
```

Expected: deploy completes and containers restart.

- [ ] **Step 6: Verify remote services**

Run:

```bash
ssh ubuntu-server 'curl -fsS http://127.0.0.1:8000/health >/dev/null && printf BACKEND_OK\\n; curl -fsS http://127.0.0.1:8080/health >/dev/null && printf MCP_OK\\n; curl -fsS http://127.0.0.1/ >/dev/null && printf FRONTEND_OK\\n'
```

Expected:

```text
BACKEND_OK
MCP_OK
FRONTEND_OK
```

- [ ] **Step 7: Verify provenance**

Run:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && printf HEAD= && git rev-parse --short=8 HEAD && docker exec trinity-backend sh -c "env | grep -E \"^(VERSION|GIT_COMMIT|GIT_COMMIT_SUBJECT|GIT_BRANCH|BUILD_DATE)=\""'
```

Expected: HEAD and container `GIT_COMMIT` match the pushed commit.

---

## Self-Review Notes

- Spec coverage: plan covers MCP create-agent fields, MCP deploy-local fields, backend DeployLocalRequest fields, deploy-local override precedence, validation, tests, and deployment verification.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: uses snake_case field names to match backend `AgentConfig` and existing MCP JSON payload style.
