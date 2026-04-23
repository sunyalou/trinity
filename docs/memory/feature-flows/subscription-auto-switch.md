# Feature Flow: Subscription Auto-Switch (SUB-003)

> **Requirement**: `docs/requirements/SUB-003-subscription-auto-switch.md`
> **Issue**: #153
> **Status**: Implemented (2026-03-21)

## Overview

Automatically switches an agent to a different subscription when it encounters 2+ consecutive rate-limit (429) errors from the Claude API. Opt-in via system setting.

## Flow

```
Agent container detects rate limit → returns 429 to backend
    ↓
Backend catches 429 in:
  - TaskExecutionService.execute_task() [schedules, MCP, agent-to-agent]
  - chat_with_agent() [interactive chat]
  - background task handler [async tasks]
    ↓
subscription_auto_switch.handle_rate_limit_error(agent_name)
    ↓
Check: setting enabled? → No → return None
    ↓ Yes
Check: agent has subscription? → No → return None
    ↓ Yes
Record rate-limit event, get consecutive count
    ↓
Count < 2? → return None (wait for more)
    ↓ ≥ 2
Find best alternative subscription (fewest agents, not rate-limited)
    ↓
No alternative? → return None (log warning)
    ↓ Found
Switch: DB update + container restart + log activity + send notification
    ↓
Return switch result to caller → 429 response includes auto_switch info
```

## Files

| Layer | File | Purpose |
|-------|------|---------|
| DB | `src/backend/db/subscriptions.py` | Rate-limit event CRUD, best-alternative selection |
| DB | `src/backend/db/migrations.py` | `subscription_rate_limit_events` table |
| DB | `src/backend/database.py` | Delegation methods |
| Service | `src/backend/services/subscription_auto_switch.py` | Orchestration: detect, switch, log, notify |
| Router | `src/backend/routers/subscriptions.py` | Setting GET/PUT endpoints |
| Service | `src/backend/services/task_execution_service.py` | 429 interception for all execution paths (schedules, MCP, agent-to-agent) |
| Router | `src/backend/routers/chat.py` | 429 interception in chat proxy + background tasks |
| Frontend | `src/frontend/src/views/Settings.vue` | Toggle in Subscriptions section |
| Tests | `tests/test_subscription_auto_switch.py` | Smoke tests |
| Tests | `tests/unit/test_subscription_auto_switch_pingpong.py` | Unit regression for #444 ping-pong prevention; `TestRateLimitAging` (#476) pins 2h-window correctness |
| Tests | `tests/unit/test_iso_cutoff.py` | Format parity between `iso_cutoff(N)` and `utc_now_iso()` (#476) |
| Util | `src/backend/utils/helpers.py::iso_cutoff` | Canonical cutoff helper for ISO-Z TEXT comparisons (#476) |
| Spec | `docs/requirements/SUB-003-subscription-auto-switch.md` | Full requirements |

## Database

### subscription_rate_limit_events

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| agent_name | TEXT | Agent that hit the limit |
| subscription_id | TEXT FK | Subscription that was rate-limited |
| error_message | TEXT | Error details |
| occurred_at | TEXT | ISO timestamp |

### System Setting

| Key | Default | Description |
|-----|---------|-------------|
| `auto_switch_subscriptions` | `"false"` | Enable/disable auto-switch |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/subscriptions/settings/auto-switch` | Get setting state |
| PUT | `/api/subscriptions/settings/auto-switch?enabled=true` | Toggle setting |

## Selection Strategy

1. Exclude current subscription
2. Order by agent_count ascending (load-balance)
3. Skip any subscription with rate-limit events in last 2 hours
4. Return first viable candidate, or None

## 2h Window Correctness (Issue #476)

The "last 2 hours" filter in `is_subscription_rate_limited()` and
`record_rate_limit_event()` now uses `iso_cutoff(2)` passed as a bound
parameter — not SQLite's `datetime('now', '-2 hours')`. The two functions
produce different string formats (`T` separator + `Z` suffix vs. space
separator, no suffix); lexicographic compare on the old form tripped at
position 10 (`T` (0x54) > space (0x20)), making every event with today's
date pass the filter regardless of clock time. Net effect before the fix:
events didn't age out until UTC midnight, and a single 429 early in the day
marked a subscription as rate-limited for the rest of the UTC day, draining
viable alternatives within minutes of the first real outage.

Same correction applied to the 24h cleanup cutoff and the parallel
`db/dashboard_history.py` / `db/schedules.py` stats queries that shared the
pattern.

## Cleanup Wiring

`cleanup_old_rate_limit_events()` deletes events with `occurred_at <
iso_cutoff(24)`. It is invoked hourly from `CleanupService._run_cleanup_inner`
(phase 6, every 12th cycle at the 5-min loop interval). Prior to #476 it had
zero production callers — the mis-comparison made the table look empty
anyway, so the omission was silent.

## Edge Cases

- **All subscriptions exhausted**: No switch, error surfaces as normal 429. `_perform_auto_switch` does **not** clear rate-limit events for the old subscription — those events are the signal that keeps `is_subscription_rate_limited()` truthful, so the just-drained sub is not offered as a candidate on the next cycle (issue #444).
- **API key agents**: Auto-switch only applies to subscription-based agents
- **Flip-flopping**: 2-consecutive-error requirement prevents immediate re-switch
- **Concurrent switches**: SQLite serialization prevents races
- **Cleanup**: Records older than 24h are pruned hourly by `CleanupService` (phase 6, #476); the 2h "is rate-limited" window drives candidate filtering independently of cleanup
