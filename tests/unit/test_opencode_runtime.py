from __future__ import annotations

import json
import subprocess
import threading
import types

import pytest
from fastapi import HTTPException

from agent_server.services import runtime_adapter


class _FakePipe:
    def __init__(self, lines):
        self._lines = list(lines)
        self.read_lines = []
        self.drained = threading.Event()

    def readline(self):
        if not self._lines:
            self.drained.set()
            return ""
        line = self._lines.pop(0)
        self.read_lines.append(line)
        if not self._lines:
            self.drained.set()
        return line


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


def test_opencode_parser_extracts_nested_text_part_events():
    from agent_server.services.opencode_runtime import parse_opencode_events

    output = "\n".join([
        json.dumps({
            "type": "step_start",
            "sessionID": "ses_open_1",
            "part": {"type": "step-start"},
        }),
        json.dumps({
            "type": "text",
            "sessionID": "ses_open_1",
            "part": {
                "type": "text",
                "text": "Hello. How can I help you?",
                "time": {"start": 1, "end": 2},
            },
        }),
        json.dumps({
            "type": "step_finish",
            "sessionID": "ses_open_1",
            "part": {"type": "step-finish", "tokens": {"input": 3, "output": 4}},
        }),
    ])

    text, _execution_log, _metadata, raw_messages, _session_id = parse_opencode_events(
        output,
        "deepseek-openai/deepseek-v4-flash",
    )

    assert text == "Hello. How can I help you?"
    assert raw_messages[1]["part"]["text"] == "Hello. How can I help you?"


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
async def test_execute_headless_publishes_stdout_json_events_live(monkeypatch):
    from agent_server.services import opencode_runtime

    published = []
    event_released = threading.Event()
    wait_called = threading.Event()
    stdout = _FakePipe([
        json.dumps({"type": "session", "sessionID": "ses_live"}) + "\n",
        json.dumps({"type": "message", "text": "hello live"}) + "\n",
        "not json\n",
    ])
    stderr = _FakePipe([])

    class FakeRegistry:
        def register(self, execution_id, process, metadata=None):
            pass

        def publish_log_entry_threadsafe(self, execution_id, entry):
            published.append((execution_id, entry))
            if len(published) == 2:
                event_released.set()

        def active_execution_pids(self, exclude_execution_id=None):
            return []

        def unregister_threadsafe(self, execution_id):
            pass

    class FakeProcess:
        pid = 999996
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        def wait(self, timeout=None):
            wait_called.set()
            assert event_released.wait(timeout=1)
            stdout.drained.wait(timeout=1)
            stderr.drained.wait(timeout=1)
            return self.returncode

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: FakeRegistry())
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    text, raw_messages, metadata, session_id = await opencode_runtime.OpenCodeRuntime().execute_headless(
        prompt="hello",
        model="openai/gpt-5",
        timeout_seconds=1,
        execution_id="exec-live",
    )

    assert wait_called.is_set()
    assert text == "hello live"
    assert session_id == "ses_live"
    assert metadata.execution_id == "exec-live"
    assert raw_messages == [
        {"type": "session", "sessionID": "ses_live"},
        {"type": "message", "text": "hello live"},
    ]
    assert published == [
        ("exec-live", {"type": "session", "sessionID": "ses_live"}),
        ("exec-live", {"type": "message", "text": "hello live"}),
    ]


@pytest.mark.asyncio
async def test_opencode_publish_failure_does_not_stop_stdout_parsing(monkeypatch):
    from agent_server.services import opencode_runtime

    published = []
    stdout = _FakePipe([
        json.dumps({"type": "message", "text": "first"}) + "\n",
        json.dumps({"type": "message", "text": "second"}) + "\n",
    ])
    stderr = _FakePipe([])

    class FakeRegistry:
        def register(self, execution_id, process, metadata=None):
            pass

        def publish_log_entry_threadsafe(self, execution_id, entry):
            published.append((execution_id, entry))
            if len(published) == 1:
                raise RuntimeError("transient publish failure")

        def active_execution_pids(self, exclude_execution_id=None):
            return []

        def unregister_threadsafe(self, execution_id):
            pass

    class FakeProcess:
        pid = 999994
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        def wait(self, timeout=None):
            stdout.drained.wait(timeout=1)
            stderr.drained.wait(timeout=1)
            return self.returncode

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: FakeRegistry())
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    text, raw_messages, _metadata, _session_id = await opencode_runtime.OpenCodeRuntime().execute_headless(
        prompt="hello",
        model="openai/gpt-5",
        timeout_seconds=1,
        execution_id="exec-publish-fail",
    )

    assert text == "first\nsecond"
    assert raw_messages == [
        {"type": "message", "text": "first"},
        {"type": "message", "text": "second"},
    ]
    assert published == [
        ("exec-publish-fail", {"type": "message", "text": "first"}),
        ("exec-publish-fail", {"type": "message", "text": "second"}),
    ]


@pytest.mark.asyncio
async def test_opencode_drains_stderr_concurrently(monkeypatch):
    from agent_server.services import opencode_runtime

    stdout = _FakePipe([json.dumps({"type": "message", "text": "ok"}) + "\n"])
    stderr = _FakePipe([f"stderr {index}\n" for index in range(5)])

    class FakeProcess:
        pid = 999995
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        def wait(self, timeout=None):
            assert stderr.drained.wait(timeout=1)
            stdout.drained.wait(timeout=1)
            return self.returncode

    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    text, raw_messages, _metadata, _session_id = await opencode_runtime.OpenCodeRuntime().execute_headless(
        prompt="hello",
        model="openai/gpt-5",
        timeout_seconds=1,
        execution_id="exec-stderr-live",
    )

    assert text == "ok"
    assert raw_messages == [{"type": "message", "text": "ok"}]
    assert stderr.read_lines == [f"stderr {index}\n" for index in range(5)]


@pytest.mark.asyncio
async def test_opencode_nonzero_stderr_detail_is_sanitized(monkeypatch):
    from agent_server.services import opencode_runtime

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"

    class FakeProcess:
        pid = 999999
        returncode = 2
        stdout = _FakePipe([])
        stderr = _FakePipe([f"failed with OPENAI_API_KEY={secret}\n"])

        def wait(self, timeout=None):
            self.stderr.drained.wait(timeout=1)
            return self.returncode

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
            self.stdout = _FakePipe([json.dumps({"type": "message", "text": "ok"}) + "\n"])
            self.stderr = _FakePipe([])

        def wait(self, timeout=None):
            self.stdout.drained.wait(timeout=1)
            self.stderr.drained.wait(timeout=1)
            return self.returncode

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

    joined_threads = []

    class FakeThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            pass

        def join(self, timeout=None):
            joined_threads.append((self.target.__name__, timeout))

    class FakeProcess:
        pid = 999998
        returncode = None
        stdout = _FakePipe([])
        stderr = _FakePipe([])

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout)

    monkeypatch.setattr(opencode_runtime.OpenCodeRuntime, "is_available", lambda self: True)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "threading", types.SimpleNamespace(Thread=FakeThread))
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
    assert joined_threads == [("read_stdout", 1), ("read_stderr", 1)]
