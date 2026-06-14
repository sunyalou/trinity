"""Unit tests for the Codex runtime (#1187).

Covers the pure, deterministic pieces of ``codex_runtime.py``:

  * ``calculate_codex_cost`` — token→cost with cached-input pricing and the
    "reasoning is a subset of output, don't double-count" rule.
  * ``parse_codex_jsonl`` — Codex ``--json`` event stream → ExecutionMetadata,
    response text, tool activity log, and error classification.
  * ``_finalize_codex_response`` — the ``-o`` output file is authoritative; the
    JSONL ``agent_message`` is only a fallback.
  * ``CodexRuntime.capabilities`` — the conservative-but-honest capability flags.
  * error→HTTP mapping in ``execute_headless`` — a dropped subprocess pipe is a
    502 (NOT 503, which collides with the SUB-003 auth switch); a generic
    runtime failure is a 500 (NOT 503 — it must not be read as AUTH).

The async subprocess itself is stubbed; these tests never spawn ``codex``.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import codex_runtime  # noqa: E402
from agent_server.services.codex_runtime import (  # noqa: E402
    CodexRuntime,
    calculate_codex_cost,
    parse_codex_jsonl,
    _finalize_codex_response,
)
from agent_server.services.runtime_adapter import RuntimeCapabilities  # noqa: E402


# ---------------------------------------------------------------------------
# calculate_codex_cost
# ---------------------------------------------------------------------------

def test_calculate_codex_cost_no_reasoning_double_count():
    """reasoning_output_tokens is a SUBSET of output_tokens — cost must be
    output_tokens * rate, never output_tokens + reasoning_output_tokens."""
    model = "gpt-5.1-codex"
    rates = codex_runtime.CODEX_PRICING[model]

    # 0 cached so the input side is unambiguous.
    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=500,  # of which 300 were reasoning
        model=model,
    )
    expected = (1000 / 1000) * rates["input"] + (500 / 1000) * rates["output"]
    assert cost == pytest.approx(round(expected, 6))

    # Sanity: a (buggy) double-count would be strictly larger.
    double_counted = (1000 / 1000) * rates["input"] + (
        (500 + 300) / 1000
    ) * rates["output"]
    assert cost < double_counted


def test_calculate_codex_cost_cached_pricing():
    """Cached input tokens bill at the cheaper cached rate; only the
    uncached remainder bills at the full input rate."""
    model = "gpt-5.1-codex"
    rates = codex_runtime.CODEX_PRICING[model]

    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=0,
        model=model,
    )
    expected = (600 / 1000) * rates["input"] + (400 / 1000) * rates["cached"]
    assert cost == pytest.approx(round(expected, 6))
    # Cached must be cheaper than billing all 1000 at the full input rate.
    assert cost < (1000 / 1000) * rates["input"]


def test_calculate_codex_cost_default_fallback():
    """An unknown model falls back to the 'default' pricing, never KeyErrors."""
    cost = calculate_codex_cost(
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=1000,
        model="some-future-model-x",
    )
    rates = codex_runtime.CODEX_PRICING["default"]
    expected = (1000 / 1000) * rates["input"] + (1000 / 1000) * rates["output"]
    assert cost == pytest.approx(round(expected, 6))


def test_calculate_codex_cost_cached_never_exceeds_input():
    """Defensive: cached_input_tokens > input_tokens must not produce a
    negative uncached charge."""
    cost = calculate_codex_cost(
        input_tokens=100,
        cached_input_tokens=500,  # nonsensical, but must not go negative
        output_tokens=0,
        model="gpt-5.1-codex",
    )
    assert cost >= 0


# ---------------------------------------------------------------------------
# parse_codex_jsonl
# ---------------------------------------------------------------------------

def _events_to_lines(events) -> list[str]:
    return [json.dumps(e) for e in events]


def test_codex_runtime_parser_thread_and_usage():
    """thread.started → session_id; turn.completed.usage → tokens + cost
    (cached → cache_read_tokens; reasoning NOT double-counted)."""
    events = [
        {"type": "thread.started", "thread_id": "thr_abc123"},
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 2000,
                "cached_input_tokens": 500,
                "output_tokens": 800,
                "reasoning_output_tokens": 300,
            },
        },
    ]
    response, log, metadata, raw = parse_codex_jsonl(
        _events_to_lines(events), model="gpt-5.1-codex"
    )

    assert metadata.session_id == "thr_abc123"
    assert metadata.input_tokens == 2000
    assert metadata.output_tokens == 800
    assert metadata.cache_read_tokens == 500
    assert metadata.cost_usd is not None and metadata.cost_usd > 0
    # raw transcript captured every line
    assert len(raw) == len(events)


def test_codex_runtime_parser_agent_message_response():
    """item.completed/agent_message text becomes the response (JSONL fallback)."""
    events = [
        {"type": "thread.started", "thread_id": "thr_x"},
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "Hello world"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    assert "Hello world" in response


def test_codex_runtime_parser_tool_items():
    """command_execution / web_search items produce tool activity log entries
    and bump the tool count."""
    events = [
        {"type": "thread.started", "thread_id": "thr_y"},
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_1",
                "type": "command_execution",
                "command": "ls -la",
                "exit_code": 0,
                "status": "completed",
                "aggregated_output": "a\nb\n",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "ws_1",
                "type": "web_search",
                "query": "trinity agent platform",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    tool_uses = [e for e in log if e.type == "tool_use"]
    assert len(tool_uses) >= 2
    tools = {e.tool for e in tool_uses}
    # command_execution maps to a shell-ish tool; web_search to a search tool
    assert any("ash" in t or "hell" in t or "ommand" in t for t in tools)
    assert any("earch" in t for t in tools)


def test_codex_runtime_parser_turn_failed():
    """turn.failed → error_type/error_message captured on metadata."""
    events = [
        {"type": "thread.started", "thread_id": "thr_z"},
        {"type": "turn.failed", "error": {"message": "model is overloaded"}},
    ]
    response, log, metadata, raw = parse_codex_jsonl(_events_to_lines(events))
    assert metadata.error_message and "overloaded" in metadata.error_message


def test_codex_runtime_parser_tolerates_garbage_lines():
    """A non-JSON line must not kill the parser — later events still parse."""
    lines = [
        "not json at all",
        json.dumps({"type": "thread.started", "thread_id": "thr_g"}),
        "{ partial",
        json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}
        ),
    ]
    response, log, metadata, raw = parse_codex_jsonl(lines)
    assert metadata.session_id == "thr_g"
    assert metadata.input_tokens == 5


# ---------------------------------------------------------------------------
# -o output file is authoritative
# ---------------------------------------------------------------------------

def test_finalize_response_prefers_output_file():
    """The -o file content (durable record) wins over JSONL-assembled parts."""
    assert (
        _finalize_codex_response("FROM FILE", ["from jsonl"]) == "FROM FILE"
    )


def test_finalize_response_falls_back_to_jsonl():
    """When the -o file is empty/missing, fall back to JSONL response parts."""
    assert _finalize_codex_response(None, ["part a", "part b"]) == "part a\npart b"
    assert _finalize_codex_response("", ["only jsonl"]) == "only jsonl"


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_codex_capabilities():
    caps = CodexRuntime.capabilities()
    assert isinstance(caps, RuntimeCapabilities)
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is False  # MVP: Session tab stays Claude/Gemini
    assert caps.mcp_support is True
    assert caps.cost_reporting == "estimated"  # no native cost → derived from tokens


# ---------------------------------------------------------------------------
# error → HTTP status mapping (execute_headless)
# ---------------------------------------------------------------------------

@pytest.fixture
def available_runtime(monkeypatch):
    """A CodexRuntime whose is_available() is forced True so the early 503
    availability guard doesn't fire."""
    rt = CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    return rt


