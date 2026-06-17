"""
Agent autonomy mode and API key settings database operations.

Handles autonomy mode (scheduled task auto-execution) and platform API key toggle.
"""

from typing import Dict

from sqlalchemy import select, update, func

from ..engine import get_engine
from ..tables import agent_ownership


class AutonomyMixin:
    """Mixin for agent autonomy mode and API key settings."""

    # =========================================================================
    # Agent API Key Settings
    # =========================================================================

    def get_use_platform_api_key(self, agent_name: str) -> bool:
        """Check if agent should use platform API key (default: True)."""
        stmt = select(
            func.coalesce(agent_ownership.c.use_platform_api_key, 1).label(
                "use_platform_api_key"
            )
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return bool(row["use_platform_api_key"])
        return True  # Default to using platform key

    def set_use_platform_api_key(self, agent_name: str, use_platform_key: bool) -> bool:
        """Set whether agent should use platform API key."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(use_platform_api_key=1 if use_platform_key else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Autonomy Mode
    # =========================================================================

    def get_autonomy_enabled(self, agent_name: str) -> bool:
        """Check if autonomy mode is enabled for agent (scheduled tasks run automatically)."""
        stmt = select(
            func.coalesce(agent_ownership.c.autonomy_enabled, 0).label(
                "autonomy_enabled"
            )
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return bool(row["autonomy_enabled"])
        return False  # Default to disabled

    def set_autonomy_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Set whether autonomy mode is enabled for agent."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(autonomy_enabled=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_all_agents_autonomy_status(self) -> Dict[str, bool]:
        """Get autonomy status for all agents (for dashboard display)."""
        stmt = select(
            agent_ownership.c.agent_name,
            func.coalesce(agent_ownership.c.autonomy_enabled, 0).label(
                "autonomy_enabled"
            ),
        ).where(agent_ownership.c.deleted_at.is_(None))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {row["agent_name"]: bool(row["autonomy_enabled"]) for row in rows}
