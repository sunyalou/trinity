"""
Proactive Message Service (Issue #321).

Enables agents to send proactive messages to users across channels (Telegram, Slack, web).
Handles authorization, recipient resolution, rate limiting, and dispatch.

Key features:
- Explicit opt-in consent via allow_proactive flag on agent_sharing
- Redis-based rate limiting (survives restarts)
- Audit logging for all sends
- Channel resolution by verified email
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum

from database import db
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

# Rate limiting configuration
RATE_LIMIT_MAX_PER_HOUR = 10  # messages per recipient per hour
RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour


class ProactiveMessageError(Exception):
    """Base exception for proactive messaging errors."""
    pass


class NotAuthorizedError(ProactiveMessageError):
    """Recipient has not opted in to proactive messages."""
    pass


class RecipientNotFoundError(ProactiveMessageError):
    """No channel endpoint found for the recipient email."""
    pass


class RateLimitedError(ProactiveMessageError):
    """Rate limit exceeded for this agent-recipient pair."""
    pass


class ChannelDeliveryError(ProactiveMessageError):
    """Failed to deliver message via channel."""
    pass


@dataclass
class DeliveryResult:
    """Result of a proactive message delivery attempt."""
    success: bool
    channel: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class ProactiveMessageService:
    """
    Service for sending proactive messages from agents to users.

    Authorization: Agent can only message users who have:
    1. Been shared the agent AND set allow_proactive=1, OR
    2. Are the owner of the agent (always allowed)

    Channel resolution:
    - telegram: Look up telegram_chat_links by verified_email
    - slack: Look up Slack user by email via users.lookupByEmail API
    - web: WebSocket push + persist to public_chat_messages
    - auto: Try channels in order: telegram -> slack -> web
    """

    def __init__(self):
        self._redis_client = None

    def _get_redis(self):
        """Lazy-load Redis client."""
        if self._redis_client is None:
            from routers.auth import get_redis_client
            self._redis_client = get_redis_client()
        return self._redis_client

    def _rate_limit_key(self, agent_name: str, recipient_email: str) -> str:
        """Build rate limit key including both agent and recipient."""
        return f"proactive_msg:{agent_name}:{recipient_email.lower()}"

    def _check_rate_limit(self, agent_name: str, recipient_email: str) -> bool:
        """Check if sending is allowed under rate limits. Returns True if OK."""
        redis = self._get_redis()
        if not redis:
            # Redis unavailable — allow (degraded mode)
            logger.warning("Redis unavailable for rate limiting, allowing message")
            return True

        key = self._rate_limit_key(agent_name, recipient_email)
        try:
            count = redis.get(key)
            if count is None:
                return True
            return int(count) < RATE_LIMIT_MAX_PER_HOUR
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}")
            return True

    def _increment_rate_limit(self, agent_name: str, recipient_email: str) -> None:
        """Increment the rate limit counter for this agent-recipient pair."""
        redis = self._get_redis()
        if not redis:
            return

        key = self._rate_limit_key(agent_name, recipient_email)
        try:
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, RATE_LIMIT_WINDOW_SECONDS)
            pipe.execute()
        except Exception as e:
            logger.warning(f"Rate limit increment failed: {e}")

    def _audit_send(
        self,
        agent_name: str,
        recipient_email: str,
        channel: str,
        success: bool,
        error: Optional[str] = None,
        message_preview: Optional[str] = None,
    ) -> None:
        """Log proactive message send to audit trail."""
        try:
            platform_audit_service.log_event(
                event_type=AuditEventType.PROACTIVE_MESSAGE,
                event_action="send",
                actor_type="agent",
                actor_id=agent_name,
                target_type="user",
                target_id=recipient_email,
                source="proactive_message_service",
                details={
                    "channel": channel,
                    "success": success,
                    "error": error,
                    "message_preview": message_preview[:100] if message_preview else None,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to audit proactive message: {e}")

    async def send_message(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: Literal["auto", "telegram", "slack", "web"] = "auto",
        reply_to_thread: bool = False,
    ) -> DeliveryResult:
        """
        Send a proactive message to a user.

        Args:
            agent_name: The agent sending the message
            recipient_email: Verified email of the recipient
            text: Message content
            channel: Target channel or "auto" for automatic selection
            reply_to_thread: Continue in last thread if one exists (channel-dependent)

        Returns:
            DeliveryResult with success status and channel used

        Raises:
            NotAuthorizedError: Recipient hasn't opted in
            RecipientNotFoundError: No channel endpoint for recipient
            RateLimitedError: Rate limit exceeded
            ChannelDeliveryError: Channel delivery failed
        """
        recipient_email = recipient_email.lower()
        message_preview = text[:100] if text else ""

        # 1. Authorization check
        if not db.can_agent_message_email(agent_name, recipient_email):
            self._audit_send(agent_name, recipient_email, channel, False, "not_authorized")
            raise NotAuthorizedError(
                f"Agent '{agent_name}' is not authorized to message '{recipient_email}'. "
                "Recipient must opt in via allow_proactive flag."
            )

        # 2. Rate limit check
        if not self._check_rate_limit(agent_name, recipient_email):
            self._audit_send(agent_name, recipient_email, channel, False, "rate_limited")
            raise RateLimitedError(
                f"Rate limit exceeded: max {RATE_LIMIT_MAX_PER_HOUR} messages per hour to this recipient."
            )

        # 3. Channel resolution and delivery
        channels_to_try = (
            ["telegram", "slack", "web"] if channel == "auto"
            else [channel]
        )

        last_error = None
        for ch in channels_to_try:
            try:
                result = await self._deliver_via_channel(
                    agent_name, recipient_email, text, ch, reply_to_thread
                )
                if result.success:
                    self._increment_rate_limit(agent_name, recipient_email)
                    self._audit_send(
                        agent_name, recipient_email, ch, True,
                        message_preview=message_preview
                    )
                    return result
                last_error = result.error
            except RecipientNotFoundError:
                # Try next channel
                continue
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Proactive message to {ch} failed: {e}")
                continue

        # All channels failed
        error_msg = last_error or "No delivery channel available for recipient"
        self._audit_send(agent_name, recipient_email, channel, False, error_msg)
        raise RecipientNotFoundError(error_msg)

    async def _deliver_via_channel(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: str,
        reply_to_thread: bool,
    ) -> DeliveryResult:
        """Deliver message via specific channel."""
        if channel == "telegram":
            return await self._deliver_telegram(agent_name, recipient_email, text)
        elif channel == "slack":
            return await self._deliver_slack(agent_name, recipient_email, text)
        elif channel == "web":
            return await self._deliver_web(agent_name, recipient_email, text)
        else:
            raise ValueError(f"Unknown channel: {channel}")

    async def _deliver_telegram(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via Telegram."""
        from adapters.telegram_adapter import TelegramAdapter

        # Get binding for this agent
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            raise RecipientNotFoundError(f"No Telegram bot configured for agent '{agent_name}'")

        # Look up chat link by verified email
        chat_link = db.get_telegram_chat_link_by_verified_email(binding["id"], recipient_email)
        if not chat_link:
            raise RecipientNotFoundError(
                f"No Telegram user with verified email '{recipient_email}' for agent '{agent_name}'"
            )

        # Get bot token (decrypted)
        bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            raise ChannelDeliveryError("Failed to retrieve bot token")

        # Send via adapter
        adapter = TelegramAdapter()
        try:
            # The chat_link contains telegram_user_id which is the chat_id for DMs
            chat_id = chat_link["telegram_user_id"]
            result = await adapter._send_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
            if result:
                message_id = result.get("message_id")
                return DeliveryResult(
                    success=True,
                    channel="telegram",
                    message_id=str(message_id) if message_id else None,
                )
            else:
                return DeliveryResult(success=False, channel="telegram", error="Send failed")
        except Exception as e:
            logger.error(f"Telegram delivery failed: {e}")
            return DeliveryResult(success=False, channel="telegram", error=str(e))

    async def _deliver_slack(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via Slack DM."""
        from services.slack_service import slack_service

        # Get Slack workspace connections
        # We need to find a workspace where this user exists
        workspaces = db.get_all_slack_workspaces()
        if not workspaces:
            raise RecipientNotFoundError("No Slack workspaces connected")

        for workspace in workspaces:
            bot_token = workspace.get("bot_token")
            if not bot_token:
                continue

            # Try to find user by email in this workspace
            user = await slack_service.get_user_by_email(bot_token, recipient_email)
            if not user:
                continue

            # Open DM channel
            channel_id = await slack_service.open_dm_channel(bot_token, user["id"])
            if not channel_id:
                continue

            # Send message with agent identity
            success, error = await slack_service.send_message(
                bot_token=bot_token,
                channel=channel_id,
                text=text,
                username=agent_name,
            )

            if success:
                return DeliveryResult(success=True, channel="slack")
            else:
                return DeliveryResult(success=False, channel="slack", error=error)

        raise RecipientNotFoundError(
            f"User '{recipient_email}' not found in any connected Slack workspace"
        )

    async def _deliver_web(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via web (WebSocket push + DB persist).

        NOTE: Web delivery requires the recipient to have an active public link session
        with this agent. For v1, we skip web delivery and rely on Telegram/Slack.
        Full web delivery (with inbox-style persistence) is deferred to v2.
        """
        # v1: Web delivery not yet implemented - requires refactoring public_chat
        # to support agent_name-based sessions instead of link_id-based sessions.
        raise RecipientNotFoundError(
            f"Web delivery not yet implemented for proactive messaging. "
            f"Recipient '{recipient_email}' must have Telegram or Slack configured."
        )


# Singleton instance
proactive_message_service = ProactiveMessageService()
