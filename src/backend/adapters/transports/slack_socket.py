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
import time
from adapters.transports.base import ChannelTransport

logger = logging.getLogger(__name__)

# Watchdog config
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_PING_TIMEOUT_SECONDS = 5
WATCHDOG_BACKOFF_INITIAL_SECONDS = 60
WATCHDOG_BACKOFF_MAX_SECONDS = 300


class SlackSocketTransport(ChannelTransport):
    """Slack Socket Mode — outbound WebSocket, no public URL needed."""

    def __init__(self, app_token: str, adapter, router):
        super().__init__(adapter, router)
        self.app_token = app_token
        self.client = None
        self._watchdog_task = None
        self._consecutive_failures = 0

    @property
    def is_connected(self) -> bool:
        """Check actual connection health, not just the running flag."""
        if not self._running or not self.client:
            return False
        session = self.client.current_session
        return session is not None and not session.closed

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
            self._consecutive_failures = 0
            self._watchdog_task = asyncio.create_task(self._watchdog())
            logger.info("Slack Socket Mode transport connected (watchdog started)")
        except ImportError:
            logger.error("slack_sdk[socket-mode] not installed. Run: pip install slack_sdk[socket-mode]")
        except Exception as e:
            logger.error(f"Failed to start Slack Socket Mode: {e}")
            self.client = None

    async def stop(self) -> None:
        """Disconnect from Slack."""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
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

    async def _watchdog(self) -> None:
        """Application-level watchdog — detects silent socket death and forces reconnect.

        The SDK has its own monitor (monitor_current_session) that pings and reconnects,
        but if that monitor task dies, the socket stays dead forever with no recovery.
        This watchdog is independent of the SDK.

        Checks every 60s:
        1. Session dead (None or closed) → reconnect
        2. SDK monitor dead (cancelled/done) → reconnect
        3. Ping/pong health check — timeout means connection is dead → reconnect

        Uses exponential backoff (60s → 120s → 240s → 300s cap) on consecutive failures.
        Logs a prominent error when backoff hits the cap, so ops can investigate.
        """
        logger.info("Socket Mode watchdog started")
        while self._running:
            try:
                interval = self._get_backoff_interval()
                await asyncio.sleep(interval)

                if not self._running or not self.client:
                    break

                reason = self._check_health()

                if reason is None:
                    # Session and monitor look OK — ping to verify
                    reason = await self._ping_check()

                if reason:
                    await self._attempt_reconnect(reason)
                else:
                    # Healthy — reset backoff
                    if self._consecutive_failures > 0:
                        logger.info("Socket Mode watchdog: connection recovered, resetting backoff")
                    self._consecutive_failures = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Never crash the backend — log and continue
                logger.error(f"Socket Mode watchdog unexpected error: {e}", exc_info=True)

        logger.info("Socket Mode watchdog stopped")

    def _check_health(self) -> str | None:
        """Check session and monitor state. Returns failure reason or None."""
        session = self.client.current_session

        if session is None or session.closed:
            return "session is dead/closed"

        monitor = self.client.current_session_monitor
        if monitor is None:
            return "SDK monitor is None"
        if monitor.done():
            return "SDK monitor task exited"

        return None

    async def _ping_check(self) -> str | None:
        """Ping the WebSocket. Returns failure reason or None."""
        session = self.client.current_session
        if not session:
            return "session gone before ping"
        try:
            await asyncio.wait_for(session.ping(), timeout=WATCHDOG_PING_TIMEOUT_SECONDS)
            return None
        except asyncio.TimeoutError:
            return f"ping timeout ({WATCHDOG_PING_TIMEOUT_SECONDS}s)"
        except Exception as e:
            return f"ping failed: {e}"

    async def _attempt_reconnect(self, reason: str) -> None:
        """Try to reconnect. Tracks consecutive failures for backoff."""
        self._consecutive_failures += 1
        backoff = self._get_backoff_interval()

        if backoff >= WATCHDOG_BACKOFF_MAX_SECONDS:
            logger.error(
                f"SLACK SOCKET MODE UNREACHABLE — {reason}. "
                f"Failed {self._consecutive_failures} consecutive reconnect attempts. "
                f"Next retry in {backoff}s. Check Slack app token and network connectivity."
            )
        else:
            logger.warning(
                f"Socket Mode watchdog: {reason} — attempting reconnect "
                f"(attempt {self._consecutive_failures}, next check in {backoff}s)"
            )

        try:
            # The SDK's connect_to_new_endpoint uses an internal lock,
            # so concurrent calls (from SDK monitor + our watchdog) are safe
            await self.client.connect_to_new_endpoint()
            logger.info(
                f"Socket Mode watchdog: reconnected successfully "
                f"after {self._consecutive_failures} attempt(s)"
            )
            self._consecutive_failures = 0
        except Exception as e:
            logger.error(f"Socket Mode watchdog: reconnect failed: {e}")

    def _get_backoff_interval(self) -> int:
        """Exponential backoff: 60 → 120 → 240 → 300 (cap)."""
        if self._consecutive_failures == 0:
            return WATCHDOG_INTERVAL_SECONDS
        backoff = WATCHDOG_BACKOFF_INITIAL_SECONDS * (2 ** (self._consecutive_failures - 1))
        return min(backoff, WATCHDOG_BACKOFF_MAX_SECONDS)
