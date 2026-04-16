"""
Agent sharing database operations.

Handles share/unshare agents by email, share lookups, and permission checks.
"""

import sqlite3
from datetime import datetime
from typing import Optional, List

from db.connection import get_db_connection
from db_models import AgentShare


class SharingMixin:
    """Mixin for agent sharing operations. Requires self._user_ops and ownership methods."""

    @staticmethod
    def _row_to_agent_share(row) -> AgentShare:
        """Convert an agent_sharing row to an AgentShare model."""
        return AgentShare(
            id=row["id"],
            agent_name=row["agent_name"],
            shared_with_email=row["shared_with_email"],
            shared_by_id=row["shared_by_id"],
            shared_by_email=row["shared_by_email"] or "unknown",
            created_at=datetime.fromisoformat(row["created_at"])
        )

    def share_agent(self, agent_name: str, owner_username: str, share_with_email: str) -> Optional[AgentShare]:
        """
        Share an agent with another user by email.
        - Validates owner has permission to share
        - Creates sharing record with email (user doesn't need to exist yet)
        - Returns share details or None if failed
        """
        # Get owner and validate permission
        owner = self._user_ops.get_user_by_username(owner_username)
        if not owner:
            return None

        # Check if user can share (owner or admin)
        if not self.can_user_share_agent(owner_username, agent_name):
            return None

        # Prevent self-sharing (check if owner's email matches target email)
        owner_email = owner.get("email") or ""
        if owner_email and owner_email.lower() == share_with_email.lower():
            return None

        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at)
                    VALUES (?, ?, ?, ?)
                """, (agent_name, share_with_email.lower(), owner["id"], now))
                conn.commit()
                share_id = cursor.lastrowid

                return AgentShare(
                    id=share_id,
                    agent_name=agent_name,
                    shared_with_email=share_with_email.lower(),
                    shared_by_id=owner["id"],
                    shared_by_email=owner.get("email") or owner.get("username") or "unknown",
                    created_at=datetime.fromisoformat(now)
                )
            except sqlite3.IntegrityError:
                # Already shared with this email
                return None

    def unshare_agent(self, agent_name: str, owner_username: str, share_with_email: str) -> bool:
        """Remove sharing access for an email."""
        # Validate permission
        if not self.can_user_share_agent(owner_username, agent_name):
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM agent_sharing
                WHERE agent_name = ? AND shared_with_email = ?
            """, (agent_name, share_with_email.lower()))
            conn.commit()
            return cursor.rowcount > 0

    def get_agent_shares(self, agent_name: str) -> List[AgentShare]:
        """Get all emails an agent is shared with."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.id, s.agent_name, s.shared_with_email, s.shared_by_id, s.created_at,
                       COALESCE(sb.email, sb.username) as shared_by_email
                FROM agent_sharing s
                JOIN users sb ON s.shared_by_id = sb.id
                WHERE s.agent_name = ?
                ORDER BY s.created_at DESC
            """, (agent_name,))
            return [self._row_to_agent_share(row) for row in cursor.fetchall()]

    def get_shared_agents(self, username: str) -> List[str]:
        """Get all agent names shared with a user (by their email)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return []

        user_email = user.get("email")
        if not user_email:
            return []

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM agent_sharing WHERE shared_with_email = ?
            """, (user_email.lower(),))
            return [row["agent_name"] for row in cursor.fetchall()]

    def is_agent_shared_with_email(self, agent_name: str, email: str) -> bool:
        """Check if an agent is shared with the given email directly."""
        if not email:
            return False
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM agent_sharing
                WHERE agent_name = ? AND shared_with_email = ?
            """, (agent_name, email.lower()))
            return cursor.fetchone() is not None

    def email_has_agent_access(self, agent_name: str, email: str) -> bool:
        """Cross-channel access check (Issue #311).

        Returns True when the email is owner, admin, or in agent_sharing for
        the given agent. Used by the channel router gate.
        """
        if not email:
            return False
        user = self._user_ops.get_user_by_email(email)
        if user:
            if user.get("role") == "admin":
                return True
            owner = self.get_agent_owner(agent_name)
            if owner and owner["owner_username"] == user["username"]:
                return True
        return self.is_agent_shared_with_email(agent_name, email)

    def is_agent_shared_with_user(self, agent_name: str, username: str) -> bool:
        """Check if an agent is shared with a specific user (by their email)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        user_email = user.get("email")
        if not user_email:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM agent_sharing
                WHERE agent_name = ? AND shared_with_email = ?
            """, (agent_name, user_email.lower()))
            return cursor.fetchone() is not None

    def can_user_share_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can share an agent (only owner or admin)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        if user["role"] == "admin":
            return True

        owner = self.get_agent_owner(agent_name)
        return owner and owner["owner_username"] == username

    def delete_agent_shares(self, agent_name: str) -> int:
        """Delete all sharing records for an agent (when agent is deleted)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_sharing WHERE agent_name = ?", (agent_name,))
            conn.commit()
            return cursor.rowcount

    # =========================================================================
    # Proactive Messaging (Issue #321)
    # =========================================================================

    def can_agent_message_email(self, agent_name: str, email: str) -> bool:
        """Check if agent is authorized to send proactive messages to this email.

        Authorization requires:
        1. User is in agent_sharing for this agent AND allow_proactive = 1
        2. OR user is the owner of the agent (always allowed)
        """
        if not email:
            return False

        # Check if owner
        owner = self.get_agent_owner(agent_name)
        if owner:
            owner_user = self._user_ops.get_user_by_username(owner["owner_username"])
            if owner_user and owner_user.get("email", "").lower() == email.lower():
                return True

        # Check sharing with allow_proactive flag
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM agent_sharing
                WHERE agent_name = ? AND shared_with_email = ? AND allow_proactive = 1
            """, (agent_name, email.lower()))
            return cursor.fetchone() is not None

    def set_allow_proactive(
        self, agent_name: str, email: str, allow: bool, setter_username: str
    ) -> bool:
        """Update allow_proactive flag for a sharing record.

        Only the owner or admin can modify this flag.
        Returns True if updated, False if not authorized or share not found.
        """
        if not self.can_user_share_agent(setter_username, agent_name):
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_sharing
                SET allow_proactive = ?
                WHERE agent_name = ? AND shared_with_email = ?
            """, (1 if allow else 0, agent_name, email.lower()))
            conn.commit()
            return cursor.rowcount > 0

    def get_proactive_enabled_shares(self, agent_name: str) -> List[str]:
        """Get all emails that have opted in to receive proactive messages from this agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT shared_with_email FROM agent_sharing
                WHERE agent_name = ? AND allow_proactive = 1
            """, (agent_name,))
            return [row[0] for row in cursor.fetchall()]
