"""
Agent access policy settings (Issue #311).

Stores per-agent channel access policy:
- require_email: gate requires a verified email on incoming messages
- open_access: anyone with a verified email may talk (access_requests skipped)
"""

from db.connection import get_db_connection


class AccessPolicyMixin:
    """Mixin for per-agent access policy (require_email, open_access)."""

    def get_access_policy(self, agent_name: str) -> dict:
        """Return {'require_email': bool, 'open_access': bool} for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(require_email, 0) AS require_email,
                       COALESCE(open_access, 0) AS open_access
                FROM agent_ownership
                WHERE agent_name = ?
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
        if not row:
            return {"require_email": False, "open_access": False}
        return {
            "require_email": bool(row["require_email"]),
            "open_access": bool(row["open_access"]),
        }

    def set_access_policy(
        self,
        agent_name: str,
        require_email: bool,
        open_access: bool,
    ) -> bool:
        """Update access policy for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_ownership
                SET require_email = ?, open_access = ?
                WHERE agent_name = ?
                """,
                (1 if require_email else 0, 1 if open_access else 0, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
