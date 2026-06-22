# Runtime Streaming Experience Design

Date: 2026-06-21

## Goal

Provide a full real-time task streaming experience across Trinity's user-facing execution surfaces:

- ChatPanel: stream assistant text into the chat while the task runs.
- TasksPanel: show live task status, assistant preview, and tool activity for manually launched tasks.
- ExecutionDetail: show a live runtime-neutral transcript for Claude Code and OpenCode in Phase 1, with Gemini event parsing compatibility and Gemini live publishing as a follow-up.

The implementation should preserve reliable final results by continuing to poll persisted execution status and using the final persisted response as the source of truth when execution completes.

## Current State

### Frontend

- `ChatPanel.vue` submits `/api/agents/{agent}/task` with `async_mode: true`, subscribes to `/executions/{id}/stream`, but only uses stream events to update loading labels. The assistant message is added only after polling returns a final execution response.
- `TasksPanel.vue` launches manual tasks synchronously and waits for `/task` to complete. It does not subscribe to live execution streams.
- `ExecutionDetail.vue` already subscribes to the execution stream for running executions, but its parser is Claude-centric and does not understand OpenCode nested text events or runtime-neutral event shapes.
- Static execution-log parsing is duplicated between TasksPanel and ExecutionDetail.

### Backend and Agent Server

- Backend `/api/agents/{name}/executions/{execution_id}/stream` proxies the agent server SSE stream unchanged after validating access.
- Agent server `ProcessRegistry` can publish and stream raw execution events.
- Claude Code paths publish raw stream-json lines while processes run.
- OpenCode previously used blocking `process.communicate()` and therefore could not stream until process completion.
- Gemini reads process stdout incrementally in places, but does not yet publish runtime events through `ProcessRegistry` consistently.

## Recommended Approach

Use raw runtime events as the transport format for now, and normalize them in a shared frontend layer.

This balances scope and reliability:

- Avoids a large backend-wide normalized-event migration.
- Lets existing Claude streaming continue working.
- Makes OpenCode usable in real time once it publishes stdout events line-by-line.
- Gives all frontend surfaces one shared parser and stream reader instead of three divergent implementations.

## Architecture

### Agent Server: Runtime Event Publishing

#### Phase Boundary

Phase 1 success means:

- Claude Code existing live streaming continues to work.
- OpenCode publishes live stdout events and all three frontend surfaces display them in real time.
- Gemini-like event shapes are understood by the frontend parser where encountered, but Gemini runtime live publishing is not a Phase 1 blocker.

Gemini live publishing remains a Phase 2 parity task unless it can be added safely without expanding Phase 1 risk.

#### OpenCode

Convert OpenCode runtime execution from blocking stdout collection to incremental stdout reading.

Expected behavior:

1. Start the `opencode run --format json ...` subprocess.
2. Register the execution in `ProcessRegistry` before stdout reading begins.
3. Read stdout line-by-line while the process runs.
4. Drain stdout and stderr concurrently. Stderr must never be drained only after process completion because a full stderr pipe can deadlock the child process.
5. For each valid stdout JSON line:
   - sanitize the event,
   - append it to `raw_messages`,
   - publish it through `ProcessRegistry` using a thread-safe mechanism,
   - update accumulated response text and metadata where practical.
6. Accumulate stderr lines in a bounded buffer for sanitized final error reporting.
7. On completion, parse accumulated raw events as a final consistency pass and return the final response text, raw messages, metadata, and session id.
8. Always unregister the execution in `finally` so stream subscribers receive `stream_end`.

Thread-safety requirement:

- If OpenCode stdout/stderr readers run in worker threads, they must not call `asyncio.Queue.put_nowait()` directly on subscriber queues.
- Publishing must either marshal queue writes back onto the event loop with `loop.call_soon_threadsafe(...)`, or use an async subprocess implementation where stdout reading and queue publication occur on the event loop.
- The implementation plan must choose one approach explicitly before code is written.

