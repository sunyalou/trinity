# Feature: Conditional Schedule Pre-Check (SCHED-COND-001 / Issue #454)

## Overview
Optional template-supplied hook that lets a scheduled cron tick be skipped deterministically without waking Claude. Before firing a cron-triggered chat, the scheduler calls a **backend** internal endpoint, which `docker exec`s `~/.trinity/pre-check.py` inside the target agent container. Non-empty stdout becomes the chat prompt; empty stdout + exit 0 records a skipped execution. Eliminates per-tick token burn on poll-driven agents (PR reviewers, inbox monitors, alert routers).

## User Story
As the author of a poll-driven agent template, I want a cheap deterministic gate to run before Claude wakes so empty polls (scan→no work) don't burn tokens. As a Trinity operator, I don't want to configure the gate per schedule — the agent template owns it, I just schedule the cadence.

## Entry Points
- **Template contract**: ship `~/.trinity/pre-check.py` as a standalone executable script. Prints chat prompt to stdout when work is found; exits 0 with empty stdout to skip; exits non-zero on error (fail-open).
- **Backend endpoint** (internal, `X-Internal-Secret` auth): `POST /api/internal/agents/{name}/pre-check` — runs the script and returns stdout + exit code. Called only by `trinity-scheduler`.
- **No operator-facing API change**: schedule CRUD endpoints and the Schedules UI are unchanged. Operators create normal cron schedules; agents own the gate.

## Frontend Layer
No UI change. Skipped executions appear in the existing schedule executions list alongside `success`/`failed` rows — the frontend already renders the `status` field as a badge.

## Backend Layer
**Router**: `src/backend/routers/internal.py` — `POST /api/internal/agents/{name}/pre-check`

Uses `services/docker_service.execute_command_in_container` (the same primitive as `services/git_service.py` persistent-state allowlist, `services/ssh_service.py` key provisioning, `services/agent_service/terminal.py`, `routers/system_agent.py`, `adapters/message_router.py` Slack ingest, etc.).

Two exec steps:
1. `test -f /home/developer/.trinity/pre-check.py` (5s timeout). If the file doesn't exist, return `{"hook_present": False}` immediately — scheduler treats as "no decision, fire as usual."
2. `python3 /home/developer/.trinity/pre-check.py` (60s timeout). Returns the output (capped at 32 KB) and exit code.

Returns:
```json
{"hook_present": true, "exit_code": 0, "stdout": "Review PR #1\n", "stderr": ""}
```

## Scheduler Layer
**Service**: `src/scheduler/service.py` — `_run_pre_check(agent_name)`

Calls the backend's internal endpoint (not the agent directly — topology stays "scheduler → backend → agent"). Translates the backend response into a scheduler decision:

| Backend response | Scheduler decision |
|---|---|
| `hook_present: false` | `None` → fire as usual (backward compat for templates without a hook) |
| `exit_code != 0` | `None` → fail-open + log stderr (broken hook must not suppress work) |
| `exit_code == 0`, empty stdout | `{"fire": False, "reason": "pre-check returned empty stdout"}` → record skipped execution |
| `exit_code == 0`, non-empty stdout | `{"fire": True, "message": stdout.strip()}` → fire with stdout as override |
| HTTP error / malformed JSON / timeout | `None` → fail-open + log |

Intercept point in `_execute_schedule_with_lock`: called only when `triggered_by == "schedule"` (cron). Manual triggers bypass entirely.

```python
effective_message = schedule.message
if triggered_by == "schedule":
    decision = await self._run_pre_check(schedule.agent_name)
    if decision is not None:
        if not decision.get("fire", True):
            self.db.create_skipped_execution(...)
            self._publish_event({"type": "schedule_execution_skipped", ...})
            return
        override = decision.get("message")
        if override:
            effective_message = override
# ... fires with effective_message
```

