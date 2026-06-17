"""
System Views operations for agent organization (ORG-001 Phase 2).

System Views are saved filters that group agents by tags.
Views can be private (owned) or shared (visible to all users).

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. The public API of ``SystemViewOperations`` is
unchanged.
"""

import json
import secrets
from typing import List, Optional

from sqlalchemy import select, insert, update, delete, func, or_, cast, Text

from .engine import get_engine
from .tables import system_views, users, agent_tags
from db_models import SystemView, SystemViewCreate, SystemViewUpdate
from utils.helpers import utc_now_iso


# Column selection mirroring the original JOIN projection (sv.* + owner_email).
_VIEW_COLUMNS = (
    system_views.c.id,
    system_views.c.name,
    system_views.c.description,
    system_views.c.icon,
    system_views.c.color,
    system_views.c.filter_tags,
    system_views.c.owner_id,
    system_views.c.is_shared,
    system_views.c.created_at,
    system_views.c.updated_at,
    users.c.email.label("owner_email"),
)

# system_views.owner_id is TEXT (holds a stringified user id) but users.id is
# INTEGER. SQLite coerces across the JOIN; PostgreSQL rejects `text = integer`
# (#300). Cast users.id to text so both sides match on either backend.
_VIEW_JOIN = system_views.outerjoin(
    users, system_views.c.owner_id == cast(users.c.id, Text)
)


class SystemViewOperations:
    """Database operations for system views."""

    def create_view(self, owner_id: str, data: SystemViewCreate) -> SystemView:
        """
        Create a new system view.

        Args:
            owner_id: The user ID creating the view
            data: View creation data

        Returns:
            The created SystemView
        """
        view_id = f"sv_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()

        # Normalize and validate tags
        filter_tags = sorted(set(t.lower().strip() for t in data.filter_tags if t.strip()))

        with get_engine().begin() as conn:
            conn.execute(
                insert(system_views).values(
                    id=view_id,
                    name=data.name.strip(),
                    description=data.description.strip() if data.description else None,
                    icon=data.icon,
                    color=data.color,
                    filter_tags=json.dumps(filter_tags),
                    owner_id=str(owner_id),  # owner_id column is TEXT (#300)
                    is_shared=1 if data.is_shared else 0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return self._get_view_conn(conn, view_id)

    def get_view(self, view_id: str) -> Optional[SystemView]:
        """Get a system view by ID."""
        with get_engine().connect() as conn:
            return self._get_view_conn(conn, view_id)

    def _get_view_conn(self, conn, view_id: str) -> Optional[SystemView]:
        """Fetch a single view (and its agent count) using an existing connection."""
        stmt = (
            select(*_VIEW_COLUMNS)
            .select_from(_VIEW_JOIN)
            .where(system_views.c.id == view_id)
        )
        row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return self._row_to_view(row, conn)

    def list_user_views(self, user_id: str) -> List[SystemView]:
        """
        List all views accessible to a user (owned + shared).

        Args:
            user_id: The user ID

        Returns:
            List of SystemView objects sorted by name
        """
        stmt = (
            select(*_VIEW_COLUMNS)
            .select_from(_VIEW_JOIN)
            .where(or_(system_views.c.owner_id == str(user_id), system_views.c.is_shared == 1))
            .order_by(system_views.c.name.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_view(row, conn) for row in rows]

    def list_all_views(self) -> List[SystemView]:
        """List all system views (admin use)."""
        stmt = (
            select(*_VIEW_COLUMNS)
            .select_from(_VIEW_JOIN)
            .order_by(system_views.c.name.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_view(row, conn) for row in rows]

    def update_view(self, view_id: str, data: SystemViewUpdate) -> Optional[SystemView]:
        """
        Update a system view.

        Args:
            view_id: The view ID
            data: Update data (only provided fields are updated)

        Returns:
            The updated SystemView or None if not found
        """
        values = {}

        if data.name is not None:
            values["name"] = data.name.strip()

        if data.description is not None:
            values["description"] = data.description.strip() if data.description else None

        if data.icon is not None:
            values["icon"] = data.icon

        if data.color is not None:
            values["color"] = data.color

        if data.filter_tags is not None:
            filter_tags = sorted(set(t.lower().strip() for t in data.filter_tags if t.strip()))
            values["filter_tags"] = json.dumps(filter_tags)

        if data.is_shared is not None:
            values["is_shared"] = 1 if data.is_shared else 0

        if not values:
            return self.get_view(view_id)

        values["updated_at"] = utc_now_iso()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(system_views).where(system_views.c.id == view_id).values(**values)
            )
            if result.rowcount == 0:
                return None
            return self._get_view_conn(conn, view_id)

    def delete_view(self, view_id: str) -> bool:
        """
        Delete a system view.

        Args:
            view_id: The view ID

        Returns:
            True if deleted, False if not found
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(system_views).where(system_views.c.id == view_id)
            )
            return result.rowcount > 0

    def get_view_owner(self, view_id: str) -> Optional[str]:
        """Get the owner_id of a view."""
        stmt = select(system_views.c.owner_id).where(system_views.c.id == view_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
            return row[0] if row else None

    def can_user_edit_view(self, user_id: str, view_id: str, is_admin: bool = False) -> bool:
        """Check if a user can edit a view (owner or admin)."""
        if is_admin:
            return True
        owner_id = self.get_view_owner(view_id)
        return owner_id is not None and str(owner_id) == str(user_id)

    def can_user_view(self, user_id: str, view_id: str) -> bool:
        """Check if a user can view a system view (owner or shared)."""
        stmt = select(system_views.c.id).where(
            system_views.c.id == view_id,
            or_(system_views.c.owner_id == str(user_id), system_views.c.is_shared == 1),
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def _row_to_view(self, row, conn) -> SystemView:
        """Convert a database row (RowMapping) to a SystemView object with agent count."""
        filter_tags = json.loads(row["filter_tags"]) if row["filter_tags"] else []

        # Count agents matching any of the filter tags
        agent_count = 0
        if filter_tags:
            count_stmt = select(
                func.count(func.distinct(agent_tags.c.agent_name))
            ).where(agent_tags.c.tag.in_(filter_tags))
            result = conn.execute(count_stmt).first()
            agent_count = result[0] if result else 0

        return SystemView(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            icon=row["icon"],
            color=row["color"],
            filter_tags=filter_tags,
            owner_id=row["owner_id"],
            owner_email=row["owner_email"],
            is_shared=bool(row["is_shared"]),
            agent_count=agent_count,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
