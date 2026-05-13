"""
Agent metadata batch queries and rename operations.

Handles N+1 query optimization and cross-table agent rename.
"""

import sqlite3
from typing import List, Dict

from db.connection import get_db_connection


class MetadataMixin:
    """Mixin for batch metadata queries and agent rename operations."""

    # =========================================================================
    # Batch Metadata Query (N+1 Fix)
    # =========================================================================

    def get_accessible_agent_names(self, user_email: str, is_admin: bool = False) -> List[str]:
        """
        Get list of agent names the user can access.

        Used by /ws/events endpoint to filter events to user's accessible agents.

        Args:
            user_email: User's email address
            is_admin: True if user is admin (sees all agents)

        Returns:
            List of agent names the user can access (owned + shared, or all if admin)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            if is_admin:
                # Admin sees all agents
                cursor.execute("SELECT agent_name FROM agent_ownership")
                return [row["agent_name"] for row in cursor.fetchall()]

            # Get owned + shared agents
            cursor.execute("""
                SELECT DISTINCT agent_name FROM (
                    SELECT ao.agent_name FROM agent_ownership ao
                    JOIN users u ON ao.owner_id = u.id
                    WHERE LOWER(u.email) = LOWER(?)
                    UNION
                    SELECT agent_name FROM agent_sharing
                    WHERE LOWER(shared_with_email) = LOWER(?)
                )
            """, (user_email, user_email))
            return [row["agent_name"] for row in cursor.fetchall()]

    # =========================================================================
    # Agent Rename (RENAME-001)
    # =========================================================================

    def rename_agent(self, old_name: str, new_name: str) -> bool:
        """
        Rename an agent by updating all database references.

        Issue #816: the per-table UPDATE list is now driven by the single
        source of truth at `db.agent_cleanup.AGENT_REFS`. Adding a new
        agent-referencing column anywhere requires an entry there — the
        parity check at `tests/unit/test_agent_cleanup_parity.py` fails
        otherwise.

        Args:
            old_name: Current agent name
            new_name: New agent name (must be unique)

        Returns:
            True if rename succeeded, False if failed
        """
        # Import here to avoid a circular: agent_cleanup → db.connection
        # → db package init → agent_settings → metadata.
        from db.agent_cleanup import cascade_rename

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                # Check if old agent exists
                cursor.execute("SELECT 1 FROM agent_ownership WHERE agent_name = ?", (old_name,))
                if not cursor.fetchone():
                    return False

                # Check if new name is already taken
                cursor.execute("SELECT 1 FROM agent_ownership WHERE agent_name = ?", (new_name,))
                if cursor.fetchone():
                    return False

                # Parent table (NOT in AGENT_REFS — agent_ownership is the
                # primary; cascade_rename handles every child reference).
                cursor.execute(
                    "UPDATE agent_ownership SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Single-source-of-truth pass over every child reference.
                cascade_rename(conn, old_name, new_name)

                conn.commit()
                return True

            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def can_user_rename_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can rename an agent (only owner or admin, NOT system agents)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Check if this is a system agent - NO ONE can rename system agents
        owner = self.get_agent_owner(agent_name)
        if owner and owner.get("is_system", False):
            return False

        # Admins can rename any non-system agent
        if user["role"] == "admin":
            return True

        # Only owners can rename their agents
        if owner and owner["owner_username"] == username:
            return True

        return False

    def get_all_agent_metadata(self, user_email: str = None) -> Dict[str, Dict]:
        """
        Fetch all agent metadata in a SINGLE query.

        This eliminates the N+1 query problem by joining all related tables
        and returning a dict keyed by agent_name.

        Args:
            user_email: Current user's email for checking share access

        Returns:
            Dict mapping agent_name to metadata dict containing:
            - owner_id, owner_username, owner_email
            - is_system, autonomy_enabled, use_platform_api_key
            - memory_limit, cpu_limit
            - github_repo, github_branch
            - is_shared_with_user (bool)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Single query that joins all needed tables
            cursor.execute("""
                SELECT
                    ao.agent_name,
                    ao.owner_id,
                    u.username as owner_username,
                    u.email as owner_email,
                    COALESCE(ao.is_system, 0) as is_system,
                    COALESCE(ao.autonomy_enabled, 0) as autonomy_enabled,
                    COALESCE(ao.read_only_mode, 0) as read_only_enabled,
                    COALESCE(ao.use_platform_api_key, 1) as use_platform_api_key,
                    ao.memory_limit,
                    ao.cpu_limit,
                    ao.avatar_updated_at,
                    gc.github_repo,
                    gc.working_branch as github_branch,
                    CASE
                        WHEN s.id IS NOT NULL THEN 1
                        ELSE 0
                    END as is_shared_with_user
                FROM agent_ownership ao
                LEFT JOIN users u ON ao.owner_id = u.id
                LEFT JOIN agent_git_config gc ON gc.agent_name = ao.agent_name
                LEFT JOIN agent_sharing s ON s.agent_name = ao.agent_name
                    AND LOWER(s.shared_with_email) = LOWER(?)
            """, (user_email or '',))

            result = {}
            for row in cursor.fetchall():
                result[row["agent_name"]] = {
                    "owner_id": row["owner_id"],
                    "owner_username": row["owner_username"],
                    "owner_email": row["owner_email"],
                    "is_system": bool(row["is_system"]),
                    "autonomy_enabled": bool(row["autonomy_enabled"]),
                    "read_only_enabled": bool(row["read_only_enabled"]),
                    "use_platform_api_key": bool(row["use_platform_api_key"]),
                    "memory_limit": row["memory_limit"],
                    "cpu_limit": row["cpu_limit"],
                    "github_repo": row["github_repo"],
                    "github_branch": row["github_branch"],
                    "is_shared_with_user": bool(row["is_shared_with_user"]),
                    "avatar_updated_at": row["avatar_updated_at"],
                }

            return result
