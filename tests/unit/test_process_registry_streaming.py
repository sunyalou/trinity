from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from unittest.mock import MagicMock

import pytest

from agent_server.services.process_registry import ProcessRegistry


def _fake_process(pid: int = 1234) -> MagicMock:
    process = MagicMock(spec=subprocess.Popen)
    process.pid = pid
    process.poll.return_value = None
    return process


@pytest.mark.unit
class TestProcessRegistryStreaming:
    @pytest.mark.asyncio
    async def test_threadsafe_publish_from_worker_thread_reaches_subscriber(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        queue = registry.subscribe_logs("exec-1")
        assert queue is not None

        entry = {"type": "assistant", "message": "hello"}
        worker = threading.Thread(
            target=registry.publish_log_entry_threadsafe,
            args=("exec-1", entry),
        )
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()

        assert await asyncio.wait_for(queue.get(), timeout=1) == entry

    @pytest.mark.asyncio
    async def test_threadsafe_publish_then_unregister_preserves_final_log_order(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        queue = registry.subscribe_logs("exec-1")
        assert queue is not None

        final_log = {"type": "assistant", "message": "final"}

        def worker_finalize():
            registry.publish_log_entry_threadsafe("exec-1", final_log)
            registry.unregister_threadsafe("exec-1")

        worker = threading.Thread(target=worker_finalize)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()

        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)

        assert [first, second] == [final_log, {"type": "stream_end"}]
        assert queue.empty()
        assert registry.get_buffered_logs("exec-1") == [
            final_log,
            {"type": "stream_end"},
        ]

    def test_late_publish_after_unregister_does_not_append_after_stream_end(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        registry.publish_log_entry("exec-1", {"type": "log", "index": 1})
        registry.unregister("exec-1")
        registry.publish_log_entry("exec-1", {"type": "log", "index": 2})

        buffered = registry.get_buffered_logs("exec-1")
        assert buffered == [
            {"type": "log", "index": 1},
            {"type": "stream_end"},
        ]

    def test_completed_buffer_replays_entries_plus_stream_end_after_unregister(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        first = {"type": "system", "message": "started"}
        second = {"type": "assistant", "message": "done"}

        registry.publish_log_entry("exec-1", first)
        registry.publish_log_entry("exec-1", second)
        registry.unregister("exec-1")

        assert registry.get_buffered_logs("exec-1") == [
            first,
            second,
            {"type": "stream_end"},
        ]

        replay_queue = registry.subscribe_logs("exec-1")
        assert replay_queue is not None
        assert replay_queue.get_nowait() == first
        assert replay_queue.get_nowait() == second
        assert replay_queue.get_nowait() == {"type": "stream_end"}
        assert replay_queue.empty()

    def test_completed_buffer_global_cap_evicts_oldest_completed_executions(self):
        registry = ProcessRegistry()
        registry._completed_buffer_limit = 2

        for index in range(3):
            execution_id = f"exec-{index}"
            registry.register(execution_id, _fake_process(pid=1000 + index))
            registry.publish_log_entry(execution_id, {"type": "log", "index": index})
            registry.unregister(execution_id)

        assert registry.get_buffered_logs("exec-0") is None
        assert registry.get_buffered_logs("exec-1") == [
            {"type": "log", "index": 1},
            {"type": "stream_end"},
        ]
        assert registry.get_buffered_logs("exec-2") == [
            {"type": "log", "index": 2},
            {"type": "stream_end"},
        ]

    def test_unregister_delivers_stream_end_to_full_live_queue(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        queue = registry.subscribe_logs("exec-1")
        assert queue is not None

        for index in range(queue.maxsize):
            queue.put_nowait({"type": "log", "index": index})

        registry.unregister("exec-1")

        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())

        assert {"type": "stream_end"} in drained

    def test_completed_replay_preserves_stream_end_when_buffer_exceeds_queue_capacity(self):
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())

        for index in range(600):
            registry.publish_log_entry("exec-1", {"type": "log", "index": index})
        registry.unregister("exec-1")

        replay_queue = registry.subscribe_logs("exec-1")
        assert replay_queue is not None

        drained = []
        while not replay_queue.empty():
            drained.append(replay_queue.get_nowait())

        assert {"type": "stream_end"} in drained

    def test_completed_buffer_expires_after_ttl(self, monkeypatch):
        import agent_server.services.process_registry as pr_mod

        monkeypatch.setattr(pr_mod, "RECENTLY_COMPLETED_TTL_SECONDS", 0.01)
        registry = ProcessRegistry()
        registry.register("exec-1", _fake_process())
        registry.publish_log_entry("exec-1", {"type": "log"})
        registry.unregister("exec-1")
        assert registry.get_buffered_logs("exec-1") is not None

        registry._recently_completed["exec-1"] -= 1

        assert registry.get_buffered_logs("exec-1") is None

    @pytest.mark.asyncio
    async def test_completed_static_stream_emits_single_stream_end(self, monkeypatch):
        from agent_server.routers import chat

        registry = MagicMock()
        registry.is_execution_running.return_value = False
        registry.get_buffered_logs.return_value = [
            {"type": "log", "message": "done"},
            {"type": "stream_end"},
            {"type": "log", "message": "late"},
        ]
        monkeypatch.setattr(chat, "get_process_registry", lambda: registry)

        response = await chat.stream_execution_log("exec-1")
        body = ""
        async for chunk in response.body_iterator:
            body += chunk.decode() if isinstance(chunk, bytes) else chunk

        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        assert events.count({"type": "stream_end"}) == 1
        assert events == [
            {"type": "log", "message": "done"},
            {"type": "stream_end"},
        ]

    def test_production_paths_use_threadsafe_publish_and_finalize(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        publishing_paths = (
            "docker/base-image/agent_server/services/claude_code.py",
            "docker/base-image/agent_server/services/headless_executor.py",
        )
        for relative_path in publishing_paths:
            source = (root / relative_path).read_text()
            assert ".publish_log_entry_threadsafe(" in source
            assert ".publish_log_entry(" not in source

        finalize_paths = publishing_paths + (
            "docker/base-image/agent_server/services/opencode_runtime.py",
        )
        for relative_path in finalize_paths:
            source = (root / relative_path).read_text()
            assert ".unregister_threadsafe(" in source
            assert ".unregister(" not in source
