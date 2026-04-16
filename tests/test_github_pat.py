"""
Tests for Per-Agent GitHub PAT Configuration (#347)

Tests the API endpoints for managing per-agent GitHub Personal Access Tokens.
"""
import pytest


class TestGitHubPATStatus:
    """Test GET /api/agents/{name}/github-pat endpoint."""

    def test_get_pat_status_no_git_config(self, auth_headers, test_agent):
        """Agent without git config should show global source."""
        import requests

        response = requests.get(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["agent_name"] == test_agent
        assert data["configured"] == False
        assert data["source"] == "global"
        assert "has_global" in data

    def test_get_pat_status_unauthorized(self, test_agent):
        """Should require authentication."""
        import requests

        response = requests.get(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat"
        )

        assert response.status_code == 401


class TestSetGitHubPAT:
    """Test PUT /api/agents/{name}/github-pat endpoint."""

    def test_set_pat_invalid_token(self, auth_headers, test_agent):
        """Invalid PAT should be rejected with 400."""
        import requests

        response = requests.put(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat",
            headers=auth_headers,
            json={"pat": "invalid-token-12345"}
        )

        # GitHub validation should fail
        assert response.status_code == 400
        assert "Invalid" in response.json().get("detail", "") or "GitHub" in response.json().get("detail", "")

    def test_set_pat_empty_token(self, auth_headers, test_agent):
        """Empty PAT should be rejected."""
        import requests

        response = requests.put(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat",
            headers=auth_headers,
            json={"pat": ""}
        )

        assert response.status_code == 400
        assert "empty" in response.json().get("detail", "").lower()

    def test_set_pat_no_git_config(self, auth_headers, test_agent):
        """Should fail if agent has no git config."""
        import requests

        # Assuming test_agent doesn't have git initialized
        response = requests.put(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat",
            headers=auth_headers,
            json={"pat": "ghp_test123456789"}
        )

        # Either validation fails first (400) or no git config (400)
        assert response.status_code == 400


class TestClearGitHubPAT:
    """Test DELETE /api/agents/{name}/github-pat endpoint."""

    def test_clear_pat_success(self, auth_headers, test_agent):
        """Clearing PAT should succeed even if not configured."""
        import requests

        response = requests.delete(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "global"

    def test_clear_pat_unauthorized(self, test_agent):
        """Should require authentication."""
        import requests

        response = requests.delete(
            f"http://localhost:8000/api/agents/{test_agent}/github-pat"
        )

        assert response.status_code == 401


class TestGitPATDBOperations:
    """Test database operations for GitHub PAT."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encryption and decryption should preserve the PAT value."""
        import os
        # Skip if encryption key not configured
        if not os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
            pytest.skip("CREDENTIAL_ENCRYPTION_KEY not configured")

        from db.agent_settings.git_pat import GitPATMixin

        mixin = GitPATMixin()
        original_pat = "ghp_test123456789abcdef"

        encrypted = mixin._encrypt_github_pat(original_pat)
        decrypted = mixin._decrypt_github_pat(encrypted)

        assert decrypted == original_pat

    def test_decrypt_invalid_data(self):
        """Decrypting invalid data should return None."""
        import os
        if not os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
            pytest.skip("CREDENTIAL_ENCRYPTION_KEY not configured")

        from db.agent_settings.git_pat import GitPATMixin

        mixin = GitPATMixin()
        result = mixin._decrypt_github_pat("not-valid-encrypted-data")

        assert result is None