The OpenCode parser must continue to support nested text events such as:

```json
{
  "type": "text",
  "part": {
    "type": "text",
    "text": "Hello!"
  }
}
```

#### Gemini

Gemini streaming parity is part of the full C direction, but should be staged after OpenCode unless the same abstraction makes it trivial.

Minimum acceptable first implementation:

- Frontend normalizer understands Gemini-like event shapes.
- Existing Gemini final-result behavior remains unchanged.

Follow-up implementation:

- Register Gemini executions in `ProcessRegistry`.
- Publish parsed stdout events line-by-line as raw events.
- Unregister in `finally`.

### Backend Proxy

Keep backend stream proxy pass-through for this iteration.

The backend remains responsible for:

- access control,
- connecting to `http://agent-{name}:8000/api/executions/{execution_id}/stream`,
- forwarding SSE chunks,
- sending proxy-level error and `stream_end` events when appropriate.

No backend normalized envelope is required in this phase.

### ProcessRegistry Buffering

Improve reliability for late subscribers and reconnects:

- Keep completed execution buffers for `RECENTLY_COMPLETED_TTL_SECONDS` after unregister.
- Do not delete `_log_buffers[execution_id]` immediately on unregister.
- Preserve `stream_end` replay for completed buffered executions.
- Keep the existing per-execution cap of `_max_buffer_size = 1000` entries unless tests show it is insufficient.
- Add a global completed-buffer cap of 100 executions. When exceeded, evict the oldest completed buffers first.
- Cleanup may be lazy on registry read/write operations, but it must enforce both TTL and global cap.

If a stream cannot be recovered, the frontend must continue polling final execution status and then use persisted response/log data.

## Frontend Design

### Shared Stream Reader

Add a composable:

```text
src/frontend/src/composables/useExecutionStream.js
```

Responsibilities:

- open authenticated SSE fetch requests,
- parse SSE frames correctly across arbitrary chunk boundaries,
- support multi-line `data:` fields joined with newline before JSON parsing,
- ignore keepalive comments,
- expose callbacks for raw events, errors, and end-of-stream,
- support cancellation and cleanup,
- retry short-lived retryable stream errors with a bounded policy.

Suggested interface:

```js
const stream = useExecutionStream({
  agentName,
  executionId,
  token,
  onEvent(rawEvent) {},
  onError(errorEvent) {},
  onEnd() {},
})

stream.start()
stream.cancel()
```

Retry contract:

- Retry only explicit retryable stream errors, such as backend proxy events with `retryable: true`.
- Use at most 5 retries.
- Use exponential backoff starting at 250 ms and capped at 2 seconds.
- Stop retrying once final execution polling reports a terminal state.
- Do not retry authorization failures or malformed execution ids.

### Shared Event Normalizer

Add a runtime-neutral parser:

```text
src/frontend/src/utils/executionEventNormalizer.js
```

It maps raw runtime events to canonical UI events:

```js
{
  kind: 'status',
  label,
  eventId,
  sequence,
  timestamp,
  sourceRuntime,
  rawType,
}
{
  kind: 'assistant_text',
  text,
  mode: 'delta' | 'snapshot',
  eventId,
  sequence,
  timestamp,
  sourceRuntime,
  rawType,
}
{ kind: 'tool_start', id, name, input, eventId, sequence, timestamp, sourceRuntime, rawType }
{ kind: 'tool_result', id, name, output, success, eventId, sequence, timestamp, sourceRuntime, rawType }
{ kind: 'metadata', model, tokens, cost, duration, eventId, sequence, timestamp, sourceRuntime, rawType }
{ kind: 'done', eventId, sequence, timestamp, sourceRuntime, rawType }
{ kind: 'error', message, retryable, eventId, sequence, timestamp, sourceRuntime, rawType }
```

Supported raw inputs:

