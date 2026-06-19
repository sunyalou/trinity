"""
Database operations for Slack integration (SLACK-001).

Handles:
- Slack connection CRUD (workspace to public link)
- User verification management
- Pending verification state machine

Bot tokens are encrypted at rest via `services.credential_encryption`
(AES-256-GCM, JSON envelope) — same pattern as `db/slack_channels.py`,
`db/telegram_channels.py`, and `db/whatsapp_channels.py`. See #453.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300) so it runs unchanged on both SQLite and PostgreSQL. Queries are
built from the table handles in ``db/tables.py``; the engine is resolved via
``db/engine.py``. The public API of ``SlackOperations`` is unchanged.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, and_

from .engine import get_engine, make_insert
from .tables import (
    slack_link_connections,
    slack_user_verifications,
    slack_pending_verifications,
    agent_public_links,
)
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class SlackOperations:
    """Operations for managing Slack integrations."""

    # =========================================================================
    # Encryption helpers (same pattern as SlackChannelOperations,
    # TelegramChannelOperations, WhatsAppChannelOperations)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service.

        Raises ValueError on first call if CREDENTIAL_ENCRYPTION_KEY is unset.
        Write paths fail loudly; read paths catch and return None.
        """
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_token(self, token: str) -> str:
        svc = self._get_encryption_service()
        return svc.encrypt({"bot_token": token})

    def _decrypt_token(self, encrypted: str) -> Optional[str]:
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("bot_token")
        except Exception as e:
            logger.error(f"Failed to decrypt Slack bot token: {e}")
            # Fallback: legacy plaintext rows (Slack bot tokens always start xoxb-).
            # The one-shot migration in db/migrations.py re-encrypts these on the
            # next backend restart; this fallback keeps runtime working in the
            # interim and on developer machines that haven't run the migration.
            if encrypted and encrypted.startswith("xoxb-"):
                logger.warning(
                    "Slack bot token stored in plaintext — will re-encrypt on next migration"
                )
                return encrypted
            return None

    # =========================================================================
    # Slack Connection Operations
    # =========================================================================

    def create_slack_connection(
        self,
        link_id: str,
        slack_team_id: str,
        slack_team_name: Optional[str],
        slack_bot_token: str,
        connected_by: str
    ) -> dict:
        """Create a new Slack connection for a public link.

        Bot token is encrypted at rest (#453).
        """
        connection_id = secrets.token_urlsafe(16)
        now = utc_now_iso()
        encrypted_token = self._encrypt_token(slack_bot_token)

        with get_engine().begin() as conn:
            conn.execute(
                insert(slack_link_connections).values(
                    id=connection_id,
                    link_id=link_id,
                    slack_team_id=slack_team_id,
                    slack_team_name=slack_team_name,
                    slack_bot_token=encrypted_token,
                    connected_by=connected_by,
                    connected_at=now,
                    enabled=1,
                )
            )

        return self.get_slack_connection(connection_id)

    def get_slack_connection(self, connection_id: str) -> Optional[dict]:
        """Get a Slack connection by ID."""
        stmt = select(
            slack_link_connections.c.id,
            slack_link_connections.c.link_id,
            slack_link_connections.c.slack_team_id,
            slack_link_connections.c.slack_team_name,
            slack_link_connections.c.slack_bot_token,
            slack_link_connections.c.connected_by,
            slack_link_connections.c.connected_at,
            slack_link_connections.c.enabled,
        ).where(slack_link_connections.c.id == connection_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        return self._row_to_connection(row)

    def get_slack_connection_by_link(self, link_id: str) -> Optional[dict]:
        """Get a Slack connection by public link ID."""
        stmt = select(
            slack_link_connections.c.id,
            slack_link_connections.c.link_id,
            slack_link_connections.c.slack_team_id,
            slack_link_connections.c.slack_team_name,
            slack_link_connections.c.slack_bot_token,
            slack_link_connections.c.connected_by,
            slack_link_connections.c.connected_at,
            slack_link_connections.c.enabled,
        ).where(slack_link_connections.c.link_id == link_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        return self._row_to_connection(row)

    def get_slack_connection_by_team(self, slack_team_id: str) -> Optional[dict]:
        """Get a Slack connection by Slack team/workspace ID."""
        stmt = (
            select(
                slack_link_connections.c.id,
                slack_link_connections.c.link_id,
                slack_link_connections.c.slack_team_id,
                slack_link_connections.c.slack_team_name,
                slack_link_connections.c.slack_bot_token,
                slack_link_connections.c.connected_by,
                slack_link_connections.c.connected_at,
                slack_link_connections.c.enabled,
                agent_public_links.c.agent_name,
                agent_public_links.c.require_email,
            )
            .select_from(
                slack_link_connections.join(
                    agent_public_links,
                    slack_link_connections.c.link_id == agent_public_links.c.id,
                )
            )
            .where(
                and_(
                    slack_link_connections.c.slack_team_id == slack_team_id,
                    slack_link_connections.c.enabled == 1,
                    agent_public_links.c.enabled == 1,
                )
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        connection = self._row_to_connection(row)
        connection["agent_name"] = row["agent_name"]
        connection["require_email"] = bool(row["require_email"])
        return connection

    def update_slack_connection(
        self,
        connection_id: str,
        enabled: Optional[bool] = None,
        slack_team_name: Optional[str] = None
    ) -> Optional[dict]:
        """Update a Slack connection."""
        values = {}

        if enabled is not None:
            values["enabled"] = 1 if enabled else 0
        if slack_team_name is not None:
            values["slack_team_name"] = slack_team_name

        if not values:
            return self.get_slack_connection(connection_id)

        with get_engine().begin() as conn:
            conn.execute(
                update(slack_link_connections)
                .where(slack_link_connections.c.id == connection_id)
                .values(**values)
            )

        return self.get_slack_connection(connection_id)

    def delete_slack_connection(self, connection_id: str) -> bool:
        """Delete a Slack connection."""
        with get_engine().begin() as conn:
            # Get link_id for cleanup
            row = conn.execute(
                select(slack_link_connections.c.link_id)
                .where(slack_link_connections.c.id == connection_id)
            ).mappings().first()
            if row:
                link_id = row["link_id"]
                # Delete related verifications
                conn.execute(
                    delete(slack_user_verifications)
                    .where(slack_user_verifications.c.link_id == link_id)
                )
                conn.execute(
                    delete(slack_pending_verifications)
                    .where(slack_pending_verifications.c.link_id == link_id)
                )
            # Delete the connection
            result = conn.execute(
                delete(slack_link_connections)
                .where(slack_link_connections.c.id == connection_id)
            )
            return result.rowcount > 0

    def delete_slack_connection_by_link(self, link_id: str) -> bool:
        """Delete a Slack connection by public link ID."""
        with get_engine().begin() as conn:
            # Delete related verifications
            conn.execute(
                delete(slack_user_verifications)
                .where(slack_user_verifications.c.link_id == link_id)
            )
            conn.execute(
                delete(slack_pending_verifications)
                .where(slack_pending_verifications.c.link_id == link_id)
            )
            # Delete the connection
            result = conn.execute(
                delete(slack_link_connections)
                .where(slack_link_connections.c.link_id == link_id)
            )
            return result.rowcount > 0

    def _row_to_connection(self, row) -> dict:
        """Convert a database row to a connection dict.

        Bot token is decrypted before return (#453). Callers (`routers/slack.py`,
        `adapters/slack_adapter.py`) receive plaintext tokens — same API
        surface as before encryption was added.
        """
        return {
            "id": row["id"],
            "link_id": row["link_id"],
            "slack_team_id": row["slack_team_id"],
            "slack_team_name": row["slack_team_name"],
            "slack_bot_token": self._decrypt_token(row["slack_bot_token"]),
            "connected_by": row["connected_by"],
            "connected_at": row["connected_at"],
            "enabled": bool(row["enabled"])
        }

    # =========================================================================
    # User Verification Operations
    # =========================================================================

    def get_user_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str
    ) -> Optional[dict]:
        """Check if a Slack user is verified for a link."""
        stmt = select(
            slack_user_verifications.c.id,
            slack_user_verifications.c.link_id,
            slack_user_verifications.c.slack_user_id,
            slack_user_verifications.c.slack_team_id,
            slack_user_verifications.c.verified_email,
            slack_user_verifications.c.verification_method,
            slack_user_verifications.c.verified_at,
        ).where(
            and_(
                slack_user_verifications.c.link_id == link_id,
                slack_user_verifications.c.slack_user_id == slack_user_id,
                slack_user_verifications.c.slack_team_id == slack_team_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        return {
            "id": row["id"],
            "link_id": row["link_id"],
            "slack_user_id": row["slack_user_id"],
            "slack_team_id": row["slack_team_id"],
            "verified_email": row["verified_email"],
            "verification_method": row["verification_method"],
            "verified_at": row["verified_at"]
        }

    def create_user_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str,
        verified_email: str,
        verification_method: str
    ) -> dict:
        """Create a user verification record."""
        verification_id = secrets.token_urlsafe(16)
        now = utc_now_iso()

        with get_engine().begin() as conn:
            # Upsert - replace if exists. Conflict target is the
            # UNIQUE(link_id, slack_user_id, slack_team_id) constraint; on
            # conflict, overwrite the verification details (id is not updated,
            # matching INSERT OR REPLACE keeping the surviving row addressable
            # by the same natural key).
            stmt = make_insert(slack_user_verifications).values(
                id=verification_id,
                link_id=link_id,
                slack_user_id=slack_user_id,
                slack_team_id=slack_team_id,
                verified_email=verified_email,
                verification_method=verification_method,
                verified_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["link_id", "slack_user_id", "slack_team_id"],
                set_={
                    "verified_email": verified_email,
                    "verification_method": verification_method,
                    "verified_at": now,
                },
            )
            conn.execute(stmt)

        return self.get_user_verification(link_id, slack_user_id, slack_team_id)

    # =========================================================================
    # Pending Verification Operations (State Machine)
    # =========================================================================

    def get_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str
    ) -> Optional[dict]:
        """Get a pending verification for a Slack user."""
        stmt = select(
            slack_pending_verifications.c.id,
            slack_pending_verifications.c.link_id,
            slack_pending_verifications.c.slack_user_id,
            slack_pending_verifications.c.slack_team_id,
            slack_pending_verifications.c.email,
            slack_pending_verifications.c.code,
            slack_pending_verifications.c.created_at,
            slack_pending_verifications.c.expires_at,
            slack_pending_verifications.c.state,
        ).where(
            and_(
                slack_pending_verifications.c.slack_user_id == slack_user_id,
                slack_pending_verifications.c.slack_team_id == slack_team_id,
                slack_pending_verifications.c.expires_at > utc_now_iso(),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        return self._row_to_pending(row)

    def create_pending_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str,
        email: Optional[str] = None,
        code: Optional[str] = None,
        state: str = "awaiting_email"
    ) -> dict:
        """Create a pending verification entry."""
        pending_id = secrets.token_urlsafe(16)
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=10)).isoformat()

        with get_engine().begin() as conn:
            # Delete any existing pending verification for this user
            conn.execute(
                delete(slack_pending_verifications).where(
                    and_(
                        slack_pending_verifications.c.slack_user_id == slack_user_id,
                        slack_pending_verifications.c.slack_team_id == slack_team_id,
                    )
                )
            )
            # Create new one
            conn.execute(
                insert(slack_pending_verifications).values(
                    id=pending_id,
                    link_id=link_id,
                    slack_user_id=slack_user_id,
                    slack_team_id=slack_team_id,
                    email=email,
                    code=code,
                    created_at=now.isoformat(),
                    expires_at=expires_at,
                    state=state,
                )
            )

        return self.get_pending_verification(slack_user_id, slack_team_id)

    def update_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str,
        email: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None
    ) -> Optional[dict]:
        """Update a pending verification (transition state machine)."""
        values = {}

        if email is not None:
            values["email"] = email
        if code is not None:
            values["code"] = code
        if state is not None:
            values["state"] = state

        # Reset expiration
        values["expires_at"] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()

        with get_engine().begin() as conn:
            conn.execute(
                update(slack_pending_verifications)
                .where(
                    and_(
                        slack_pending_verifications.c.slack_user_id == slack_user_id,
                        slack_pending_verifications.c.slack_team_id == slack_team_id,
                    )
                )
                .values(**values)
            )

        return self.get_pending_verification(slack_user_id, slack_team_id)

    def delete_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str
    ) -> bool:
        """Delete a pending verification (after successful verification)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(slack_pending_verifications).where(
                    and_(
                        slack_pending_verifications.c.slack_user_id == slack_user_id,
                        slack_pending_verifications.c.slack_team_id == slack_team_id,
                    )
                )
            )
            return result.rowcount > 0

    def cleanup_expired_pending_verifications(self) -> int:
        """Clean up expired pending verifications."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(slack_pending_verifications).where(
                    slack_pending_verifications.c.expires_at < utc_now_iso()
                )
            )
            return result.rowcount

    def _row_to_pending(self, row) -> dict:
        """Convert a database row to a pending verification dict."""
        return {
            "id": row["id"],
            "link_id": row["link_id"],
            "slack_user_id": row["slack_user_id"],
            "slack_team_id": row["slack_team_id"],
            "email": row["email"],
            "code": row["code"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "state": row["state"]
        }
