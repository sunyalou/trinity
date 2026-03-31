"""
Base classes for channel adapter abstraction.

ChannelAdapter: message processing interface (parse incoming, send outgoing)
NormalizedMessage: channel-agnostic incoming message
ChannelResponse: channel-agnostic outgoing response

Each channel (Slack, Telegram, etc.) implements ChannelAdapter.
Transport details (webhook vs socket vs polling) are handled separately
in adapters/transports/.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from pydantic import BaseModel


class NormalizedMessage(BaseModel):
    """Channel-agnostic incoming message."""
    sender_id: str                      # Channel-specific user ID
    text: str                           # Message content
    channel_id: str                     # Conversation/channel identifier
    thread_id: Optional[str] = None     # Thread ID (Slack thread_ts, Telegram reply_to)
    timestamp: str                      # ISO timestamp
    metadata: dict = {}                 # Channel-specific extras (team_id, bot_token, etc.)


class ChannelResponse(BaseModel):
    """Channel-agnostic outgoing response."""
    text: str                           # Response content (may contain markdown)
    metadata: dict = {}                 # Extra context (agent_name, cost, etc.)


class ChannelAdapter(ABC):
    """
    Message processing interface — transport-agnostic.

    Each channel implements this to handle:
    - Parsing raw events into NormalizedMessage
    - Sending responses back through the channel
    - Resolving which agent handles the message

    Channel-specific concerns (verification, rich formatting, identity overrides)
    live on the concrete adapter, not here.
    """

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """Channel identifier string, e.g. 'slack', 'telegram'."""

    @abstractmethod
    def get_rate_key(self, message: NormalizedMessage) -> str:
        """Build a rate-limit key unique to this sender on this channel."""

    @abstractmethod
    def get_session_identifier(self, message: NormalizedMessage) -> str:
        """Build a session identifier for conversation persistence."""

    @abstractmethod
    def get_source_identifier(self, message: NormalizedMessage) -> str:
        """Build a source identifier for audit/execution tracking."""

    @abstractmethod
    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        """Get the bot/app token needed to send responses for this message."""

    @abstractmethod
    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Extract NormalizedMessage from a raw channel event.

        Returns None to skip the event (bot messages, unsupported types, etc.)
        """

    @abstractmethod
    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None
    ) -> None:
        """Deliver a response back to the channel."""

    @abstractmethod
    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        """
        Resolve which Trinity agent should handle this message.

        Returns agent name, or None if no agent is configured for this channel/user.
        """

    async def indicate_processing(self, message: NormalizedMessage) -> None:
        """
        Show a processing indicator to the user.

        Called when the agent starts working on a message.
        Each channel implements this differently:
        - Slack: add ⏳ reaction to the user's message
        - Telegram: send typing action
        - Discord: trigger typing indicator

        Default: no-op. Override in concrete adapters.
        """
        pass

    async def indicate_done(self, message: NormalizedMessage) -> None:
        """
        Remove the processing indicator / show completion.

        Called when the agent finishes (success or error).
        - Slack: remove ⏳, add ✅
        - Telegram: no-op (typing auto-expires)

        Default: no-op. Override in concrete adapters.
        """
        pass

    async def handle_verification(self, message: NormalizedMessage) -> bool:
        """
        Verify the sender is authorized to use the agent.

        Called before processing. Return True to proceed, False to stop.
        Channels that don't need verification should leave this as-is.

        Default: always verified. Override in concrete adapters.
        """
        return True

    async def on_response_sent(
        self,
        message: NormalizedMessage,
        agent_name: str,
    ) -> None:
        """
        Called after a response is successfully sent.

        Adapters can use this to track state, e.g.:
        - Slack: register active thread for reply-without-mention
        - Telegram: no-op

        Default: no-op. Override in concrete adapters.
        """
        pass
