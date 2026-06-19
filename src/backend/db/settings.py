"""
System settings database operations.

Stores system-wide configuration like the Trinity prompt that applies to all agents.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``system_settings`` table
in ``db/tables.py``; the engine is resolved via ``db/engine.py``.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy import select, delete

from .engine import get_engine, make_insert
from .tables import system_settings
from db_models import SystemSetting
from utils.helpers import utc_now_iso


class SettingsOperations:
    """System settings database operations."""

    @staticmethod
    def _row_to_setting(row) -> SystemSetting:
        """Convert a database row to SystemSetting model."""
        return SystemSetting(
            key=row["key"],
            value=row["value"],
            updated_at=datetime.fromisoformat(row["updated_at"])
        )

    # =========================================================================
    # Settings CRUD Operations
    # =========================================================================

    def get_setting(self, key: str) -> Optional[SystemSetting]:
        """
        Get a system setting by key.

        Returns None if the setting doesn't exist.
        """
        stmt = select(
            system_settings.c.key,
            system_settings.c.value,
            system_settings.c.updated_at,
        ).where(system_settings.c.key == key)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row:
                return self._row_to_setting(row)
            return None

    def get_setting_value(self, key: str, default: str = None) -> Optional[str]:
        """
        Get just the value of a setting.

        Returns the default if the setting doesn't exist.
        """
        setting = self.get_setting(key)
        if setting:
            return setting.value
        return default

    def set_setting(self, key: str, value: str) -> SystemSetting:
        """
        Set a system setting value (upsert).

        Creates the setting if it doesn't exist, updates if it does.
        Returns the updated setting.
        """
        now = utc_now_iso()

        stmt = (
            make_insert(system_settings)
            .values(key=key, value=value, updated_at=now)
            .on_conflict_do_update(
                index_elements=[system_settings.c.key],
                set_={"value": value, "updated_at": now},
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return SystemSetting(
            key=key,
            value=value,
            updated_at=datetime.fromisoformat(now)
        )

    def delete_setting(self, key: str) -> bool:
        """
        Delete a system setting.

        Returns True if the setting was deleted.
        """
        stmt = delete(system_settings).where(system_settings.c.key == key)
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_all_settings(self) -> List[SystemSetting]:
        """
        Get all system settings.

        Returns a list of all settings.
        """
        stmt = select(
            system_settings.c.key,
            system_settings.c.value,
            system_settings.c.updated_at,
        ).order_by(system_settings.c.key)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_setting(row) for row in rows]

    def get_settings_dict(self) -> Dict[str, str]:
        """
        Get all settings as a simple key-value dictionary.

        Useful for quick access to multiple settings.
        """
        settings = self.get_all_settings()
        return {s.key: s.value for s in settings}
