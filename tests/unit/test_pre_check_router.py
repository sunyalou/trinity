"""
Unit tests for the agent-server pre-check router (#454, follow-up to review feedback).

Covers response normalisation + router behavior without running a real
agent container. Pairs with ``tests/scheduler_tests/test_pre_check.py``
which covers the scheduler-side integration.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BASE_IMAGE = (
    Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
)
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Import the module directly rather than via the ``routers`` package so we
# don't pull in snapshot.py / git.py, which require python-multipart at
# import time.
import importlib.util  # noqa: E402

_pre_check_path = _BASE_IMAGE / "agent_server" / "routers" / "pre_check.py"
_spec = importlib.util.spec_from_file_location(
    "agent_server_pre_check_router", _pre_check_path
)
_pre_check_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pre_check_mod)

MAX_MESSAGE_BYTES = _pre_check_mod.MAX_MESSAGE_BYTES
_normalise_result = _pre_check_mod._normalise_result
router = _pre_check_mod.router


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------


class TestNormaliseResult:
    def test_skip_decision_with_reason(self):
        out = _normalise_result({"fire": False, "reason": "nothing to do"})
        assert out == {"fire": False, "reason": "nothing to do"}

    def test_fire_decision_with_message(self):
        out = _normalise_result({"fire": True, "message": "Run review"})
        assert out == {"fire": True, "message": "Run review"}

    def test_fire_decision_without_message(self):
        assert _normalise_result({"fire": True}) == {"fire": True}

    def test_oversized_message_dropped_with_truncation_marker(self):
        """Review feedback: silent drop hides template bugs. Expose the drop."""
        oversized = "x" * (MAX_MESSAGE_BYTES + 100)
        out = _normalise_result({"fire": True, "message": oversized})
        # Message must not pass through
        assert "message" not in out
        # Caller gets explicit signal that override was dropped
        assert "message_truncated" in out
        assert "exceeds" in out["message_truncated"]
        assert str(MAX_MESSAGE_BYTES) in out["message_truncated"]
        assert out["fire"] is True

    def test_message_exactly_at_cap_is_accepted(self):
        msg = "x" * MAX_MESSAGE_BYTES
        out = _normalise_result({"fire": True, "message": msg})
        assert out.get("message") == msg

    def test_reason_clamped_to_2000_chars(self):
        out = _normalise_result({"fire": False, "reason": "z" * 5000})
        assert len(out["reason"]) == 2000

    def test_non_dict_return_raises_value_error(self):
        """check() returning None/True/etc. must be surfaced as a server
        error, not silently normalised away."""
        for bad in (None, True, 42, "hello", [1, 2, 3]):
            with pytest.raises(ValueError, match="must return a dict"):
                _normalise_result(bad)

    def test_dict_missing_fire_key_raises_value_error(self):
        with pytest.raises(ValueError, match="must return a dict"):
            _normalise_result({"message": "no fire key"})


# ---------------------------------------------------------------------------
# HTTP router behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


class TestRouter:
    def test_returns_404_when_check_absent(self, app):
        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=None,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 404

    def test_returns_200_on_fire_true_with_message(self, app):
        def check():
            return {"fire": True, "message": "Review PR #1"}

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 200
        assert r.json() == {"fire": True, "message": "Review PR #1"}

    def test_returns_200_on_fire_false(self, app):
        def check():
            return {"fire": False, "reason": "no new PRs"}

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 200
        assert r.json() == {"fire": False, "reason": "no new PRs"}

    def test_returns_500_when_check_raises(self, app):
        def check():
            raise RuntimeError("template bug")

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 500
        assert "template bug" in r.json()["detail"]

    def test_returns_500_when_check_returns_non_dict(self, app):
        """Review feedback: the ValueError path of _normalise_result was
        previously untested end-to-end."""

        def check():
            return None

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 500
        assert "must return a dict" in r.json()["detail"]

    def test_oversized_message_falls_back_without_override(self, app):
        """End-to-end path for the truncation marker — scheduler sees
        message_truncated but no `message` key, so falls back to
        schedule.message."""

        def check():
            return {"fire": True, "message": "x" * (MAX_MESSAGE_BYTES + 50)}

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 200
        body = r.json()
        assert body["fire"] is True
        assert "message" not in body
        assert "message_truncated" in body

    def test_async_check_is_awaited(self, app):
        async def check():
            return {"fire": True}

        with patch.object(
            _pre_check_mod,
            "_load_check_callable",
            return_value=check,
        ):
            with TestClient(app) as c:
                r = c.post("/api/pre-check")
        assert r.status_code == 200
        assert r.json() == {"fire": True}
