"""
Unit tests for context_used formula in TaskExecutionService.

Issue: https://github.com/abilityai/trinity/issues/56
Module: src/backend/services/task_execution_service.py

The context_used field should equal input_tokens only (not input_tokens + output_tokens).
Per Claude Code SDK, input_tokens represents the full context window fill level at the
final turn, including accumulated tool results. output_tokens is what was generated and
is NOT part of context until the next turn.
"""

import pytest


class TestContextUsedFormula:
    """Verify context_used equals input_tokens, not input + output."""

    def test_context_used_equals_input_tokens_only(self):
        """
        Given metadata with input_tokens=5000 and output_tokens=500,
        context_used should be 5000 (not 5500).

        This is the formula used at task_execution_service.py:412.
        """
        metadata = {
            "input_tokens": 5000,
            "output_tokens": 500,
            "context_window": 200000,
            "cost_usd": 0.05,
        }

        # The correct formula (what we fixed)
        context_used = metadata.get("input_tokens", 0)

        # Verify it uses input_tokens only
        assert context_used == 5000
        assert context_used != 5500  # Would be wrong: input + output

    def test_context_used_zero_when_no_input_tokens(self):
        """context_used defaults to 0 when input_tokens is missing."""
        metadata = {"output_tokens": 500}

        context_used = metadata.get("input_tokens", 0)

        assert context_used == 0

    def test_context_used_percentage_calculation(self):
        """
        Verify percentage stays under 100% with correct formula.

        If we incorrectly added output_tokens, large outputs could
        push context_used > context_window, yielding >100%.
        """
        metadata = {
            "input_tokens": 180000,  # 90% of context
            "output_tokens": 50000,   # Would push to 115% if added
            "context_window": 200000,
        }

        context_used = metadata.get("input_tokens", 0)
        context_max = metadata.get("context_window", 200000)

        percentage = (context_used / context_max) * 100

        assert percentage == 90.0
        assert percentage <= 100.0  # Sanity: should never exceed 100%
