from __future__ import annotations

import json
import subprocess

import pytest
from fastapi import HTTPException

from agent_server.services import runtime_adapter


def test_runtime_factory_returns_opencode(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")

    runtime = runtime_adapter.get_runtime()

    assert runtime.__class__.__name__ == "OpenCodeRuntime"
    assert runtime.get_default_model() == "anthropic/claude-sonnet-4-5"


def test_opencode_default_model_can_use_env(monkeypatch):
    from agent_server.services.opencode_runtime import OpenCodeRuntime

    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "openai/gpt-5")

    assert OpenCodeRuntime().get_default_model() == "openai/gpt-5"


def test_opencode_state_current_model_uses_opencode_default(monkeypatch):
    from agent_server import state as state_mod

    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-5")
    monkeypatch.setattr(state_mod.AgentState, "_check_runtime_available", lambda self: True)
    monkeypatch.setattr(state_mod.AgentState, "_check_claude_code", lambda self: False)

    assert state_mod.AgentState().current_model == "openai/gpt-5"


def test_opencode_model_validation_requires_exact_provider_model():
    from agent_server.routers.chat import _is_valid_opencode_model

    assert _is_valid_opencode_model("openai/gpt-5") is True
    assert _is_valid_opencode_model("anthropic/claude-sonnet-4-5") is True
    assert _is_valid_opencode_model("sonnet") is False
    assert _is_valid_opencode_model("openai/gpt/5") is False
    assert _is_valid_opencode_model("openai/") is False
    assert _is_valid_opencode_model("/gpt-5") is False
    assert _is_valid_opencode_model("open ai/gpt-5") is False
    assert _is_valid_opencode_model("openai/gpt 5") is False


def test_opencode_runtime_uses_default_executor_and_no_per_request_version_check():
    from pathlib import Path

    source = Path("docker/base-image/agent_server/services/opencode_runtime.py").read_text()

    assert "ThreadPoolExecutor(max_workers=1" not in source
    assert "run_in_executor(None, run_subprocess)" in source
    assert "if not self.is_available():" not in source


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


def test_opencode_builds_continue_command_when_persisting_without_resume_session():
    from agent_server.services.opencode_runtime import OpenCodeRuntime

    cmd = OpenCodeRuntime().build_run_command(
        prompt="continue latest",
        model=None,
        workspace="/workspace",
        resume_session_id=None,
        persist_session=True,
    )

    assert "--continue" in cmd
    assert "--session" not in cmd


def test_opencode_builds_dangerous_command_with_skip_permissions():
    from agent_server.services.opencode_runtime import OpenCodeRuntime

    cmd = OpenCodeRuntime().build_run_command(
        prompt="do risky work",
        model=None,
        workspace="/workspace",
        resume_session_id=None,
        persist_session=False,
        permission_profile="dangerous",
    )

    assert "--dangerously-skip-permissions" in cmd


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


def test_opencode_subprocess_env_uses_restricted_permissions_for_unset_or_invalid_profile(monkeypatch):
    from agent_server.services.opencode_runtime import build_opencode_subprocess_env, build_permission_profile

    monkeypatch.delenv("OPENCODE_PERMISSION_PROFILE", raising=False)

    env = build_opencode_subprocess_env("exec-env")

    assert env["OPENCODE_PERMISSION"] == json.dumps(build_permission_profile("restricted"))

    monkeypatch.setenv("OPENCODE_PERMISSION_PROFILE", "root")
    invalid_env = build_opencode_subprocess_env("exec-env")
    assert invalid_env["OPENCODE_PERMISSION"] == json.dumps(build_permission_profile("restricted"))


def test_opencode_subprocess_env_uses_configured_permission_profile(monkeypatch):
    from agent_server.services.opencode_runtime import build_opencode_subprocess_env, build_permission_profile

    monkeypatch.setenv("OPENCODE_PERMISSION_PROFILE", "standard")

    env = build_opencode_subprocess_env("exec-env")

    assert env["OPENCODE_PERMISSION"] == json.dumps(build_permission_profile("standard"))


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