@pytest.mark.asyncio
async def test_codex_runtime_pipe_drop(available_runtime, monkeypatch):
    """A BrokenPipeError from the subprocess collector → HTTP 502, NOT 503.
    503 collides with the SUB-003 auth-switch; an early child exit is not auth."""

    async def _raise_pipe(**_kw):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_pipe)

    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute_headless(prompt="hello")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_codex_generic_failure_not_auth(available_runtime, monkeypatch, caplog):
    """A generic runtime failure surfaces as 500 (runtime-unavailable), NEVER
    503 — so the backend never infers error_code=AUTH for a non-auth Codex
    failure (#1187 decision 3)."""

    async def _raise_runtime(**_kw):
        raise RuntimeError("codex blew up for an unrelated reason")

    monkeypatch.setattr(available_runtime, "_execute_codex", _raise_runtime)

    with pytest.raises(HTTPException) as exc_info:
        await available_runtime.execute_headless(prompt="hello")
    assert exc_info.value.status_code == 500
    assert exc_info.value.status_code != 503


@pytest.mark.asyncio
async def test_codex_unavailable_raises_503(monkeypatch):
    """When the codex CLI isn't installed, execute_headless fails fast 503."""
    rt = CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        await rt.execute_headless(prompt="hi")
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# I1 — _classify_codex_failure: AUTH detection must be anchored, not a bare
# substring match (a non-auth failure that merely contains "401" → 500, not 503)
# ---------------------------------------------------------------------------

from agent_server.models import ExecutionMetadata  # noqa: E402


def _classify(stderr: str = "", error_message=None, return_code: int = 1):
    meta = ExecutionMetadata()
    meta.error_message = error_message
    return codex_runtime._classify_codex_failure(return_code, stderr, meta)


