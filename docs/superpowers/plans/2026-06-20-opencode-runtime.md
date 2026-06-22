# OpenCode Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenCode as a selectable Trinity agent runtime alongside Claude Code and Gemini CLI.

**Architecture:** Implement OpenCode at Trinity's existing runtime adapter boundary. The first version shells out to `opencode run --format json`, injects OpenCode MCP/config through runtime-specific helpers, and propagates `runtime="opencode"` through backend and frontend surfaces without renaming existing Claude-centric persistence fields.

**Tech Stack:** Python/FastAPI backend, Python agent-server runtime container, Docker base image, Vue 3 frontend, pytest unit tests, existing Trinity runtime adapter interfaces.

---

## File Structure

- Create `docker/base-image/agent_server/services/opencode_runtime.py`: OpenCode runtime implementation, command construction, JSON event parsing, permission profile generation.
- Create `tests/unit/test_opencode_runtime.py`: command-building, parser, runtime factory, and permission-profile tests.
- Modify `docker/base-image/agent_server/services/runtime_adapter.py`: route `AGENT_RUNTIME=opencode` to `OpenCodeRuntime`.
- Modify `docker/base-image/agent_server/services/trinity_mcp.py`: write OpenCode MCP config in `~/.config/opencode/opencode.json`.
- Create `tests/unit/test_opencode_mcp_config.py`: verify OpenCode MCP JSON shape and runtime dispatch.
- Modify `docker/base-image/agent_server/state.py`: runtime availability and default model support for OpenCode.
- Modify `docker/base-image/agent_server/config.py`: runtime constants/docs for OpenCode.
- Modify `docker/base-image/agent_server/routers/chat.py`: OpenCode model list/validation behavior.
- Modify `docker/base-image/agent_server/routers/info.py`: expose runtime availability without breaking `claude_available` compatibility.
- Modify `docker/base-image/Dockerfile`: install `opencode-ai` and set OpenCode env defaults.
- Modify `src/backend/models.py`: allow OpenCode runtime and add optional permission profile field.
- Modify `src/backend/services/agent_service/crud.py`: inject OpenCode runtime env and permission profile.
- Modify `src/backend/services/agent_service/deploy.py`: read OpenCode runtime and permission from templates.
- Modify `src/backend/services/docker_service.py`: discover OpenCode runtime labels/env.
- Modify `src/backend/services/agent_service/terminal.py`: support OpenCode terminal mode.
- Modify `src/backend/main.py`: add OpenCode to supported runtime metadata.
- Modify `src/backend/services/task_execution_service.py`: avoid Claude-only default model fallback for OpenCode agents.
- Modify frontend runtime UI files: `RuntimeBadge.vue`, `AgentTerminal.vue`, `CreateAgentModal.vue`, `AgentDetail.vue`.
- Modify `template.yaml`: preserve OpenCode config/state paths.
- Update docs only where user-facing runtime lists mention Claude/Gemini only.

---

### Task 1: Add OpenCode Runtime Module Tests

**Files:**
- Create: `tests/unit/test_opencode_runtime.py`
- Later modify: `docker/base-image/agent_server/services/opencode_runtime.py`
- Later modify: `docker/base-image/agent_server/services/runtime_adapter.py`

- [ ] **Step 1: Write failing tests for command construction and permissions**

Create `tests/unit/test_opencode_runtime.py` with:

```python
from __future__ import annotations

import json

import pytest

from agent_server.services import runtime_adapter


def test_runtime_factory_returns_opencode(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")

    runtime = runtime_adapter.get_runtime()

    assert runtime.__class__.__name__ == "OpenCodeRuntime"
    assert runtime.get_default_model() == "anthropic/claude-sonnet-4-5"


def test_opencode_builds_basic_headless_command(monkeypatch):
    from agent_server.services.opencode_runtime import OpenCodeRuntime

    runtime = OpenCodeRuntime()

    cmd = runtime.build_run_command(
        prompt="hello",
        model="openai/gpt-5",
        workspace="/workspace",
        resume_session_id=None,
        persist_session=False,
    )

    assert cmd == [
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        "/workspace",
        "--model",
        "openai/gpt-5",
        "hello",
    ]


def test_opencode_builds_session_resume_command():
    from agent_server.services.opencode_runtime import OpenCodeRuntime

    runtime = OpenCodeRuntime()

    cmd = runtime.build_run_command(
        prompt="continue",
        model=None,
        workspace="/workspace",
        resume_session_id="ses_abc123",
        persist_session=True,
    )

    assert "--session" in cmd
    assert "ses_abc123" in cmd
    assert "--continue" not in cmd


def test_opencode_permission_profiles_are_json_serializable():
    from agent_server.services.opencode_runtime import build_permission_profile

    for profile in ("restricted", "standard", "dangerous"):
        value = build_permission_profile(profile)
        json.dumps(value)

    restricted = build_permission_profile("restricted")
    assert restricted["read"] == "allow"
    assert restricted["edit"] == "deny"

    standard = build_permission_profile("standard")
    assert standard["edit"] == "allow"
    assert standard["bash"]["rm *"] == "deny"

    dangerous = build_permission_profile("dangerous")
    assert dangerous == "allow"


def test_invalid_opencode_permission_profile_rejected():
    from agent_server.services.opencode_runtime import build_permission_profile
    with pytest.raises(ValueError, match="Unsupported OpenCode permission profile"):
        build_permission_profile("root")


def test_opencode_parser_uses_real_agent_server_models():
    from agent_server.models import ExecutionLogEntry, ExecutionMetadata
    from agent_server.services.opencode_runtime import parse_opencode_events

    output = '\n'.join([
        '{"type":"session","sessionID":"ses_open_1"}',
        '{"type":"message","text":"hello"}',
        '{"type":"tool_call","id":"tool_1","name":"bash","input":{"cmd":"pwd"}}',
        '{"type":"usage","usage":{"input_tokens":3,"output_tokens":4}}',
    ])

    text, execution_log, metadata, raw_messages, session_id = parse_opencode_events(output, "openai/gpt-5")

    assert text == "hello"
    assert session_id == "ses_open_1"
    assert isinstance(execution_log[0], ExecutionLogEntry)
    assert execution_log[0].type == "tool_use"
    assert execution_log[0].tool == "bash"
    assert isinstance(metadata, ExecutionMetadata)
    assert metadata.model_name == "openai/gpt-5"
    assert metadata.input_tokens == 3
    assert metadata.output_tokens == 4
    assert len(raw_messages) == 4
```

