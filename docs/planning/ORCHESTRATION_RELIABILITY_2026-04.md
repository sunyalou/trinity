# Orchestration & Multi-Agent Reliability Plan

**Date:** 2026-04-13 (revised 2026-04-20)
**Status:** Proposed sequencing for execution-time orchestration, event subscriptions, and multi-agent reliability.

**Progress:** Sprint A — **7/7 complete**. Sprint B — **1/1 complete**. Sprint C — **3/5 complete**: #260 (PR #316), #271 (PR #332), #264 (PR #334). **#294 and #291 paused pending #306.** **Next: #306 (push event bus).**

**2026-04-20 revision:** After reviewing the accumulated orchestration surface (three queue abstractions, nine cleanup paths, twelve status-column writers, seven dispatch sites), the next priority shifted from finishing Sprint C to **push-based completion (#306) + consolidation** — see *Tier 2.5 — Simplification* below. The cleanup pyramid is load-bearing, so simplification is **additive-first**: new paths ship alongside old ones and the watchdog is retired only after push has soaked.

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
Sprint B (trace):       #305 ✅  ← COMPLETE
Sprint C (orchestrate): #260 ✅ → #271 ✅ → #264 ✅ → [#294 PAUSED] → [#291 PAUSED]
Sprint D (simplify):    #306 → #428 (CAPACITY-CONSOLIDATE) → #429 (CLEANUP-COLLAPSE) → #430 (PROCESS-ENGINE-DECISION)
                        (and #408 dissolves once #306 lands)
Sprint E (telemetry):   #307
Sprint F (scale):       #24, #18
```

`#95` lands alone because every other Tier 0 fix layers on top of the unified executor. The remaining Tier 0 issues are independent and can parallelize once `#95` ships.

### Dependency edges to enforce on the board

- ~~`#260 blocked-by #95`~~ — #95 and #260 shipped.
- ~~`#271 blocked-by #285`~~ — #285 and #271 shipped.
- `#294 blocked-by #95` — validation session reuses the unified execution path. #95 shipped; #294 unblocked.
- ~~`#260 blocks #271, #294, #264`~~ — #260 shipped. #294 and #264 now unblocked.
- ~~`#294` and `#264` are independent — parallelize them.~~ #264 shipped.

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
| ~~#305~~ ✅ | ~~OpenTelemetry distributed tracing (RELIABILITY-002)~~ | **Shipped** in PR #330. Auto-instrumentation for FastAPI/httpx/Redis. Trace ID in logs. 10% default sampling. Enabled via `OTEL_ENABLED=1`. |

### Considerations

- **Sampling**: start at 10% for high-volume endpoints. Full sampling only in dev.

---

## Tier 2 — Orchestration primitives (Sprint C)

**Goal:** Ship the user-visible reliability features on top of the now-solid foundation.

| # | Title | Why it's here |
|---|-------|---------------|
| ~~#260~~ ✅ | ~~Persistent task backlog (BACKLOG-001)~~ | **Shipped** in PR #316. SQLite-backed FIFO backlog with `status=queued`. Drain via `BacklogService.try_drain_one()` called on slot release. 24h stale expiry. Depth cap configurable per-agent. |
| ~~#271~~ ✅ | ~~Retry mechanism for scheduled executions~~ | **Shipped** in PR #332. Configurable `max_retries` (0-5, default 1) and `retry_delay_seconds` (30-600, default 60). Rate-limited (429) failures use 2x delay. Retries persist to DB and survive scheduler restart via `_recover_pending_retries()`. New status: `pending_retry`. |
| #294 ⏸️ | Business task validation (VALIDATE-001) **— PAUSED 2026-04-20** | Clean-context auditor session after execution. Re-examine after #306: a second full Claude session per task is a 2x cost feature that may be subsumable by cheaper primitives (output schemas, post-hoc validators) running in-process. |
| ~~#264~~ ✅ | ~~Self-execute during chat (SELF-EXEC-001)~~ | **Shipped** in PR #334. Detects source==target, sets `X-Self-Task` header, optionally injects result back into chat session via `inject_result` parameter. Uses backlog for overflow when at capacity. |
| #291 ⏸️ | Agent webhook triggers (WEBHOOK-001) **— PAUSED 2026-04-20** | External → agent dispatch. The "reuse process-engine triggers vs. parallel surface" decision is easier after #430 (PROCESS-ENGINE-DECISION) (Tier 2.5) resolves whether the engine stays at all. Re-open after that. |

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

- ✅ **Backlog depth cap**: Shipped with configurable per-agent depth, default 50 (#260).
- ✅ **FIFO only for v1**: Shipped. Priority deferred to v2 (#260).
- ✅ **Stale expiry**: 24h expiry shipped. Maintenance task cleans expired entries (#260).
- ✅ **Retry should enqueue, not dispatch directly**: Shipped — retries create new execution records and flow through the unified executor (#271).
- **Validation session is an agent call to itself**: no new execution machinery needed — it's just a task with an auditor prompt. Keeps the surface small.
- **Multi-worker drain coordination**: `BacklogService.try_drain_one()` uses atomic SQLite UPDATE with `queued_at` ordering. Single-writer model with cleanup service as fallback drainer.

---

## Tier 2.5 — Simplification (Sprint D) — **NEXT**

**Goal:** Collapse the three-queues / nine-cleanup-paths / twelve-status-writers pyramid that has accumulated across Sprints A–C. The pyramid exists because dispatch is HTTP-blocking and agent state is reconciled from three sources (Redis + DB + agent). Fix those two roots and most of the pyramid falls away.

**Premise from the 2026-04-20 review:** Each new Sprint C primitive welds itself into the pyramid; adding #294 / #291 on top first would make consolidation strictly harder. Reorder: simplify before extending.

### Sequencing within Sprint D

```
#306 (push bus) ─► soak ≥2 weeks ─► #428 (CAPACITY-CONSOLIDATE) ─► #429 (CLEANUP-COLLAPSE)
                                 └► #430 (PROCESS-ENGINE-DECISION) (parallel)
```

| # | Title | Why it's here |
|---|-------|---------------|
| #306 | **Redis Streams event bus (RELIABILITY-003)** — keystone | Enables push-based completion from agents. Retires the 1h blocking HTTP call in `TaskExecutionService` (dissolves #408). Replaces in-process `ConnectionManager.broadcast()` (`main.py:125-130`, currently `except: pass`) with `XADD`/`XREAD` + reconnect replay. Explicit `lastEventId` work on the frontend. |
| **NEW** | **#428 (CAPACITY-CONSOLIDATE)** | Merge `ExecutionQueue` + `SlotService` + `BacklogService` into one `CapacityManager` with `(max_concurrent, overflow_policy)` config. `/chat` = `(1, queue_in_memory)`. `/task` = `(N, queue_persistent)`. Depends on #306 so the drain/TTL logic has the event consumer to lean on. |
| **NEW** | **#429 (CLEANUP-COLLAPSE)** | Once agent is authoritative for "is this running?" (via push), retire Phase 1/1b/1c/3 reconciliation. Slot TTL disappears — capacity is recomputed from DB, not TTL'd. Target: 9 paths → 1 periodic `DB ⟷ agent./api/running` sync. **Do not ship until #306 has been in prod ≥2 weeks with zero observed orphans.** |
| **NEW** | **#430 (PROCESS-ENGINE-DECISION)** | Today `process_engine/engine/handlers/agent_task.py` bypasses `TaskExecutionService` entirely (architecture.md marks engine as "dormant, out of scope"). Ship one of: (a) fold `agent_task` through TES, or (b) delete the engine. Sitting in the middle means every orchestration invariant has a silent exception. |

### Architectural shift

**Before (today):** Three queue primitives, nine cleanup paths, twelve status writers, seven dispatch sites, HTTP connection held up to 3610s. Each new trigger type (retry, validation, webhook, event sub, self-exec) adds its own reconciliation wrinkle. FAILED→SUCCESS races patched by Phase 3 re-verify.

**After Sprint D:** One `CapacityManager`. Dispatch is a <5s HTTP 202; agent pushes completion via Redis Stream. Backend consumer writes the result once. One reconciliation loop (agent is source of truth). No TTL math. New trigger types add zero new cleanup paths.

### Additive-first migration (regression mitigation)

The watchdog pyramid is load-bearing *right now*. The migration must not trade known bugs for unknown ones:

1. **#306 ships alongside** the existing HTTP path — both active, push is opt-in per agent initially.
2. **#428 (CAPACITY-CONSOLIDATE)** lands behind a feature flag per agent, or class-by-class, with old Queue/Slot/Backlog classes kept until all callers have moved.
3. **#429 (CLEANUP-COLLAPSE) is the riskiest and must not ship early.** Gate it on "#306 in prod ≥2 weeks, zero orphan observations."
4. Every PR must leave the system in a shippable state — no multi-PR in-between states where both old and new paths are partially wired.

Worst case: new paths break and we fall back to existing paths. Old code gets deleted after proof, not before.

### Verification gates before exiting Tier 2.5

- Push completion success rate ≥99.9% over 2 weeks (tracked via stream consumer metrics).
- Zero orphan recoveries from Phase 0 watchdog during soak period.
- Grep for direct `SlotService` / `ExecutionQueue` / `BacklogService` instantiation returns zero hits outside `CapacityManager` and its tests.
- Single writer per `schedule_executions.status` transition, verifiable by audit.
- #408 closeable as a dissolved symptom (no code change on #408 itself).

---

## Tier 3 — Remaining push telemetry (Sprint E)

**Goal:** Finish the polling-to-push migration that #306 started.

| # | Title | Why it's here |
|---|-------|---------------|
| #307 | Agent heartbeat push (RELIABILITY-004) | Flip 30s polling (`monitoring_service.py:654`) → 5s push. Feeds monitoring + (future) circuit breaker. Uses the Redis stream established in #306. |

### Considerations

- **Redis memory**: stream trim via `MAXLEN ~10000`. Without this, a burst of activity blows up Redis.
- **Backward compat**: WebSocket event shape must not change. Frontend needs `lastEventId` support but old events should still render.

## Tier 4 — Scale (Sprint F, later)

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

### After Tier 0 + 1 (CURRENT — April 2026)

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

### Aspirational (after Tier 2.5)

```
All entry paths ─► TaskExecutionService (true single funnel, process engine folded in or gone)
                      │
                      ▼
              ┌─────────────────────────────────┐
              │       CapacityManager           │
              │  (subsumes Queue + Slot +       │
              │    Backlog; one class, one      │
              │    TTL reasoner, one counter)   │
              └──────────────┬──────────────────┘
                             │  HTTP POST /api/task → 202 (short)
                             ▼
                    AGENT CONTAINER (authoritative)
                    runs task, owns lifecycle
                             │
                             │  XADD agent-events-stream
                             ▼
              ┌─────────────────────────────────┐
              │  Event Consumer (backend)       │
              │  XREAD lastId → persist result  │
              │  → release capacity → drain     │
              │  → WebSocket fan-out            │
              └─────────────────────────────────┘

Recovery:  ONE periodic sync (DB ⟷ agent./api/running; agent wins).
Writers to schedule_executions.status:  ~4 (create, start, finish, external-cancel).
No TTL math. No multi-phase cleanup pyramid. FAILED→SUCCESS race impossible
(single-writer event consumer owns the terminal transition).
```

### After Tier 2 (partial — #260, #271, #264 shipped)

```
request at capacity ─► try slot.acquire()
                        ├─ success → execute
                        ├─ slot full → backlog.enqueue() → 202 queued  ✅ #260
                        └─ backlog full → 429

slot.release_slot() ── try_drain_one() ──► BacklogService
                                           ├─ atomic claim (UPDATE with queued_at order)
                                           └─ TaskExecutionService.execute_task_async()

Scheduler failure ─► _maybe_schedule_retry()  ✅ #271
                     ├─ check max_retries, attempt_number
                     ├─ calculate delay (2x for 429)
                     ├─ persist retry_scheduled_at to DB
                     └─ APScheduler DateTrigger → _execute_retry()

New triggers, all funnel into the same executor:
  • Webhook         ─► schedule dispatch (HMAC-signed URL)        [#291 pending]
  • Self-execute    ─► X-Self-Task, optional inject_result        ✅ #264
  • Retry           ─► new execution with retry_of_execution_id   ✅ #271
  • Validation      ─► auditor session, writes business_status    [#294 pending]
  • Event sub       ─► (already funnels)
  • Fan-out         ─► (already funnels)
```

---

## What to do next on the board

1. ~~Add `blocked-by` links: #260 ← #95, #271 ← #285, #294 ← #95.~~ All shipped.
2. ~~Bump #95 to `status-ready` with a "do this first, alone" note.~~ ✅ Shipped.
3. ~~Merge-candidate tag on #226 + #61~~ — Both shipped (PRs #323, #326).
4. ~~Merge-candidate tag on #305 + #286~~ — Both shipped (PRs #324, #330).
5. ~~Confirm scope cuts for #260: FIFO-only v1, depth 50 default, 24h expiry.~~ ✅ Shipped with these cuts in PR #316.
6. ~~Rescope #132 against `src/scheduler/service.py`~~ — ✅ Shipped in PR #328.
7. ~~Re-estimate #56~~ — ✅ Shipped in PR #329.
8. ~~Decide #291 direction~~ — **Paused 2026-04-20 pending #430 (PROCESS-ENGINE-DECISION).**
9. ~~**Next:** Pick up #294 (validation).~~ — **Paused 2026-04-20 pending #306.**
10. **Next (2026-04-20):** Pick up **#306 (Redis Streams event bus)** — keystone for Tier 2.5 simplification.
11. **Follow-up:** Create and rank the three new issues from Tier 2.5: #428 (CAPACITY-CONSOLIDATE), #429 (CLEANUP-COLLAPSE), #430 (PROCESS-ENGINE-DECISION).
12. **After #306 lands:** 2-week soak period; instrument push success rate and orphan count; *then* schedule #428 (CAPACITY-CONSOLIDATE).
13. **Re-evaluate #408** once #306 is live — expected outcome: close as dissolved (no direct code change needed).