- Claude stream-json events with `message.content[]` blocks.
- OpenCode events including `type: "text"`, nested `part.text`, `tool_call`, `tool_use`, `tool_result`, `tool_output`, `usage`, `result`, and `final` shapes.
- Gemini-like message/tool/result events where already available.

Assistant text contract:

- Canonical `assistant_text` defaults to `mode: 'delta'`.
- If a runtime event appears to contain a complete cumulative message snapshot, the normalizer must emit `mode: 'snapshot'`.
- Consumers append `delta` text and replace current draft text with `snapshot` text.
- The normalizer must avoid duplicate assistant text within the same raw event, for example when the same text exists both top-level and nested.
- Cross-event duplicate detection should use `eventId` when available; otherwise consumers may keep a small recent-text window to avoid obvious repeated snapshots.

Ordering contract:

- `sequence` is assigned by the stream consumer in receipt order when the raw runtime event has no stable sequence.
- `eventId` should prefer runtime ids such as `part.id`, tool id, message id, or result id; otherwise generate a deterministic id from execution id plus sequence.
- `timestamp` should prefer runtime timestamp and fall back to client receipt time.

### Shared Static Log Parser

Add or consolidate:

```text
src/frontend/src/utils/executionLogParser.js
```

Responsibilities:

- turn raw execution logs into transcript entries used by TasksPanel and ExecutionDetail,
- reuse `normalizeExecutionEvent()` internally,
- preserve existing Claude rendering behavior,
- add OpenCode and Gemini-compatible transcript entries.

This replaces duplicated parser logic in TasksPanel and ExecutionDetail.

## Surface Behavior

### ChatPanel

When an async task starts:

1. Add the user message immediately, as today.
2. After receiving `execution_id`, create an assistant draft message:

```js
{
  role: 'assistant',
  content: '',
  streaming: true,
  tools: [],
  transcript: []
}
```

3. Subscribe to the stream using `useExecutionStream()`.
4. For normalized events:
   - `assistant_text` with `mode: 'delta'`: append to draft message content.
   - `assistant_text` with `mode: 'snapshot'`: replace draft message content.
   - `tool_start`: update current tool/status and append to transcript.
   - `tool_result`: update tool state and transcript.
   - `status`: update loading label.
   - `error`: show non-fatal stream warning if final poll can still recover.
5. Continue polling final execution status.
6. On success:
   - replace or reconcile draft content with persisted `execution.response`,
   - set `streaming = false`,
   - keep transcript/tool metadata for detail display if already shown.
7. On failure/cancelled:
   - mark draft as failed or remove empty draft if no assistant text was streamed,
   - show the persisted error.

### TasksPanel

Manual task creation should use async execution for streaming.

Flow:

1. POST `/task` with `async_mode: true`.
2. Add or update a local task row with running status and returned `execution_id`.
3. Subscribe to the execution stream.
4. Update row fields from normalized events:
   - current status,
   - assistant preview,
   - current tool,
   - tool count,
   - latest event timestamp.
5. Continue final polling or refresh persisted executions on completion.
6. On failure or cancellation, update the local row immediately with the terminal status and persisted error if available.
7. Reconcile final response and logs from persisted execution data.

TasksPanel should not rely solely on SSE for correctness. If SSE fails, the task still completes through backend polling/refresh.

### ExecutionDetail

ExecutionDetail should become the canonical live transcript viewer.

Behavior:

- If execution is running, subscribe using `useExecutionStream()`.
- Append normalized transcript entries live.
- Display assistant text, tool starts/results, metadata, and errors in a runtime-neutral way.
- Avoid hard-coded “Claude” labels; use “Assistant”, runtime name, or model when known.
- After stream end or execution completion, fetch final persisted execution/log data and reconcile.

Reconciliation rule:

- Live transcript entries are ordered by `sequence` while streaming.
- When final persisted logs are available, replace the live transcript with parsed persisted logs if persisted logs are non-empty.
- If persisted logs are empty but live entries exist, keep the live entries and show a non-blocking note that the transcript is from the live stream.
- For ChatPanel assistant text, final persisted `execution.response` always wins over streamed draft content.

