"""
Event subscription operations for agent event pub/sub (EVT-001).

Enables agents to subscribe to events from other agents and
trigger async tasks when matching events are emitted.
"""

import json
import secrets
from typing import List, Optional
from datetime import datetime

from db.connection import get_db_connection
from db_models import EventSubscription, EventSubscriptionCreate, AgentEvent
from utils.helpers import utc_now_iso


class EventSubscriptionOperations:
    """Database operations for agent event subscriptions and events."""

    # =========================================================================
    # Subscription CRUD
    # =========================================================================

    def create_subscription(
        self,
        subscriber_agent: str,
        data: EventSubscriptionCreate,
        created_by: str
    ) -> EventSubscription:
        """Create a new event subscription."""
        sub_id = f"esub_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_event_subscriptions (
                    id, subscriber_agent, source_agent, event_type,
                    target_message, enabled, created_at, updated_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sub_id,
                subscriber_agent,
                data.source_agent,
                data.event_type,
                data.target_message,
                1 if data.enabled else 0,
                now,
                now,
                created_by
            ))
            conn.commit()

        return EventSubscription(
            id=sub_id,
            subscriber_agent=subscriber_agent,
            source_agent=data.source_agent,
            event_type=data.event_type,
            target_message=data.target_message,
            enabled=data.enabled,
            created_at=now,
            updated_at=now,
            created_by=created_by
        )

    def get_subscription(self, subscription_id: str) -> Optional[EventSubscription]:
        """Get a subscription by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, subscriber_agent, source_agent, event_type,
                       target_message, enabled, created_at, updated_at, created_by
                FROM agent_event_subscriptions
                WHERE id = ?
            """, (subscription_id,))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_subscription(row)

    def list_subscriptions(
        self,
        subscriber_agent: Optional[str] = None,
        source_agent: Optional[str] = None,
        enabled_only: bool = False,
        limit: int = 100
    ) -> List[EventSubscription]:
        """List event subscriptions with optional filters."""
        query = """
            SELECT id, subscriber_agent, source_agent, event_type,
                   target_message, enabled, created_at, updated_at, created_by
            FROM agent_event_subscriptions
            WHERE 1=1
        """
        params = []

        if subscriber_agent:
            query += " AND subscriber_agent = ?"
            params.append(subscriber_agent)

        if source_agent:
            query += " AND source_agent = ?"
            params.append(source_agent)

        if enabled_only:
            query += " AND enabled = 1"

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_subscription(row) for row in rows]

    def update_subscription(
        self,
        subscription_id: str,
        event_type: Optional[str] = None,
        target_message: Optional[str] = None,
        enabled: Optional[bool] = None
    ) -> Optional[EventSubscription]:
        """Update an existing subscription."""
        updates = []
        params = []

        if event_type is not None:
            updates.append("event_type = ?")
            params.append(event_type)

        if target_message is not None:
            updates.append("target_message = ?")
            params.append(target_message)

        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)

        if not updates:
            return self.get_subscription(subscription_id)

        now = utc_now_iso()
        updates.append("updated_at = ?")
        params.append(now)
        params.append(subscription_id)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE agent_event_subscriptions SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()

            if cursor.rowcount == 0:
                return None

        return self.get_subscription(subscription_id)

    def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription. Returns True if deleted."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_event_subscriptions WHERE id = ?",
                (subscription_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_agent_subscriptions(self, agent_name: str) -> int:
        """Delete all subscriptions where agent is subscriber or source."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_event_subscriptions WHERE subscriber_agent = ? OR source_agent = ?",
                (agent_name, agent_name)
            )
            conn.commit()
            return cursor.rowcount

    # =========================================================================
    # Event Matching
    # =========================================================================

    def find_matching_subscriptions(
        self,
        source_agent: str,
        event_type: str
    ) -> List[EventSubscription]:
        """Find enabled subscriptions that match a source agent and event type."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, subscriber_agent, source_agent, event_type,
                       target_message, enabled, created_at, updated_at, created_by
                FROM agent_event_subscriptions
                WHERE source_agent = ? AND event_type = ? AND enabled = 1
            """, (source_agent, event_type))
            rows = cursor.fetchall()

        return [self._row_to_subscription(row) for row in rows]

    # =========================================================================
    # Event Persistence
    # =========================================================================

    def create_event(
        self,
        source_agent: str,
        event_type: str,
        payload: Optional[dict] = None,
        subscriptions_triggered: int = 0
    ) -> AgentEvent:
        """Persist an emitted event."""
        event_id = f"evt_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()
        payload_json = json.dumps(payload) if payload else None

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_events (
                    id, source_agent, event_type, payload,
                    subscriptions_triggered, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                source_agent,
                event_type,
                payload_json,
                subscriptions_triggered,
                now
            ))
            conn.commit()

        return AgentEvent(
            id=event_id,
            source_agent=source_agent,
            event_type=event_type,
            payload=payload,
            subscriptions_triggered=subscriptions_triggered,
            created_at=now
        )

    def list_events(
        self,
        source_agent: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50
    ) -> List[AgentEvent]:
        """List events with optional filters."""
        query = """
            SELECT id, source_agent, event_type, payload,
                   subscriptions_triggered, created_at
            FROM agent_events
            WHERE 1=1
        """
        params = []

        if source_agent:
            query += " AND source_agent = ?"
            params.append(source_agent)

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_event(row) for row in rows]

    # =========================================================================
    # Row Converters
    # =========================================================================

    def _row_to_subscription(self, row: tuple) -> EventSubscription:
        """Convert a database row to an EventSubscription model."""
        return EventSubscription(
            id=row[0],
            subscriber_agent=row[1],
            source_agent=row[2],
            event_type=row[3],
            target_message=row[4],
            enabled=bool(row[5]),
            created_at=row[6],
            updated_at=row[7],
            created_by=row[8]
        )

    def _row_to_event(self, row: tuple) -> AgentEvent:
        """Convert a database row to an AgentEvent model."""
        payload = None
        if row[3]:
            try:
                payload = json.loads(row[3])
            except json.JSONDecodeError:
                pass

        return AgentEvent(
            id=row[0],
            source_agent=row[1],
            event_type=row[2],
            payload=payload,
            subscriptions_triggered=row[4],
            created_at=row[5]
        )
