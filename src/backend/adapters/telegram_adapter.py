"""
Telegram channel adapter implementation.

Handles Telegram-specific message parsing, response formatting (HTML),
agent resolution via bot bindings, and bot commands.

Supports:
- Text messages → routed to bot-bound agent
- Photos, documents → downloaded and passed as context
- /start, /help, /reset commands
- Typing indicator via sendChatAction
"""

import logging
import re
from typing import Optional

import httpx

from database import db
from adapters.base import ChannelAdapter, NormalizedMessage, ChannelResponse

logger = logging.getLogger(__name__)

# Telegram message length limit
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAdapter(ChannelAdapter):
    """Telegram implementation of ChannelAdapter with per-agent bot routing."""

    # =========================================================================
    # ChannelAdapter interface — identity & routing
    # =========================================================================

    @property
    def channel_type(self) -> str:
        return "telegram"

    def get_rate_key(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        return f"telegram:{bot_id}:{message.sender_id}"

    def get_session_identifier(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        chat_id = message.channel_id
        return f"{bot_id}:{message.sender_id}:{chat_id}"

    def get_source_identifier(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        return f"telegram:{bot_id}:{message.sender_id}"

    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return None
        return db.get_telegram_bot_token(agent_name)

    # =========================================================================
    # ChannelAdapter interface — message processing
    # =========================================================================

    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Parse a Telegram Update into a NormalizedMessage.

        Handles:
        - Text messages
        - Photo messages (caption + photo indicator)
        - Document messages (caption + document indicator)
        - /start, /help, /reset commands
        """
        message = raw_event.get("message")
        if not message:
            return None

        # Skip messages from bots
        from_user = message.get("from", {})
        if from_user.get("is_bot", False):
            return None

        user_id = str(from_user.get("id", ""))
        chat_id = str(message.get("chat", {}).get("id", ""))
        username = from_user.get("username")

        if not user_id or not chat_id:
            return None

        # Extract text content
        text = message.get("text", "").strip()

        # Handle media messages — extract caption or description
        media_context = self._extract_media_context(message)
        if media_context:
            text = media_context if not text else f"{text}\n\n{media_context}"

        if not text:
            return None

        # Resolve bot → agent via the binding stored in metadata by transport
        bot_id = raw_event.get("_bot_id", "")
        agent_name = raw_event.get("_agent_name", "")

        return NormalizedMessage(
            sender_id=user_id,
            text=text,
            channel_id=chat_id,
            thread_id=str(message.get("message_id", "")),
            timestamp=str(message.get("date", "")),
            metadata={
                "bot_id": bot_id,
                "agent_name": agent_name,
                "username": username,
                "has_photo": "photo" in message,
                "has_document": "document" in message,
                "raw_message": message,
            }
        )

    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None
    ) -> None:
        """Send response to Telegram chat with HTML formatting."""
        bot_token = response.metadata.get("bot_token")
        if not bot_token:
            logger.error(f"No bot token in response metadata for chat {channel_id}")
            return

        text = response.text
        if not text:
            return

        # Convert markdown to Telegram HTML
        html_text = self._markdown_to_html(text)

        # Split long messages at paragraph boundaries
        chunks = self._split_message(html_text)

        for chunk in chunks:
            await self._send_message(
                bot_token=bot_token,
                chat_id=channel_id,
                text=chunk,
                reply_to_message_id=thread_id,
                parse_mode="HTML",
            )

    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        """Resolve which agent handles this message (set by transport layer)."""
        return message.metadata.get("agent_name")

    async def indicate_processing(self, message: NormalizedMessage) -> None:
        """Send typing indicator to Telegram chat."""
        bot_token = db.get_telegram_bot_token(message.metadata.get("agent_name", ""))
        if bot_token:
            await self._send_chat_action(bot_token, message.channel_id, "typing")

    # =========================================================================
    # Bot commands
    # =========================================================================

    def is_command(self, message: NormalizedMessage) -> bool:
        """Check if message is a bot command."""
        return message.text.startswith("/")

    async def handle_command(self, message: NormalizedMessage) -> Optional[str]:
        """
        Handle /start, /help, /reset commands.
        Returns response text, or None if not a command.
        """
        text = message.text.strip()
        agent_name = message.metadata.get("agent_name", "Agent")

        if text == "/start" or text.startswith("/start "):
            return (
                f"Hello! I'm <b>{agent_name}</b>, a Trinity agent.\n\n"
                "Send me a message to get started.\n\n"
                "Commands:\n"
                "/help — List capabilities\n"
                "/reset — Clear conversation history"
            )

        if text == "/help":
            return (
                f"I'm <b>{agent_name}</b>.\n\n"
                "You can send me:\n"
                "- Text messages\n"
                "- Photos (I'll analyze them)\n"
                "- Documents (I'll read them)\n\n"
                "Commands:\n"
                "/start — Welcome message\n"
                "/help — This help text\n"
                "/reset — Clear our conversation history"
            )

        if text == "/reset":
            # Clear session — the transport/router will handle this
            return "Conversation history cleared. Let's start fresh!"

        return None

    # =========================================================================
    # Telegram API helpers
    # =========================================================================

    async def _send_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> Optional[dict]:
        """Send a message via Telegram Bot API with retry on 429."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload)

                if resp.status_code == 429:
                    # Rate limited — respect retry_after
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"Telegram rate limited, retry_after={retry_after}s")
                    import asyncio
                    await asyncio.sleep(min(retry_after, 30))
                    resp = await client.post(url, json=payload)

                if resp.status_code != 200:
                    error_body = resp.text
                    logger.error(f"Telegram sendMessage failed ({resp.status_code}): {error_body}")

                    # If HTML parsing failed, retry with plain text
                    if "can't parse entities" in error_body.lower():
                        payload["parse_mode"] = ""
                        payload["text"] = self._strip_html(text)
                        resp = await client.post(url, json=payload)
                        if resp.status_code != 200:
                            logger.error(f"Telegram plain text fallback also failed: {resp.text}")
                            return None

                return resp.json().get("result")
        except Exception as e:
            logger.error(f"Telegram sendMessage error: {e}", exc_info=True)
            return None

    async def _send_chat_action(self, bot_token: str, chat_id: str, action: str) -> None:
        """Send a chat action (typing indicator)."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendChatAction"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"chat_id": chat_id, "action": action})
        except Exception as e:
            logger.debug(f"Failed to send chat action: {e}")

    # =========================================================================
    # Message formatting
    # =========================================================================

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert common Markdown to Telegram HTML. Plain text fallback on failure."""
        try:
            # Bold: **text** or __text__
            text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
            text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
            # Italic: *text* or _text_ (but not inside words like file_name)
            text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'<i>\1</i>', text)
            # Code blocks: ```text```
            text = re.sub(r'```(\w*)\n?(.*?)```', r'<pre>\2</pre>', text, flags=re.DOTALL)
            # Inline code: `text`
            text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
            # Strikethrough: ~~text~~
            text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
            return text
        except Exception:
            return text

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags for plain text fallback."""
        return re.sub(r'<[^>]+>', '', text)

    @staticmethod
    def _split_message(text: str) -> list:
        """Split text into chunks respecting Telegram's 4096 char limit."""
        if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                chunks.append(text)
                break

            # Find a good split point — paragraph, then sentence, then hard cut
            split_at = TELEGRAM_MAX_MESSAGE_LENGTH
            for sep in ["\n\n", "\n", ". ", " "]:
                idx = text.rfind(sep, 0, TELEGRAM_MAX_MESSAGE_LENGTH)
                if idx > TELEGRAM_MAX_MESSAGE_LENGTH // 2:
                    split_at = idx + len(sep)
                    break

            chunks.append(text[:split_at])
            text = text[split_at:]

        return chunks

    # =========================================================================
    # Media context extraction
    # =========================================================================

    @staticmethod
    def _extract_media_context(message: dict) -> Optional[str]:
        """Extract descriptive context from media messages."""
        parts = []

        if "photo" in message:
            caption = message.get("caption", "")
            parts.append(f"[User sent a photo{': ' + caption if caption else ''}]")

        if "document" in message:
            doc = message["document"]
            filename = doc.get("file_name", "unknown")
            caption = message.get("caption", "")
            parts.append(f"[User sent a document: {filename}{' — ' + caption if caption else ''}]")

        if "sticker" in message:
            sticker = message["sticker"]
            emoji = sticker.get("emoji", "")
            parts.append(f"[User sent a sticker: {emoji}]")

        if "location" in message:
            loc = message["location"]
            parts.append(f"[User shared a location: {loc.get('latitude')}, {loc.get('longitude')}]")

        if "voice" in message:
            parts.append("[User sent a voice message — voice transcription is not yet available]")

        if "video" in message:
            caption = message.get("caption", "")
            parts.append(f"[User sent a video{': ' + caption if caption else ''}]")

        return "\n".join(parts) if parts else None
