"""
Base class for channel transports.

A transport handles event delivery — how raw events arrive from a channel.
It delegates message processing to the adapter and routing to the message router.

Concrete implementations:
- SlackSocketTransport: Slack Socket Mode (outbound WebSocket)
- SlackWebhookTransport: Slack HTTP webhooks (inbound POST)
- Future: TelegramPollingTransport, TelegramWebhookTransport
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ChannelTransport(ABC):
    """
    Handles how events arrive from a channel.

    Transport owns:
    - Connection lifecycle (start, stop, reconnect)
    - Authentication for that transport mode (signature verification, token auth)
    - Passing raw events to the adapter

    Transport does NOT own:
    - Message parsing (adapter)
    - Agent routing (router)
    - Response formatting (adapter)
    """

    def __init__(self, adapter, router):
        self.adapter = adapter
        self.router = router
        self._running = False

    @property
    def is_connected(self) -> bool:
        """Whether the transport is connected and running."""
        return self._running

    @abstractmethod
    async def start(self) -> None:
        """Start listening for events."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening, clean up connections."""

    async def on_event(self, raw_event: dict) -> None:
        """
        Called when a raw event arrives from the channel.

        Delegates to adapter for parsing, then router for dispatch.
        Subclasses call this after transport-specific auth/ack.
        """
        try:
            logger.info(f"on_event: raw_event keys={list(raw_event.keys())}, event_type={raw_event.get('event', {}).get('type')}")
            message = self.adapter.parse_message(raw_event)
            logger.info(f"on_event: parse_message returned {'message' if message else 'None'} (sender={message.sender_id if message else 'n/a'})")
            if message:
                await self.router.handle_message(self.adapter, message)
        except Exception as e:
            logger.error(f"Error processing channel event: {e}", exc_info=True)
