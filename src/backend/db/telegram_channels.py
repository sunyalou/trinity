"""
Database operations for Telegram bot bindings and chat tracking.

Handles:
- Bot bindings (one bot token per agent, encrypted)
- Chat link tracking (Telegram user → session mapping)
"""

import logging
import secrets
from datetime import datetime
from typing import Optional, List

from db.connection import get_db_connection

logger = logging.getLogger(__name__)


class TelegramChannelOperations:
    """Operations for Telegram bot bindings and chat links."""

    # =========================================================================
    # Encryption helpers (same pattern as SlackChannelOperations)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service."""
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
            logger.error(f"Failed to decrypt Telegram bot token: {e}")
            return None

    # =========================================================================
    # Binding Operations
    # =========================================================================

    def create_binding(
        self,
        agent_name: str,
        bot_token: str,
        bot_username: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> dict:
        """Create or update a Telegram bot binding for an agent."""
        webhook_secret = secrets.token_urlsafe(32)
        telegram_secret_token = secrets.token_urlsafe(32)
        now = datetime.utcnow().isoformat()
        encrypted_token = self._encrypt_token(bot_token)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO telegram_bindings
                (agent_name, bot_token_encrypted, bot_username, bot_id,
                 webhook_secret, telegram_secret_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_name) DO UPDATE SET
                    bot_token_encrypted = excluded.bot_token_encrypted,
                    bot_username = excluded.bot_username,
                    bot_id = excluded.bot_id,
                    webhook_secret = excluded.webhook_secret,
                    telegram_secret_token = excluded.telegram_secret_token,
                    updated_at = excluded.updated_at
            """, (agent_name, encrypted_token, bot_username, bot_id,
                  webhook_secret, telegram_secret_token, now, now))
            conn.commit()

        return self.get_binding_by_agent(agent_name)

    def get_binding_by_agent(self, agent_name: str) -> Optional[dict]:
        """Get Telegram binding for an agent. Bot token is NOT decrypted."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, bot_token_encrypted, bot_username, bot_id,
                       webhook_secret, webhook_url, telegram_secret_token,
                       last_update_id, created_at, updated_at
                FROM telegram_bindings
                WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_binding_by_bot_id(self, bot_id: str) -> Optional[dict]:
        """Resolve bot_id → agent binding (for incoming updates)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, bot_token_encrypted, bot_username, bot_id,
                       webhook_secret, webhook_url, telegram_secret_token,
                       last_update_id, created_at, updated_at
                FROM telegram_bindings
                WHERE bot_id = ?
            """, (bot_id,))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_binding_by_webhook_secret(self, webhook_secret: str) -> Optional[dict]:
        """Resolve webhook_secret → agent binding (for routing incoming webhooks)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, bot_token_encrypted, bot_username, bot_id,
                       webhook_secret, webhook_url, telegram_secret_token,
                       last_update_id, created_at, updated_at
                FROM telegram_bindings
                WHERE webhook_secret = ?
            """, (webhook_secret,))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_decrypted_bot_token(self, agent_name: str) -> Optional[str]:
        """Get decrypted bot token for an agent."""
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return None
        return self._decrypt_token(binding["bot_token_encrypted"])

    def get_all_bindings(self) -> List[dict]:
        """Get all Telegram bindings (for webhook reconciliation on startup)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, bot_token_encrypted, bot_username, bot_id,
                       webhook_secret, webhook_url, telegram_secret_token,
                       last_update_id, created_at, updated_at
                FROM telegram_bindings
            """)
            rows = cursor.fetchall()

        return [self._row_to_binding(row) for row in rows]

    def update_webhook_url(self, agent_name: str, webhook_url: str) -> None:
        """Update the registered webhook URL for a binding."""
        now = datetime.utcnow().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_bindings
                SET webhook_url = ?, updated_at = ?
                WHERE agent_name = ?
            """, (webhook_url, now, agent_name))
            conn.commit()

    def update_last_update_id(self, agent_name: str, update_id: int) -> None:
        """Update the last processed update_id for dedup."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_bindings
                SET last_update_id = ?
                WHERE agent_name = ?
            """, (update_id, agent_name))
            conn.commit()

    def delete_binding(self, agent_name: str) -> bool:
        """Delete a Telegram binding and associated chat links."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete chat links first
            cursor.execute("""
                DELETE FROM telegram_chat_links
                WHERE binding_id IN (
                    SELECT id FROM telegram_bindings WHERE agent_name = ?
                )
            """, (agent_name,))
            cursor.execute(
                "DELETE FROM telegram_bindings WHERE agent_name = ?",
                (agent_name,)
            )
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    # =========================================================================
    # Chat Link Operations
    # =========================================================================

    def get_or_create_chat_link(
        self,
        binding_id: int,
        telegram_user_id: str,
        telegram_username: Optional[str] = None,
    ) -> dict:
        """Get or create a chat link for a Telegram user."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, telegram_user_id, telegram_username,
                       session_id, message_count, created_at, last_active
                FROM telegram_chat_links
                WHERE binding_id = ? AND telegram_user_id = ?
            """, (binding_id, telegram_user_id))
            row = cursor.fetchone()

            if row:
                return self._row_to_chat_link(row)

            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO telegram_chat_links
                (binding_id, telegram_user_id, telegram_username, message_count, created_at, last_active)
                VALUES (?, ?, ?, 0, ?, ?)
            """, (binding_id, telegram_user_id, telegram_username, now, now))
            conn.commit()

            cursor.execute("""
                SELECT id, binding_id, telegram_user_id, telegram_username,
                       session_id, message_count, created_at, last_active
                FROM telegram_chat_links
                WHERE binding_id = ? AND telegram_user_id = ?
            """, (binding_id, telegram_user_id))
            return self._row_to_chat_link(cursor.fetchone())

    def increment_message_count(self, chat_link_id: int) -> None:
        """Increment message count and update last_active."""
        now = datetime.utcnow().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_chat_links
                SET message_count = message_count + 1, last_active = ?
                WHERE id = ?
            """, (now, chat_link_id))
            conn.commit()

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_binding(self, row) -> dict:
        return {
            "id": row[0],
            "agent_name": row[1],
            "bot_token_encrypted": row[2],
            "bot_username": row[3],
            "bot_id": row[4],
            "webhook_secret": row[5],
            "webhook_url": row[6],
            "telegram_secret_token": row[7],
            "last_update_id": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    def _row_to_chat_link(self, row) -> dict:
        return {
            "id": row[0],
            "binding_id": row[1],
            "telegram_user_id": row[2],
            "telegram_username": row[3],
            "session_id": row[4],
            "message_count": row[5],
            "created_at": row[6],
            "last_active": row[7],
        }