## Data Layer
**Zero schema change.** Reuses existing:
- `ExecutionStatus.SKIPPED` (already defined for Issue #46 — APScheduler max-instances drops).
- `SchedulerDatabase.create_skipped_execution(...)`.

Skip rows carry `status='skipped'`, `error="pre-check: <reason>"`, `duration_ms=0`, `started_at == completed_at`.

## WebSocket Layer
New event type: `schedule_execution_skipped`.

```json
{
  "type": "schedule_execution_skipped",
  "agent": "pr-reviewer",
  "schedule_id": "LidXcOwtDsDuFTFGvkUqCw",
  "execution_id": "I2DWodfpYpuJbs1TTZ42Ig",
  "schedule_name": "PR review poll",
  "reason": "pre-check: pre-check returned empty stdout"
}
```

## Side Effects
- Skipped execution row written to `schedule_executions`
- `schedule.last_run_at` and `next_run_at` updated (so missed-schedule detection still works)
- WebSocket event broadcast
- No `/api/internal/execute-task` call, no backend task creation, no Claude invocation, no backlog slot acquisition

## Error Handling
| Condition | Scheduler behavior |
|---|---|
| Agent container doesn't exist | Backend returns 404 → fire as usual (schedule likely stale, let execute-task path handle the 404 surfacing) |
| `~/.trinity/pre-check.py` absent | `hook_present: false` → fire as usual (backward compat) |
| `python3 .../pre-check.py` exits non-zero | Fail-open — log stderr, fire with `schedule.message` |
| Exec timeout (>60s) | Backend returns non-zero exit → fail-open |
| Backend unreachable (connection error) | Fail-open — fire as usual, log warning |
| Backend 5xx / malformed JSON | Fail-open |
| Exit 0, empty stdout | Record skipped execution, no chat dispatch |
| Exit 0, non-empty stdout | Fire with stdout as chat message (overrides `schedule.message`) |
| Manual trigger (`triggered_by != 'schedule'`) | Skip pre-check entirely — explicit operator intent always fires |

## Security
- Pre-check runs inside the agent's container as `developer`, same sandbox as chat-mode tool calls. No new privilege over what chat-mode tool calls can already do.
- **Template review expectation**: `.trinity/pre-check.py` is executed with full Python interpreter access — it can `import subprocess`, open sockets, read files. This is intentional (operators already trust the template's `CLAUDE.md`, skills, and tool invocations), but `.trinity/pre-check.py` should be reviewed with the same scrutiny as any other executable file the template ships.
- Backend endpoint is gated by the existing `X-Internal-Secret` header (C-003). Only `trinity-scheduler` and other internal services can invoke it.
- Stdout cap at 32 KB on the backend side — oversized output is truncated, still valid as a chat prompt (or truncated to "looks non-empty" which is fine for the fire-with-override path).
- Fail-open policy means a malicious/broken pre-check cannot suppress scheduled invocations (worst case: wastes tokens — today's baseline).

## Testing
**Scheduler-side** (`tests/scheduler_tests/test_pre_check.py`, 13 tests):
- `_run_pre_check` translation — `hook_present: False` → None; non-zero exit → None; empty stdout → skip; non-empty stdout → fire with message; 404 / 5xx / connection error / malformed JSON all → None (fail-open).
- `_execute_schedule_with_lock` branch — skip records execution with correct reason; fire-true with message uses override; fail-open routes through backend; fire-true without message uses `schedule.message`; manual trigger bypasses pre-check.

Full scheduler suite: 162/162 passing (was 161 before; +1 for the new `malformed JSON` case).

No test file on the agent-server side — there's no agent-server router anymore.

**Live end-to-end** (verified 2026-04-22 in local Trinity on the HTTP-endpoint version before pivoting to docker-exec):
- Empty scan → skip row in DB, `$0` cost, zero backend chat activity.
- Open PR → next tick fires with override message, Claude runs `/review`, posts comment.
- Subsequent tick with existing bot comment → `fire:false` again (stateless dedup via GitHub).

## Related Flows
- `feature-flows/agent-event-subscriptions.md` — EVT-001, the event-driven analogue (other trigger source, same "non-cron agent invocation" theme).
- `feature-flows/scheduler-service.md` — base scheduler behavior this extends.
- `docs/planning/PR_REVIEWER_AGENT.md` — the motivating use case that drove this feature.

## Architectural notes
- Preserves **Invariant #11 (Docker as source of truth)**: the pre-check primitive is `docker exec`, matching `git_service.py`'s persistent-state allowlist, `ssh_service.py`, the agent terminal, etc.
- Preserves the **"scheduler → backend → agent"** topology: scheduler never opens a direct HTTP edge to agent-server containers.
- Reuses **`execute_command_in_container`**, an established async helper in `services/docker_service.py`.
- No new schema, no new long-lived process, no new primitive — just a new internal endpoint and a scheduler branch.

## Migration / Rollout
- Zero migration required (no schema change).
- Existing schedules and agent templates behave identically after deploy (script absent → fall back to today's fire semantics).
- Templates opt in by shipping `.trinity/pre-check.py` (executable, `+x`). No Trinity-side flag.
