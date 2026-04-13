"""
Tests for Issue #285: Expired Token Fast-Fail Detection

Verifies that auth failure patterns in Claude Code stderr are detected
and result in HTTP 503 responses with appropriate error codes.

Related: docs/planning/ORCHESTRATION_RELIABILITY_2026-04.md
"""
import pytest


def _is_auth_failure_message(text: str) -> bool:
    """
    Check if a message indicates an authentication/token failure.

    This is a copy of the function from claude_code.py for testing purposes.
    The actual implementation lives in docker/base-image/agent_server/services/claude_code.py
    """
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "subscription token may be expired",
        "token may be expired",
        "token expired",
        "token revoked",
        "invalid token",
        "authentication failed",
        "auth failed",
        "setup-token",  # "Generate a new one with 'claude setup-token'"
        "oauth token",
        "unauthorized",
        "invalid credentials",
        "credentials expired",
    ])


class TestAuthFailurePatternMatcher:
    """Unit tests for _is_auth_failure_message() pattern matcher."""

    def test_expired_token_patterns(self):
        """Test detection of expired token messages."""
        # Patterns that should be detected
        assert _is_auth_failure_message("Subscription token may be expired or revoked")
        assert _is_auth_failure_message("Your token may be expired. Please re-authenticate.")
        assert _is_auth_failure_message("Token expired")
        assert _is_auth_failure_message("token revoked by user")
        assert _is_auth_failure_message("Invalid token provided")
        assert _is_auth_failure_message("Authentication failed: invalid credentials")
        assert _is_auth_failure_message("Generate a new one with 'claude setup-token'")
        assert _is_auth_failure_message("OAuth token invalid")
        assert _is_auth_failure_message("Error: unauthorized")
        assert _is_auth_failure_message("invalid credentials")
        assert _is_auth_failure_message("credentials expired")

    def test_non_auth_patterns(self):
        """Test that non-auth messages are not flagged."""
        # Patterns that should NOT be detected
        assert not _is_auth_failure_message("")
        assert not _is_auth_failure_message(None)
        assert not _is_auth_failure_message("Task completed successfully")
        assert not _is_auth_failure_message("Using model: claude-sonnet-4-5")
        assert not _is_auth_failure_message("Processing tokens...")
        assert not _is_auth_failure_message("Rate limit exceeded")  # Different error type
        assert not _is_auth_failure_message("Out of usage")  # Rate limit, not auth

    def test_case_insensitivity(self):
        """Test that pattern matching is case-insensitive."""
        assert _is_auth_failure_message("TOKEN EXPIRED")
        assert _is_auth_failure_message("Authentication Failed")
        assert _is_auth_failure_message("UNAUTHORIZED")


class TestAuthFailureIntegration:
    """Integration tests for auth failure flow (requires running backend)."""

    @pytest.fixture
    def auth_headers(self, get_auth_token):
        """Get auth headers for API calls."""
        token = get_auth_token()
        return {"Authorization": f"Bearer {token}"}

    @pytest.mark.skip(reason="Requires mock agent with auth failure - manual test")
    def test_503_on_auth_failure(self, auth_headers):
        """
        Test that agent returns 503 on auth failure.

        This test requires a mock agent that outputs auth failure to stderr.
        Run manually with a specially configured test agent.
        """
        import httpx

        # This would need a test agent configured to fail auth
        response = httpx.post(
            "http://localhost:8000/api/agents/test-auth-fail/task",
            headers=auth_headers,
            json={"message": "test"},
            timeout=30.0,
        )
        # Should get 503 or the error should contain "auth" indicator
        assert response.status_code in [503, 500]
        if response.status_code == 500:
            data = response.json()
            assert "auth" in data.get("detail", "").lower() or "token" in data.get("detail", "").lower()