def test_classify_bare_401_is_not_auth():
    """A non-auth failure whose output merely contains '401' (e.g. an upstream
    tool returning 401) must map to 500, NOT 503 — 503 is the backend AUTH
    signal the dispatch breaker counts (#1187 decision 3)."""
    status, _ = _classify(stderr="tool exited: upstream returned 401 Not Found")
    assert status == 500


def test_classify_bare_api_key_phrase_is_not_auth():
    """A failure mentioning 'api key' in passing (no auth verb) is not an auth
    failure — drop the over-broad bare 'api key' marker."""
    status, _ = _classify(stderr="please set the weather api key in your tool config")
    assert status == 500


def test_classify_real_unauthorized_is_auth():
    status, detail = _classify(stderr="Error: 401 Unauthorized")
    assert status == 503
    assert "OPENAI_API_KEY" in detail


def test_classify_invalid_api_key_is_auth():
    status, _ = _classify(error_message="invalid_api_key: the provided key is wrong")
    assert status == 503


def test_classify_rate_limit_still_429():
    status, _ = _classify(stderr="429 Too Many Requests: rate limit exceeded")
    assert status == 429


def test_classify_generic_failure_is_500():
    status, _ = _classify(stderr="segfault in tool runner", return_code=139)
    assert status == 500


# ---------------------------------------------------------------------------
# I3 — end-of-options separator: the built command ends with "--" so a prompt
# starting with "-"/"--" is parsed as the positional prompt, never as a flag.
# ---------------------------------------------------------------------------

def test_build_command_ends_with_end_of_options_separator():
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model="gpt-5.1-codex",
        sandbox_mode="danger-full-access",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id=None,
    )
    # The positional prompt is appended by _execute_codex right after this list;
    # "--" must be the final token so the prompt can never be read as options.
    assert cmd[-1] == "--"


# ---------------------------------------------------------------------------
# F-TOOLS (#1187 E2E): codex's own bubblewrap sandbox cannot create a user
# namespace inside the hardened Trinity container ("bwrap: No permissions to
# create a new namespace"), which blocks EVERY shell tool. Normal (writable)
# agents must therefore run with --sandbox danger-full-access (no internal
# sandbox; the Trinity container is the boundary, same posture as Claude/Gemini).
# Read-only mode keeps --sandbox read-only.
# ---------------------------------------------------------------------------

def test_resolve_sandbox_normal_is_danger_full_access(monkeypatch):
    """Normal (writable) mode maps to danger-full-access so codex skips its own
    bwrap sandbox (which can't run in the hardened container)."""
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    assert codex_runtime._resolve_sandbox_mode() == "danger-full-access"


def test_resolve_sandbox_read_only_stays_read_only(monkeypatch):
    """Read-only mode keeps the sandbox-native read-only enforcement."""
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: True)
    assert codex_runtime._resolve_sandbox_mode() == "read-only"


def test_build_command_normal_mode_has_no_network_access_override():
    """danger-full-access already permits network — the old
    `-c sandbox_workspace_write.network_access=true` override is dead and must
    not be emitted (it would be ignored at best, confusing at worst)."""
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model="gpt-5.1-codex",
        sandbox_mode="danger-full-access",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id=None,
    )
    joined = " ".join(cmd)
    assert "danger-full-access" in cmd
    assert "network_access" not in joined
    assert "sandbox_workspace_write" not in joined


def test_build_command_resume_still_ends_with_separator():
    rt = CodexRuntime()
    cmd = rt._build_codex_command(
        model=None,
        sandbox_mode="read-only",
        result_file="/tmp/out.txt",
        agent_home="/home/developer",
        resume_thread_id="thr_123",
    )
    assert "resume" in cmd and "thr_123" in cmd
    assert cmd[-1] == "--"
    # Arg ORDER is load-bearing: the `resume` sub-subcommand (codex 0.139.0) has
    # a narrower option set and rejects exec-level flags that trail it
    # ("unexpected argument '-C' found", exit 2). They MUST precede `resume`, and
    # `resume <id>` must be the last tokens before the "--" separator. Guards the
    # turn-2+ continuity regression.
    resume_at = cmd.index("resume")
    for exec_flag in ("--json", "-C", "--sandbox", "-o", "--skip-git-repo-check"):
        assert cmd.index(exec_flag) < resume_at, f"{exec_flag} must precede resume"
    assert cmd[resume_at + 1] == "thr_123"
    assert cmd[-2:] == ["thr_123", "--"]


# ---------------------------------------------------------------------------
# I4 — thread-id fallback: when Codex emits no thread.started, degrade to a
# fresh next turn (None), never a fabricated id that would wedge resume.
# ---------------------------------------------------------------------------

