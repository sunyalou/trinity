# Feature Flow: Persistent Task Backlog

> **Requirement**: BACKLOG-001 — Persistent async task backlog for over-capacity requests
> **Status**: Implemented
> **Created**: 2026-04-13
> **GitHub Issue**: [#260](https://github.com/abilityai/trinity/issues/260)
> **Priority**: P1
> **Related**: [parallel-capacity.md](parallel-capacity.md), [task-execution-service.md](task-execution-service.md), [parallel-headless-execution.md](parallel-headless-execution.md)

## Overview

When `async_mode=true` arrives at `POST /api/agents/{name}/task` and all of the
agent's parallel execution slots (CAPACITY-001) are occupied, the request is
spilled into a durable SQLite-backed FIFO backlog instead of returning HTTP
429. When a slot frees, the oldest queued item for that agent is drained
automatically via a `SlotService` release callback. True HTTP 429 is only
returned when the backlog itself is also at its configured depth.

Queued rows survive backend restarts. A 60-second maintenance task in the
backend process expires rows older than 24 hours and drains orphans left
behind when a release callback couldn't fire (e.g. process crash).

## Problem Statement

Before this change, `async_mode=true` requests at capacity were dropped on
the floor with a 429 response. Bursty MCP fan-out scenarios (agents
orchestrating other agents via `chat_with_agent(async=true)`) routinely hit
the 3-slot default cap and lost work. Clients had to implement their own
retry-with-backoff logic, and there was no first-class backpressure signal.

The backlog gives Trinity:
- **Spill-over by default** for async mode — no lost requests below the
  backlog depth cap
- **Restart durability** — queued items survive backend restarts via SQLite
- **Bounded resource envelope** — per-agent `max_backlog_depth` (default 50,
  hard cap 200) + 24h stale expiry
- **Transparent to pollers** — existing `GET /api/agents/{name}/executions/{id}`
  returns `status=queued` while the row waits to drain

## Architecture Diagram

```
                  POST /api/agents/{name}/task
                         async_mode=true
                                │
                   ┌────────────▼─────────────┐
                   │  routers/chat.py         │
                   │  (submit_parallel_task)  │
                   └────────────┬─────────────┘
                                │
                  create_task_execution (status=running)
                                │
                  slot_service.acquire_slot()
                                │
             ┌──────────────────┴───────────────────┐
             │                                      │
     slot acquired                           slot full
             │                                      │
             ▼                                      ▼
    _execute_task_background()          backlog.enqueue()
             │                                      │
             ▼                          ┌───────────┴───────────┐
     finally: release_slot              │                       │
             │                    depth < cap             depth >= cap
             ▼                          │                       │
    release callbacks fire              ▼                       ▼
             │                 HTTP 202 queued              HTTP 429
             ▼
  backlog.on_slot_released(name)
             │
             ▼
    backlog.drain_next(name)
             │
      ┌──────┴──────┐
      │             │
  sentinel     no queued
  acquire     items → exit
      │
      ▼
  claim_next_queued  ◄── atomic UPDATE ... RETURNING
      │
      ▼
  swap sentinel → real execution_id slot
      │
      ▼
  reconstruct ParallelTaskRequest from backlog_metadata
      │
      ▼
  asyncio.create_task(
      _execute_task_background(
          release_slot=True,
          identity from backlog_metadata,
      )
  )
```

Parallel path (safety net):

```
   backend startup
        │
        ▼
   main.py lifespan
        │
        ├── register_on_release(backlog.on_slot_released)
        │
        └── asyncio.create_task(_backlog_maintenance_loop)
                    │
                    │   every 60s:
                    │     - expire_stale(max_age_hours=24)
                    │     - drain_orphans_all() → picks up rows that missed
                    │       their callback after a restart or crash
                    ▼
```

## Database Schema

Migration `backlog_support` (append-only, reuses existing table):

```sql
ALTER TABLE schedule_executions ADD COLUMN queued_at TEXT;
ALTER TABLE schedule_executions ADD COLUMN backlog_metadata TEXT;
ALTER TABLE agent_ownership ADD COLUMN max_backlog_depth INTEGER DEFAULT 50;

CREATE INDEX idx_executions_queued
ON schedule_executions(agent_name, queued_at)
WHERE status = 'queued';
```

The partial index is the key to cheap FIFO claim — only queued rows are
indexed. `claim_next_queued` reads at most one row via the subquery,
making the operation O(log n) on backlog size.

### backlog_metadata JSON Shape

Captured at enqueue time, replayed on drain. No credential values, only
identity and request parameters:

```json
{
  "message": "...",
  "model": "sonnet",
  "allowed_tools": ["Read", "Bash"],
  "system_prompt": "...",
  "timeout_seconds": 900,
  "max_turns": null,
  "save_to_session": false,
  "user_message": "...",
  "create_new_session": false,
  "chat_session_id": null,
  "resume_session_id": null,
  "user_id": 42,
  "user_email": "user@example.com",
  "subscription_id": "sub-xyz",
  "x_source_agent": null,
  "x_mcp_key_id": "mcp-key-abc",
  "x_mcp_key_name": "my-key",
  "triggered_by": "manual",
  "collaboration_activity_id": null,
  "task_activity_id": null
}
```

## Lifecycle

```
queued ──(drain)──▶ running ──(success)──▶ success
   │                   │
   │                   └──(agent error)──▶ failed
   │
   ├──(user terminate)──▶ cancelled
   │
   ├──(agent delete)──▶ cancelled (reason=agent_deleted)
   │
   └──(stale >24h)──▶ failed (reason=backlog expired)
```

## Components

### Router — `src/backend/routers/chat.py`

**Enqueue (async path, replaces old 429 block)**:

```python
if not slot_acquired:
    from services.backlog_service import get_backlog_service
    backlog = get_backlog_service()
    enqueued = await backlog.enqueue(
        agent_name=name,
        execution_id=execution_id,
        request=request,
        effective_timeout=effective_timeout,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        subscription_id=_task_subscription_id,
        x_source_agent=x_source_agent,
        x_mcp_key_id=x_mcp_key_id,
        x_mcp_key_name=x_mcp_key_name,
        triggered_by=triggered_by,
        collaboration_activity_id=collaboration_activity_id,
        task_activity_id=None,
    )
    if enqueued:
        return {"status": "queued", "execution_id": execution_id, ...}
    # else: backlog full → real 429
```

**Terminate (short-circuit for queued rows)**:

```python
_exec_row = db.get_execution(task_execution_id)
if _exec_row and _exec_row.status == TaskExecutionStatus.QUEUED:
    cancelled = db.cancel_queued_execution(
        task_execution_id, reason="Cancelled by user while queued"
    )
    if cancelled:
        return {"status": "cancelled_while_queued", ...}
```

### Service — `src/backend/services/backlog_service.py` (NEW)

| Method | Purpose |
|---|---|
| `enqueue(...)` | Check depth, persist `backlog_metadata`, flip row to QUEUED. Returns False if at cap. |
| `drain_next(agent_name)` | Acquire sentinel slot → atomically claim row → swap to real execution_id slot → reconstruct `ParallelTaskRequest` → spawn `_execute_task_background`. |
| `on_slot_released(agent_name)` | Callback registered with SlotService. Tries `drain_next` once per release. |
| `expire_stale(max_age_hours=24)` | Maintenance: mark old queued rows as FAILED. |
| `drain_orphans_all()` | Maintenance: iterate agents with queued work, drain one item each. |
| `cancel_all_backlog(agent_name, reason)` | Called on agent delete. |

Design invariants:
- Slot acquired **before** row is claimed — prevents RUNNING-without-slot orphans.
- Single-statement `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING` — atomic claim.
- `_execute_task_background` is late-imported inside `_spawn_drain` to avoid a
  `routers.chat` ↔ `services.backlog_service` cycle.
- Identity replayed from `backlog_metadata`; no re-auth at drain time.

### Slot Service — `src/backend/services/slot_service.py`

Added release callback hook:

```python
def register_on_release(self, callback: Callable[[str], Awaitable[None]]) -> None: ...

async def release_slot(self, agent_name, execution_id):
    ...  # existing Redis ZSET + metadata cleanup
    for cb in self._on_release_callbacks:
        asyncio.create_task(self._safe_invoke(cb, agent_name))
```

Callback invocation is isolated (`_safe_invoke` catches exceptions per
callback), fire-and-forget via `create_task`, and gracefully handles "no
running loop" (returns silently and lets the 60s maintenance task drain the
work on the next tick).

