"""
Slack Socket Mode transport — outbound WebSocket connection.

No public URL needed. Trinity connects out to Slack.
Default transport for local development.

Requires:
- slack_sdk[socket-mode] package
- App-Level Token (xapp-...) with connections:write scope
- Socket Mode enabled in Slack App settings
"""

import logging
import asyncio
from adapters.transports.base import ChannelTransport

logger = logging.getLogger(__name__)


class SlackSocketTransport(ChannelTransport):
    """Slack Socket Mode — outbound WebSocket, no public URL needed."""

    def __init__(self, app_token: str, adapter, router):
        super().__init__(adapter, router)
        self.app_token = app_token
        self.client = None

    async def start(self) -> None:
        """Connect to Slack via WebSocket.

        Non-blocking: if connection fails (invalid token, network error),
        logs the error but does not block startup or crash the backend.
        """
        # Validate token format before attempting connection
        if not self.app_token or not self.app_token.startswith("xapp-"):
            logger.error("Invalid Slack App Token format (must start with 'xapp-'). Socket Mode not started.")
            return

        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            self.client = SocketModeClient(
                app_token=self.app_token,
                auto_reconnect_enabled=False,  # Don't auto-reconnect on invalid token
            )
            self.client.socket_mode_request_listeners.append(self._handle_request)

            # Connect with timeout to prevent blocking startup
            try:
                await asyncio.wait_for(self.client.connect(), timeout=10)
            except asyncio.TimeoutError:
                logger.error("Slack Socket Mode connection timed out (10s). Check app token and network.")
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                return

            # Connection succeeded — now enable auto-reconnect for resilience
            self.client.auto_reconnect_enabled = True
            self._running = True
            logger.info("Slack Socket Mode transport connected")
        except ImportError:
            logger.error("slack_sdk[socket-mode] not installed. Run: pip install slack_sdk[socket-mode]")
        except Exception as e:
            logger.error(f"Failed to start Slack Socket Mode: {e}")
            self.client = None

    async def stop(self) -> None:
        """Disconnect from Slack."""
        self._running = False
        if self.client:
            try:
                await self.client.disconnect()
                logger.info("Slack Socket Mode transport disconnected")
            except Exception as e:
                logger.warning(f"Error disconnecting Socket Mode: {e}")
            self.client = None

    async def _handle_request(self, client, req) -> None:
        """Handle incoming Socket Mode request."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        logger.info(f"Socket Mode received: type={req.type}, payload_type={req.payload.get('event', {}).get('type') if isinstance(req.payload, dict) else 'n/a'}")

        # Acknowledge immediately — no timeout concerns
        response = SocketModeResponse(envelope_id=req.envelope_id)
        await client.send_socket_mode_response(response)

        if req.type == "events_api":
            # Process in a task but log errors
            async def _process():
                try:
                    await self.on_event(req.payload)
                except Exception as e:
                    logger.error(f"Error processing event: {e}", exc_info=True)
            asyncio.create_task(_process())
        elif req.type == "interactive":
            # Future: handle Block Kit button clicks (agent selection, etc.)
            logger.info(f"Received interactive event: {req.payload.get('type')}")
        else:
            logger.info(f"Unhandled Socket Mode request type: {req.type}")