def test_resolve_session_id_none_when_thread_missing():
    meta = ExecutionMetadata()  # session_id stays None
    assert codex_runtime._resolve_returned_session_id(meta) is None


def test_resolve_session_id_is_thread_id_when_present():
    meta = ExecutionMetadata()
    meta.session_id = "thr_real"
    assert codex_runtime._resolve_returned_session_id(meta) == "thr_real"


@pytest.mark.asyncio
async def test_chat_does_not_cache_fake_thread_id(available_runtime, monkeypatch):
    """A chat turn that returns no thread id must leave _chat_thread_id None so
    the next turn runs fresh rather than `codex exec resume <fake-uuid>`."""

    async def _fake(**_kw):
        return ("resp", [], ExecutionMetadata(), [], None)

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute("hello")
    assert available_runtime._chat_thread_id is None


# ---------------------------------------------------------------------------
# I5 — .env key parser tolerates `export ` prefix and inline comments.
# ---------------------------------------------------------------------------

def _write_env(tmp_path, monkeypatch, contents: str):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(contents)


def test_load_api_key_tolerates_export_prefix(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "export OPENAI_API_KEY=sk-exported\n")
    assert codex_runtime._load_openai_api_key() == "sk-exported"


def test_load_api_key_strips_inline_comment(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "OPENAI_API_KEY=sk-plain # primary key\n")
    assert codex_runtime._load_openai_api_key() == "sk-plain"


def test_load_api_key_quoted_value_keeps_hash(tmp_path, monkeypatch):
    """A '#' inside a quoted value is part of the value, not a comment."""
    _write_env(tmp_path, monkeypatch, 'OPENAI_API_KEY="sk-a#b"\n')
    assert codex_runtime._load_openai_api_key() == "sk-a#b"


def test_load_api_key_plain_still_works(tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, "CODEX_API_KEY=ck-123\n")
    assert codex_runtime._load_openai_api_key() == "ck-123"


# ---------------------------------------------------------------------------
# I2 — reader-executor selection: the concurrent /api/task path uses the
# unbounded default executor; the lock-serialized chat path keeps the bounded
# single-worker executor (parity with Claude's headless path).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_headless_requests_concurrent_reader(available_runtime, monkeypatch):
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], "thr")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute_headless(prompt="hi")
    assert captured.get("concurrent_reader") is True


@pytest.mark.asyncio
async def test_chat_requests_serialized_reader(available_runtime, monkeypatch):
    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return ("r", [], ExecutionMetadata(), [], "thr")

    monkeypatch.setattr(available_runtime, "_execute_codex", _fake)
    await available_runtime.execute("hi")
    assert captured.get("concurrent_reader") is False


# ---------------------------------------------------------------------------
# CSO #1187 finding 2 — the -o result filename is derived from execution_id;
# a '/' or '..' must never let it escape CODEX_HOME (defense-in-depth).
# ---------------------------------------------------------------------------

def test_safe_result_token_blocks_traversal(tmp_path):
    codex_home = str(tmp_path)
    for hostile in ("../../etc/passwd", "a/b/c", "..", "../sibling"):
        token = codex_runtime._safe_result_token(hostile)
        assert "/" not in token and "\\" not in token
        path = os.path.join(codex_home, f"{token}-last.txt")
        # The result file stays directly inside CODEX_HOME — no escape.
        assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(codex_home)


def test_safe_result_token_passes_clean_id_through():
    clean = "exec_AbC-123_def.456"
    assert codex_runtime._safe_result_token(clean) == clean


def test_safe_result_token_never_empty():
    # A pathological all-separator id still yields a usable filename token.
    assert codex_runtime._safe_result_token("/") == "codex"
    assert codex_runtime._safe_result_token("") == "codex"


# ---------------------------------------------------------------------------
# CSO #1187 finding 3 — read-only detection: an absent config is the normal
# writable state (silent); a present-but-corrupt config fails OPEN but LOGS
# (parity with the Claude reference hook, which also fails open + logs).
# ---------------------------------------------------------------------------

def test_is_read_only_missing_file_is_silent(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", tmp_path / "ro.json")
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    assert codex_runtime._is_read_only() is False
    assert not any("read-only config" in r.getMessage() for r in caplog.records)


def test_is_read_only_enabled_true(tmp_path, monkeypatch):
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"enabled": True}))
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    assert codex_runtime._is_read_only() is True


def test_is_read_only_enabled_false(tmp_path, monkeypatch):
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"enabled": False}))
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    assert codex_runtime._is_read_only() is False


def test_is_read_only_malformed_fails_open_and_logs(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "ro.json"
    cfg.write_text("{not valid json")
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", cfg)
    caplog.set_level(logging.WARNING, logger="agent_server.services.codex_runtime")
    assert codex_runtime._is_read_only() is False
    assert any("malformed" in r.getMessage() for r in caplog.records)
