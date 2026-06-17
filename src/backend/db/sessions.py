"""
Agent session and session-message persistence (Session tab).

Parallels db/chat.py but adds Claude Code --resume UUID caching and per-message
audit fields (cache_read_tokens, claude_session_id). See
docs/planning/SESSION_TAB_2026-04.md for the design.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL.
"""

import secrets
from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, func, and_, case

from .engine import get_engine
from .tables import agent_sessions, agent_session_messages
from db_models import AgentSession, AgentSessionMessage, SessionMessageInsert
from utils.helpers import utc_now_iso


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class SessionOperations:
    """Agent session and session-message database operations."""

    @staticmethod
    def _row_to_session(row) -> AgentSession:
        row_keys = row.keys()
        return AgentSession(
            id=row["id"],
            agent_name=row["agent_name"],
            user_id=row["user_id"],
            user_email=row["user_email"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            message_count=row["message_count"],
            total_cost=row["total_cost"],
            total_context_used=row["total_context_used"],
            total_context_max=row["total_context_max"],
            status=row["status"],
            subscription_id=row["subscription_id"],
            cached_claude_session_id=row["cached_claude_session_id"],
            last_resume_at=_parse_dt(row["last_resume_at"]),
            consecutive_resume_failures=row["consecutive_resume_failures"],
            compact_count=(row["compact_count"] or 0) if "compact_count" in row_keys else 0,
        )

    @staticmethod
    def _row_to_message(row) -> AgentSessionMessage:
        row_keys = row.keys()
        return AgentSessionMessage(
            id=row["id"],
            session_id=row["session_id"],
            agent_name=row["agent_name"],
            user_id=row["user_id"],
            user_email=row["user_email"],
            role=row["role"],
            content=row["content"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            cost=row["cost"],
            context_used=row["context_used"],
            context_max=row["context_max"],
            cache_read_tokens=row["cache_read_tokens"],
            tool_calls=row["tool_calls"],
            execution_time_ms=row["execution_time_ms"],
            claude_session_id=row["claude_session_id"],
            compact_metadata=row["compact_metadata"] if "compact_metadata" in row_keys else None,
        )

    # ---- session lifecycle -------------------------------------------------

    def create_session(
        self,
        agent_name: str,
        user_id: int,
        user_email: str,
        subscription_id: Optional[str] = None,
    ) -> AgentSession:
        """Create a new agent_sessions row with empty cached_claude_session_id."""
        session_id = secrets.token_urlsafe(16)
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_sessions).values(
                    id=session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                    started_at=now,
                    last_message_at=now,
                    subscription_id=subscription_id,
                )
            )

            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_session(row)

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_session(row) if row else None

    def list_sessions(
        self,
        agent_name: str,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[AgentSession]:
        conds = [agent_sessions.c.agent_name == agent_name]
        if user_id is not None:
            conds.append(agent_sessions.c.user_id == user_id)
        if status:
            conds.append(agent_sessions.c.status == status)
        stmt = (
            select(agent_sessions)
            .where(and_(*conds))
            .order_by(agent_sessions.c.last_message_at.desc())
        )
        with get_engine().connect() as conn:
            return [self._row_to_session(r) for r in conn.execute(stmt).mappings()]

    def delete_session(self, session_id: str) -> bool:
        """Delete the session and all its messages.

        agent_session_messages has ON DELETE CASCADE, but the platform doesn't
        enable PRAGMA foreign_keys, so we delete messages explicitly.
        """
        with get_engine().begin() as conn:
            conn.execute(
                delete(agent_session_messages).where(
                    agent_session_messages.c.session_id == session_id
                )
            )
            result = conn.execute(
                delete(agent_sessions).where(agent_sessions.c.id == session_id)
            )
            return result.rowcount > 0

    # ---- messages ----------------------------------------------------------

    def add_session_message(self, msg: SessionMessageInsert) -> AgentSessionMessage:
        """Insert a session message and update session aggregate stats.

        ``compact_metadata`` is a JSON-encoded list of CompactEvent dicts (the
        agent server's stream parser captures them from
        ``{"type":"system","subtype":"compact_boundary"}`` events). The matching
        ``compact_event_count`` bumps the session's running ``compact_count``
        tally so the frontend can drive the inline reset-memory hint without
        scanning per-message rows.
        """
        message_id = secrets.token_urlsafe(16)
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_session_messages).values(
                    id=message_id,
                    session_id=msg.session_id,
                    agent_name=msg.agent_name,
                    user_id=msg.user_id,
                    user_email=msg.user_email,
                    role=msg.role,
                    content=msg.content,
                    timestamp=now,
                    cost=msg.cost,
                    context_used=msg.context_used,
                    context_max=msg.context_max,
                    cache_read_tokens=msg.cache_read_tokens,
                    tool_calls=msg.tool_calls,
                    execution_time_ms=msg.execution_time_ms,
                    claude_session_id=msg.claude_session_id,
                    compact_metadata=msg.compact_metadata,
                )
            )

            # total_context_used reflects the most recent assistant turn's
            # cache size, capped at total_context_max. Claude Code's auto-
            # compact (~85% of the model window) silently resets the cache
            # mid-turn, so a watermark would asymptote near the compact
            # threshold and stop conveying anything useful — the value
            # users care about is "what did the last turn cost," which
            # bounces honestly between low (post-compact rebuild) and high
            # (heavy turn that didn't compact).
            #
            # The original SQL used SQLite's two-arg MIN(a, b) scalar; that is
            # an aggregate in PostgreSQL, so we express the same "cap at
            # total_context_max" via a portable CASE.
            new_context_used = func.coalesce(
                msg.context_used, agent_sessions.c.total_context_used
            )
            capped_context_used = case(
                (
                    new_context_used > agent_sessions.c.total_context_max,
                    agent_sessions.c.total_context_max,
                ),
                else_=new_context_used,
            )
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == msg.session_id)
                .values(
                    last_message_at=now,
                    message_count=agent_sessions.c.message_count + 1,
                    total_cost=agent_sessions.c.total_cost + func.coalesce(msg.cost or 0, 0),
                    total_context_used=capped_context_used,
                    total_context_max=func.coalesce(
                        msg.context_max, agent_sessions.c.total_context_max
                    ),
                    compact_count=agent_sessions.c.compact_count + msg.compact_event_count,
                )
            )

            row = conn.execute(
                select(agent_session_messages).where(
                    agent_session_messages.c.id == message_id
                )
            ).mappings().first()
            return self._row_to_message(row)

    def get_session_messages(
        self, session_id: str, limit: int = 100
    ) -> List[AgentSessionMessage]:
        """Return the most recent ``limit`` messages, oldest-first for display."""
        # Most-recent ``limit`` rows (DESC), then re-ordered oldest-first.
        subq = (
            select(agent_session_messages)
            .where(agent_session_messages.c.session_id == session_id)
            .order_by(agent_session_messages.c.timestamp.desc())
            .limit(limit)
            .subquery()
        )
        stmt = select(subq).order_by(subq.c.timestamp.asc())
        with get_engine().connect() as conn:
            return [self._row_to_message(r) for r in conn.execute(stmt).mappings()]

    # ---- Claude session UUID cache ----------------------------------------

    def get_cached_claude_session_id(self, session_id: str) -> Optional[str]:
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_sessions.c.cached_claude_session_id).where(
                    agent_sessions.c.id == session_id
                )
            ).mappings().first()
            return row["cached_claude_session_id"] if row else None

    def update_cached_claude_session_id(
        self, session_id: str, claude_session_id: str
    ) -> bool:
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .values(cached_claude_session_id=claude_session_id)
            )
            return result.rowcount > 0

    def clear_cached_claude_session_id(self, session_id: str) -> bool:
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .values(cached_claude_session_id=None)
            )
            return result.rowcount > 0

    # ---- resume health -----------------------------------------------------

    def mark_resume_failure(self, session_id: str) -> int:
        """Increment consecutive_resume_failures and return the new count."""
        with get_engine().begin() as conn:
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .values(
                    consecutive_resume_failures=agent_sessions.c.consecutive_resume_failures
                    + 1
                )
            )
            row = conn.execute(
                select(agent_sessions.c.consecutive_resume_failures).where(
                    agent_sessions.c.id == session_id
                )
            ).mappings().first()
            return row["consecutive_resume_failures"] if row else 0

    def mark_resume_success(self, session_id: str) -> bool:
        """Reset failure counter and stamp last_resume_at."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .values(
                    consecutive_resume_failures=0,
                    last_resume_at=utc_now_iso(),
                )
            )
            return result.rowcount > 0

    # ---- cleanup support (Phase 4.2) --------------------------------------

    def list_active_claude_session_ids(self, agent_name: str) -> List[str]:
        """All currently-cached Claude UUIDs for an agent — the keep set.

        The Phase 4.2 cleanup service uses this as the allowlist when reaping
        orphan JSONLs from ``~/.claude/projects/-home-developer/`` inside the
        agent container. A JSONL whose UUID is not in this set has no session
        row referencing it (deleted, reset, or post-fallback orphan) and is
        safe to delete after the age-guard window.
        """
        stmt = select(agent_sessions.c.cached_claude_session_id).where(
            and_(
                agent_sessions.c.agent_name == agent_name,
                agent_sessions.c.cached_claude_session_id.isnot(None),
            )
        )
        with get_engine().connect() as conn:
            return [
                row["cached_claude_session_id"]
                for row in conn.execute(stmt).mappings()
            ]