## Error Handling

- SSE connection failure must not fail the task.
- Frontend should display a lightweight stream warning only if useful, while continuing final polling.
- If stream events contain malformed JSON, skip the event and continue.
- If a runtime emits duplicate text chunks, normalizer should minimize duplication per event.
- If a runtime emits cumulative snapshots, consumers must replace draft text rather than append it.
- If final persisted response differs from streamed draft, final response wins.
- If OpenCode exits non-zero, existing sanitized error behavior remains.

## Testing Strategy

### Agent Server Tests

- OpenCode parser extracts nested `part.text` events.
- OpenCode runtime publishes stdout JSON lines through `ProcessRegistry` while running.
- OpenCode final response equals accumulated streamed assistant text for simple text-only runs.
- ProcessRegistry retains completed buffers for the configured TTL.
- OpenCode stdout publishing is thread-safe when stdout is read from a worker thread.
- OpenCode drains stdout and stderr concurrently and does not deadlock when stderr emits many lines.
- Backend stream proxy preserves SSE frames, converts agent 404 into a retryable error where applicable, and emits `stream_end` on proxy errors.

### Frontend Tests

- `normalizeExecutionEvent()` maps Claude assistant text/tool events.
- `normalizeExecutionEvent()` maps OpenCode nested text/tool events.
- `normalizeExecutionEvent()` maps Gemini-like message/tool events where supported.
- `normalizeExecutionEvent()` handles duplicate top-level/nested text in one raw event.
- `normalizeExecutionEvent()` distinguishes delta and snapshot text semantics.
- `useExecutionStream()` parses SSE `data:` events, ignores keepalives, handles `stream_end`, and cancels cleanly.
- `useExecutionStream()` handles chunk boundaries, multi-line `data:` frames, malformed frames, retryable stream errors, and bounded retry stop conditions.
- ChatPanel appends streamed assistant text into a draft message and reconciles with final response.
- TasksPanel updates a running task row from streamed text/tool/status events.
- ExecutionDetail renders OpenCode text/tool events through the shared parser.

### Remote Verification

On the Ubuntu deployment:

1. Rebuild backend/frontend if changed.
2. Rebuild `trinity-agent-base:latest` for agent-server runtime changes.
3. Recreate OpenCode test agent containers so they use the new base image.
4. Run a deterministic long-running OpenCode or mock OpenCode task that emits multiple JSONL text/tool events with delays. A real model/tool run may be used as a final smoke test, but deterministic delayed JSONL is preferred for verification.
5. Verify:
   - ChatPanel shows streaming assistant text before completion.
   - TasksPanel row updates while running.
   - ExecutionDetail shows live transcript while running.
   - Final persisted response matches or cleanly reconciles with streamed content.

## Non-Goals

- Do not replace backend SSE proxy with a normalized backend event envelope in this phase.
- Do not require token-level streaming if a runtime only emits line-level JSON events.
- Do not remove final polling or persisted response reconciliation.
- Do not redesign the visual layout beyond what is required for live transcript display.

## Rollout Plan

1. Shared frontend stream infrastructure and normalizer.
2. OpenCode runtime line-by-line publishing.
3. ChatPanel live assistant draft.
4. TasksPanel async streaming rows.
5. ExecutionDetail shared runtime-neutral transcript.
6. Gemini live publish parity follow-up if not completed in the main pass.

## Open Questions Resolved

- Scope: full C experience across ChatPanel, TasksPanel, and ExecutionDetail for Claude Code and OpenCode in Phase 1; Gemini live publishing may follow in Phase 2.
- Transport: keep raw runtime events over SSE for this phase.
- Correctness source: final persisted execution response remains authoritative.
- OpenCode priority: implement live publish first because it is the immediate runtime under test.