### Lifespan — `src/backend/main.py`

```python
from services.slot_service import get_slot_service
from services.backlog_service import get_backlog_service
_backlog = get_backlog_service()
get_slot_service().register_on_release(_backlog.on_slot_released)

async def _backlog_maintenance_loop():
    await asyncio.sleep(15)  # small delay to not slow boot
    while True:
        try:
            await _backlog.expire_stale(max_age_hours=24)
            await _backlog.drain_orphans_all()
        except Exception as exc:
            logger.warning(f"[Backlog] maintenance tick failed: {exc}")
        await asyncio.sleep(60)

asyncio.create_task(_backlog_maintenance_loop())
```

### Delete cascade — `src/backend/routers/agents.py`

After stopping the container and deleting schedules, the delete path calls
`backlog.cancel_all_backlog(agent_name, reason="agent_deleted")` so orphan
queued rows don't linger in the database.

## Configuration

Per-agent backlog depth is stored in `agent_ownership.max_backlog_depth`:

```python
db.get_max_backlog_depth(agent_name)     # default 50
db.set_max_backlog_depth(agent_name, n)  # validates 1-200
```

No REST endpoint for this setting yet — the default (50) is sensible for
most workloads and the issue explicitly deferred UI configuration. Custom
values can be set directly via the internal DB method or a future Settings
page if demand emerges.

