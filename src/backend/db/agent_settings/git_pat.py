"""
Agent GitHub PAT database operations.

Handles per-agent GitHub Personal Access Token storage with encryption (#347).
PAT is encrypted at rest using AES-256-GCM via CredentialEncryptionService.
"""

import logging
from typing import Optional

from db.connection import get_db_connection

logger = logging.getLogger(__name__)


class GitPATMixin:
    """Mixin for per-agent GitHub PAT management."""

    # =========================================================================
    # Encryption helpers (same pattern as slack_channels.py)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service."""
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_github_pat(self, pat: str) -> str:
        """Encrypt a GitHub PAT for storage."""
        svc = self._get_encryption_service()
        return svc.encrypt({"github_pat": pat})

    def _decrypt_github_pat(self, encrypted: str) -> Optional[str]:
        """Decrypt a GitHub PAT from storage. Returns None if decryption fails."""
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("github_pat")
        except Exception as e:
            logger.warning(f"Failed to decrypt GitHub PAT: {e}")
            return None

    # =========================================================================
    # GitHub PAT Operations
    # =========================================================================

    def get_agent_github_pat(self, agent_name: str) -> Optional[str]:
        """
        Get the decrypted GitHub PAT for an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            Decrypted PAT string, or None if not configured or decryption fails
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT github_pat_encrypted
                FROM agent_git_config WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()

            if not row or not row["github_pat_encrypted"]:
                return None

            return self._decrypt_github_pat(row["github_pat_encrypted"])

    def set_agent_github_pat(self, agent_name: str, pat: str) -> bool:
        """
        Set the GitHub PAT for an agent (encrypted at rest).

        Args:
            agent_name: Name of the agent
            pat: GitHub Personal Access Token

        Returns:
            True if update succeeded, False if agent has no git config
        """
        encrypted = self._encrypt_github_pat(pat)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_git_config SET github_pat_encrypted = ?
                WHERE agent_name = ?
            """, (encrypted, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def clear_agent_github_pat(self, agent_name: str) -> bool:
        """
        Clear the GitHub PAT for an agent (revert to global PAT).

        Args:
            agent_name: Name of the agent

        Returns:
            True if update succeeded, False if agent has no git config
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_git_config SET github_pat_encrypted = NULL
                WHERE agent_name = ?
            """, (agent_name,))
            conn.commit()
            return cursor.rowcount > 0

    def has_agent_github_pat(self, agent_name: str) -> bool:
        """
        Check if an agent has a custom GitHub PAT configured.

        Args:
            agent_name: Name of the agent

        Returns:
            True if agent has a custom PAT, False otherwise
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT github_pat_encrypted IS NOT NULL as has_pat
                FROM agent_git_config WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()
            return bool(row and row["has_pat"])
