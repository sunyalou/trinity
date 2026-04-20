# Feature: Fan-Out Parallel Task Dispatch (FANOUT-001)

## Overview
Dispatches N independent tasks to an agent in parallel (throttled by asyncio semaphore), collects results with an optional overall deadline, and returns aggregated per-task results. Each subtask follows the standard TaskExecutionService path for full dashboard observability.

## Recent Changes
- **Issue #418 (feature/418-inter-agent-timeout)**: `timeout_seconds` is now optional and governs only the outer fan-out-wide deadline. Individual subtasks are always bounded by the target agent's configured `execution_timeout_seconds` (TIMEOUT-001). Previously a hardcoded 600s default capped every subtask regardless of per-agent configuration.

## User Story
As an agent orchestrator, I want to fan out multiple independent tasks to an agent in parallel so that embarrassingly parallel workloads (batch predictions, parallel analysis, ensemble methods) complete faster than sequential execution.

## Entry Points
- **API**: `POST /api/agents/{name}/fan-out` -- authenticated endpoint
- **MCP**: `fan_out` tool registered in MCP server

No frontend UI entry point exists; this is an API/MCP-only feature.

## MCP Layer

### Tool Registration
- `src/mcp-server/src/server.ts:192` -- `server.addTool(chatTools.fanOut)`