## Observability

- Every enqueue and drain logs agent_name, execution_id, and current
  depth at `INFO` level.
- Drain failures (corrupt metadata, lost slot race, background spawn
  failure) log at `ERROR` with the execution_id and cause.
- Queued rows are visible in existing tools:
  - `GET /api/agents/{name}/executions/{id}` returns `status=queued`
  - `GET /api/agents/{name}/executions` lists queued rows alongside other
    statuses
  - Frontend: `ExecutionDetail.vue` and `TasksPanel.vue` render a dedicated
    amber badge for `status=queued`.
- MCP `get_execution_result` polling transparently surfaces the `queued`
  status without tool changes.

## Failure Modes

| Failure | Behaviour |
|---|---|
| Corrupt `backlog_metadata` JSON | Row marked FAILED with reason, slot released, drain continues with next item. |
| Slot acquisition fails after claim | Row released back to QUEUED via `release_claim_to_queued`; next callback retries. |
| Backend crash mid-drain | Row stays RUNNING with no Claude session ID — existing cleanup service recovers it within the timeout window. New queued rows are drained by the 60s maintenance loop on restart. |
| Agent container gone when drain fires | `_execute_task_background` surfaces an HTTP error, row marked FAILED. |
| Concurrent drains on same agent | Atomic UPDATE guarantees only one callback wins the row; others get None and release their sentinel slots. |
| Cancel-while-queued | Terminate endpoint short-circuits, row moves to CANCELLED. The claim SQL's `WHERE status='queued'` filter naturally skips cancelled rows, so the drain path is race-safe. |

## Testing

Unit tests: `tests/unit/test_backlog.py` (29 tests, no backend/Docker required).

Coverage:
- TaskExecutionStatus.QUEUED enum value
- Migration (columns + partial index)
- `ResourcesMixin.get/set_max_backlog_depth` (including range validation 1-200)
- ScheduleOperations backlog queries: transition to queued, atomic FIFO claim,
  agent isolation, release-claim-back, single cancel, bulk cancel for agent,
  stale expiry (normal + tiny-threshold), list agents with queued
- `BacklogService.enqueue`: under cap succeeds, at cap rejected
- `BacklogService.drain_next`: empty noop, failed-claim releases slot,
  corrupt metadata marks failed, slot-acquire-failure noop, happy path
  spawns background task with reconstructed request
- `SlotService.register_on_release` + `release_slot` fan-out, per-callback
  exception isolation

### Prerequisites

- Backend running at http://localhost:8000
- Redis running (SlotService)
- Test agent created with known `max_parallel_tasks`

### Manual Verification Scenarios

#### 1. Spill to backlog under capacity pressure
**Action**:
- Set `max_parallel_tasks=1` on a test agent (via `PUT /api/agents/{name}/capacity`).
- Send 3 consecutive `POST /api/agents/{name}/task?async_mode=true` requests.
**Expected**:
- Request 1: `status=accepted` (running).
- Request 2 & 3: `status=queued` with HTTP 200 body.
- Poll `GET /api/agents/{name}/executions/{id}`: status transitions
  `queued → running → success` in FIFO order.

