"""
Agent avatar identity database operations.

Handles avatar identity prompts, defaults, and custom avatar management.
"""

from typing import Optional, Dict

from sqlalchemy import select, update

from ..engine import get_engine
from ..tables import agent_ownership


class AvatarMixin:
    """Mixin for agent avatar identity operations."""

    def set_avatar_identity(self, agent_name: str, prompt: str, updated_at: str) -> bool:
        """Set avatar identity prompt and updated timestamp for an agent.
        Also clears is_default_avatar flag (custom avatar overrides default).
        """
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(
                avatar_identity_prompt=prompt,
                avatar_updated_at=updated_at,
                is_default_avatar=0,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_avatar_identity(self, agent_name: str) -> Optional[Dict]:
        """Get avatar identity prompt and metadata for an agent."""
        stmt = select(
            agent_ownership.c.avatar_identity_prompt,
            agent_ownership.c.avatar_updated_at,
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row and row["avatar_identity_prompt"]:
            return {
                "identity_prompt": row["avatar_identity_prompt"],
                "updated_at": row["avatar_updated_at"],
            }
        return None

    def clear_avatar_identity(self, agent_name: str) -> bool:
        """Clear avatar identity prompt and timestamp for an agent.
        Also clears is_default_avatar flag.
        """
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(
                avatar_identity_prompt=None,
                avatar_updated_at=None,
                is_default_avatar=0,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_agents_without_custom_avatar(self) -> list:
        """Return agents that need a default avatar (no avatar or existing default).

        Returns list of dicts with agent_name for agents where:
        - avatar_updated_at IS NULL (no avatar at all), OR
        - is_default_avatar = 1 (has a default that can be overwritten)
        """
        stmt = select(agent_ownership.c.agent_name).where(
            (
                agent_ownership.c.avatar_updated_at.is_(None)
                | (agent_ownership.c.is_default_avatar == 1)
            )
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [{"agent_name": row["agent_name"]} for row in rows]

    def set_default_avatar(self, agent_name: str, identity_prompt: str, updated_at: str) -> bool:
        """Set avatar as a default (auto-generated) avatar."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(
                avatar_identity_prompt=identity_prompt,
                avatar_updated_at=updated_at,
                is_default_avatar=1,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
