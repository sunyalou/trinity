"""
Unit test conftest — overrides the parent conftest's autouse fixtures.

These tests run without a backend connection (no Docker, no API).
"""
import pytest


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override parent's cleanup_after_test that requires api_client."""
    yield
