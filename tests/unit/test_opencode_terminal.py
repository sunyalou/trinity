from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def load_terminal_module(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    fastapi.WebSocket = object
    fastapi.WebSocketDisconnect = Exception

    database = types.ModuleType("database")
    database.db = object()

    services = types.ModuleType("services")
    docker_service = types.ModuleType("services.docker_service")
    docker_service.docker_client = object()
    docker_service.get_agent_container = lambda _agent_name: None

    docker_utils = types.ModuleType("services.docker_utils")
    docker_utils.container_reload = lambda _container: None
    docker_utils.api_exec_create = lambda *args, **kwargs: None
    docker_utils.api_exec_start = lambda *args, **kwargs: None

    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setitem(sys.modules, "services", services)
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service)
    monkeypatch.setitem(sys.modules, "services.docker_utils", docker_utils)

    terminal_path = Path(__file__).resolve().parents[2] / "src/backend/services/agent_service/terminal.py"
    spec = importlib.util.spec_from_file_location("terminal_under_test", terminal_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_terminal_command_modes(monkeypatch):
    terminal = load_terminal_module(monkeypatch)

    assert terminal.build_terminal_command("opencode") == ["opencode"]
    assert terminal.build_terminal_command("claude") == ["claude"]
    assert terminal.build_terminal_command("gemini") == ["gemini"]
    assert terminal.build_terminal_command("bash") == ["/bin/bash"]
    assert terminal.build_terminal_command("unknown") == ["/bin/bash"]
