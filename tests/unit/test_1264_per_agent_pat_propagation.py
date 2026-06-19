"""Tests for #1264 — per-agent GitHub PAT propagation to existing containers.

Covers the decision logic added so a per-agent PAT configured AFTER a (possibly
tokenless) container was created actually reaches the container:

  * github_pat_propagation_service._patch_env_github_pat — ADDS the GITHUB_PAT
    line when the .env lacks it (the tokenless-container case), replaces when present.
  * github_pat_propagation_service.propagate_pat_to_single_agent — short-circuits
    for a stopped agent; on a running agent injects .env + re-templates the remote.
  * agent_service.check_github_pat_env_matches — a tokenless container that
    now has an effective PAT + git config reports a mismatch (recreate needed).

Service-level unit tests; rely on the repo conftest for the REDIS_URL stub.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# Bind the REAL modules/callables at import time. This file collects before the
# siblings that replace services.agent_service.* with MagicMocks in sys.modules
# (test_start_agent_skip_inject, test_inject_assigned_credentials), so these
# references stay valid even after that pollution. Use these bound names in the
# tests rather than re-importing (which would pick up the mocks at run time).
import services.agent_service.helpers as helpers_mod          # noqa: E402
import services.github_pat_propagation_service as svc          # noqa: E402
import routers.git as gitmod                                   # noqa: E402
from services import git_service                               # noqa: E402

check_github_pat_env_matches = helpers_mod.check_github_pat_env_matches
_patch_env_github_pat = svc._patch_env_github_pat
_env_has_github_pat = svc._env_has_github_pat
propagate_pat_to_single_agent = svc.propagate_pat_to_single_agent


# ---------------------------------------------------------------------------
# _patch_env_github_pat — add-if-missing (the #1264 tokenless case)
# ---------------------------------------------------------------------------

def test_patch_adds_github_pat_line_when_missing():
    env = "ANTHROPIC_API_KEY=\"sk-x\"\nFOO=bar\n"
    out = _patch_env_github_pat(env, "ghp_token")
    assert _env_has_github_pat(out)
    assert 'GITHUB_PAT="ghp_token"' in out
    # original lines preserved
    assert "FOO=bar" in out


def test_patch_replaces_existing_github_pat_line():
    env = 'GITHUB_PAT="old"\nFOO=bar\n'
    out = _patch_env_github_pat(env, "newtok")
    assert "newtok" in out and "old" not in out
    assert out.count("GITHUB_PAT=") == 1


# ---------------------------------------------------------------------------
# propagate_pat_to_single_agent
# ---------------------------------------------------------------------------

def test_propagate_single_agent_stopped_short_circuits(monkeypatch):
    class _A:
        def __init__(self, name, status):
            self.name, self.status = name, status

    monkeypatch.setattr(svc, "list_all_agents_fast", lambda: [_A("a1", "stopped")])
    res = asyncio.run(svc.propagate_pat_to_single_agent("a1", "ghp_x"))
    assert res == {"applied": False, "reason": "agent_not_running"}


def test_propagate_single_agent_running_injects_and_retemplates(monkeypatch):
    class _A:
        def __init__(self, name, status):
            self.name, self.status = name, status

    monkeypatch.setattr(svc, "list_all_agents_fast", lambda: [_A("a1", "running")])

    # Fake the agent-server .env read/inject HTTP round-trip.
    class _Resp:
        def __init__(self, payload=None):
            self._p = payload or {}
        def raise_for_status(self):
            return None
        def json(self):
            return self._p

    injected = {}

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            return _Resp({"files": {".env": "FOO=bar\n"}})
        async def post(self, url, json=None):
            injected["env"] = json["files"][".env"]
            return _Resp()

    monkeypatch.setattr(svc.httpx, "AsyncClient", _Client)

    # git config present → remote re-template attempted; stub it.
    monkeypatch.setattr(svc.db, "get_git_config",
                        lambda n: type("G", (), {"github_repo": "org/repo"})())
    called = {}
    async def _fake_update(agent_name, pat, repo):
        called.update(agent=agent_name, pat=pat, repo=repo)
        return True
    monkeypatch.setattr(git_service, "update_remote_pat", _fake_update)

    res = asyncio.run(svc.propagate_pat_to_single_agent("a1", "ghp_tok"))

    assert res["applied"] is True
    assert res["env_updated"] is True
    assert res["remote_updated"] is True
    assert 'GITHUB_PAT="ghp_tok"' in injected["env"]      # added to the tokenless .env
    assert called == {"agent": "a1", "pat": "ghp_tok", "repo": "org/repo"}


# ---------------------------------------------------------------------------
# check_github_pat_env_matches — recreate decision
# ---------------------------------------------------------------------------

def _fake_container(env_lines):
    class _C:
        attrs = {"Config": {"Env": env_lines}}
    return _C()


def test_tokenless_container_with_pat_and_git_needs_recreate(monkeypatch):
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "ghp_xyz")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())  # git configured
    # container has no GITHUB_PAT in env
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is False


def test_tokenless_container_without_git_no_recreate(monkeypatch):
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "ghp_xyz")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: None)  # no git sync
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is True


def test_tokenless_container_no_pat_anywhere_no_recreate(monkeypatch):
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is True


def test_existing_pat_match_and_mismatch_preserved(monkeypatch):
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "ghp_current")
    # matching → True (no recreate)
    assert check_github_pat_env_matches(
        _fake_container(["GITHUB_PAT=ghp_current"]), "a1") is True
    # stale → False (recreate)
    assert check_github_pat_env_matches(
        _fake_container(["GITHUB_PAT=ghp_old"]), "a1") is False
