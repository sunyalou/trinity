"""
Database operations for per-agent access requests (Issue #311).

When a user tries to talk to an agent across any channel without being
owner / admin / shared / and the agent is not open-access, we record a
pending request. Owners can approve (which inserts into agent_sharing)
or deny.
"""

import secrets
import sqlite3
from datetime import datetime
from typing import List, Optional

from .connection import get_db_connection


class AccessRequestOperations:
    """Operations for access_requests table."""

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "email": row["email"],
            "channel": row["channel"],
            "requested_at": row["requested_at"],
            "status": row["status"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
        }

    def upsert_pending(
        self,
        agent_name: str,
        email: str,
        channel: Optional[str] = None,
    ) -> dict:
        """Insert or refresh a pending access request for (agent_name, email).

        If an approved/denied record exists and the user now has no access
        anyway (e.g. share removed), we reset to pending so the owner sees it.
        """
        email = email.lower()
        now = datetime.utcnow().isoformat()
        rid = secrets.token_urlsafe(16)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO access_requests
                    (id, agent_name, email, channel, requested_at, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (rid, agent_name, email, channel, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Already exists — refresh status to pending and update timestamp
                cursor.execute(
                    """
                    UPDATE access_requests
                    SET status = 'pending',
                        requested_at = ?,
                        channel = COALESCE(?, channel),
                        decided_by = NULL,
                        decided_at = NULL
                    WHERE agent_name = ? AND email = ?
                    """,
                    (now, channel, agent_name, email),
                )
                conn.commit()

            cursor.execute(
                "SELECT * FROM access_requests WHERE agent_name = ? AND email = ?",
                (agent_name, email),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_for_agent(
        self,
        agent_name: str,
        status: Optional[str] = "pending",
    ) -> List[dict]:
        """List access requests for an agent, optionally filtered by status."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """
                    SELECT * FROM access_requests
                    WHERE agent_name = ? AND status = ?
                    ORDER BY requested_at DESC
                    """,
                    (agent_name, status),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM access_requests
                    WHERE agent_name = ?
                    ORDER BY requested_at DESC
                    """,
                    (agent_name,),
                )
            rows = cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, request_id: str) -> Optional[dict]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,))
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def decide(
        self,
        request_id: str,
        approve: bool,
        decided_by_user_id: int,
    ) -> Optional[dict]:
        """Mark a request approved or denied. Returns updated row."""
        now = datetime.utcnow().isoformat()
        new_status = "approved" if approve else "denied"
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE access_requests
                SET status = ?, decided_by = ?, decided_at = ?
                WHERE id = ?
                """,
                (new_status, decided_by_user_id, now, request_id),
            )
            conn.commit()
            cursor.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,))
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def delete_for_agent(self, agent_name: str) -> int:
        """Delete all access requests for an agent (on agent deletion)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM access_requests WHERE agent_name = ?", (agent_name,))
            conn.commit()
            return cursor.rowcount
