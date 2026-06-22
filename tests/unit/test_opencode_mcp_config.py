from __future__ import annotations

import json

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
    assert data["mcp"]["trinity"] == {
        "type": "remote",
        "url": "http://trinity-mcp:8080/mcp",
        "headers": {"Authorization": "Bearer {env:TRINITY_MCP_API_KEY}"},
        "enabled": True,
    }


def test_configure_opencode_local_mcp_servers(monkeypatch, tmp_path):
    target_home = tmp_path / "home" / "developer"
    monkeypatch.setattr(trinity_mcp, "OPENCODE_HOME", target_home)
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")

    ok = trinity_mcp.configure_mcp_servers({
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]}
    })

    assert ok is True
    data = json.loads((target_home / ".config" / "opencode" / "opencode.json").read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["mcp"]["filesystem"] == {
        "type": "local",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        "enabled": True,
    }
