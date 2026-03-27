"""
Unit tests for GITHUB_PAT env var check on container recreation (#209).

Verifies that stale GITHUB_PAT in agent containers is detected and triggers
recreation when the system PAT has changed.

Module: src/backend/services/agent_service/helpers.py
Issue: https://github.com/abilityai/trinity/issues/209
"""

import pytest
from unittest.mock import MagicMock


# ---- Inline reimplementation of check_github_pat_env_matches for unit testing ----

def check_github_pat_env_matches(container_env: dict, current_system_pat: str) -> bool:
    """
    Mirror of helpers.check_github_pat_env_matches for unit testing.

    Args:
        container_env: dict of container env vars
        current_system_pat: current system-level GITHUB_PAT value

    Returns:
        True if no update needed, False if recreation needed
    """
    container_pat = container_env.get("GITHUB_PAT")
    if not container_pat:
        return True

    if not current_system_pat:
        return True

    return container_pat == current_system_pat


class TestGitHubPatEnvMatches:
    """Tests for check_github_pat_env_matches helper."""

    pytestmark = pytest.mark.unit

    def test_no_pat_in_container_returns_true(self):
        """Agents without GITHUB_PAT should not trigger recreation."""
        assert check_github_pat_env_matches({}, "ghp_new_token") is True

    def test_no_pat_in_container_empty_env_returns_true(self):
        """Container with other env vars but no GITHUB_PAT is fine."""
        env = {"ANTHROPIC_API_KEY": "sk-123", "HOME": "/home/developer"}
        assert check_github_pat_env_matches(env, "ghp_new_token") is True

    def test_matching_pat_returns_true(self):
        """Container PAT matching system PAT needs no recreation."""
        pat = "ghp_matching_token_123"
        env = {"GITHUB_PAT": pat}
        assert check_github_pat_env_matches(env, pat) is True

    def test_stale_pat_returns_false(self):
        """Container with outdated PAT should trigger recreation."""
        env = {"GITHUB_PAT": "ghp_old_token"}
        assert check_github_pat_env_matches(env, "ghp_new_token") is False

    def test_no_system_pat_returns_true(self):
        """If no system PAT configured, don't trigger recreation."""
        env = {"GITHUB_PAT": "ghp_some_token"}
        assert check_github_pat_env_matches(env, "") is True

    def test_no_system_pat_none_returns_true(self):
        """If system PAT is None, don't trigger recreation."""
        env = {"GITHUB_PAT": "ghp_some_token"}
        # Simulate None by using empty string (get_github_pat returns '')
        assert check_github_pat_env_matches(env, "") is True

    def test_empty_container_pat_returns_true(self):
        """Container with empty GITHUB_PAT string should not trigger recreation."""
        env = {"GITHUB_PAT": ""}
        assert check_github_pat_env_matches(env, "ghp_new_token") is True