### Tool Definition
- `src/mcp-server/src/tools/chat.ts:351-457` -- `fan_out` tool
- Parameters: `agent_name`, `tasks[]`, `timeout_seconds` (optional; no default — when omitted, no outer deadline is applied and each sub-task is bounded by the target agent's configured `execution_timeout_seconds`), `max_concurrency`, `model`, `system_prompt`, `allowed_tools`
- Access control: calls `checkAgentAccess()` (same rules as `chat_with_agent`)
- Delegates to `TrinityClient.fanOut()`

### Client Method
- `src/mcp-server/src/client.ts:610-704` -- `fanOut()` method
- Sets headers: `Authorization`, `X-Via-MCP`, `X-Source-Agent`, `X-MCP-Key-ID`, `X-MCP-Key-Name`
- Builds request body with `tasks`, `agent`, `max_concurrency`, `policy`, `model`, `system_prompt`, `allowed_tools`; `timeout_seconds` is conditionally spread in only when the caller provided it, so the backend sees `None` on omission and falls back to per-agent `execution_timeout_seconds`
- HTTP ceiling = `(options?.timeout_seconds ?? 7200) + 60` seconds — covers the platform max per-agent timeout (7200s) + 60s buffer so the HTTP fetch doesn't abort before the backend finishes (#418)
- Calls `POST /api/agents/{name}/fan-out`

## Backend Layer

### Router
- `src/backend/routers/fan_out.py` -- registered in `main.py:41,450`
- Prefix: `/api/agents`, tag: `fan-out`

### Request Validation (Pydantic)
```python
class FanOutRequest(BaseModel):
    tasks: List[FanOutTask]                    # 1-50 tasks, unique IDs
    agent: str = "self"                        # v1: self-only
    timeout_seconds: Optional[int] = None      # 10-3600 when set; None = per-agent default (#418)
    max_concurrency: int = 3                   # 1-10
    policy: str = "best-effort"                # only value supported
    model: Optional[str]
    system_prompt: Optional[str]
    allowed_tools: Optional[List[str]]
```

- Task IDs: regex `^[a-zA-Z0-9_-]{1,64}$`, must be unique
- Max tasks: 50 (`MAX_TASKS`)
- Max concurrency: 10 (`MAX_CONCURRENCY`)
- Timeout range: 10-3600 seconds (or `None` for per-agent default, #418). The validator short-circuits and returns `None` when the field is omitted.
- Policy: only `"best-effort"` supported in v1
- Cross-agent fan-out (`agent != "self"` and `agent != name`): returns 400

### Endpoint Handler
- `src/backend/routers/fan_out.py:126` -- `fan_out()`
- Auth: `get_current_user` + `get_authorized_agent`
- Origin tracking headers: `X-Source-Agent`, `X-Via-MCP`, `X-MCP-Key-ID`, `X-MCP-Key-Name`

### Business Logic Flow
1. Validate `request.agent` is `"self"` or matches path `name` (v1 restriction)
2. Convert `FanOutTask` list to `FanOutTaskInput` dataclasses
3. Determine `source_agent` from header or path name
4. Call `FanOutService.execute()` with all parameters + origin tracking fields
5. Map `FanOutResult` to `FanOutResponse` Pydantic model

### FanOutService
- `src/backend/services/fan_out_service.py:67` -- `FanOutService` class
- Singleton via `get_fan_out_service()` (module-level `_fan_out_service`)

#### `execute()` method (line 70)
1. Generate `fan_out_id` = `fo_{secrets.token_urlsafe(12)}`
2. Get `TaskExecutionService` singleton
3. Create `asyncio.Semaphore(max_concurrency)` for throttling
4. Define `run_subtask()` coroutine for each task:
   - Acquires semaphore
   - Calls `task_service.execute_task()` with `triggered_by="fan_out"`, `fan_out_id=fan_out_id`, and **`timeout_seconds=None`** so TaskExecutionService resolves the target agent's configured `execution_timeout_seconds` (TIMEOUT-001, #418)
   - Maps result to `FanOutTaskResult` (completed or failed)
   - Catches `CancelledError` (deadline exceeded) and general exceptions
5. Dispatch all coroutines via `asyncio.gather(*coroutines, return_exceptions=True)`. The gather is **conditionally wrapped** in `asyncio.timeout(timeout_seconds)` only when the caller supplied an outer deadline (#418). Without a deadline, the gather runs unwrapped — each subtask is still individually bounded by per-agent `execution_timeout_seconds`.
6. On `TimeoutError`: mark unfinished tasks as failed with `error_code="timeout"` (only reachable when outer deadline was set)
7. Build ordered results matching input task order
8. Return `FanOutResult` with aggregate counts

Log line format: `[FanOut] Starting {fan_out_id}: {N} tasks on '{agent}' (concurrency={max_concurrency}, deadline={deadline_desc})` where `deadline_desc` is either `"{N}s"` or `"per-agent"`.

### Data Models
```python
@dataclass
class FanOutTaskInput:
    id: str
    message: str

@dataclass
class FanOutTaskResult:
    id: str
    status: str           # "completed" | "failed"
    response: Optional[str]
    error: Optional[str]
    error_code: Optional[str]
    execution_id: Optional[str]
    cost: Optional[float]
    context_used: Optional[int]
    duration_ms: Optional[int]

@dataclass
class FanOutResult:
    fan_out_id: str
    status: str           # "completed" | "deadline_exceeded"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]
```

## Data Layer

### Database Migration
- `src/backend/db/migrations.py:895` -- `_migrate_execution_fan_out_id()`
- Migration #30 (`execution_fan_out_id`)
- Adds `fan_out_id TEXT` column to `schedule_executions` table
- Creates index: `idx_executions_fan_out ON schedule_executions(fan_out_id)`

### Model
- `src/backend/db_models.py:170` -- `fan_out_id: Optional[str]` on `ScheduleExecution` dataclass

### Execution Record Creation
- `src/backend/db/schedules.py:451` -- `create_task_execution()` accepts `fan_out_id` parameter
- `src/backend/db/schedules.py:476` -- INSERT includes `fan_out_id` column
- `src/backend/db/schedules.py:128` -- row mapper reads `fan_out_id` from result set

### TaskExecutionService Integration
- `src/backend/services/task_execution_service.py:134` -- `triggered_by="fan_out"` (new trigger type)
- `src/backend/services/task_execution_service.py:146` -- `fan_out_id` parameter passed through to `db.create_task_execution()`
- Each subtask gets its own execution record, capacity slot, and activity tracking via the standard path

## Side Effects
- **Execution Records**: Each subtask creates a `schedule_executions` row with `triggered_by="fan_out"` and shared `fan_out_id`
- **Capacity Slots**: Each subtask acquires/releases a parallel execution slot via `SlotService`
- **Activity Tracking**: Standard activity tracking from `TaskExecutionService` applies per subtask
- **WebSocket**: Standard execution status broadcasts from `TaskExecutionService` apply per subtask
- **No dedicated fan-out WebSocket event**: The fan-out itself does not broadcast; individual subtask events flow through existing channels

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| No tasks provided | 422 | "At least one task is required" |
| Too many tasks (>50) | 422 | "Maximum 50 tasks per fan-out" |
| Duplicate task IDs | 422 | "Duplicate task IDs: {dupes}" |
| Invalid task ID format | 422 | "Task ID must be 1-64 alphanumeric..." |
| Concurrency out of range | 422 | "max_concurrency must be between 1 and 10" |
| Timeout out of range | 422 | "timeout_seconds must be between 10 and 3600" (only validated when field is set; `None`/omitted is accepted) |
| Unsupported policy | 422 | "Only 'best-effort' policy is supported" |
| Cross-agent target | 400 | "Fan-out target must be 'self' or '{name}'" |
| Agent not found | 404 | From `get_authorized_agent` dependency |
| Auth failure | 401 | From `get_current_user` dependency |
| Overall deadline exceeded | 200 | `status: "deadline_exceeded"`, unfinished tasks get `error_code: "timeout"` (only reachable when `timeout_seconds` was explicitly set) |
| Per-subtask timeout (per-agent config) | 200 | Per-task `status: "failed"` with `error_code: "timeout"` from TaskExecutionService; other subtasks continue |
| Individual subtask failure | 200 | Per-task `status: "failed"` with `error` and `error_code` |

## Request/Response Example

### Request
```json
POST /api/agents/my-agent/fan-out
{
  "tasks": [
    {"id": "task-1", "message": "Analyze Q1 revenue"},
    {"id": "task-2", "message": "Analyze Q2 revenue"},
    {"id": "task-3", "message": "Analyze Q3 revenue"}
  ],
  "max_concurrency": 3,
  "timeout_seconds": 300,
  "model": "sonnet"
}
```

### Response
```json
{
  "fan_out_id": "fo_abc123def456",
  "status": "completed",
  "total": 3,
  "completed": 3,
  "failed": 0,
  "results": [
    {
      "id": "task-1",
      "status": "completed",
      "response": "Q1 revenue was...",
      "execution_id": "exec_xyz",
      "cost": 0.05,
      "context_used": 12000,
      "duration_ms": 8500
    },
    ...
  ]
}
```

## Testing

### Prerequisites
- Backend running at `http://localhost:8000`
- At least one running agent

### Test Steps
1. **Action**: Send fan-out request with 3 tasks
   **Expected**: All 3 tasks complete, `status: "completed"`
   **Verify**: `GET /api/agents/{name}/executions` shows 3 records with same `fan_out_id`

2. **Action**: Send fan-out with `max_concurrency: 1`
   **Expected**: Tasks execute sequentially (only 1 at a time)
   **Verify**: Execution timestamps show sequential pattern

3. **Action**: Send fan-out with very short `timeout_seconds: 10` and complex tasks
   **Expected**: `status: "deadline_exceeded"`, unfinished tasks have `error_code: "timeout"`

4. **Action**: Send fan-out with `agent: "other-agent"`
   **Expected**: 400 error "Cross-agent fan-out is not yet supported"

5. **Action**: Send fan-out with duplicate task IDs
   **Expected**: 422 validation error

## Architecture Notes
- Concurrency is managed by `asyncio.Semaphore` -- safe because asyncio is single-threaded (no preemption between awaits)
- `asyncio.gather(return_exceptions=True)` ensures all coroutines complete even if one raises
- `asyncio.timeout()` wraps the entire gather for the overall deadline **only when `timeout_seconds` is set**; otherwise the gather runs unwrapped and each subtask is bounded by per-agent `execution_timeout_seconds` (#418)
- Results dict is safe for concurrent writes in asyncio's cooperative model
- v1 is self-only (agent fans out to itself); cross-agent fan-out is a future extension

## Related Flows
- [task-execution-service.md](task-execution-service.md) -- Each subtask uses the standard execution path
- [parallel-capacity.md](parallel-capacity.md) -- Subtasks consume parallel execution slots
- [parallel-headless-execution.md](parallel-headless-execution.md) -- Similar stateless execution model
- [mcp-orchestration.md](mcp-orchestration.md) -- MCP tool registration
- [AUDIT-001-execution-origin-tracking.md](AUDIT-001-execution-origin-tracking.md) -- Origin tracking headers
