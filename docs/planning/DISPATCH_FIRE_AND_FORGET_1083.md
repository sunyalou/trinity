# Fire-and-Forget Dispatch — Design (#1083)

> **Status:** Proposed — review before implementation.
> **Epic:** #1045 (Agent Infrastructure) · umbrella #1081 (pull migration) · "bankable win #2".
> **Theme:** reliability · **Priority:** P1 · **Complexity:** medium.
> **Author:** (claimed via /sprint) · **Date:** 2026-06-10.

## 1. Problem

`task_execution_service.execute_task` dispatches a turn with a **blocking**
HTTP call:

```py
# task_execution_service.py:746
response = await agent_post_with_retry(agent_name, "/api/task", payload,
                                       timeout=timeout_seconds + 10)   # up to ~3600s
```

The agent's `/api/task` (`agent_server/routers/chat.py:107`) is **synchronous** —
it runs the full Claude turn and returns the result in the HTTP response. So for
the entire turn a single hung/wedged turn (the "Cornelius" class — a wedged MCP
tool call) pins **three** backend resources:

1. a backend **coroutine** (blocked on the `await`),
2. a **capacity slot** (acquired before the call, released in the `finally` only
   when the coroutine returns),
3. it **feeds the dispatch breaker** (#526) on the eventual timeout — and a few
   of these cascade the breaker open, failing healthy work fleet-wide.

Per `TARGET_ARCHITECTURE.md` (pull coordination, §Coordination Model): "a hung
turn … consumes an *agent* worker, not a backend resource, and there is no
dispatch breaker to trip and cascade." #1083 is the step that delivers that
property **independently of** the full push→pull migration.

## 2. Acceptance criteria → mechanism

| AC | Mechanism |
|----|-----------|
| Backend dispatch no longer blocks a coroutine for the turn | Agent `/api/task` returns **202 Accepted** after starting the turn in the background; `execute_task` returns right after the ACK. |
| A hung turn consumes no backend slot; capacity unaffected | The capacity slot becomes a **lease** keyed by `execution_id`, released by the result callback (or by the reaper if the agent never reports). It is no longer held by a blocked coroutine; a hung turn holds only a passive DB/Redis lease, reclaimed by the existing TTL reaper. |
| Terminal state applied from the agent's result report under the CAS guard | A new internal endpoint `POST /api/internal/executions/{id}/result` runs sections 5–7 of today's `execute_task` (sanitize → persist → terminal write via the existing `update_execution_status` **CAS contract** → breaker outcome → slot release). |
| A single slow/hung turn does not trip the dispatch breaker | The breaker is fed by **callback outcomes** (AUTH-only, #526 D10), never by a blocking-call timeout. A turn with no callback simply leases-out and is reaped; no breaker failure is recorded. |

## 3. Building blocks that already exist (reuse, don't rebuild)

- **Slot is already `execution_id`-keyed.** `capacity.release(agent_name, execution_id)`
  and `release_if_matches(...)` exist (`capacity_manager.py:338/350`). The lease
  model needs *release-by-eid on callback* — already the signature.
- **Lease reaper already exists.** `SlotService.reclaim_expired` + the per-agent
  dynamic slot TTL (#226/#913) + `cleanup_service` stale-running recovery (#129)
  + canary **E-01** (terminal-state closure: no `running` row older than
  `timeout + SLOT_TTL_BUFFER`). These already release slots/flip rows for turns
  that never reach terminal. We extend the slot TTL to bound the **lease**.
- **CAS terminal write already exists.** `db/schedules.py:update_execution_status`
  has the CAS contract (models.py:202; #1082 "status-as-projection"). The callback
  reuses it verbatim — a duplicate/late result POST is absorbed.
- **Idempotency.** The trigger is already deduped (`(scope, key)`, #525). The
  result callback is made idempotent by the CAS guard + a result-sentinel
  (status already terminal ⇒ callback is a no-op replay).
- **Internal-secret auth.** `routers/internal.py` already authenticates agent→backend
  via `X-Internal-Secret` — the callback endpoint reuses it (Invariant #8 exception).

## 4. Target flow

```
producer → execute_task(dispatch mode):
   1. acquire lease-slot (as today; keyed by execution_id)
   2. mark dispatched
   3. POST agent /api/task?async=1  →  agent: 202 Accepted (turn started in bg)
   4. execute_task RETURNS  {status: DISPATCHED, execution_id}   ← no coroutine held

agent worker (background, in-container):
   run headless turn (unchanged) → on finish:
   POST backend /api/internal/executions/{id}/result   {response, metadata, error?, terminal_reason}

backend callback handler:
   - CAS terminal write (update_execution_status)         ← AC #3
   - sanitize + persist transcript/cost (sections 5–6)
   - dispatch-breaker outcome (AUTH-only)                 ← AC #4
   - capacity.release(agent_name, execution_id)           ← AC #2 (lease released)
   - complete activity + drain next backlog item

reaper (existing, extended):
   slot lease TTL = timeout + SLOT_TTL_BUFFER. If no callback by then:
   reclaim slot + flip row FAILED(LEASE_EXPIRED) (canary E-01 already guards this).
```

### Agent-server contract change (`/api/task`)
- New optional request flag `async_result: true` (or an `?async=1` query / a
  `result_callback_url` field). When set:
  - start `execute_headless(...)` as a background task (the agent-server already
    owns an asyncio loop; the in-container process registry already tracks the
    subprocess for termination),
  - return `202 {execution_id, status: "accepted"}` immediately,
  - on completion (success **or** failure, incl. the headless timeout/stall paths
    from #970/#1094) POST the **same result envelope** today returned inline to
    `POST {TRINITY_BACKEND_URL}/api/internal/executions/{execution_id}/result`
    with `X-Internal-Secret`.
  - The result envelope carries a **typed terminal-reason** (`status` +
    `error_code`) — the same taxonomy the inline path already classifies
    (`error_classifier.py`), so the backend callback does **not** re-run the
    substring classifier. (This is the result-contract tightening
    `TARGET_ARCHITECTURE.md` §478 calls out; #1083 is where it first ships on the
    async edge.)
- Mirrors **Invariant #5** (agent server mirrors backend) and **#13** (MCP/agent
  surfaces stay in sync). When `async_result` is absent the handler is byte-for-byte
  today's synchronous path — **the fallback**.

### Backend callback endpoint (new)
`POST /api/internal/executions/{execution_id}/result` (`routers/internal.py`,
`X-Internal-Secret`, no JWT — Invariant #8 exception, same as `/internal/execute-task`).
Body = the result envelope. It calls a new
`task_execution_service.apply_result(execution_id, envelope)` that contains the
**extracted** sections 5–7 of `execute_task` (no logic duplication — `execute_task`
sync path and the callback share one `apply_result`). Idempotent: if the row is
already terminal, return `200 {replayed: true}` and do nothing (CAS + sentinel).

## 5. Dual-mode flag + rollout

- Global env flag **`DISPATCH_ASYNC`** (default **OFF**) gates the whole change.
  OFF ⇒ today's synchronous dispatch verbatim (zero behavior change). ON ⇒
  dispatch+callback.
- Per-agent opt-in is **not** needed for v1 (it's a transport detail, not a
  feature surface), but the flag read mirrors `dispatch_breaker_active()` so a
  per-agent override can be added later cheaply.
- **Side-effect safety (the #1084 gate).** Fire-and-forget *by itself* does not
  re-run turns — there is no new re-delivery here (the lease reaper flips a lost
  turn to FAILED, it does **not** re-queue it in v1). So #1083 does **not** require
  effect-scoped idempotency keys (#1084) the way full pull re-delivery does.
  **Explicitly out of scope for v1:** lease-expiry **re-queue** (that is pull's
  re-delivery and needs #1084). v1 lease-expiry = FAIL, not retry.
- **Rollback:** flip `DISPATCH_ASYNC=0` and restart the backend — the next
  dispatch is synchronous again. In-flight async turns already dispatched will
  still call back (the callback endpoint stays mounted regardless of the flag);
  the reaper covers any that don't.

## 6. Reaper / lease semantics

- The slot lease TTL = `execution_timeout_seconds + SLOT_TTL_BUFFER` (already the
  slot TTL today, #913). No change to the value; the change is that **nothing
  holds the slot open via a live coroutine**, so the TTL is now the *only* bound —
  which is exactly what canary **E-01** already asserts (no running row older than
  `timeout + 300s`). The cleanup watchdog's `reclaim_expired` already releases such
  slots; we ensure it ALSO flips the row to `FAILED(LEASE_EXPIRED)` and records the
  activity terminal (today it relies on the coroutine's `finally`, which no longer
  runs in async mode).
- **Lost-callback path** (agent died / container recreated mid-turn): lease
  expires → reaper FAILs the row → operator sees a clean terminal, not a phantom
  `running`. Canary E-01/S-01 stay green.

## 7. Soak / verification plan

1. **Unit:** `apply_result` produces identical terminal rows to the current inline
   path for the full matrix (success, AUTH fail, agent error, timeout, reader-race,
   empty-result) — golden-output test against the existing terminal writes.
2. **Idempotency:** double-POST the same result envelope → one terminal write,
   second returns `replayed:true`; slot released exactly once.
3. **Hung-turn soak (the headline):** induce a wedged MCP call (the #1094 stall
   repro). Assert: `execute_task` returns in < 2s; **no** backend coroutine
   blocked; the slot is a lease (reclaimed at `timeout+buffer`); the dispatch
   breaker **does not** open; other agents' dispatch is unaffected.
4. **Lost-callback:** kill the agent container mid-turn → lease expires → row
   FAILED(LEASE_EXPIRED); canary E-01/S-01 green.
5. **Flag parity:** full API test suite green with `DISPATCH_ASYNC=0` (no
   regression) and `=1`.
6. **Breaker re-wire:** AUTH failure via callback opens the breaker exactly as the
   inline AUTH terminal does today (#526 D10 preserved).

## 8. Staging (recommended sequence)

Even though this lands behind one flag, two PRs keep reviews honest:

- **PR1 (infra, dormant):** extract sections 5–7 into `apply_result`; add the
  `POST /api/internal/executions/{id}/result` endpoint; make the reaper flip
  `FAILED(LEASE_EXPIRED)` + activity terminal. Sync path refactored to *call*
  `apply_result` (proves parity). No behavior change; `DISPATCH_ASYNC` unused.
- **PR2 (cutover, flagged):** agent-server async-accept + result POST; `execute_task`
  dispatch+return under `DISPATCH_ASYNC`; soak. Requires a base-image rebuild
  (agent-server change — same rollout note as #1098).

## 9. Interactions

- **#1082 (status-as-projection / CAS):** the callback's terminal write *is* a CAS
  projection. Already shipped on `update_execution_status` (models.py:202) — #1083
  consumes it.
- **#526 (dispatch breaker):** repurposed input — fed by callback outcomes, not
  blocking timeouts. Still a gate in v1 (not yet the pull "alert-only" repurpose).
- **#1084 (effect-scoped idempotency):** **not** required by v1 because v1 does
  not re-deliver. It *is* required before lease-expiry becomes re-queue (a later,
  pull-aligned step).
- **Pull migration (#1081):** this is the natural intermediate — once agents pull,
  the dispatch POST disappears entirely and the callback becomes the worker's
  result report. #1083 builds that callback contract early.

## 10. Risks / open questions

1. **Agent-server async run lifecycle.** The background turn must survive the
   202 response and still be killable via the process registry (timeout/cancel).
   Need to confirm `execute_headless` can run detached from the request coroutine
   without losing the registry handle (it already registers by `execution_id`).
2. **Backend URL reachability for the callback.** The agent already reaches the
   backend (`TRINITY_BACKEND_URL`, used by `/internal/decrypt-and-inject` and the
   #307 heartbeat). Reuse that; on callback POST failure the agent retries with
   backoff, and the reaper is the backstop.
3. **`max_parallel_tasks` enforcement during the lease window.** Capacity is still
   the backend lease count in v1 (physical agent-side enforcement is the pull step,
   not this one) — so the lease must be acquired *before* dispatch and only released
   on terminal, preserving the current concurrency bound. Confirmed: that is the
   design (lease = today's slot, minus the coroutine).
4. **Ordering vs. activity/WS events.** The callback path must emit the same
   `agent_activity` completion + WS events the inline path does (moved into
   `apply_result`).

## 11. Decision requested

Approve (a) the **target flow** (§4) and the **callback contract** (§4), (b) the
**dual-mode `DISPATCH_ASYNC` flag** with sync as the verbatim fallback, (c) the
**v1 scope boundary** — lease-expiry = FAIL, not re-queue; #1084 not required for
v1, and (d) the **two-PR staging** (§8). On approval I implement PR1 first.