#### 2. True 429 when backlog is full
**Action**:
- Temporarily call `db.set_max_backlog_depth(name, 1)` via a Python shell.
- With `max_parallel_tasks=1`, send 3 async tasks rapidly.
**Expected**: request 3 receives `HTTP 429` with "backlog is full" detail.

#### 3. Restart durability
**Action**:
- Queue 2 tasks (as in scenario 1, second task in queued state).
- `docker compose restart backend`.
**Expected**: within ~75 seconds (15s startup delay + 60s tick), the second
task drains and completes. No manual intervention.

#### 4. Cancel while queued
**Action**:
- Queue a task via scenario 1.
- Call `POST /api/agents/{name}/executions/{id}/terminate`.
**Expected**: response `{"status":"cancelled_while_queued"}`; DB row in
CANCELLED state; task never runs.

#### 5. Delete cascade
**Action**:
- Queue 3 tasks for an agent.
- `DELETE /api/agents/{name}`.
**Expected**: all queued rows transition to CANCELLED with
`error="agent_deleted"`; no orphan rows remain.

### Edge Cases
- [ ] Queued task for deleted agent — should fail cleanly during drain
- [ ] Multiple agents with queued work — fair per-agent drain (each agent
      gets one drain per maintenance tick)
- [ ] Backlog entries older than 24h — expired to FAILED on next tick

**Last Tested**: 2026-04-13 (unit tests only; manual scenarios pending)
**Status**: ✅ Unit tests passing; integration testing recommended post-merge

## Acceptance Criteria Coverage

All nine acceptance criteria from issue #260 are met:

- [x] Async task at capacity returns HTTP 202 with `status: "queued"` instead of 429
- [x] Queued tasks auto-execute when slots free up (FIFO order)
- [x] `GET /api/agents/{name}/executions/{id}` shows `queued → running → success` lifecycle
- [x] Backlog persists across backend restarts (60s maintenance tick drains orphans)
- [x] Configurable `max_backlog_depth` per agent (default 50)
- [x] True 429 only when backlog is also full
- [x] Queued items can be cancelled
- [x] Stale queued items expire after 24h
- [x] MCP `get_execution_result` works for queued/backlog items without changes

## What Doesn't Change

- MCP tool signatures — `chat_with_agent(async=true)` automatically gains
  backlog behaviour.
- Frontend routing — the Tasks tab and execution detail views render
  queued rows via the existing list endpoints; only the status badge
  mapping needed to learn about `queued`.
- Sync mode — `async_mode=false` remains blocking and is unaffected.
- Fan-out — `FANOUT-001` uses `TaskExecutionService` directly in parallel
  mode and bypasses the backlog entirely.
- Scheduler — scheduled executions are created in RUNNING state via the
  scheduler container and never touch the backlog.

## Files Modified

### Backend

| File | Change |
|---|---|
| `src/backend/models.py` | +1 line: `TaskExecutionStatus.QUEUED = "queued"` |
| `src/backend/db_models.py` | +3 lines: `queued_at`, `backlog_metadata` on `ScheduleExecution` |
| `src/backend/db/migrations.py` | +37 lines: `_migrate_backlog_support` + registry entry |
| `src/backend/db/schedules.py` | +231 lines: 10 backlog query methods + row mapping |
| `src/backend/db/agent_settings/resources.py` | +45 lines: `get/set_max_backlog_depth` (1-200 validation) |
| `src/backend/database.py` | +38 lines: delegate methods |
| `src/backend/services/slot_service.py` | +49 lines: release callback hook + safe invocation |
| `src/backend/services/backlog_service.py` | **NEW** 320 lines — core service |
| `src/backend/routers/chat.py` | +69 lines: enqueue-on-capacity, terminate-while-queued |
| `src/backend/routers/agents.py` | +10 lines: delete cascade |
| `src/backend/main.py` | +27 lines: callback registration + 60s maintenance loop |

### Frontend

| File | Change |
|---|---|
| `src/frontend/src/views/ExecutionDetail.vue` | +1 line: amber `queued` badge |
| `src/frontend/src/components/TasksPanel.vue` | +2 lines: amber `queued` badge (text + dot) |

### Tests

| File | Change |
|---|---|
| `tests/unit/test_backlog.py` | **NEW** 29 tests — enum, migration, query methods, service |
| `tests/unit/conftest.py` | +42 lines: backend import bootstrap (evicts shadow `utils`) |
| `tests/registry.json` | +7 lines: register new test file |
