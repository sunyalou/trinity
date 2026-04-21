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
from utils.helpers import utc_now_iso

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
        now = utc_now_iso()
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
        now = utc_now_iso()
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
        """Delete a Telegram binding and associated chat links and group configs."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete group configs first
            cursor.execute("""
                DELETE FROM telegram_group_configs
                WHERE binding_id IN (
                    SELECT id FROM telegram_bindings WHERE agent_name = ?
                )
            """, (agent_name,))
            # Delete chat links
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

            now = utc_now_iso()
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

    def get_chat_link(self, binding_id: int, telegram_user_id: str) -> Optional[dict]:
        """Look up a chat link without creating it."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, telegram_user_id, telegram_username,
                       session_id, message_count, created_at, last_active,
                       verified_email, verified_at
                FROM telegram_chat_links
                WHERE binding_id = ? AND telegram_user_id = ?
            """, (binding_id, telegram_user_id))
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "binding_id": row[1],
            "telegram_user_id": row[2],
            "telegram_username": row[3],
            "session_id": row[4],
            "message_count": row[5],
            "created_at": row[6],
            "last_active": row[7],
            "verified_email": row[8],
            "verified_at": row[9],
        }

    def get_verified_email(self, binding_id: int, telegram_user_id: str) -> Optional[str]:
        """Return the verified email for this Telegram user, or None."""
        link = self.get_chat_link(binding_id, telegram_user_id)
        return link["verified_email"] if link else None

    def set_verified_email(
        self,
        binding_id: int,
        telegram_user_id: str,
        email: str,
    ) -> bool:
        """Persist a verified email onto the chat link (auto-creates the row)."""
        now = utc_now_iso()
        email = email.lower()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Ensure row exists
            cursor.execute("""
                INSERT OR IGNORE INTO telegram_chat_links
                (binding_id, telegram_user_id, message_count, created_at, last_active)
                VALUES (?, ?, 0, ?, ?)
            """, (binding_id, telegram_user_id, now, now))
            cursor.execute("""
                UPDATE telegram_chat_links
                SET verified_email = ?, verified_at = ?
                WHERE binding_id = ? AND telegram_user_id = ?
            """, (email, now, binding_id, telegram_user_id))
            conn.commit()
            return cursor.rowcount > 0

    def clear_verified_email(self, binding_id: int, telegram_user_id: str) -> bool:
        """Unbind a verified email (logout)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_chat_links
                SET verified_email = NULL, verified_at = NULL
                WHERE binding_id = ? AND telegram_user_id = ?
            """, (binding_id, telegram_user_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_chat_link_by_verified_email(
        self, binding_id: int, email: str
    ) -> Optional[dict]:
        """Reverse lookup: find a chat link by verified email for proactive messaging (#321).

        Returns the chat link for a user who has verified with this email,
        or None if no such user exists for this binding.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, telegram_user_id, telegram_username,
                       session_id, message_count, created_at, last_active,
                       verified_email, verified_at
                FROM telegram_chat_links
                WHERE binding_id = ? AND verified_email = ?
                ORDER BY last_active DESC
                LIMIT 1
            """, (binding_id, email.lower()))
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "binding_id": row[1],
            "telegram_user_id": row[2],
            "telegram_username": row[3],
            "session_id": row[4],
            "message_count": row[5],
            "created_at": row[6],
            "last_active": row[7],
            "verified_email": row[8],
            "verified_at": row[9],
        }

    def increment_message_count(self, chat_link_id: int) -> None:
        """Increment message count and update last_active."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_chat_links
                SET message_count = message_count + 1, last_active = ?
                WHERE id = ?
            """, (now, chat_link_id))
            conn.commit()

    # =========================================================================
    # Group Config Operations (TGRAM-GROUP)
    # =========================================================================

    def get_or_create_group_config(
        self,
        binding_id: int,
        chat_id: str,
        chat_title: Optional[str] = None,
        chat_type: str = "group",
    ) -> dict:
        """Get or create a group config. Auto-creates on first interaction."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, chat_id, chat_title, chat_type,
                       trigger_mode, welcome_enabled, welcome_text,
                       is_active, created_at, updated_at,
                       verified_by_email, verified_at
                FROM telegram_group_configs
                WHERE binding_id = ? AND chat_id = ?
            """, (binding_id, chat_id))
            row = cursor.fetchone()

            if row:
                # Re-activate if inactive (bot removed then re-added) and update title
                needs_update = False
                if chat_title and chat_title != row[3]:
                    needs_update = True
                if not row[8]:  # is_active == 0
                    needs_update = True

                if needs_update:
                    cursor.execute("""
                        UPDATE telegram_group_configs
                        SET chat_title = ?, is_active = 1, updated_at = ?
                        WHERE id = ?
                    """, (chat_title or row[3], now, row[0]))
                    conn.commit()
                    return {**self._row_to_group_config(row),
                            "chat_title": chat_title or row[3],
                            "is_active": True}
                return self._row_to_group_config(row)

            cursor.execute("""
                INSERT INTO telegram_group_configs
                (binding_id, chat_id, chat_title, chat_type,
                 trigger_mode, welcome_enabled, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'mention', 0, 1, ?, ?)
            """, (binding_id, chat_id, chat_title, chat_type, now, now))
            conn.commit()

            cursor.execute("""
                SELECT id, binding_id, chat_id, chat_title, chat_type,
                       trigger_mode, welcome_enabled, welcome_text,
                       is_active, created_at, updated_at,
                       verified_by_email, verified_at
                FROM telegram_group_configs
                WHERE binding_id = ? AND chat_id = ?
            """, (binding_id, chat_id))
            return self._row_to_group_config(cursor.fetchone())

    def get_group_config(self, binding_id: int, chat_id: str) -> Optional[dict]:
        """Get group config for a specific chat."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, chat_id, chat_title, chat_type,
                       trigger_mode, welcome_enabled, welcome_text,
                       is_active, created_at, updated_at,
                       verified_by_email, verified_at
                FROM telegram_group_configs
                WHERE binding_id = ? AND chat_id = ?
            """, (binding_id, chat_id))
            row = cursor.fetchone()
        return self._row_to_group_config(row) if row else None

    def get_groups_for_binding(self, binding_id: int) -> List[dict]:
        """Get all group configs for a bot binding."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, binding_id, chat_id, chat_title, chat_type,
                       trigger_mode, welcome_enabled, welcome_text,
                       is_active, created_at, updated_at,
                       verified_by_email, verified_at
                FROM telegram_group_configs
                WHERE binding_id = ? AND is_active = 1
                ORDER BY chat_title
            """, (binding_id,))
            rows = cursor.fetchall()
        return [self._row_to_group_config(row) for row in rows]

    def get_groups_for_agent(self, agent_name: str) -> List[dict]:
        """Get all group configs for an agent (via binding lookup)."""
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return []
        return self.get_groups_for_binding(binding["id"])

    def update_group_config(
        self,
        group_config_id: int,
        trigger_mode: Optional[str] = None,
        welcome_enabled: Optional[bool] = None,
        welcome_text: Optional[str] = None,
    ) -> Optional[dict]:
        """Update group config settings."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            updates = ["updated_at = ?"]
            values = [now]

            if trigger_mode is not None:
                updates.append("trigger_mode = ?")
                values.append(trigger_mode)
            if welcome_enabled is not None:
                updates.append("welcome_enabled = ?")
                values.append(1 if welcome_enabled else 0)
            if welcome_text is not None:
                updates.append("welcome_text = ?")
                values.append(welcome_text)

            values.append(group_config_id)
            cursor.execute(f"""
                UPDATE telegram_group_configs
                SET {', '.join(updates)}
                WHERE id = ?
            """, values)
            conn.commit()

            cursor.execute("""
                SELECT id, binding_id, chat_id, chat_title, chat_type,
                       trigger_mode, welcome_enabled, welcome_text,
                       is_active, created_at, updated_at,
                       verified_by_email, verified_at
                FROM telegram_group_configs
                WHERE id = ?
            """, (group_config_id,))
            row = cursor.fetchone()
        return self._row_to_group_config(row) if row else None

    def deactivate_group_config(self, binding_id: int, chat_id: str) -> bool:
        """Mark a group config as inactive (bot removed from group)."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_group_configs
                SET is_active = 0, updated_at = ?
                WHERE binding_id = ? AND chat_id = ?
            """, (now, binding_id, chat_id))
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    def delete_groups_for_binding(self, binding_id: int) -> int:
        """Delete all group configs for a binding (when bot is disconnected)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM telegram_group_configs WHERE binding_id = ?",
                (binding_id,)
            )
            count = cursor.rowcount
            conn.commit()
        return count

    # =========================================================================
    # Group Verification (group_auth_mode support)
    # =========================================================================

    def is_group_verified(self, binding_id: int, chat_id: str) -> bool:
        """Check if a group has at least one verified member."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT verified_by_email
                FROM telegram_group_configs
                WHERE binding_id = ? AND chat_id = ?
            """, (binding_id, chat_id))
            row = cursor.fetchone()
        return bool(row and row[0])

    def get_group_verified_email(self, binding_id: int, chat_id: str) -> Optional[str]:
        """Get the email that verified this group, if any."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT verified_by_email
                FROM telegram_group_configs
                WHERE binding_id = ? AND chat_id = ?
            """, (binding_id, chat_id))
            row = cursor.fetchone()
        return row[0] if row else None

    def set_group_verified(self, binding_id: int, chat_id: str, email: str) -> bool:
        """Mark a group as verified by the given email."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_group_configs
                SET verified_by_email = ?, verified_at = ?, updated_at = ?
                WHERE binding_id = ? AND chat_id = ?
            """, (email.lower(), now, now, binding_id, chat_id))
            conn.commit()
            return cursor.rowcount > 0

    def clear_group_verification(self, binding_id: int, chat_id: str) -> bool:
        """Clear verification status for a group (e.g., when verified user leaves)."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE telegram_group_configs
                SET verified_by_email = NULL, verified_at = NULL, updated_at = ?
                WHERE binding_id = ? AND chat_id = ?
            """, (now, binding_id, chat_id))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_group_config(self, row) -> dict:
        return {
            "id": row[0],
            "binding_id": row[1],
            "chat_id": row[2],
            "chat_title": row[3],
            "chat_type": row[4],
            "trigger_mode": row[5],
            "welcome_enabled": bool(row[6]),
            "welcome_text": row[7],
            "is_active": bool(row[8]),
            "created_at": row[9],
            "updated_at": row[10],
            "verified_by_email": row[11] if len(row) > 11 else None,
            "verified_at": row[12] if len(row) > 12 else None,
        }

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
