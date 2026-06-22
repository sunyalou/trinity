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
