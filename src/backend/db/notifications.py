"""
Notification operations for agent notifications (NOTIF-001).

Enables agents to send structured notifications to the Trinity platform.
Notifications are persisted and broadcast via WebSocket.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``agent_notifications``
table in ``db/tables.py`` (dialect-agnostic expressions, no ``?``/``%s``
placeholders), and the engine is resolved via ``db/engine.py``.
"""

import json
import secrets
from typing import List, Optional

from sqlalchemy import select, insert, update, delete, func, and_

from .engine import get_engine
from .tables import agent_notifications
from db_models import Notification, NotificationCreate
from utils.helpers import utc_now_iso

# Columns returned for a full notification record (stable ordering for reads).
_NOTIFICATION_COLUMNS = (
    agent_notifications.c.id,
    agent_notifications.c.agent_name,
    agent_notifications.c.notification_type,
    agent_notifications.c.title,
    agent_notifications.c.message,
    agent_notifications.c.priority,
    agent_notifications.c.category,
    agent_notifications.c.metadata,
    agent_notifications.c.status,
    agent_notifications.c.created_at,
    agent_notifications.c.acknowledged_at,
    agent_notifications.c.acknowledged_by,
)


class NotificationOperations:
    """Database operations for agent notifications."""

    def create_notification(
        self,
        agent_name: str,
        data: NotificationCreate
    ) -> Notification:
        """
        Create a new notification.

        Args:
            agent_name: The agent sending the notification
            data: Notification data

        Returns:
            The created notification
        """
        notification_id = f"notif_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()

        # Serialize metadata to JSON if provided
        metadata_json = json.dumps(data.metadata) if data.metadata else None

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_notifications).values(
                    id=notification_id,
                    agent_name=agent_name,
                    notification_type=data.notification_type,
                    title=data.title[:200],  # Enforce max length
                    message=data.message,
                    priority=data.priority,
                    category=data.category,
                    metadata=metadata_json,
                    status="pending",
                    created_at=now,
                )
            )

        return Notification(
            id=notification_id,
            agent_name=agent_name,
            notification_type=data.notification_type,
            title=data.title[:200],
            message=data.message,
            priority=data.priority,
            category=data.category,
            metadata=data.metadata,
            status="pending",
            created_at=now,
            acknowledged_at=None,
            acknowledged_by=None
        )

    def get_notification(self, notification_id: str) -> Optional[Notification]:
        """
        Get a notification by ID.

        Args:
            notification_id: The notification ID

        Returns:
            The notification or None if not found
        """
        stmt = select(*_NOTIFICATION_COLUMNS).where(
            agent_notifications.c.id == notification_id
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        return self._row_to_notification(row)

    def list_notifications(
        self,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[List[str]] = None,
        category: Optional[str] = None,
        limit: int = 100
    ) -> List[Notification]:
        """
        List notifications with optional filters.

        Args:
            agent_name: Filter by agent name
            status: Filter by status (pending, acknowledged, dismissed)
            priority: Filter by priority levels
            category: Filter by category (e.g., 'health' for monitoring alerts)
            limit: Maximum number of results

        Returns:
            List of notifications
        """
        conditions = []

        if agent_name:
            conditions.append(agent_notifications.c.agent_name == agent_name)

        if status:
            conditions.append(agent_notifications.c.status == status)

        if priority:
            conditions.append(agent_notifications.c.priority.in_(priority))

        if category:
            conditions.append(agent_notifications.c.category == category)

        stmt = select(*_NOTIFICATION_COLUMNS)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(agent_notifications.c.created_at.desc()).limit(limit)

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_notification(row) for row in rows]

    def list_agent_notifications(
        self,
        agent_name: str,
        status: Optional[str] = None,
        limit: int = 50
    ) -> List[Notification]:
        """
        List notifications for a specific agent.

        Args:
            agent_name: The agent name
            status: Optional status filter
            limit: Maximum number of results

        Returns:
            List of notifications
        """
        return self.list_notifications(
            agent_name=agent_name,
            status=status,
            limit=limit
        )

    def acknowledge_notification(
        self,
        notification_id: str,
        acknowledged_by: str
    ) -> Optional[Notification]:
        """
        Acknowledge a notification.

        Args:
            notification_id: The notification ID
            acknowledged_by: User ID who acknowledged

        Returns:
            The updated notification or None if not found
        """
        now = utc_now_iso()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_notifications)
                .where(
                    and_(
                        agent_notifications.c.id == notification_id,
                        agent_notifications.c.status == "pending",
                    )
                )
                .values(
                    status="acknowledged",
                    acknowledged_at=now,
                    acknowledged_by=acknowledged_by,
                )
            )

            if result.rowcount == 0:
                # Check if notification exists
                exists = conn.execute(
                    select(agent_notifications.c.id).where(
                        agent_notifications.c.id == notification_id
                    )
                ).first()
                if not exists:
                    return None

        return self.get_notification(notification_id)

    def dismiss_notification(
        self,
        notification_id: str,
        dismissed_by: str
    ) -> Optional[Notification]:
        """
        Dismiss a notification.

        Args:
            notification_id: The notification ID
            dismissed_by: User ID who dismissed

        Returns:
            The updated notification or None if not found
        """
        now = utc_now_iso()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_notifications)
                .where(agent_notifications.c.id == notification_id)
                .values(
                    status="dismissed",
                    acknowledged_at=now,
                    acknowledged_by=dismissed_by,
                )
            )

            if result.rowcount == 0:
                return None

        return self.get_notification(notification_id)

    def dismiss_all(
        self,
        dismissed_by: str,
        agent_name: Optional[str] = None,
        accessible_agent_names: Optional[set] = None,
    ) -> int:
        """Dismiss all non-dismissed notifications in one statement (#1017).

        Targets pending AND acknowledged rows — a button named "Clear All"
        must clear the visible feed, which shows both.

        accessible_agent_names: None = no filter; empty set = no-op (a
        zero-agent user must not touch anything); non-empty = SQL IN filter.

        Returns the number of notifications dismissed.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return 0

        now = utc_now_iso()
        conds = [agent_notifications.c.status.in_(("pending", "acknowledged"))]
        if accessible_agent_names is not None:
            conds.append(agent_notifications.c.agent_name.in_(sorted(accessible_agent_names)))
        if agent_name:
            conds.append(agent_notifications.c.agent_name == agent_name)

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_notifications)
                .where(and_(*conds))
                .values(status="dismissed", acknowledged_at=now, acknowledged_by=dismissed_by)
            )
            return result.rowcount

    def delete_agent_notifications(self, agent_name: str) -> int:
        """
        Delete all notifications for an agent.

        Args:
            agent_name: The agent name

        Returns:
            Number of notifications deleted
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_notifications).where(
                    agent_notifications.c.agent_name == agent_name
                )
            )
            return result.rowcount

    def count_pending_notifications(
        self,
        agent_name: Optional[str] = None,
        agent_names: Optional[List[str]] = None
    ) -> int:
        """
        Count pending notifications.

        Args:
            agent_name: Optional filter by a single agent
            agent_names: Optional filter by a set of agents (fleet-wide count
                scoped to the caller's accessible agents). An empty list means
                "no accessible agents" → 0 (not "all agents").

        Returns:
            Count of pending notifications
        """
        # Empty accessible set → nothing to count (avoid invalid `IN ()` SQL).
        if agent_names is not None and len(agent_names) == 0:
            return 0

        conditions = [agent_notifications.c.status == "pending"]

        if agent_name:
            conditions.append(agent_notifications.c.agent_name == agent_name)

        if agent_names:
            conditions.append(agent_notifications.c.agent_name.in_(agent_names))

        stmt = select(func.count()).select_from(agent_notifications).where(
            and_(*conditions)
        )

        with get_engine().connect() as conn:
            return conn.execute(stmt).scalar_one()

    def _row_to_notification(self, row) -> Notification:
        """Convert a database row (RowMapping) to a Notification model."""
        metadata = None
        if row["metadata"]:
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                pass

        return Notification(
            id=row["id"],
            agent_name=row["agent_name"],
            notification_type=row["notification_type"],
            title=row["title"],
            message=row["message"],
            priority=row["priority"],
            category=row["category"],
            metadata=metadata,
            status=row["status"],
            created_at=row["created_at"],
            acknowledged_at=row["acknowledged_at"],
            acknowledged_by=row["acknowledged_by"]
        )