def test_opencode_parser_handles_result_final_events():
    from agent_server.services.opencode_runtime import parse_opencode_events

    text, _execution_log, _metadata, _raw_messages, _session_id = parse_opencode_events(
        json.dumps({"type": "result", "result": "done from result"}),
        "openai/gpt-5",
    )

    assert text == "done from result"


def test_opencode_parser_sanitizes_raw_messages_response_and_tool_data():
    from agent_server.services.opencode_runtime import parse_opencode_events
    from agent_server.utils.credential_sanitizer import REDACTION_PLACEHOLDER

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"
    output = '\n'.join([
        json.dumps({"type": "message", "text": f"response has {secret}"}),
        json.dumps({
            "type": "tool_call",
            "id": "tool_secret",
            "name": "bash",
            "input": {"cmd": f"curl -H 'Authorization: Bearer {secret}' example.com"},
        }),
        json.dumps({"type": "tool_result", "id": "tool_secret", "name": "bash", "output": f"token={secret}"}),
    ])

    text, execution_log, _metadata, raw_messages, _session_id = parse_opencode_events(output, "openai/gpt-5")

    assert secret not in text
    assert REDACTION_PLACEHOLDER in text
    assert secret not in json.dumps(raw_messages)
    assert REDACTION_PLACEHOLDER in raw_messages[0]["text"]
    assert secret not in execution_log[0].input["cmd"]
    assert secret not in execution_log[1].output


@pytest.mark.asyncio
async def test_opencode_nonzero_stderr_detail_is_sanitized(monkeypatch):
    from agent_server.services import opencode_runtime

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"

    class FakeProcess:
        pid = 999999
        returncode = 2

        def communicate(self, timeout=None):
            return "", f"failed with OPENAI_API_KEY={secret}"

    monkeypatch.setattr(opencode_runtime.OpenCodeRuntime, "is_available", lambda self: True)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    runtime = opencode_runtime.OpenCodeRuntime()
    with pytest.raises(HTTPException) as exc_info:
        await runtime._run_opencode(
            prompt="hello",
            model="openai/gpt-5",
            timeout_seconds=1,
            execution_id="exec-stderr",
            resume_session_id=None,
            persist_session=False,
        )

    assert exc_info.value.status_code == 500
    assert secret not in exc_info.value.detail
    assert "***REDACTED***" in exc_info.value.detail


@pytest.mark.asyncio
async def test_opencode_system_prompt_reaches_command_prompt(monkeypatch):
    from agent_server.services import opencode_runtime

    captured = {}

    class FakeProcess:
        pid = 999997
        returncode = 0

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

        def communicate(self, timeout=None):
            return json.dumps({"type": "message", "text": "ok"}), ""

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda cmd, **kwargs: FakeProcess(cmd, **kwargs))
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    await opencode_runtime.OpenCodeRuntime()._run_opencode(
        prompt="implement feature",
        model="openai/gpt-5",
        timeout_seconds=1,
        execution_id="exec-system",
        resume_session_id=None,
        persist_session=False,
        system_prompt="be concise",
    )

    final_prompt = captured["cmd"][-1]
    assert "System instructions:\nbe concise" in final_prompt
    assert "User request:\nimplement feature" in final_prompt


@pytest.mark.asyncio
async def test_opencode_timeout_returns_504_if_post_termination_communicate_times_out(monkeypatch):
    from agent_server.services import opencode_runtime

    class FakeProcess:
        pid = 999998
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout)

    monkeypatch.setattr(opencode_runtime.OpenCodeRuntime, "is_available", lambda self: True)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "_terminate_process_group", lambda *args, **kwargs: None)
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    runtime = opencode_runtime.OpenCodeRuntime()
    with pytest.raises(HTTPException) as exc_info:
        await runtime._run_opencode(
            prompt="hello",
            model="openai/gpt-5",
            timeout_seconds=1,
            execution_id="exec-timeout",
            resume_session_id=None,
            persist_session=False,
        )

    assert exc_info.value.status_code == 504
