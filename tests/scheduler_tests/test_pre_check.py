"""
Tests for the agent-owned pre-check hook (#454).

Covers:
- AgentClient.pre_check returns dict on 200, None on 404, None on error
- SchedulerService skips execution when pre_check returns fire=False
- SchedulerService uses override message when pre_check returns fire=True with message
- SchedulerService falls through to normal fire when pre_check returns None (fail-open)
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path

_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scheduler.agent_client import AgentClient, AgentNotReachableError
from scheduler.models import ExecutionStatus


# ---------------------------------------------------------------------------
# AgentClient.pre_check unit tests
# ---------------------------------------------------------------------------


class TestAgentClientPreCheck:
    @pytest.mark.asyncio
    async def test_returns_decision_on_200(self):
        client = AgentClient("pr-reviewer")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"fire": True, "message": "review this"}
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result == {"fire": True, "message": "review this"}

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        """Endpoint absent — scheduler should fall back to normal fire."""
        client = AgentClient("no-hook-agent")
        response = MagicMock()
        response.status_code = 404
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_5xx(self):
        client = AgentClient("broken-agent")
        response = MagicMock()
        response.status_code = 500
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unreachable(self):
        client = AgentClient("unreachable-agent")
        with patch.object(
            client,
            "_request",
            AsyncMock(side_effect=AgentNotReachableError("timeout")),
        ):
            result = await client.pre_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_malformed_json(self):
        client = AgentClient("sketchy-agent")
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_fire_field(self):
        client = AgentClient("wrong-shape-agent")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"message": "hi"}  # no "fire"
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_skip_decision(self):
        client = AgentClient("pr-reviewer")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"fire": False, "reason": "no new PRs"}
        with patch.object(client, "_request", AsyncMock(return_value=response)):
            result = await client.pre_check()
        assert result == {"fire": False, "reason": "no new PRs"}


# ---------------------------------------------------------------------------
# SchedulerService._execute_schedule_with_lock pre-check branch
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_scheduler(db_with_data):
    """Build a SchedulerService with mocked dependencies so we can drive the
    pre-check branch of `_execute_schedule_with_lock` without real networking."""
    from scheduler.service import SchedulerService

    svc = SchedulerService.__new__(SchedulerService)
    svc.db = db_with_data
    svc.lock_manager = MagicMock()
    svc._publish_event = AsyncMock()
    svc._call_backend_execute_task = AsyncMock(
        return_value={"status": "dispatched"}
    )
    svc._get_next_run_time = MagicMock(return_value=None)
    return svc


class TestSchedulerPreCheckBranch:
    @pytest.mark.asyncio
    async def test_skip_when_fire_false(self, mock_scheduler, db_with_data):
        """pre-check fire=False → create skipped execution, no chat dispatch."""
        mock_scheduler._run_pre_check = AsyncMock(
            return_value={"fire": False, "reason": "no new PRs"}
        )

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_not_called()

        mock_scheduler._publish_event.assert_awaited()
        event = mock_scheduler._publish_event.await_args.args[0]
        assert event["type"] == "schedule_execution_skipped"
        assert event["reason"] == "no new PRs"

        skipped = db_with_data.get_execution(event["execution_id"])
        assert skipped is not None
        assert skipped.status == ExecutionStatus.SKIPPED
        assert "no new PRs" in (skipped.error or "")

    @pytest.mark.asyncio
    async def test_fire_with_override_message(self, mock_scheduler, db_with_data):
        """pre-check fire=True with message → chat dispatched with override."""
        mock_scheduler._run_pre_check = AsyncMock(
            return_value={"fire": True, "message": "Review abilityai/trinity#371"}
        )

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Review abilityai/trinity#371"

    @pytest.mark.asyncio
    async def test_fail_open_when_pre_check_returns_none(
        self, mock_scheduler, db_with_data
    ):
        """pre-check returns None (e.g. 404) → fire as usual with schedule.message."""
        mock_scheduler._run_pre_check = AsyncMock(return_value=None)

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Run morning report"

    @pytest.mark.asyncio
    async def test_fire_true_without_message_uses_schedule_message(
        self, mock_scheduler, db_with_data
    ):
        mock_scheduler._run_pre_check = AsyncMock(return_value={"fire": True})

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Run morning report"

    @pytest.mark.asyncio
    async def test_manual_trigger_bypasses_pre_check(
        self, mock_scheduler, db_with_data
    ):
        """Manual triggers are explicit operator intent — pre-check must not run."""
        mock_scheduler._run_pre_check = AsyncMock()

        await mock_scheduler._execute_schedule_with_lock(
            "schedule-1", triggered_by="manual"
        )

        mock_scheduler._run_pre_check.assert_not_called()
        mock_scheduler._call_backend_execute_task.assert_awaited_once()
