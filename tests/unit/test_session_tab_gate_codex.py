"""Session-tab runtime gate (#1187 Phase H).

The cached-UUID ``--resume`` turn is gated so a Codex agent runs a stateless
turn instead. The gate must:
  * recognize codex as a non-resume runtime,
  * leave Claude (and Gemini, in the MVP) resume-capable,
  * fail safe (assume resume-capable) on any Docker lookup hiccup.
"""

from __future__ import annotations

import types

from routers import sessions


def _status(runtime):
    return types.SimpleNamespace(runtime=runtime)


def test_codex_in_no_resume_constant():
    assert "codex" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME


def test_supports_resume_false_for_codex(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("codex")
    )
    assert sessions._supports_session_tab_resume("a") is False


def test_supports_resume_true_for_claude(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("claude-code")
    )
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_true_for_gemini_in_mvp(monkeypatch):
    """Only codex is gated in the MVP — Gemini keeps its (existing) Session tab."""
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("gemini-cli")
    )
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_true_when_container_missing(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: None)
    assert sessions._supports_session_tab_resume("a") is True


def test_supports_resume_defaults_true_on_lookup_failure(monkeypatch):
    def _boom(name):
        raise RuntimeError("docker socket down")

    monkeypatch.setattr(sessions, "get_agent_container", _boom)
    # Must not raise, and must fail safe to resume-capable.
    assert sessions._supports_session_tab_resume("a") is True
