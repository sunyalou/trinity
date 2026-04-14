"""
Structured logging configuration for Trinity.
Logs go to stdout and are captured by Vector.

Includes OpenTelemetry trace ID for log-trace correlation (RELIABILITY-002).
"""
import logging
import json
from datetime import datetime

from opentelemetry import trace


class JsonFormatter(logging.Formatter):
    """Format logs as JSON for easy parsing by Vector."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add OpenTelemetry trace context for log-trace correlation
        span = trace.get_current_span()
        span_context = span.get_span_context()
        if span_context.is_valid:
            log_entry["trace_id"] = format(span_context.trace_id, "032x")
            log_entry["span_id"] = format(span_context.span_id, "016x")

        # Add extra fields if present
        if hasattr(record, "event_type"):
            log_entry["event_type"] = record.event_type
        if hasattr(record, "agent_name"):
            log_entry["agent_name"] = record.agent_name
        if hasattr(record, "user_id"):
            log_entry["user_id"] = record.user_id
        if hasattr(record, "user_email"):
            log_entry["user_email"] = record.user_email
        if hasattr(record, "action"):
            log_entry["action"] = record.action
        if hasattr(record, "result"):
            log_entry["result"] = record.result
        if hasattr(record, "details"):
            log_entry["details"] = record.details

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


def setup_logging():
    """Configure structured JSON logging for production."""
    # Remove existing handlers
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Create JSON handler for stdout
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    # Configure root logger
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # Configure Trinity loggers
    for logger_name in ["trinity", "trinity.agents", "trinity.auth", "trinity.errors"]:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)

    # Quiet noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a Trinity logger instance."""
    return logging.getLogger(f"trinity.{name}")
