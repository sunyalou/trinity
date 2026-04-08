"""
Tests for webhook HMAC signature verification (#266).

Verifies that outbound webhooks from the Process Engine include
X-Trinity-Signature and X-Trinity-Timestamp headers with valid
HMAC-SHA256 signatures.

Module: src/backend/services/process_engine/events/webhook_publisher.py
"""
import hashlib
import hmac
import importlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# Add backend path
_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def _get_publisher_module(secret_key="test-secret-key"):
    """Import webhook_publisher module with mocked heavy dependencies.

    Returns the module so callers can patch httpx on it directly.
    """
    # Mock modules that cause import chain issues
    mock_modules = {}
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "models", "utils.helpers",
        "services.docker_service", "services.docker_utils",
    ]:
        mock_modules[mod] = Mock()

    # Mock config with our test secret
    mock_config = Mock()
    mock_config.SECRET_KEY = secret_key

    with patch.dict("sys.modules", mock_modules):
        with patch.dict("sys.modules", {"config": mock_config}):
            # Force reimport to pick up mocked config
            mod_path = "services.process_engine.events.webhook_publisher"
            if mod_path in sys.modules:
                del sys.modules[mod_path]
            for key in list(sys.modules.keys()):
                if key.startswith("services.process_engine"):
                    del sys.modules[key]

            import services.process_engine.events.webhook_publisher as wp_mod

    return wp_mod


def _get_publisher_class(secret_key="test-secret-key"):
    """Shorthand to get just the class."""
    return _get_publisher_module(secret_key).WebhookEventPublisher


def _compute_expected_signature(
    secret_key: str, payload_bytes: bytes, timestamp: str
) -> str:
    """Recompute signature the same way the publisher does."""
    signing_key = hmac.new(
        secret_key.encode(), b"webhook-signing", hashlib.sha256
    ).digest()
    message = f"{timestamp}.".encode() + payload_bytes
    return hmac.new(signing_key, message, hashlib.sha256).hexdigest()


class TestComputeSignature:
    """Test the _compute_signature static method."""

    def test_deterministic(self):
        """Same inputs produce same signature."""
        Cls = _get_publisher_class("test-key")
        sig1 = Cls._compute_signature(b'{"test":true}', "1700000000")
        sig2 = Cls._compute_signature(b'{"test":true}', "1700000000")
        assert sig1 == sig2

    def test_changes_with_payload(self):
        """Different payloads produce different signatures."""
        Cls = _get_publisher_class("test-key")
        sig1 = Cls._compute_signature(b'{"a":1}', "1700000000")
        sig2 = Cls._compute_signature(b'{"b":2}', "1700000000")
        assert sig1 != sig2

    def test_changes_with_timestamp(self):
        """Different timestamps produce different signatures."""
        Cls = _get_publisher_class("test-key")
        sig1 = Cls._compute_signature(b'{"test":true}', "1700000000")
        sig2 = Cls._compute_signature(b'{"test":true}', "1700000001")
        assert sig1 != sig2

    def test_changes_with_secret(self):
        """Different SECRET_KEYs produce different signatures."""
        ClsA = _get_publisher_class("key-a")
        ClsB = _get_publisher_class("key-b")
        sig1 = ClsA._compute_signature(b'{"test":true}', "1700000000")
        sig2 = ClsB._compute_signature(b'{"test":true}', "1700000000")
        assert sig1 != sig2

    def test_matches_manual_computation(self):
        """Signature matches manual HMAC-SHA256 computation."""
        secret = "my-secret"
        Cls = _get_publisher_class(secret)
        payload = b'{"event":"test"}'
        ts = "1700000000"

        actual = Cls._compute_signature(payload, ts)
        expected = _compute_expected_signature(secret, payload, ts)
        assert actual == expected

    def test_hex_length(self):
        """Signature is a 64-character hex string (SHA-256)."""
        Cls = _get_publisher_class("key")
        sig = Cls._compute_signature(b'{}', "0")
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)


