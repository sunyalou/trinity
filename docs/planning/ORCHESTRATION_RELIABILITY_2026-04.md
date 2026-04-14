# Orchestration & Multi-Agent Reliability Plan

**Date:** 2026-04-13
**Status:** Proposed sequencing for execution-time orchestration, event subscriptions, and multi-agent reliability.

**Progress:** Sprint A — **7/7 complete**: #95 (PR #320), #285 (PR #322), #226 (PR #323), #286 (PR #324), #61 (PR #326), #132 (PR #328), #56 (PR #329). **Sprint B next: #305.**

---

## Problem statement

Trinity has accumulated ~20 issues touching execution-time orchestration. They exist because the current design has **parallel code paths for the same logical operation** (sync chat, async task, scheduler, fan-out, event subscriptions) that drift apart, and because **state-corruption bugs at the bottom of the stack** (orphaned processes, lost error context, fixed slot TTLs) make higher-level features unreliable.

BACKLOG-001 (#260) is the marquee item, but it can't land cleanly until:
1. The async execution path is unified with `TaskExecutionService` (#95).
2. The lower-level bugs are fixed so the backlog doesn't inherit them.

Shipping #260 on top of today's foundation would produce a *persistent* backlog of *corrupt* executions.

---

## Sequencing

```
Sprint A (unblock):     #95 ✅, #285 ✅, #226 ✅, #286 ✅, #61 ✅, #132 ✅, #56 ✅  ← COMPLETE
Sprint B (trace):       #305
Sprint C (orchestrate): #260 → #271 → #294 → #264 → #291
Sprint D (push telemetry): #306, #307
Sprint E (scale):       #24, #18
```

`#95` lands alone because every other Tier 0 fix layers on top of the unified executor. The remaining Tier 0 issues are independent and can parallelize once `#95` ships.

### Dependency edges to enforce on the board

- `#260 blocked-by #95` — backlog drain must call unified executor, not re-implement launch logic.
- ~~`#271 blocked-by #285`~~ — #285 shipped; #271 unblocked.
- `#294 blocked-by #95` — validation session reuses the unified execution path.
- `#260 blocks #271, #294, #264` — retry/validation/self-execute all benefit from uniform overflow semantics; landing them first creates divergent queue behavior.
- Inside Sprint C, `#294` and `#264` are independent of each other once `#260` lands — parallelize them instead of serializing the whole tier.

### Merge candidates (single PR surface)

- ~~**#226 + #61**~~: #226 shipped (PR #323). #61 remains — wire backend cleanup into agent's existing terminate endpoint.
- **#305**: tracing. Can now build on #286's preserved error context (shipped in PR #324) — trace ID can be appended to the combined error message.

---

## Tier 0 — Fix state-corruption bugs (Sprint A)

**Goal:** Make execution state authoritative and correct. No new features.

| # | Title | Why it's here |
|---|-------|---------------|
| ~~#95~~ ✅ | ~~Route async task mode through `TaskExecutionService`~~ | **Shipped** on `feature/95-unified-async-executor`. `_execute_task_background()` deleted. Async `/task` now delegates to `execute_task(slot_already_held=True)` via `_run_async_task_with_persistence` thin wrapper. 429-upfront preserved via router-side pre-acquire. Service gained `parent_activity_id`, `extra_activity_details`, `slot_already_held` params. Sync+async share `_persist_chat_session` helper. E3 fix: `subscription_id` now snapshotted on pre-created execution record. |
| ~~#285~~ ✅ | ~~Expired subscription tokens cause hour-long zombie executions~~ | **Shipped** in PR #322. Added stderr scanner in `TaskExecutionService` that detects auth failure patterns (`unauthorized`, `invalid.*token`, `expired.*credential`, etc.) and aborts early. Execution marked failed with `auth_failure` error type. No more hour-long zombies from expired tokens. |
| ~~#61~~ ✅ | ~~Backend cleanup doesn't invoke agent termination~~ | **Shipped** in PR #326. Added `terminate_execution_on_agent()` helper to `TaskExecutionService` that calls agent's `/api/executions/{id}/terminate` endpoint. Wired into timeout handler and cleanup service slot reclaim path. Best-effort with watchdog safety net. 8 unit tests. |
| ~~#226~~ ✅ | ~~Stale-slot cleanup ignores per-agent TTL on the standalone path~~ | **Shipped** in PR #323. `cleanup_stale_slots()` now accepts `agent_timeouts` dict and uses per-agent TTL (timeout + 5min buffer) instead of fixed 20-min default. Cleanup service passes `db.get_all_execution_timeouts()` to slot service. |
| ~~#286~~ ✅ | ~~Cleanup overwrites real error~~ | **Shipped** in PR #324. Added `/api/executions/{id}/last-error` agent endpoint + `ProcessRegistry.get_last_error()` to extract error from log buffer. `_recover_execution()` now fetches original error before marking failed, combines with cleanup reason (`"{original}. Cleanup: {reason}"`), sanitizes via `sanitize_text()`, truncates to 2000 chars. No schema change needed — reuses existing `error` column with richer content. |
| ~~#132~~ ✅ | ~~APScheduler skips triggers when `max_instances=1` reached~~ | **Shipped** in PR #328. True fire-and-forget: `_call_backend_execute_task()` now spawns `asyncio.create_task(_poll_and_finalize())` and returns immediately with `"dispatched"` status. Job function no longer blocks on polling, so APScheduler doesn't skip subsequent triggers. `last_run_at` updated immediately on dispatch (not completion) for missed-schedule detection. Background tasks tracked in `_active_poll_tasks` set for graceful shutdown. 4 new tests in `test_async_dispatch.py`. |
| ~~#56~~ ✅ | ~~Consistent context usage tracking~~ | **Shipped** in PR #329. Fixed `TaskExecutionService` to use `input_tokens` only (not `input+output`). Per Claude Code SDK, `input_tokens` represents the full context window fill level including accumulated tool results. `AgentClient` already had the correct pattern. |

### Architectural shift

**Before:** Two execution code paths (sync + async) with duplicated slot/activity/sanitization logic. Backend cleanup doesn't call the agent's existing terminate endpoint, leaving processes running. Cleanup overwrites diagnostic data. Scheduler skips triggers silently when prior runs overlap.

**After:** Single `TaskExecutionService` funnel. Backend timeout calls agent terminate → existing SIGINT→SIGKILL path runs. Slot TTL comes from per-slot metadata on every cleanup path. Cleanup preserves original error by fetching from agent log buffer and combining with cleanup reason in `error` field (no schema change). Scheduler uses true fire-and-forget: job function returns immediately after dispatch, background task polls and publishes events.

### Verification gates before exiting Tier 0

- ✅ Grep for `_execute_task_background` returns zero hits outside docs (shipped in #95). Shadow-run deemed unnecessary for the async-path refactor: sync-mode already delegated to `execute_task()` pre-#95, and public/internal async paths had been using the same `execute_task(execution_id=...)` pattern in production; the refactor is a code-path unification rather than a semantics change. Parity tests cover the two observable invariants (429-upfront, `parallel_mode` activity flag).
- Integration test: force a 5-second timeout on a real agent task, assert (a) zero orphan `claude` processes inside the container, (b) execution row `error` field contains both original error context and cleanup reason (combined format), (c) slot is released within TTL+buffer.
- ✅ Execution `error` field now contains combined message: original error from agent log buffer + cleanup reason. Sanitized and truncated to 2000 chars (#286 shipped).
- ✅ Scheduler job function returns immediately after dispatch (#132 shipped). Skips prevented by true fire-and-forget: polling moved to background task. 127 scheduler tests pass.

---

## Tier 1 — Tracing (Sprint B)

**Goal:** Get a single trace ID across hops before shipping orchestration features, so failures are diagnosable. Heartbeat and WS rewrite are deferred to Sprint D — they're bigger surfaces than #260 itself, and gating reliability work on a WebSocket rewrite is the wrong tradeoff.

| # | Title | Why it's here |
|---|-------|---------------|
| #305 | OpenTelemetry distributed tracing (RELIABILITY-002) | OTel Collector already in `docker-compose.yml`. ~30 lines + 3 packages. Enables single trace ID across Agent A → B → C. Pairs with #286 so `cleanup_reason` carries the trace ID. |

### Considerations

- **Sampling**: start at 10% for high-volume endpoints. Full sampling only in dev.

---

## Tier 2 — Orchestration primitives (Sprint C)

**Goal:** Ship the user-visible reliability features on top of the now-solid foundation.

| # | Title | Why it's here |
|---|-------|---------------|
| #260 | Persistent task backlog (BACKLOG-001) | Async requests at capacity go to `status=queued` instead of 429. `SlotService.release_slot` publishes via Redis pub/sub (`PUBLISH slot:released:{agent}`) → any subscribing worker calls `BacklogService.drain_next` and claims the next row atomically. **Do not ship an in-process callback** — see multi-worker note below. |
| #271 | Retry mechanism for scheduled executions | Pairs with #285 (need fast-fail first). Retries flow through backlog, not bypass it. Scheduler `date` trigger survives restart. |
| #294 | Business task validation (VALIDATE-001) | Clean-context auditor session after execution. Reuses unified executor (#95). Writes `business_status` separate from technical `status`. |
| #264 | Self-execute during chat (SELF-EXEC-001) | Thin layer: detect source==target, flag `X-Self-Task`, optionally `inject_result` back into chat session. Uses backlog for overflow. |
| #291 | Agent webhook triggers (WEBHOOK-001) | External → agent dispatch. HMAC-signed URL. **Distinct from existing process-engine webhooks** (`routers/triggers.py`) which trigger BPMN process executions. Before building, decide: reuse the process-engine trigger surface (lower surface area) or ship a parallel agent-scoped trigger surface (clearer mental model, but exactly the parallel-paths problem this plan exists to fix). Default recommendation: reuse, with an `agent_task` shortcut process. |

### Architectural shift

**Before:** Overflow = 429 hard reject. Retry = none. Validation = none. Scheduler, webhooks, MCP, UI each have subtly different failure modes.

**After:** A single invariant — *every* trigger type (user chat, schedule, webhook, retry, validation, self-execute, fan-out, event subscription) produces the same shape:

```
(trigger) ─► execution record (pending|queued)
             ─► TaskExecutionService
                ├─ slot acquired (or enqueued)
                ├─ traced
                ├─ PID tracked
                ├─ heartbeat monitored
                └─ terminal with preserved error
```

Retry and validation are **not new infrastructure** — they're just new trigger sources that produce more execution records. That's the architectural payoff.

### Considerations

- **Backlog depth cap**: default 50, hard cap 200. Unbounded queues mask capacity problems.
- **FIFO only for v1**: priority can come later. Don't ship two ordering systems before one is proven.
- **Stale expiry**: 24h. Queued items older than that are unlikely to still be relevant.
- **Retry should enqueue, not dispatch directly**: ensures retries respect capacity and the "one path" invariant.
- **Validation session is an agent call to itself**: no new execution machinery needed — it's just a task with an auditor prompt. Keeps the surface small.
- **Multi-worker drain coordination**: with multiple uvicorn workers, the worker that releases a slot may not hold the in-process queue. The release → drain handoff must go through Redis pub/sub (`PUBLISH slot:released:{agent}`) and any worker that subscribes can claim the next row via `SELECT … FOR UPDATE` / atomic UPDATE. Don't ship an in-process callback — it works in dev and silently stalls in production.

---

## Tier 3 — Push telemetry (Sprint D)

**Goal:** Move the remaining polling loops to push, now that the executor and queue are stable.

| # | Title | Why it's here |
|---|-------|---------------|
| #306 | Redis Streams event bus for WebSocket (RELIABILITY-003) | Replaces in-process `ConnectionManager.broadcast()` (`main.py:125-130`, currently `except: pass`). `XADD`/`XREAD` with reconnect replay. Bigger surface than #260 itself — explicit `lastEventId` work on the frontend. |
| #307 | Agent heartbeat push (RELIABILITY-004) | Flip 30s polling (`monitoring_service.py:654`) → 5s push. Feeds monitoring + (future) circuit breaker. Uses existing Redis. |

### Considerations

- **Redis memory**: stream trim via `MAXLEN ~10000`. Without this, a burst of activity blows up Redis.
- **Backward compat**: WebSocket event shape must not change. Frontend needs `lastEventId` support but old events should still render.

## Tier 4 — Scale (later)

| # | Title |
|---|-------|
| #24 | Horizontal agent scalability (pools + load balancing) |
| #18 | Unified Executions Dashboard (EXEC-022) |

### Why last

- Horizontal scaling only pays off after backlog (Tier 2) is the scheduling authority and heartbeat (Tier 3) tells the router which instances are live. Otherwise scaling multiplies broken parts.
- Unified dashboard is low engineering risk but benefits from Redis Streams (#306) for live updates and consistent execution records (Tier 0) for meaningful aggregation.

---

## Already shipped (do not re-plan)

- **EVT-001** (#169, closed) — `routers/event_subscriptions.py`, SQLite-backed pub/sub, permission-gated, template interpolation.
- **Fan-out** (#230, closed) — `services/fan_out_service.py` + `routers/fan_out.py`, barrier-wait, per-task status.
- **Cleanup watchdog** (#129/#94, closed) — active reconciliation + passive stale recovery in `cleanup_service.py`.
- **Event Bus #22** (closed) — subsumed by EVT-001 SQLite path; upgrade to Redis Streams covered by #306.
- **Agent process termination** — `docker/base-image/agent_server/services/process_registry.py` already implements SIGINT→SIGKILL with wait. #61 wires backend cleanup into this existing endpoint, not new infrastructure.
- **Scheduler async dispatch (`SCHED-ASYNC-001`)** — `src/scheduler/service.py:823` already does fire-and-forget + DB polling. #132 is a tuning task on top of this, not a rewrite.

### Out of scope (intentionally separate)

- **Process Engine** has its own execution surface: `process_engine/engine/handlers/agent_task.py` calls `AgentClient.chat()` directly, bypassing `TaskExecutionService`, slot service, and cleanup service. It is intentionally outside this consolidation. Slot/timeout/PID work in Tier 0 does not apply to process steps. *Known limitation:* a process kicking off many parallel `agent_task` steps will not respect agent capacity. **File a tracking issue** (suggested title: "Process Engine `agent_task` should funnel through TaskExecutionService") so this surfaces in grooming rather than rotting inside this doc.

---

## Architecture snapshots

### Today

```
HTTP sync chat   ─► routers/chat.py
HTTP async task  ─► routers/chat.py ─► _execute_task_background()  ◄── duplicate path
MCP chat_with_agent ─► TaskExecutionService
MCP fan_out      ─► TaskExecutionService
Schedule         ─► APScheduler ─► HTTP /task ─► poll status up-to-1h (blocks!)
Event emit       ─► SQLite row ─► trigger matching subs
Webhook          ─── none ───

Slot ZSET (fixed 20-min TTL) ◄── cleanup now preserves errors (#286 shipped)
Container (orphaned claude PID accumulates on timeout)
WebSocket: in-process broadcast, except: pass, no replay
Tracing: none. Heartbeat: 30s backend poll.
```

### After Tier 0 + 1

```
All entry paths ─► TaskExecutionService (single path)
                      ├─ execute_task()        (sync)
                      └─ execute_task_async()  (background)
                      ├─ traceparent propagated
                      ├─ PID tracked
                      └─ activities, sanitization, timeout kill

agent container ── heartbeat push 5s ──► Redis (TTL 15s)
                ── stderr scanner ──► fast-fail on auth error
                ── OTel trace ──► collector

Slot ZSET (per-agent TTL from metadata)
cleanup_service preserves original error (fetched from agent log buffer) + cleanup reason in combined `error` field
APScheduler fire-and-forget, async status consumer
WebSocket ◄── Redis Streams (XADD/XREAD) with replay
```

### After Tier 2

```
request at capacity ─► try slot.acquire()
                        ├─ success → execute
                        ├─ slot full → backlog.enqueue() → 202 queued
                        └─ backlog full → 429

slot.release_slot() ── callback ──► backlog.drain_next()
                                    ├─ atomic claim (SELECT + UPDATE)
                                    └─ TaskExecutionService.execute_task_async()

New triggers, all funnel into the same executor:
  • Webhook         ─► schedule dispatch (HMAC-signed URL)
  • Self-execute    ─► X-Self-Task, optional inject_result
  • Retry           ─► new execution with retry_of_execution_id
  • Validation      ─► auditor session, writes business_status
  • Event sub       ─► (already funnels)
  • Fan-out         ─► (already funnels)
```

---

## What to do next on the board

1. ~~Add `blocked-by` links: #260 ← #95, #271 ← #285, #294 ← #95.~~ #95 landed; #260 and #294 now have only the remaining Tier 0 dependencies (none direct from #95).
2. ~~Bump #95 to `status-ready` with a "do this first, alone" note.~~ ✅ Done — PR pending merge.
3. Merge-candidate tag on #226 + #61 (single PR for container process lifecycle, both wire backend cleanup into the existing agent terminate endpoint).
4. ~~Merge-candidate tag on #305 + #286~~ — #286 shipped (PR #324). #305 can now build on the preserved error context.
5. Confirm scope cuts for #260: FIFO-only v1, depth 50 default, 24h expiry. Defer priority levels and WebSocket completion pings to v2.
6. Rescope #132 against `src/scheduler/service.py` — fire-and-forget already exists; the open work is the skip-on-overlap policy.
7. Re-estimate #56 — at least 5–7 distinct call sites with three formulas; not "trivial."
8. Decide #291 direction: reuse process-engine triggers (recommended) vs. parallel agent-scoped trigger surface.
