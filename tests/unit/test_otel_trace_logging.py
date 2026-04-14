"""
Unit tests for OpenTelemetry trace ID injection in logs.

Issue: https://github.com/abilityai/trinity/issues/305
Module: src/backend/logging_config.py

When OpenTelemetry tracing is active, the JsonFormatter should inject
trace_id and span_id into log entries for log-trace correlation.
"""

import json
import logging
import os
import pytest
import sys

# Add src/backend to path for logging_config import
_backend_path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, os.path.abspath(_backend_path))

# Check if OpenTelemetry is available
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


@pytest.mark.skipif(not OTEL_AVAILABLE, reason="OpenTelemetry packages not installed")
class TestTraceIdLogging:
    """Verify trace_id and span_id are injected into logs correctly."""

    def test_log_includes_trace_id_when_span_active(self):
        """
        Log entries should include trace_id and span_id when inside an active span.

        RELIABILITY-002: Log-trace correlation requires trace context in logs.
        """
        # Set up a tracer provider
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(__name__)

        from logging_config import JsonFormatter

        # Create a logger with our formatter
        formatter = JsonFormatter()

        # Start a span and create a log record
        with tracer.start_as_current_span("test_span") as span:
            span_context = span.get_span_context()

            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="Test message",
                args=(),
                exc_info=None,
            )

            # Format the record
            output = formatter.format(record)
            log_data = json.loads(output)

            # Verify trace_id and span_id are present
            assert "trace_id" in log_data, "trace_id should be in log entry"
            assert "span_id" in log_data, "span_id should be in log entry"

            # Verify format: trace_id is 32 hex chars, span_id is 16 hex chars
            assert len(log_data["trace_id"]) == 32, "trace_id should be 32 hex characters"
            assert len(log_data["span_id"]) == 16, "span_id should be 16 hex characters"

            # Verify they match the actual span context
            expected_trace_id = format(span_context.trace_id, "032x")
            expected_span_id = format(span_context.span_id, "016x")
            assert log_data["trace_id"] == expected_trace_id
            assert log_data["span_id"] == expected_span_id

    def test_log_excludes_trace_id_when_no_span(self):
        """
        Log entries should NOT include trace_id when no span is active.

        This prevents polluting logs with invalid trace context.
        """
        # Reset tracer provider to ensure no active span
        provider = TracerProvider()
        trace.set_tracer_provider(provider)

        from logging_config import JsonFormatter

        formatter = JsonFormatter()

        # Create a log record outside any span
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message without span",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        log_data = json.loads(output)

        # trace_id and span_id should NOT be present (invalid span context)
        # Note: get_current_span() returns a non-recording span with invalid context
        assert "trace_id" not in log_data or log_data.get("trace_id") is None, \
            "trace_id should not be in log entry when no span is active"

    def test_log_preserves_standard_fields(self):
        """
        JsonFormatter should preserve standard log fields alongside trace context.
        """
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(__name__)

        from logging_config import JsonFormatter

        formatter = JsonFormatter()

        with tracer.start_as_current_span("test_span"):
            record = logging.LogRecord(
                name="test.module",
                level=logging.WARNING,
                pathname="test.py",
                lineno=42,
                msg="Warning message",
                args=(),
                exc_info=None,
            )

            output = formatter.format(record)
            log_data = json.loads(output)

            # Standard fields should be present
            assert "timestamp" in log_data
            assert log_data["level"] == "WARNING"
            assert log_data["logger"] == "test.module"
            assert log_data["message"] == "Warning message"

            # Trace fields should also be present
            assert "trace_id" in log_data
            assert "span_id" in log_data
