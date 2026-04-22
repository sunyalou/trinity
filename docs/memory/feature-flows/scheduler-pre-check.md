# Feature: Conditional Schedule Pre-Check (SCHED-COND-001 / Issue #454)

## Overview
Optional agent-owned hook that lets a scheduled cron tick be skipped deterministically without waking Claude. The scheduler calls `POST /api/pre-check` on the target agent before firing a cron-triggered chat; `fire=False` records a skipped execution with zero Claude token cost. Eliminates per-tick token burn on poll-driven agents (PR reviewers, inbox monitors, alert routers).

## User Story
As the author of a poll-driven agent template, I want my agent's own code to decide whether each scheduled tick actually needs to run, so that cheap deterministic checks (scan GitHub, list unread mail, compare cost threshold) replace a full Claude turn on every empty poll.

## Entry Points
- **Agent container endpoint** (new): `POST http://agent-<name>:8000/api/pre-check` — internal network only; called by the scheduler service.
- **Template contract**: drop `~/.trinity/pre-check.py` with a top-level `def check()` returning `{"fire": bool, "message": str?, "reason": str?}`.
- **No operator-facing API change**: schedule CRUD endpoints and the Schedules UI are unchanged. Operators create normal cron schedules; agents own the gate.

## Frontend Layer
No UI change. Skipped executions appear in the existing schedule executions list alongside `success`/`failed` rows — visible immediately because the frontend already renders the `status` field as a badge.

## Agent Server Layer
**Router**: `docker/base-image/agent_server/routers/pre_check.py`

- Dynamically loads `/home/developer/.trinity/pre-check.py` via `importlib.util.spec_from_file_location`. No caching beyond Python's default `sys.modules` behavior.
- Returns 404 if the file is absent (fail-open signal to scheduler).
- Accepts both sync and async `check()` functions (`inspect.iscoroutinefunction` branch).
- Normalises the return value: clamps `reason` to 2000 chars; requires `fire` key or raises 500. If `check()` returns a non-dict (e.g. `None`, `True`), the normaliser raises `ValueError` which surfaces as a 500 — scheduler treats that as fail-open.
- **Oversized `message` override**: if `message` exceeds 32 KB UTF-8, the router drops it from the response and sets a sibling `message_truncated: "override dropped: N bytes exceeds 32000 cap"` key. The scheduler sees no `message`, falls back to `schedule.message`, and operators can inspect the truncated-reason in agent-server logs (logged at `ERROR`). Templates must produce compact prompts or rely on `schedule.message`.
- Any exception in `check()` → 500 with the exception text. Scheduler treats 500 as fail-open.

**Registration**: `docker/base-image/agent_server/routers/__init__.py` and `main.py` mount `pre_check_router` alongside existing routers (chat, files, git, skills, dashboard).

## Scheduler Layer
**Client**: `src/scheduler/agent_client.py`

New `AgentClient.pre_check(timeout=60.0) -> Optional[dict]`:
- 404 / 5xx / timeout / malformed JSON / missing `fire` field → returns `None`.
- Valid 200 → returns decision dict.
- `None` signals "no decision, fire as usual" (fail-open).

**Service**: `src/scheduler/service.py`

New `_run_pre_check(agent_name)` wraps the client call with a broad `except Exception` — any unexpected error also returns `None`.

Intercept point: `_execute_schedule_with_lock` calls `_run_pre_check` only when `triggered_by == "schedule"` (cron). Manual triggers bypass entirely.

```python
effective_message = schedule.message
if triggered_by == "schedule":
    decision = await self._run_pre_check(schedule.agent_name)
    if decision is not None:
        if not decision.get("fire", True):
            skipped = self.db.create_skipped_execution(...)
            # publish event, update run times, return — do not create execution
            return
        override = decision.get("message")
        if override and isinstance(override, str):
            effective_message = override
```

`effective_message` is then threaded through `create_execution()` and `_call_backend_execute_task()`.

