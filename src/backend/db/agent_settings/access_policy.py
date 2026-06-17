"""
Agent access policy settings (Issue #311).

Stores per-agent channel access policy:
- require_email: gate requires a verified email on incoming messages
- open_access: anyone with a verified email may talk (access_requests skipped)
- group_auth_mode: auth mode for group chats ('none', 'any_verified')
"""

from sqlalchemy import select, update, and_, func

from ..engine import get_engine
from ..tables import agent_ownership


class AccessPolicyMixin:
    """Mixin for per-agent access policy (require_email, open_access, group_auth_mode)."""

    def get_access_policy(self, agent_name: str) -> dict:
        """Return access policy for an agent.

        Returns:
            {
                'require_email': bool,
                'open_access': bool,
                'group_auth_mode': str  # 'none' or 'any_verified'
            }
        """
        stmt = select(
            func.coalesce(agent_ownership.c.require_email, 0).label("require_email"),
            func.coalesce(agent_ownership.c.open_access, 0).label("open_access"),
            func.coalesce(agent_ownership.c.group_auth_mode, "none").label("group_auth_mode"),
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return {"require_email": False, "open_access": False, "group_auth_mode": "none"}
        return {
            "require_email": bool(row["require_email"]),
            "open_access": bool(row["open_access"]),
            "group_auth_mode": row["group_auth_mode"] or "none",
        }

    def set_access_policy(
        self,
        agent_name: str,
        require_email: bool,
        open_access: bool,
        group_auth_mode: str = "none",
    ) -> bool:
        """Update access policy for an agent.

        Args:
            agent_name: Agent name
            require_email: Require verified email for DMs
            open_access: Anyone with verified email can chat (skip access_requests)
            group_auth_mode: 'none' (no auth in groups) or 'any_verified' (at least one verified member)
        """
        if group_auth_mode not in ("none", "any_verified"):
            group_auth_mode = "none"
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(
                require_email=1 if require_email else 0,
                open_access=1 if open_access else 0,
                group_auth_mode=group_auth_mode,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
