"""
Agent file-sharing toggle (amazing-file-outbound, FILES-001).

Per-agent opt-in flag that controls whether the agent gets a Docker volume
mounted at /home/developer/public/. When enabled, the agent can call the
`share_file` MCP tool to mint a public download URL for files it writes there.

This mixin only manages the toggle + volume-name convention. Actual volume
creation happens in services/agent_service/crud.py mirroring the
shared-folders pattern.
"""

from sqlalchemy import select, update, func, and_

from ..engine import get_engine
from ..tables import agent_ownership


class FileSharingMixin:
    """Mixin for the per-agent file-sharing opt-in toggle."""

    # =========================================================================
    # Toggle state
    # =========================================================================

    def get_file_sharing_enabled(self, agent_name: str) -> bool:
        """Whether the agent has file sharing enabled. Default: False."""
        stmt = select(
            func.coalesce(agent_ownership.c.file_sharing_enabled, 0).label(
                "file_sharing_enabled"
            )
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row["file_sharing_enabled"]) if row else False

    def set_file_sharing_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Flip the toggle. Returns True if a row was updated."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(file_sharing_enabled=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Volume / mount conventions
    # =========================================================================

    @staticmethod
    def get_public_volume_name(agent_name: str) -> str:
        """Docker volume name for the agent's publish dir."""
        return f"agent-{agent_name}-public"

    @staticmethod
    def get_public_mount_path() -> str:
        """Where the volume is mounted inside the agent container."""
        return "/home/developer/public"