## Data Layer
**Zero schema change.** Reuses existing:
- `ExecutionStatus.SKIPPED` (already defined in `src/scheduler/models.py` for Issue #46's max-instances drop handling).
- `SchedulerDatabase.create_skipped_execution(...)` (already written to record APScheduler-dropped runs).

Skip rows carry `status='skipped'`, `error=f"pre-check: {reason}"`, `duration_ms=0`, `started_at == completed_at`.

## WebSocket Layer
New event type: `schedule_execution_skipped`

```json
{
  "type": "schedule_execution_skipped",
  "agent": "pr-reviewer",
  "schedule_id": "LidXcOwtDsDuFTFGvkUqCw",
  "execution_id": "I2DWodfpYpuJbs1TTZ42Ig",
  "schedule_name": "PR review poll",
  "reason": "no new PRs"
}
```

## Side Effects
- Skipped execution row written to `schedule_executions` table (visible in UI immediately)
- `schedule.last_run_at` and `next_run_at` updated (so missed-schedule detection still works)
- WebSocket event broadcast to any subscribed UIs
- No `/api/internal/execute-task` call, no backend task creation, no Claude invocation, no backlog slot acquisition

## Error Handling
| Condition | Scheduler behavior |
|---|---|
| Agent container unreachable | Fires as usual (fail-open); logs warning |
| `/api/pre-check` returns 404 | Fires as usual (backward compat for templates without a hook) |
| `/api/pre-check` returns 5xx | Fires as usual; logs warning |
| Response times out | Fires as usual; logs warning |
| Response missing `fire` field | Fires as usual; logs warning |
| `fire=false` | Records skipped execution, no chat dispatch |
| `fire=true` with `message` | Fires chat with override message |
| `fire=true` without `message` | Fires chat with `schedule.message` (existing behavior) |
| Manual trigger (`triggered_by != 'schedule'`) | Skips pre-check entirely — explicit operator intent always fires |

## Security
- Pre-check runs inside the agent's container as `developer`, same sandbox as chat-mode tool calls. No new privilege over what chat-mode tool calls can already do.
- **Template review expectation**: `~/.trinity/pre-check.py` is executed with full Python interpreter access — it can `import subprocess`, open sockets, read files. This is intentional (operators already trust the template's `CLAUDE.md`, skills, and tool invocations), but `.trinity/pre-check.py` should be reviewed with the same scrutiny as any other executable file the template ships.
- Stdout/message cap at 32 KB UTF-8 — oversized payloads are dropped from the response and the drop is surfaced via a `message_truncated` field + `ERROR` log, not silently swallowed.
- Fail-open policy means a malicious/broken pre-check cannot suppress scheduled invocations (worst case: wastes tokens — today's baseline).
- Invariant #5 ("Agent Server Mirrors Backend") preserved: agent exposes an HTTP contract, scheduler proxies to it.
- Invariant #1 ("Three-layer backend: router → service → db") preserved on the scheduler side — CLI wrapper → service → database.

## Testing
**Scheduler-side** (`tests/scheduler_tests/test_pre_check.py`, 12 tests):
- `AgentClient.pre_check` — returns decision on 200, None on 404, None on 5xx, None on unreachable, None on malformed JSON, None on missing `fire` field, normal pass on fire-false, normal pass on fire-true-with-message.
- `SchedulerService._execute_schedule_with_lock` — skip records execution with correct reason, fire-true with message uses override, fail-open routes through backend, fire-true without message uses schedule.message, manual trigger bypasses pre-check.

**Agent-server router** (`tests/unit/test_pre_check_router.py`, 15 tests):
- `_normalise_result` — skip/fire/override cases; oversized message drops with `message_truncated` marker; reason clamped to 2000 chars; non-dict return raises `ValueError`; missing `fire` field raises.
- Router HTTP — 404 when `check()` absent, 200 on skip/fire, 500 when `check()` raises, 500 when `check()` returns non-dict, oversized-message fall-back path, async `check()` awaited correctly.

Full scheduler + unit suite: 176/176 passing.

**Live end-to-end** (verified 2026-04-22 in local Trinity):
- Empty scan on `dolho/pr-reviewer-agent` → `fire:false` → skip row in DB, `$0` cost, zero backend activity.
- Open PR on that repo → next tick `fire:true`, override message with PR list → Claude runs `/review`, posts comment.
- Subsequent tick with existing bot comment → `fire:false` again (stateless dedup via GitHub-hosted comment thread).

## Related Flows
- `feature-flows/agent-event-subscriptions.md` — EVT-001, the event-driven analogue (other trigger source, same "non-cron agent invocation" theme).
- `feature-flows/scheduler-service.md` — base scheduler behavior this extends.
- `docs/planning/PR_REVIEWER_AGENT.md` — the motivating use case that drove this feature.

## Migration / Rollout
- Zero migration required (no schema change).
- Existing schedules and agent templates behave identically after deploy (endpoint absent → fall back to today's fire semantics).
- Templates opt in by shipping `~/.trinity/pre-check.py`. No Trinity-side flag.