- [ ] **Step 2: Run tests and verify they fail because implementation is missing**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_runtime.py -q
```

Expected: fails with `ModuleNotFoundError: No module named 'agent_server.services.opencode_runtime'` or factory returning Claude runtime.

- [ ] **Step 3: Create minimal OpenCode runtime implementation**

Create `docker/base-image/agent_server/services/opencode_runtime.py`:

```python
"""OpenCode CLI runtime implementation for Trinity agents."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME, os_setsid_if_available
from ..utils.orphan_sweep import kill_cgroup_orphans
from .runtime_adapter import AgentRuntime
from .process_registry import get_process_registry

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opencode-subproc")

DEFAULT_OPENCODE_MODEL = "anthropic/claude-sonnet-4-5"
DEFAULT_WORKSPACE = "/workspace"


def build_permission_profile(profile: Optional[str]):
    profile = (profile or "restricted").lower()
    if profile == "restricted":
        return {
            "read": "allow",
            "webfetch": "allow",
            "edit": "deny",
            "bash": "deny",
            "external_directory": {"/workspace/**": "allow"},
        }
    if profile == "standard":
        return {
            "read": "allow",
            "webfetch": "allow",
            "edit": "allow",
            "bash": {
                "git *": "allow",
                "npm *": "allow",
                "pnpm *": "allow",
                "python *": "allow",
                "pytest *": "allow",
                "rm *": "deny",
                "sudo *": "deny",
                "*": "ask",
            },
            "external_directory": {"/workspace/**": "allow", "/tmp/**": "allow"},
        }
    if profile == "dangerous":
        return "allow"
    raise ValueError(f"Unsupported OpenCode permission profile: {profile}")


def parse_opencode_events(output: str, model: Optional[str] = None) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], str]:
    raw_messages: List[Dict] = []
    response_parts: List[str] = []
    session_id = ""
    metadata = ExecutionMetadata()
    metadata.model_name = model or DEFAULT_OPENCODE_MODEL
    metadata.context_window = 200000
    execution_log: List[ExecutionLogEntry] = []

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            response_parts.append(stripped)
            continue
        raw_messages.append(msg)
        session_id = msg.get("sessionID") or msg.get("session_id") or msg.get("sessionId") or session_id
        text = msg.get("text") or msg.get("content") or msg.get("message")
        if isinstance(text, str):
            response_parts.append(text)
        if msg.get("type") in {"tool", "tool_call", "tool-start"}:
            execution_log.append(
                ExecutionLogEntry(
                    id=msg.get("id") or str(uuid.uuid4()),
                    type="tool_use",
                    tool=msg.get("name") or msg.get("tool") or "opencode_tool",
                    input=msg.get("input") if isinstance(msg.get("input"), dict) else None,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                )
            )
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        metadata.input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or metadata.input_tokens
        metadata.output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or metadata.output_tokens

    return "\n".join(part for part in response_parts if part).strip(), execution_log, metadata, raw_messages, session_id


class OpenCodeRuntime(AgentRuntime):
    def is_available(self) -> bool:
        try:
            result = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        return os.getenv("AGENT_RUNTIME_MODEL") or os.getenv("OPENCODE_MODEL") or DEFAULT_OPENCODE_MODEL

    def get_context_window(self, model: Optional[str] = None) -> int:
        return 200000

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        from .trinity_mcp import _configure_opencode_mcp_servers
        return _configure_opencode_mcp_servers(mcp_servers)

    def build_run_command(
        self,
        prompt: str,
        model: Optional[str],
        workspace: str,
        resume_session_id: Optional[str],
        persist_session: bool,
    ) -> List[str]:
        cmd = ["opencode", "run", "--format", "json", "--dir", workspace]
        chosen_model = model or self.get_default_model()
        if chosen_model:
            cmd.extend(["--model", chosen_model])
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        elif persist_session:
            cmd.append("--continue")
        if (os.getenv("OPENCODE_PERMISSION_PROFILE") or "restricted").lower() == "dangerous":
            cmd.append("--dangerously-skip-permissions")
        cmd.append(prompt)
        return cmd

    def _build_env(self, execution_id: str) -> Dict[str, str]:
        env = {**os.environ, EXECUTION_TAG_NAME: execution_id}
        env.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "1")
        env.setdefault("OPENCODE_DISABLE_MODELS_FETCH", "1")
        profile = env.get("OPENCODE_PERMISSION_PROFILE", "restricted")
        env["OPENCODE_PERMISSION"] = json.dumps(build_permission_profile(profile))
        return env

    async def _run_opencode(self, prompt: str, model: Optional[str], system_prompt: Optional[str], timeout_seconds: int, execution_id: Optional[str], resume_session_id: Optional[str], persist_session: bool):
        if not self.is_available():
            raise HTTPException(status_code=503, detail="OpenCode CLI is not available in this container")
        if system_prompt:
            prompt = f"{system_prompt}\n\n{prompt}"
        execution_id = execution_id or str(uuid.uuid4())
        workspace = os.getenv("WORKSPACE_DIR", DEFAULT_WORKSPACE)
        cmd = self.build_run_command(prompt, model, workspace, resume_session_id, persist_session)
        env = self._build_env(execution_id)

        try:
            loop = asyncio.get_running_loop()
            registry = get_process_registry()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                preexec_fn=os_setsid_if_available(),
            )
            registry.register(execution_id, process, {"runtime": "opencode", "cmd": "opencode run"})

            def read_child():
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                    return process.returncode, stdout, stderr
                finally:
                    try:
                        kill_cgroup_orphans()
                    finally:
                        registry.unregister(execution_id)

            return_code, stdout, stderr = await loop.run_in_executor(_executor, read_child)
            if return_code != 0:
                detail = (stderr or stdout or "OpenCode execution failed")[-2000:]
                raise HTTPException(status_code=502, detail=f"OpenCode execution failed: {detail}")
            text, log, metadata, raw, session_id = parse_opencode_events(stdout, model or self.get_default_model())
            metadata.model_name = model or self.get_default_model()
            metadata.execution_id = execution_id
            metadata.session_id = session_id or None
            return text, log, metadata, raw, session_id
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail=f"OpenCode execution timed out after {timeout_seconds}s") from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("OpenCode execution error")
            raise HTTPException(status_code=500, detail=f"OpenCode task execution error: {exc}") from exc

    async def execute(self, prompt: str, model: Optional[str] = None, continue_session: bool = False, stream: bool = False, system_prompt: Optional[str] = None, execution_id: Optional[str] = None):
        text, log, metadata, raw, session_id = await self._run_opencode(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            timeout_seconds=900,
            execution_id=execution_id,
            resume_session_id=None,
            persist_session=continue_session,
        )
        return text, log, metadata, raw

    async def execute_headless(self, prompt: str, model: Optional[str] = None, allowed_tools: Optional[List[str]] = None, system_prompt: Optional[str] = None, timeout_seconds: int = 900, max_turns: Optional[int] = None, execution_id: Optional[str] = None, resume_session_id: Optional[str] = None, persist_session: bool = False, images: Optional[List[Dict]] = None):
        text, log, metadata, raw, session_id = await self._run_opencode(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            timeout_seconds=timeout_seconds,
            execution_id=execution_id,
            resume_session_id=resume_session_id,
            persist_session=persist_session,
        )
        return text, raw, metadata, session_id


_opencode_runtime: Optional[OpenCodeRuntime] = None


def get_opencode_runtime() -> OpenCodeRuntime:
    global _opencode_runtime
    if _opencode_runtime is None:
        _opencode_runtime = OpenCodeRuntime()
    return _opencode_runtime
```

- [ ] **Step 4: Wire runtime factory**

Modify `docker/base-image/agent_server/services/runtime_adapter.py` in `get_runtime()`:

```python
    if runtime_type == "opencode":
        from .opencode_runtime import get_opencode_runtime
        runtime = get_opencode_runtime()
        logger.info("Using OpenCode runtime")
        return runtime

    if runtime_type == "gemini-cli" or runtime_type == "gemini":
```

- [ ] **Step 5: Run runtime tests and verify pass**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_runtime.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add docker/base-image/agent_server/services/opencode_runtime.py docker/base-image/agent_server/services/runtime_adapter.py tests/unit/test_opencode_runtime.py
git commit -m "feat: add OpenCode runtime adapter"
```

---

### Task 2: Add OpenCode MCP Configuration

**Files:**
- Modify: `docker/base-image/agent_server/services/trinity_mcp.py`
- Create: `tests/unit/test_opencode_mcp_config.py`

- [ ] **Step 1: Write failing MCP config tests**

Create `tests/unit/test_opencode_mcp_config.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from agent_server.services import trinity_mcp


def test_inject_trinity_mcp_writes_opencode_config(monkeypatch, tmp_path):
    target_home = tmp_path / "home" / "developer"
    monkeypatch.setattr(trinity_mcp, "OPENCODE_HOME", target_home)
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    monkeypatch.setenv("TRINITY_MCP_URL", "http://trinity-mcp:8080/mcp")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "secret-key")

    assert trinity_mcp.inject_trinity_mcp_if_configured() is True

    config_file = target_home / ".config" / "opencode" / "opencode.json"
    data = json.loads(config_file.read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["mcp"]["trinity"]["type"] == "remote"
    assert data["mcp"]["trinity"]["url"] == "http://trinity-mcp:8080/mcp"
    assert data["mcp"]["trinity"]["headers"]["Authorization"] == "Bearer {env:TRINITY_MCP_API_KEY}"


def test_configure_opencode_local_mcp_servers(monkeypatch, tmp_path):
    target_home = tmp_path / "home" / "developer"
    monkeypatch.setattr(trinity_mcp, "OPENCODE_HOME", target_home)
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")

    ok = trinity_mcp.configure_mcp_servers({
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]}
    })

    assert ok is True
    data = json.loads((target_home / ".config" / "opencode" / "opencode.json").read_text())
    assert data["mcp"]["filesystem"] == {
        "type": "local",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        "enabled": True,
    }
```

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_mcp_config.py -q
```

Expected: fails because `OPENCODE_HOME` and OpenCode MCP functions do not exist.

- [ ] **Step 3: Implement OpenCode MCP helpers**

Modify `docker/base-image/agent_server/services/trinity_mcp.py` near module globals:

```python
OPENCODE_HOME = Path("/home/developer")
```

Modify runtime dispatch:

```python
    if runtime == "opencode":
        return _inject_opencode_mcp(trinity_mcp_url, trinity_mcp_api_key)
    if runtime == "gemini-cli":
        return _inject_gemini_mcp(trinity_mcp_url, trinity_mcp_api_key)
```

and:

```python
    if runtime == "opencode":
        return _configure_opencode_mcp_servers(mcp_servers)
    if runtime == "gemini-cli":
        return _configure_gemini_mcp_servers(mcp_servers)
```

Append helpers:

```python
def _opencode_config_file() -> Path:
    config_dir = OPENCODE_HOME / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "opencode.json"


def _read_opencode_config() -> dict:
    config_file = _opencode_config_file()
    if config_file.exists() and config_file.read_text().strip():
        return json.loads(config_file.read_text())
    return {"$schema": "https://opencode.ai/config.json", "mcp": {}}


def _write_opencode_config(config: dict) -> None:
    config.setdefault("$schema", "https://opencode.ai/config.json")
    config.setdefault("mcp", {})
    _opencode_config_file().write_text(json.dumps(config, indent=2))


def _inject_opencode_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    try:
        config = _read_opencode_config()
        config.setdefault("mcp", {})["trinity"] = {
            "type": "remote",
            "url": trinity_mcp_url,
            "headers": {"Authorization": "Bearer {env:TRINITY_MCP_API_KEY}"},
            "enabled": True,
        }
        _write_opencode_config(config)
        logger.info("Injected Trinity MCP server into OpenCode config")
        return True
    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for OpenCode: {e}")
        return False


def _configure_opencode_mcp_servers(mcp_servers: dict) -> bool:
    try:
        config = _read_opencode_config()
        config.setdefault("mcp", {})
        for server_name, server in mcp_servers.items():
            command = server.get("command")
            args = server.get("args", [])
            if not command:
                logger.warning(f"Skipping MCP server '{server_name}': no command specified")
                continue
            config["mcp"][server_name] = {
                "type": "local",
                "command": [command] + args,
                "enabled": True,
            }
        _write_opencode_config(config)
        return True
    except Exception as e:
        logger.warning(f"Failed to configure MCP servers for OpenCode: {e}")
        return False
```

- [ ] **Step 4: Run MCP tests**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_mcp_config.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add docker/base-image/agent_server/services/trinity_mcp.py tests/unit/test_opencode_mcp_config.py
git commit -m "feat: configure MCP for OpenCode runtime"
```

---

### Task 3: Install OpenCode and Expose Runtime Availability

**Files:**
- Modify: `docker/base-image/Dockerfile`
- Modify: `docker/base-image/agent_server/state.py`
- Modify: `docker/base-image/agent_server/config.py`
- Modify: `docker/base-image/agent_server/routers/info.py`
- Modify: `docker/base-image/agent_server/routers/chat.py`

- [ ] **Step 1: Write failing availability/model tests**

Add to `tests/unit/test_opencode_runtime.py`:

```python
def test_opencode_default_model_can_use_env(monkeypatch):
    from agent_server.services.opencode_runtime import OpenCodeRuntime
    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "openai/gpt-5")
    assert OpenCodeRuntime().get_default_model() == "openai/gpt-5"
```

- [ ] **Step 2: Run targeted test**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_runtime.py::test_opencode_default_model_can_use_env -q
```

Expected: pass if Task 1 implementation is present; if it fails, fix `get_default_model()` before continuing.

- [ ] **Step 3: Update Dockerfile install**

Modify `docker/base-image/Dockerfile` in the npm global install section to include OpenCode:

```dockerfile
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli opencode-ai@latest \
    && npm cache clean --force

ENV OPENCODE_DISABLE_AUTOUPDATE=1
ENV OPENCODE_DISABLE_MODELS_FETCH=1
```

If the Dockerfile currently installs packages in multiple lines, preserve existing npm retry/proxy config and add only `opencode-ai@latest` plus the two `ENV` lines.

- [ ] **Step 4: Update agent-server config comments/constants**

Modify `docker/base-image/agent_server/config.py` so runtime docs mention:

```python
# Runtime selection: "claude-code", "gemini-cli", or "opencode"
AGENT_RUNTIME = os.getenv("AGENT_RUNTIME", "claude-code")
OPENCODE_DEFAULT_MODEL = os.getenv("OPENCODE_DEFAULT_MODEL", "anthropic/claude-sonnet-4-5")
```

Use existing naming style if `AGENT_RUNTIME` already exists.

- [ ] **Step 5: Update state availability detection**

Modify `docker/base-image/agent_server/state.py` where runtime availability is checked. The current state object uses `self.agent_runtime`, not `self.runtime`. Add `_check_opencode()` mirroring the existing `_check_claude_code()` / `_check_gemini_cli()` helpers:

```python
    def _check_opencode(self) -> bool:
        try:
            result = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False
```

Then update runtime availability initialization:

```python
        if self.agent_runtime == "opencode":
            self.runtime_available = self._check_opencode()
        elif self.agent_runtime in ("gemini-cli", "gemini"):
            self.runtime_available = self._check_gemini_cli()
        else:
            self.runtime_available = self._check_claude_code()
```

Update default context window handling so OpenCode gets a deterministic value:

```python
        if self.agent_runtime == "opencode":
            return 200000
```

- [ ] **Step 6: Update info/chat runtime surfaces**

In `docker/base-image/agent_server/routers/chat.py`, include OpenCode models as free-form provider/model values. Add a small list for UI hints:

```python
OPENCODE_MODELS = [
    "anthropic/claude-sonnet-4-5",
    "openai/gpt-5",
    "google/gemini-2.5-pro",
]
```

In `GET /api/model`, preserve the existing response shape (`model`, `runtime`, `available_models`, `note`) and add an OpenCode branch before the Claude fallback:

```python
    if agent_state.agent_runtime == "opencode":
        return {
            "model": agent_state.current_model,
            "runtime": runtime,
            "available_models": OPENCODE_MODELS,
            "note": "OpenCode models use provider/model format, for example anthropic/claude-sonnet-4-5 or openai/gpt-5.",
        }
```

In `PUT /api/model`, add an OpenCode validation branch before the Claude fallback. Accept non-empty provider/model strings and reject Claude-style bare aliases for OpenCode:

```python
    if runtime == "opencode":
        candidate = request.model.strip()
        if candidate and "/" in candidate and not candidate.startswith("/") and not candidate.endswith("/"):
            agent_state.current_model = candidate
            logger.info(f"Model changed to: {candidate}")
            return {
                "status": "success",
                "model": agent_state.current_model,
                "note": "OpenCode model will be used for subsequent messages",
            }
        raise HTTPException(
            status_code=400,
            detail="Invalid OpenCode model. Use provider/model format, for example anthropic/claude-sonnet-4-5.",
        )
```

In `docker/base-image/agent_server/routers/info.py`, preserve `claude_available` and add a neutral runtime availability field if the model supports extras, or add explicit `opencode_available` if the response model is strict:

```python
opencode_available=agent_state.agent_runtime == "opencode" and agent_state.runtime_available
```

- [ ] **Step 7: Run agent-server tests**

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_runtime.py tests/unit/test_agent_server_hardening.py -q
```

Expected: pass.

- [ ] **Step 8: Commit Task 3**

```bash
git add docker/base-image/Dockerfile docker/base-image/agent_server/config.py docker/base-image/agent_server/state.py docker/base-image/agent_server/routers/chat.py docker/base-image/agent_server/routers/info.py tests/unit/test_opencode_runtime.py
git commit -m "feat: expose OpenCode runtime availability"
```

---

### Task 4: Backend Runtime Propagation

**Files:**
- Modify: `src/backend/models.py`
- Modify: `src/backend/services/agent_service/crud.py`
- Modify: `src/backend/services/agent_service/deploy.py`
- Modify: `src/backend/services/docker_service.py`
- Modify: `src/backend/services/task_execution_service.py`
- Modify: `src/backend/main.py`
- Add tests under `tests/unit/` if existing fixtures allow direct imports.

- [ ] **Step 1: Write failing backend model test**

Create `tests/unit/test_opencode_backend_models.py`:

```python
from __future__ import annotations

from models import AgentConfig
from pydantic import ValidationError


def test_agent_config_accepts_opencode_runtime_permission():
    config = AgentConfig(
        name="opencode-agent",
        runtime="opencode",
        runtime_model="openai/gpt-5",
        runtime_permission="standard",
    )

    assert config.runtime == "opencode"
    assert config.runtime_model == "openai/gpt-5"
    assert config.runtime_permission == "standard"


def test_agent_config_rejects_unknown_runtime():
    try:
        AgentConfig(name="bad-runtime", runtime="vim")
    except ValidationError as exc:
        assert "runtime" in str(exc)
    else:
        raise AssertionError("AgentConfig accepted an unsupported runtime")
```

- [ ] **Step 2: Run test and verify fails**

Run:

```bash
PYTHONPATH=src/backend pytest tests/unit/test_opencode_backend_models.py -q
```

Expected: fails because `runtime_permission` is not a model field.

- [ ] **Step 3: Add backend model field**

Modify `src/backend/models.py` imports and `AgentConfig`:

```python
from pydantic import BaseModel, Field, field_validator
```

Then in `AgentConfig`:

```python
    runtime: Optional[str] = "claude-code"  # "claude-code", "gemini-cli", or "opencode"
    runtime_model: Optional[str] = None  # Runtime-specific model override
    runtime_permission: Optional[str] = "restricted"  # OpenCode: restricted, standard, dangerous

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        allowed = {"claude-code", "claude", "gemini-cli", "gemini", "opencode"}
        if value not in allowed:
            raise ValueError(f"Unsupported runtime: {value}")
        return value

    @field_validator("runtime_permission")
    @classmethod
    def validate_runtime_permission(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        allowed = {"restricted", "standard", "dangerous"}
        if value not in allowed:
            raise ValueError(f"Unsupported runtime_permission: {value}")
        return value
```

Update `AgentStatus.runtime` comment similarly.

- [ ] **Step 4: Inject OpenCode env in agent creation**

In `src/backend/services/agent_service/crud.py`, define a normalized runtime before `env_vars` construction:

```python
    runtime = (config.runtime or "claude-code").lower()
```

Then locate container environment construction near existing `AGENT_RUNTIME` and `AGENT_RUNTIME_MODEL`. Ensure these entries are present:

```python
env_vars["AGENT_RUNTIME"] = runtime
if config.runtime_model:
    env_vars["AGENT_RUNTIME_MODEL"] = config.runtime_model
if runtime == "opencode":
    env_vars["OPENCODE_PERMISSION_PROFILE"] = config.runtime_permission or "restricted"
    env_vars["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env_vars["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
```

If the code uses list-style env vars instead of a dict, add equivalent list entries.

- [ ] **Step 5: Avoid Claude subscription token injection for OpenCode**

In `src/backend/services/agent_service/crud.py`, wrap Claude OAuth subscription-token injection so it only runs for Claude Code:

```python
if runtime in {"claude-code", "claude"}:
    # existing Claude subscription token injection
```

Leave `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, and other provider env vars available to OpenCode because OpenCode can use provider envs.

- [ ] **Step 6: Template deploy reads permission**

In `src/backend/services/agent_service/deploy.py`, where `runtime.type` and `runtime.model` are extracted from `template.yaml`, add:

```python
runtime_permission = runtime_config.get("permission", "restricted")
```

and pass it into `AgentConfig(runtime_permission=runtime_permission)`.

- [ ] **Step 7: Docker discovery recognizes OpenCode**

In `src/backend/services/docker_service.py`, fix the label mismatch. Creation currently writes `trinity.agent-runtime`, while fast listing reads `trinity.runtime`. Preserve backward compatibility by reading both, preferring the existing creation label:

```python
runtime = labels.get("trinity.agent-runtime") or labels.get("trinity.runtime") or "claude-code"
```

In `src/backend/services/agent_service/crud.py`, ensure container labels include both keys during creation so old and new readers agree:

```python
"trinity.agent-runtime": runtime,
"trinity.runtime": runtime,
```

Do not normalize `opencode` to another value.

- [ ] **Step 8: Version endpoint lists OpenCode**

In `src/backend/main.py` around `/api/version` payload construction, change supported runtimes to:

```python
"runtimes": ["claude-code", "gemini-cli", "opencode"]
```

- [ ] **Step 9: Task model fallback respects runtime**

In `src/backend/services/task_execution_service.py`, the current code around model fallback only has `agent_name` and `model`. Add a small helper before the fallback to discover runtime/model from Docker labels/env for this agent:

```python
def _get_agent_runtime_defaults(agent_name: str) -> tuple[str, str | None]:
    from services.docker_service import get_agent_container
    container = get_agent_container(agent_name)
    if not container:
        return "claude-code", None
    labels = container.labels or {}
    runtime = labels.get("trinity.agent-runtime") or labels.get("trinity.runtime") or "claude-code"
    env = container.attrs.get("Config", {}).get("Env", []) or []
    env_map = dict(item.split("=", 1) for item in env if "=" in item)
    return runtime, env_map.get("AGENT_RUNTIME_MODEL") or None
```

Then replace the missing-model fallback with:

```python
if agent_runtime == "opencode":
    model = agent_runtime_model or "anthropic/claude-sonnet-4-5"
elif agent_runtime == "gemini-cli":
    model = agent_runtime_model or "gemini-3-flash"
else:
    model = existing_claude_default
```

Use the variable names already available in that function; do not introduce a second source of truth.

The full shape should be:

```python
if model is None:
    agent_runtime, agent_runtime_model = _get_agent_runtime_defaults(agent_name)
    if agent_runtime == "opencode":
        model = agent_runtime_model or "anthropic/claude-sonnet-4-5"
    elif agent_runtime in {"gemini-cli", "gemini"}:
        model = agent_runtime_model or "gemini-3-flash"
    else:
        model = settings_service.get_platform_default_model()
```

- [ ] **Step 10: Run backend tests**

Run:

```bash
PYTHONPATH=src/backend pytest tests/unit/test_opencode_backend_models.py tests/unit/test_926_version_endpoint.py -q
```

Expected: pass.

- [ ] **Step 11: Commit Task 4**

```bash
git add src/backend/models.py src/backend/services/agent_service/crud.py src/backend/services/agent_service/deploy.py src/backend/services/docker_service.py src/backend/services/task_execution_service.py src/backend/main.py tests/unit/test_opencode_backend_models.py
git commit -m "feat: propagate OpenCode runtime through backend"
```

---

### Task 5: Backend Terminal Mode

**Files:**
- Modify: `src/backend/services/agent_service/terminal.py`

- [ ] **Step 1: Write failing terminal mapping test if a test module exists**

Create `tests/unit/test_opencode_terminal.py`. If `terminal.py` has no command-builder helper, extract one in Step 2 and test it here:

```python
from __future__ import annotations

from services.agent_service import terminal


def test_terminal_command_for_opencode_mode():
    cmd = terminal.build_terminal_command(mode="opencode")
    assert cmd[0] == "opencode"
```

- [ ] **Step 2: Implement OpenCode terminal mode**

In `src/backend/services/agent_service/terminal.py`, add a small helper near the existing mode-selection code:

```python
def build_terminal_command(mode: str) -> list[str]:
    if mode == "claude":
        return ["claude"]
    if mode == "gemini":
        return ["gemini"]
    if mode == "opencode":
        return ["opencode"]
    return ["bash"]
```

Then replace the existing inline mode-to-command mapping with this helper. The OpenCode behavior is:

```python
elif mode == "opencode":
    command = ["opencode"]
```

If the existing mapping is a dict, add:

```python
"opencode": ["opencode"],
```

- [ ] **Step 3: Run terminal tests or import check**

Run:

```bash
PYTHONPATH=src/backend python -m py_compile src/backend/services/agent_service/terminal.py
PYTHONPATH=src/backend pytest tests/unit/test_opencode_terminal.py -q
```

Expected: compile exits 0 and the test passes.

- [ ] **Step 4: Commit Task 5**

```bash
git add src/backend/services/agent_service/terminal.py tests/unit/test_opencode_terminal.py
git commit -m "feat: add OpenCode terminal mode"
```

If no test file was created, run:

```bash
git add src/backend/services/agent_service/terminal.py
git commit -m "feat: add OpenCode terminal mode"
```

---

### Task 6: Frontend Runtime UI

**Files:**
- Modify: `src/frontend/src/components/RuntimeBadge.vue`
- Modify: `src/frontend/src/components/AgentTerminal.vue`
- Modify: `src/frontend/src/components/CreateAgentModal.vue`
- Modify: `src/frontend/src/views/AgentDetail.vue`

- [ ] **Step 1: Update RuntimeBadge**

In `RuntimeBadge.vue`, add OpenCode to the runtime display map:

```js
opencode: {
  label: 'OpenCode',
  class: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200',
}
```

If the component uses computed `runtimeLabel` and `badgeClass` instead of a map, add explicit `runtime === 'opencode'` branches.

- [ ] **Step 2: Update AgentTerminal labels/modes**

In `AgentTerminal.vue`, add OpenCode to terminal mode options:

```js
{ value: 'opencode', label: 'OpenCode' }
```

Where default mode is selected from runtime, add:

```js
if (props.agent?.runtime === 'opencode') return 'opencode'
```

- [ ] **Step 3: Update CreateAgentModal runtime choice**

In `CreateAgentModal.vue`, add a runtime option:

```js
{ value: 'opencode', label: 'OpenCode', description: 'Run this agent with OpenCode CLI' }
```

Add a permission select for OpenCode if the modal already has advanced runtime options:

```js
runtime_permission: 'restricted'
```

and submit it with the agent payload:

```js
runtime_permission: form.runtime_permission,
```

- [ ] **Step 4: Update AgentDetail default model logic**

In `AgentDetail.vue`, change runtime-specific default model logic:

```js
if (agent.value?.runtime === 'opencode') {
  return 'anthropic/claude-sonnet-4-5'
}
```

Ensure the model input accepts slash-containing provider/model strings and does not validate OpenCode against Claude short aliases.

- [ ] **Step 5: Build frontend**

Run:

If `node_modules` is absent, run `npm ci` first. Then run `npm run build`:

```bash
test -d node_modules || npm ci
npm run build
```

Working directory:

```text
/Users/yalou/src/trinity/src/frontend
```

Expected: Vite build completes successfully.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/frontend/src/components/RuntimeBadge.vue src/frontend/src/components/AgentTerminal.vue src/frontend/src/components/CreateAgentModal.vue src/frontend/src/views/AgentDetail.vue
git commit -m "feat: add OpenCode runtime UI"
```

---

### Task 7: Template and Documentation Updates

**Files:**
- Modify: `template.yaml`
- Modify: `README.md`
- Modify: `docs/GEMINI_SUPPORT.md` or create `docs/OPENCODE_SUPPORT.md`

- [ ] **Step 1: Preserve OpenCode config/state paths**

Modify `template.yaml` persistent-state allowlist to include:

```yaml
  - opencode.json
  - .opencode/
  - .config/opencode/opencode.json
```

Keep existing `.claude` and `.mcp.json` entries. Do not preserve `.local/share/opencode/` in v1 because it can contain `auth.json` provider credentials and the approved design keeps OpenCode credentials environment-driven.

- [ ] **Step 2: Add OpenCode docs**

Create `docs/OPENCODE_SUPPORT.md` with this content:

````markdown
# OpenCode Runtime Support

Trinity supports OpenCode as an agent runtime alongside Claude Code and Gemini CLI.

Use `runtime: opencode` in templates or choose OpenCode in the create-agent UI.

Example template runtime block:

```yaml
runtime:
  type: opencode
  model: anthropic/claude-sonnet-4-5
  permission: restricted
```

Permission profiles:

- `restricted`: read/web analysis by default; edit and bash denied.
- `standard`: normal development operations allowed; destructive commands denied or ask-based.
- `dangerous`: passes OpenCode's dangerous permission bypass flag.

OpenCode models use provider/model format, for example `anthropic/claude-sonnet-4-5`, `openai/gpt-5`, or `google/gemini-2.5-pro`.
```
````

- [ ] **Step 3: Update README runtime list**

In `README.md`, update the multi-runtime line from Claude/Gemini only to:

```markdown
- **Multi-Runtime Support** — Choose between Claude Code, Gemini CLI, or OpenCode per agent.
```

- [ ] **Step 4: Run markdown-free syntax checks**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in ['docs/OPENCODE_SUPPORT.md', 'README.md', 'template.yaml']:
    assert Path(path).exists(), path
print('DOCS_OK')
PY
```

Expected: `DOCS_OK`.

- [ ] **Step 5: Commit Task 7**

```bash
git add template.yaml README.md docs/OPENCODE_SUPPORT.md
git commit -m "docs: document OpenCode runtime support"
```

---

### Task 8: End-to-End Verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Run backend and agent-server unit tests**

Run:

```bash
PYTHONPATH=src/backend:docker/base-image pytest \
  tests/unit/test_opencode_runtime.py \
  tests/unit/test_opencode_mcp_config.py \
  tests/unit/test_opencode_backend_models.py \
  tests/unit/test_926_version_endpoint.py \
  tests/unit/test_gemini_runtime_pipe_drop.py -q
```

Expected: all pass.

- [ ] **Step 1b: Verify existing runtime factory aliases still resolve**

Add this regression test to `tests/unit/test_opencode_runtime.py`:

```python
def test_existing_runtime_factory_values_still_resolve(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    assert runtime_adapter.get_runtime().__class__.__name__ == "ClaudeCodeRuntime"

    monkeypatch.setenv("AGENT_RUNTIME", "gemini-cli")
    assert runtime_adapter.get_runtime().__class__.__name__ == "GeminiRuntime"
```

Run:

```bash
PYTHONPATH=docker/base-image pytest tests/unit/test_opencode_runtime.py::test_existing_runtime_factory_values_still_resolve -q
```

Expected: pass.

- [ ] **Step 2: Build frontend**

Run:

```bash
npm run build
```

Working directory:

```text
/Users/yalou/src/trinity/src/frontend
```

Expected: build succeeds.

- [ ] **Step 3: Build base image**

Run:

```bash
docker build -f docker/base-image/Dockerfile -t trinity-agent-base:opencode-test docker/base-image
```

Expected: image builds and npm installs `opencode-ai`.

- [ ] **Step 4: Verify OpenCode binary in image**

Run:

```bash
docker run --rm trinity-agent-base:opencode-test opencode --version
```

Expected: prints an OpenCode version and exits 0.

- [ ] **Step 5: Verify runtime factory in image**

Run:

```bash
docker run --rm -e AGENT_RUNTIME=opencode trinity-agent-base:opencode-test python - <<'PY'
from agent_server.services.runtime_adapter import get_runtime
r = get_runtime()
print(r.__class__.__name__)
PY
```

Expected:

```text
OpenCodeRuntime
```

- [ ] **Step 6: Commit any verification-only fixes**

If verification required fixes, commit them with a focused message:

```bash
git add <fixed-files>
git commit -m "fix: complete OpenCode runtime verification"
```

If no fixes were needed, do not create an empty commit.