class TestSendWebhook:
    """Test that _send_webhook includes correct headers."""

    @pytest.mark.asyncio
    async def test_includes_signature_headers(self):
        """_send_webhook adds X-Trinity-Signature and X-Trinity-Timestamp."""
        wp_mod = _get_publisher_module("test-key")
        publisher = wp_mod.WebhookEventPublisher()
        payload = {"event_type": "process_completed"}

        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_httpx = wp_mod.httpx
        wp_mod.httpx = MagicMock()
        wp_mod.httpx.AsyncClient = MagicMock(return_value=mock_client)
        wp_mod.httpx.TimeoutException = original_httpx.TimeoutException
        try:
            await publisher._send_webhook("https://example.com/hook", payload)
        finally:
            wp_mod.httpx = original_httpx

        headers = captured["headers"]
        assert "X-Trinity-Signature" in headers
        assert headers["X-Trinity-Signature"].startswith("v1=")
        assert len(headers["X-Trinity-Signature"]) == 3 + 64  # "v1=" + 64 hex

        assert "X-Trinity-Timestamp" in headers
        ts = int(headers["X-Trinity-Timestamp"])
        assert abs(ts - int(time.time())) < 5

    @pytest.mark.asyncio
    async def test_signature_verifiable_by_receiver(self):
        """Receiver can verify the signature using the documented algorithm."""
        secret = "verify-me"
        wp_mod = _get_publisher_module(secret)
        publisher = wp_mod.WebhookEventPublisher()
        payload = {"event_type": "process_completed", "id": "abc-123"}

        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_httpx = wp_mod.httpx
        wp_mod.httpx = MagicMock()
        wp_mod.httpx.AsyncClient = MagicMock(return_value=mock_client)
        wp_mod.httpx.TimeoutException = original_httpx.TimeoutException
        try:
            await publisher._send_webhook("https://example.com/hook", payload)
        finally:
            wp_mod.httpx = original_httpx

        # Simulate receiver verification
        body = captured["content"]
        timestamp = captured["headers"]["X-Trinity-Timestamp"]
        received_sig = captured["headers"]["X-Trinity-Signature"].removeprefix("v1=")

        expected_sig = _compute_expected_signature(secret, body, timestamp)
        assert received_sig == expected_sig

    @pytest.mark.asyncio
    async def test_uses_content_not_json_kwarg(self):
        """Sends pre-serialized bytes via content= so signature matches body."""
        wp_mod = _get_publisher_module("test")
        publisher = wp_mod.WebhookEventPublisher()
        payload = {"z": 1, "a": 2}

        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_httpx = wp_mod.httpx
        wp_mod.httpx = MagicMock()
        wp_mod.httpx.AsyncClient = MagicMock(return_value=mock_client)
        wp_mod.httpx.TimeoutException = original_httpx.TimeoutException
        try:
            await publisher._send_webhook("https://example.com/hook", payload)
        finally:
            wp_mod.httpx = original_httpx

        assert "content" in captured
        assert "json" not in captured
        assert json.loads(captured["content"]) == payload

    @pytest.mark.asyncio
    async def test_sorted_keys_for_deterministic_body(self):
        """JSON body uses sorted keys for deterministic serialization."""
        wp_mod = _get_publisher_module("test")
        publisher = wp_mod.WebhookEventPublisher()
        payload = {"z": 1, "a": 2, "m": 3}

        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_httpx = wp_mod.httpx
        wp_mod.httpx = MagicMock()
        wp_mod.httpx.AsyncClient = MagicMock(return_value=mock_client)
        wp_mod.httpx.TimeoutException = original_httpx.TimeoutException
        try:
            await publisher._send_webhook("https://example.com/hook", payload)
        finally:
            wp_mod.httpx = original_httpx

        body = captured["content"].decode()
        assert body == '{"a":2,"m":3,"z":1}'
