"""OpenCode CLI runtime implementation for Trinity agents."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.credential_sanitizer import sanitize_dict, sanitize_text
from ..utils.orphan_sweep import kill_cgroup_orphans
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime
from .subprocess_lifecycle import _capture_pgid, _terminate_process_group

logger = logging.getLogger(__name__)


def build_permission_profile(profile: str):
    """Return OpenCode permission JSON for a Trinity permission profile."""
    if profile == "restricted":
        return {
            "read": "allow",
            "edit": "deny",
            "write": "deny",
            "bash": "deny",
            "webfetch": "deny",
        }
    if profile == "standard":
        return {
            "read": "allow",
            "edit": "allow",
            "write": "allow",
            "bash": {
                "rm *": "deny",
                "rm -r *": "deny",
                "rm -rf *": "deny",
                "sudo *": "deny",
                "git push *": "deny",
                "allow": "ask",
            },
            "webfetch": "allow",
        }
    if profile == "dangerous":
        return "allow"
    raise ValueError(f"Unsupported OpenCode permission profile: {profile}")


def get_effective_permission_profile() -> str:
    """Return a supported OpenCode permission profile, defaulting safely."""
    profile = os.getenv("OPENCODE_PERMISSION_PROFILE") or "restricted"
    try:
        build_permission_profile(profile)
    except ValueError:
        return "restricted"
    return profile


def build_opencode_subprocess_env(execution_id: str) -> Dict[str, str]:
    """Build subprocess environment with execution and permission controls."""
    profile = get_effective_permission_profile()
    return {
        **os.environ,
        EXECUTION_TAG_NAME: execution_id,
        "OPENCODE_PERMISSION": json.dumps(build_permission_profile(profile)),
    }


def build_prompt(prompt: str, system_prompt: Optional[str] = None) -> str:
    """Compose final prompt sent to OpenCode."""
    if not system_prompt:
        return prompt
    return f"System instructions:\n{system_prompt}\n\nUser request:\n{prompt}"


def parse_opencode_events(
    output: str,
    model: Optional[str] = None,
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], Optional[str]]:
    """Parse OpenCode JSON/JSONL output into Trinity execution models."""
    response_parts: List[str] = []
    execution_log: List[ExecutionLogEntry] = []
    raw_messages: List[Dict] = []
    metadata = ExecutionMetadata(model_name=model)
    session_id: Optional[str] = None
    now = datetime.utcnow().isoformat() + "Z"

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event = sanitize_dict(event)
        raw_messages.append(event)
        event_type = event.get("type")

        if event_type == "session":
            session_id = event.get("sessionID") or event.get("session_id") or event.get("id")
            metadata.session_id = session_id
        elif event_type in {"message", "assistant_message", "result", "final"}:
            for key in ("text", "content", "message", "result"):
                text = event.get(key)
                if isinstance(text, str) and text:
                    response_parts.append(sanitize_text(text))
                    break
        elif event_type in {"tool_call", "tool_use"}:
            tool_id = str(event.get("id") or event.get("callID") or uuid.uuid4())
            tool_name = str(event.get("name") or event.get("tool") or "unknown")
            tool_input = sanitize_dict(event.get("input")) if isinstance(event.get("input"), dict) else {}
            execution_log.append(
                ExecutionLogEntry(
                    id=tool_id,
                    type="tool_use",
                    tool=tool_name,
                    input=tool_input,
                    timestamp=str(event.get("timestamp") or now),
                )
            )
        elif event_type in {"tool_result", "tool_output"}:
            tool_id = str(event.get("id") or event.get("callID") or uuid.uuid4())
            tool_name = str(event.get("name") or event.get("tool") or "unknown")
            output_value = event.get("output") or event.get("text") or event.get("content")
            if output_value is not None and not isinstance(output_value, str):
                output_value = json.dumps(output_value)
            if isinstance(output_value, str):
                output_value = sanitize_text(output_value)
            execution_log.append(
                ExecutionLogEntry(
                    id=tool_id,
                    type="tool_result",
                    tool=tool_name,
                    output=output_value,
                    success=event.get("success"),
                    duration_ms=event.get("duration_ms"),
                    timestamp=str(event.get("timestamp") or now),
                )
            )
        elif event_type == "usage":
            usage = event.get("usage") or event
            metadata.input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
            metadata.output_tokens = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
            if usage.get("cost_usd") is not None:
                metadata.cost_usd = usage.get("cost_usd")

    metadata.tool_count = len([entry for entry in execution_log if entry.type == "tool_use"])
    if not metadata.model_name:
        metadata.model_name = model
    return "\n".join(response_parts), execution_log, metadata, raw_messages, session_id


class OpenCodeRuntime(AgentRuntime):
    """OpenCode implementation of the AgentRuntime interface."""

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["opencode", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        return os.getenv("AGENT_RUNTIME_MODEL") or os.getenv("OPENCODE_DEFAULT_MODEL") or "anthropic/claude-sonnet-4-5"

    def get_context_window(self, model: Optional[str] = None) -> int:
        return 200000

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        del mcp_servers
        # OpenCode MCP config generation is implemented in a later plan task.
        return True

    def build_run_command(
        self,
        *,
        prompt: str,
        model: Optional[str],
        workspace: str,
        resume_session_id: Optional[str],
        persist_session: bool,
        permission_profile: Optional[str] = None,
    ) -> List[str]:
        cmd = ["opencode", "run", "--format", "json", "--dir", workspace]
        if model:
            cmd.extend(["--model", model])
        if permission_profile == "dangerous":
            cmd.append("--dangerously-skip-permissions")
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        elif persist_session:
            cmd.append("--continue")
        cmd.append(prompt)
        return cmd

    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        del stream
        text, raw_messages, metadata, _session_id = await self._run_opencode(
            prompt=prompt,
            model=model,
            timeout_seconds=900,
            execution_id=execution_id,
            resume_session_id=None,
            persist_session=continue_session,
            system_prompt=system_prompt,
        )
        parsed_text, execution_log, parsed_metadata, parsed_raw, _ = parse_opencode_events(
            "\n".join(json.dumps(message) for message in raw_messages),
            model or self.get_default_model(),
        )
        if not parsed_text and text:
            parsed_text = text
        parsed_metadata.execution_id = metadata.execution_id
        return parsed_text, execution_log, parsed_metadata, parsed_raw

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[Dict], ExecutionMetadata, str]:
        del allowed_tools, max_turns, images
        text, raw_messages, metadata, session_id = await self._run_opencode(
            prompt=prompt,
            model=model,
            timeout_seconds=timeout_seconds,
            execution_id=execution_id,
            resume_session_id=resume_session_id,
            persist_session=persist_session,
            system_prompt=system_prompt,
        )
        return text, raw_messages, metadata, session_id or metadata.execution_id or ""

    async def _run_opencode(
        self,
        *,
        prompt: str,
        model: Optional[str],
        timeout_seconds: int,
        execution_id: Optional[str],
        resume_session_id: Optional[str],
        persist_session: bool,
        system_prompt: Optional[str] = None,
    ) -> Tuple[str, List[Dict], ExecutionMetadata, Optional[str]]:
        execution_id = execution_id or str(uuid.uuid4())
        workspace = os.getenv("WORKSPACE_DIR") or str(Path.cwd())
        effective_model = model or self.get_default_model()
        permission_profile = get_effective_permission_profile()
        final_prompt = build_prompt(prompt, system_prompt)
        cmd = self.build_run_command(
            prompt=final_prompt,
            model=effective_model,
            workspace=workspace,
            resume_session_id=resume_session_id,
            persist_session=persist_session,
            permission_profile=permission_profile,
        )
        registry = get_process_registry()

        def run_subprocess() -> Tuple[str, int]:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=build_opencode_subprocess_env(execution_id),
            )
            process_pgid = _capture_pgid(process)
            registry.register(execution_id, process, metadata={
                "type": "opencode",
                "message_preview": prompt[:100],
                "pgid": process_pgid,
            })
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid, execution_tag=execution_id)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "[OpenCode] process still did not drain after timeout teardown; returning 504"
                        )
                    raise HTTPException(status_code=504, detail=f"OpenCode execution timed out after {timeout_seconds} seconds")
                if process.returncode != 0:
                    detail = sanitize_text(stderr or stdout or "OpenCode failed")[:300]
                    raise HTTPException(status_code=500, detail=f"OpenCode execution failed (exit code {process.returncode}): {detail}")
                return stdout, process.returncode or 0
            finally:
                try:
                    preserve = registry.active_execution_pids(exclude_execution_id=execution_id)
                    kill_cgroup_orphans(extra_pids=preserve)
                except Exception:
                    logger.exception("[OpenCode] cgroup sweep raised — continuing")
                registry.unregister(execution_id)

        loop = asyncio.get_event_loop()
        stdout, _return_code = await loop.run_in_executor(None, run_subprocess)
        text, _execution_log, metadata, raw_messages, session_id = parse_opencode_events(stdout, effective_model)
        metadata.execution_id = execution_id
        return text, raw_messages, metadata, session_id


_opencode_runtime: Optional[OpenCodeRuntime] = None


def get_opencode_runtime() -> OpenCodeRuntime:
    """Get or create the global OpenCode runtime instance."""
    global _opencode_runtime
    if _opencode_runtime is None:
        _opencode_runtime = OpenCodeRuntime()
    return _opencode_runtime
