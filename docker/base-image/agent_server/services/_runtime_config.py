"""Runtime guardrails configuration for Claude Code execution.

GUARD-003: CLI budget & scope controls. Guardrails runtime config is written
by startup.sh via /opt/trinity/hooks/write-runtime-config.py and is root-owned
0444 so the agent cannot rewrite it. We read it on every Claude Code
invocation so backend-initiated config updates (via container recreation)
take effect without restarting the agent-server process.

Extracted from `claude_code.py` per #122 (issue split). Kept as a separate
module so both `claude_code.py` (chat path) and `headless_executor.py` (task
path) can import it without a circular dependency.
"""
from __future__ import annotations

import json

_GUARDRAILS_RUNTIME_PATH = "/opt/trinity/guardrails-runtime.json"
_GUARDRAILS_BASELINE_PATH = "/opt/trinity/guardrails-baseline.json"
_DEFAULT_MAX_TURNS_CHAT = 50
_DEFAULT_MAX_TURNS_TASK = 50
_DEFAULT_EXECUTION_TIMEOUT_SEC = 1800  # GUARD-003 (#313): 30 min wall clock for chat


def _load_guardrails() -> dict:
    """Load guardrails config, falling back to baseline, then {}."""
    for path in (_GUARDRAILS_RUNTIME_PATH, _GUARDRAILS_BASELINE_PATH):
        try:
            with open(path) as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            continue
    return {}
