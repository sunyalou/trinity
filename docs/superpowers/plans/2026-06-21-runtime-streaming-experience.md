# Runtime Streaming Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build real-time streaming across ChatPanel, TasksPanel, and ExecutionDetail for Claude Code and OpenCode, with Gemini-compatible parsing staged for later live publish.

**Architecture:** Keep raw runtime events over the existing SSE proxy. Add a shared frontend SSE reader, runtime-neutral event normalizer, and shared execution-log parser. Make OpenCode publish stdout JSONL incrementally through a thread-safe ProcessRegistry path while preserving final persisted execution response as the source of truth.

**Tech Stack:** Vue 3 Composition API, Axios/fetch ReadableStream SSE, Python FastAPI, Docker agent-server runtime adapters, pytest, frontend Node scripts/tests.

---

## Spec

Approved spec:

```text
docs/superpowers/specs/2026-06-21-runtime-streaming-experience-design.md
```

## File Structure

### Frontend files

- Create `src/frontend/src/composables/useExecutionStream.js`
  - Owns authenticated SSE fetch, frame parsing, cancellation, bounded retry, and lifecycle callbacks.
- Create `src/frontend/src/utils/executionEventNormalizer.js`
  - Converts raw Claude/OpenCode/Gemini-like events into canonical events.
- Create `src/frontend/src/utils/executionLogParser.js`
  - Converts raw persisted/live logs into transcript entries for TasksPanel and ExecutionDetail.
- Modify `src/frontend/src/components/ChatPanel.vue`
  - Replace local SSE reader with `useExecutionStream()` and append streamed assistant draft content.
- Modify `src/frontend/src/components/TasksPanel.vue`
  - Submit manual tasks with `async_mode: true`, subscribe to execution stream, and update running task rows live.
- Modify `src/frontend/src/views/ExecutionDetail.vue`
  - Replace local SSE reader/parser with shared stream reader/parser and runtime-neutral labels.
- Add `src/frontend/scripts/test-execution-streaming.mjs`
  - Lightweight Node assertions for normalizer/parser/SSE parser helpers.

### Agent/backend files

- Modify `docker/base-image/agent_server/services/process_registry.py`
  - Add thread-safe publishing and completed-buffer retention.
- Modify `docker/base-image/agent_server/services/opencode_runtime.py`
  - Replace blocking `communicate()` path with concurrent stdout/stderr readers and live event publication.
- Modify `docker/base-image/agent_server/routers/chat.py`
  - Update comments from Claude-only to runtime-neutral if touched.
- Modify `src/backend/routers/chat.py`
  - Add/adjust proxy tests only if behavior changes; keep pass-through contract.

### Tests

- Modify `tests/unit/test_opencode_runtime.py`
- Modify or add `tests/unit/test_recently_completed_buffer.py`
- Add `tests/unit/test_process_registry_streaming.py` if existing registry tests do not fit.
- Add frontend script `src/frontend/scripts/test-execution-streaming.mjs`.

---

## Task 1: Shared frontend event normalizer and parser

**Files:**
- Create: `src/frontend/src/utils/executionEventNormalizer.js`
- Create: `src/frontend/src/utils/executionLogParser.js`
- Create: `src/frontend/scripts/test-execution-streaming.mjs`

- [ ] **Step 1: Write failing frontend normalizer tests**

Create `src/frontend/scripts/test-execution-streaming.mjs` with these assertions:

```js
import assert from 'node:assert/strict'
import {
  normalizeExecutionEvent,
  normalizeExecutionEvents,
} from '../src/utils/executionEventNormalizer.js'
import { parseExecutionLog } from '../src/utils/executionLogParser.js'

function kinds(events) {
  return events.map((event) => event.kind)
}

const openCodeText = {
  type: 'text',
  timestamp: 1782025075217,
  sessionID: 'ses_open_1',
  part: {
    id: 'prt_text_1',
    type: 'text',
    text: 'Hello live stream',
  },
}

const openCodeTool = {
  type: 'tool_call',
  id: 'tool_1',
  name: 'bash',
  input: { cmd: 'pwd' },
}

const claudeAssistant = {
  type: 'assistant',
  message: {
    content: [
      { type: 'text', text: 'Claude text' },
      { type: 'tool_use', id: 'tool_2', name: 'Read', input: { file_path: 'README.md' } },
    ],
  },
}

const duplicateText = {
  type: 'text',
  text: 'Same text',
  part: { id: 'prt_dup', type: 'text', text: 'Same text' },
}

const geminiText = {
  type: 'message',
  role: 'assistant',
  content: [{ type: 'text', text: 'Gemini-like text' }],
}

let events = normalizeExecutionEvent(openCodeText, { sequence: 1, runtime: 'opencode' })
assert.equal(events.length, 1)
assert.equal(events[0].kind, 'assistant_text')
assert.equal(events[0].text, 'Hello live stream')
assert.equal(events[0].mode, 'delta')
assert.equal(events[0].eventId, 'prt_text_1')
assert.equal(events[0].sourceRuntime, 'opencode')

events = normalizeExecutionEvent(openCodeTool, { sequence: 2, runtime: 'opencode' })
assert.equal(events[0].kind, 'tool_start')
assert.equal(events[0].id, 'tool_1')
assert.equal(events[0].name, 'bash')

events = normalizeExecutionEvent(claudeAssistant, { sequence: 3, runtime: 'claude-code' })
assert.deepEqual(kinds(events), ['assistant_text', 'tool_start'])
assert.equal(events[0].text, 'Claude text')
assert.equal(events[1].name, 'Read')

events = normalizeExecutionEvent(duplicateText, { sequence: 4, runtime: 'opencode' })
assert.equal(events.length, 1)
assert.equal(events[0].text, 'Same text')

events = normalizeExecutionEvent(geminiText, { sequence: 5, runtime: 'gemini-cli' })
assert.equal(events[0].kind, 'assistant_text')
assert.equal(events[0].text, 'Gemini-like text')

const normalized = normalizeExecutionEvents([openCodeText, openCodeTool], { runtime: 'opencode' })
assert.deepEqual(kinds(normalized), ['assistant_text', 'tool_start'])

const transcript = parseExecutionLog([openCodeText, openCodeTool], { runtime: 'opencode' })
assert.equal(transcript[0].type, 'assistant_text')
assert.equal(transcript[0].content, 'Hello live stream')
assert.equal(transcript[1].type, 'tool_start')
assert.equal(transcript[1].tool, 'bash')

console.log('execution streaming tests passed')
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
```

Expected: FAIL with module-not-found errors for `executionEventNormalizer.js` or `executionLogParser.js`.

- [ ] **Step 3: Implement `executionEventNormalizer.js`**

Create `src/frontend/src/utils/executionEventNormalizer.js`:

```js
function asArray(value) {
  if (Array.isArray(value)) return value
  if (value === undefined || value === null) return []
  return [value]
}

function getRawType(raw) {
  return raw?.type || raw?.part?.type || raw?.message?.type || 'unknown'
}

function pickTimestamp(raw) {
  return raw?.timestamp || raw?.part?.timestamp || raw?.created_at || new Date().toISOString()
}

function baseEvent(raw, context = {}) {
  const sequence = context.sequence ?? 0
  const eventId =
    raw?.part?.id ||
    raw?.id ||
    raw?.message?.id ||
    raw?.messageID ||
    raw?.sessionID && `${raw.sessionID}:${sequence}` ||
    `${context.executionId || 'execution'}:${sequence}`
  return {
    eventId,
    sequence,
    timestamp: pickTimestamp(raw),
    sourceRuntime: context.runtime || raw?.runtime || raw?.sourceRuntime || 'unknown',
    rawType: getRawType(raw),
  }
}

function uniqueTexts(texts) {
  const seen = new Set()
  return texts.filter((text) => {
    if (typeof text !== 'string' || !text) return false
    if (seen.has(text)) return false
    seen.add(text)
    return true
  })
}

function collectText(value, out = []) {
  if (!value) return out
  if (typeof value === 'string') {
    out.push(value)
    return out
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectText(item, out))
    return out
  }
  if (typeof value !== 'object') return out
  if (typeof value.text === 'string' && (!value.type || ['text', 'message', 'assistant_message'].includes(value.type))) {
    out.push(value.text)
  }
  for (const key of ['part', 'content', 'parts', 'message', 'result']) {
    if (value[key] !== undefined && !(key === 'message' && typeof value[key] === 'string')) {
      collectText(value[key], out)
    }
  }
  return out
}

function toolStart(raw, context) {
  const name = raw?.name || raw?.tool || raw?.part?.name
  if (!name) return null
  return {
    kind: 'tool_start',
    id: String(raw.id || raw.callID || raw.part?.id || `${context.executionId || 'tool'}:${context.sequence}`),
    name: String(name),
    input: raw.input || raw.part?.input || {},
    ...baseEvent(raw, context),
  }
}

function toolResult(raw, context) {
  const output = raw.output ?? raw.text ?? raw.content ?? raw.part?.output
  return {
    kind: 'tool_result',
    id: String(raw.id || raw.callID || raw.part?.id || `${context.executionId || 'tool'}:${context.sequence}`),
    name: String(raw.name || raw.tool || raw.part?.name || 'unknown'),
    output,
    success: raw.success,
    ...baseEvent(raw, context),
  }
}

function fromClaudeContent(raw, context) {
  const output = []
  const content = raw?.message?.content
  for (const block of asArray(content)) {
    if (!block || typeof block !== 'object') continue
    if (block.type === 'text' && block.text) {
      output.push({ kind: 'assistant_text', text: block.text, mode: 'delta', ...baseEvent({ ...raw, id: block.id }, context) })
    } else if (block.type === 'tool_use') {
      output.push({
        kind: 'tool_start',
        id: String(block.id || `${context.executionId || 'tool'}:${context.sequence}`),
        name: String(block.name || 'unknown'),
        input: block.input || {},
        ...baseEvent({ ...raw, id: block.id }, context),
      })
    } else if (block.type === 'tool_result') {
      output.push({
        kind: 'tool_result',
        id: String(block.tool_use_id || block.id || `${context.executionId || 'tool'}:${context.sequence}`),
        name: String(block.name || 'unknown'),
        output: block.content,
        success: block.is_error === undefined ? undefined : !block.is_error,
        ...baseEvent({ ...raw, id: block.tool_use_id || block.id }, context),
      })
    } else if (block.type === 'thinking') {
      output.push({ kind: 'status', label: 'Thinking...', ...baseEvent(raw, context) })
    }
  }
  return output
}

export function normalizeExecutionEvent(raw, context = {}) {
  if (!raw || typeof raw !== 'object') return []
  if (raw.type === 'stream_end') return [{ kind: 'done', ...baseEvent(raw, context) }]
  if (raw.type === 'error') return [{ kind: 'error', message: raw.message || 'Stream error', retryable: Boolean(raw.retryable), ...baseEvent(raw, context) }]

  const claudeEvents = fromClaudeContent(raw, context)
  if (claudeEvents.length > 0) return claudeEvents

  if (['tool_call', 'tool_use'].includes(raw.type)) return [toolStart(raw, context)].filter(Boolean)
  if (['tool_result', 'tool_output'].includes(raw.type)) return [toolResult(raw, context)]
  if (raw.type === 'usage' || raw.type === 'result' || raw.type === 'final') {
    const usage = raw.usage || raw
    return [{
      kind: 'metadata',
      model: raw.model || raw.model_name,
      tokens: {
        input: usage.input_tokens ?? usage.inputTokens,
        output: usage.output_tokens ?? usage.outputTokens,
      },
      cost: usage.cost_usd,
      duration: usage.duration_ms,
      ...baseEvent(raw, context),
    }]
  }

  const texts = uniqueTexts(collectText(raw))
  if (texts.length > 0) {
    const mode = raw.snapshot === true || raw.is_snapshot === true ? 'snapshot' : 'delta'
    return texts.map((text) => ({ kind: 'assistant_text', text, mode, ...baseEvent(raw, context) }))
  }

  if (raw.type === 'session' || raw.type === 'step_start') return [{ kind: 'status', label: 'Starting...', ...baseEvent(raw, context) }]
  if (raw.type === 'step_finish') return [{ kind: 'status', label: 'Finishing...', ...baseEvent(raw, context) }]
  return []
}

export function normalizeExecutionEvents(rawEvents, context = {}) {
  const output = []
  rawEvents.forEach((raw, index) => {
    output.push(...normalizeExecutionEvent(raw, { ...context, sequence: index + 1 }))
  })
  return output
}
```

- [ ] **Step 4: Implement `executionLogParser.js`**

Create `src/frontend/src/utils/executionLogParser.js`:

```js
import { normalizeExecutionEvents } from './executionEventNormalizer.js'

export function parseExecutionLog(rawLog, options = {}) {
  const rawEvents = Array.isArray(rawLog) ? rawLog : []
  const normalized = normalizeExecutionEvents(rawEvents, options)
  return normalized.map((event) => {
    if (event.kind === 'assistant_text') {
      return {
        id: event.eventId,
        type: 'assistant_text',
        content: event.text,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      }
    }
    if (event.kind === 'tool_start') {
      return {
        id: event.eventId,
        type: 'tool_start',
        tool: event.name,
        input: event.input,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      }
    }
    if (event.kind === 'tool_result') {
      return {
        id: event.eventId,
        type: 'tool_result',
        tool: event.name,
        output: event.output,
        success: event.success,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      }
    }
    return {
      id: event.eventId,
      type: event.kind,
      label: event.label,
      timestamp: event.timestamp,
      runtime: event.sourceRuntime,
      rawType: event.rawType,
    }
  })
}
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
```

Expected: `execution streaming tests passed`.

---

## Task 2: Shared frontend SSE reader

**Files:**
- Modify: `src/frontend/scripts/test-execution-streaming.mjs`
- Create: `src/frontend/src/composables/useExecutionStream.js`

- [ ] **Step 1: Extend tests for SSE frame parsing**

Append this to `src/frontend/scripts/test-execution-streaming.mjs`:

```js
import { parseSseFrames } from '../src/composables/useExecutionStream.js'

let parsed = parseSseFrames('data: {"type":"text"}\n\n')
assert.equal(parsed.events.length, 1)
assert.equal(parsed.events[0].type, 'text')
assert.equal(parsed.buffer, '')

parsed = parseSseFrames('data: {"type"', '')
assert.equal(parsed.events.length, 0)
parsed = parseSseFrames(':"text"}\n\n', parsed.buffer)
assert.equal(parsed.events[0].type, 'text')

parsed = parseSseFrames('data: {"a":1,\ndata: "b":2}\n\n')
assert.deepEqual(parsed.events[0], { a: 1, b: 2 })

parsed = parseSseFrames(': keepalive\n\ndata: {"type":"stream_end"}\n\n')
assert.equal(parsed.events[0].type, 'stream_end')
```

- [ ] **Step 2: Run tests to verify fail**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
```

Expected: FAIL because `useExecutionStream.js` does not exist.

- [ ] **Step 3: Implement `useExecutionStream.js`**

Create `src/frontend/src/composables/useExecutionStream.js`:

```js
export function parseSseFrames(chunk, existingBuffer = '') {
  const combined = existingBuffer + chunk
  const frames = combined.split(/\r?\n\r?\n/)
  const buffer = frames.pop() || ''
  const events = []

  for (const frame of frames) {
    const dataLines = []
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    if (dataLines.length === 0) continue
    const payload = dataLines.join('\n')
    try {
      events.push(JSON.parse(payload))
    } catch (_err) {
      events.push({ type: 'error', message: 'Malformed stream event', retryable: false })
    }
  }

  return { events, buffer }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export function useExecutionStream({ agentName, executionId, token, onEvent, onError, onEnd }) {
  let cancelled = false
  let reader = null
  let retryCount = 0
  const maxRetries = 5

  async function connect() {
    while (!cancelled) {
      try {
        const response = await fetch(`/api/agents/${agentName}/executions/${executionId}/stream`, {
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: 'text/event-stream',
          },
        })
        if (!response.ok) {
          onError?.({ message: `Stream failed with HTTP ${response.status}`, retryable: response.status === 404 })
          if (response.status !== 404 || retryCount >= maxRetries) break
          await delay(Math.min(2000, 250 * 2 ** retryCount++))
          continue
        }

        retryCount = 0
        reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!cancelled) {
          const { done, value } = await reader.read()
          if (done) break
          const parsed = parseSseFrames(decoder.decode(value, { stream: true }), buffer)
          buffer = parsed.buffer
          for (const event of parsed.events) {
            if (event.type === 'stream_end') {
              onEnd?.()
              return
            }
            if (event.type === 'error') {
              onError?.(event)
              if (event.retryable && retryCount < maxRetries) {
                await delay(Math.min(2000, 250 * 2 ** retryCount++))
                break
              }
              continue
            }
            onEvent?.(event)
          }
        }
        break
      } catch (err) {
        onError?.({ message: err?.message || 'Stream connection failed', retryable: retryCount < maxRetries })
        if (retryCount >= maxRetries) break
        await delay(Math.min(2000, 250 * 2 ** retryCount++))
      }
    }
    onEnd?.()
  }

  return {
    start() {
      cancelled = false
      connect()
    },
    cancel() {
      cancelled = true
      if (reader) reader.cancel().catch(() => {})
    },
  }
}
```

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
```

Expected: `execution streaming tests passed`.

---

## Task 3: ProcessRegistry thread-safe publishing and completed-buffer retention

**Files:**
- Modify: `docker/base-image/agent_server/services/process_registry.py`
- Add or modify: `tests/unit/test_process_registry_streaming.py`

- [ ] **Step 1: Write failing ProcessRegistry tests**

Create `tests/unit/test_process_registry_streaming.py`:

```python
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from agent_server.services.process_registry import ProcessRegistry


class FakeProcess:
    pid = 12345
    returncode = None

    def poll(self):
        return self.returncode


@pytest.mark.asyncio
async def test_publish_log_entry_threadsafe_from_worker_thread():
    registry = ProcessRegistry()
    registry.register("exec-thread", FakeProcess())
    queue = registry.subscribe_logs("exec-thread")

    def publish():
        registry.publish_log_entry_threadsafe("exec-thread", {"type": "text", "part": {"text": "hi"}})

    thread = threading.Thread(target=publish)
    thread.start()
    thread.join(timeout=2)

    event = await asyncio.wait_for(queue.get(), timeout=2)
    assert event["type"] == "text"
    assert event["part"]["text"] == "hi"


@pytest.mark.asyncio
async def test_completed_buffer_replays_entries_and_stream_end():
    registry = ProcessRegistry()
    registry.register("exec-buffer", FakeProcess())
    registry.publish_log_entry("exec-buffer", {"type": "text", "part": {"text": "done"}})
    registry.unregister("exec-buffer")

    buffered = registry.get_buffered_logs("exec-buffer")
    assert buffered[-2]["part"]["text"] == "done"
    assert buffered[-1]["type"] == "stream_end"


def test_completed_buffer_global_cap_evicts_oldest():
    registry = ProcessRegistry()
    registry._completed_buffer_limit = 2
    for idx in range(3):
        execution_id = f"exec-{idx}"
        registry.register(execution_id, FakeProcess())
        registry.publish_log_entry(execution_id, {"type": "text", "part": {"text": str(idx)}})
        registry.unregister(execution_id)

    assert registry.get_buffered_logs("exec-0") is None
    assert registry.get_buffered_logs("exec-1") is not None
    assert registry.get_buffered_logs("exec-2") is not None
```

- [ ] **Step 2: Run tests to verify fail**

Run:

```bash
python -m pytest tests/unit/test_process_registry_streaming.py -v
```

Expected: FAIL because `publish_log_entry_threadsafe` and completed buffer retention are not implemented.

- [ ] **Step 3: Implement registry changes**

Modify `docker/base-image/agent_server/services/process_registry.py`:

1. Add fields in `__init__`:

```python
self._completed_buffer_limit = 100
self._completed_buffer_times: Dict[str, float] = {}
try:
    self._loop = asyncio.get_running_loop()
except RuntimeError:
    self._loop = None
```

2. Add helper methods inside `ProcessRegistry`:

```python
def _remember_loop(self):
    try:
        self._loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

def _cleanup_completed_buffers_locked(self):
    now = time.time()
    expired = [
        execution_id
        for execution_id, completed_at in self._completed_buffer_times.items()
        if now - completed_at > RECENTLY_COMPLETED_TTL_SECONDS
    ]
    for execution_id in expired:
        self._log_buffers.pop(execution_id, None)
        self._completed_buffer_times.pop(execution_id, None)

    while len(self._completed_buffer_times) > self._completed_buffer_limit:
        oldest = min(self._completed_buffer_times, key=self._completed_buffer_times.get)
        self._log_buffers.pop(oldest, None)
        self._completed_buffer_times.pop(oldest, None)

def publish_log_entry_threadsafe(self, execution_id: str, entry: dict):
    loop = self._loop
    if loop and loop.is_running():
        loop.call_soon_threadsafe(self.publish_log_entry, execution_id, entry)
    else:
        self.publish_log_entry(execution_id, entry)
```

3. In `register()`, call `self._remember_loop()` before acquiring or while holding the lock.

4. In `unregister()`, replace immediate buffer deletion with appending stream end and retaining buffer:

```python
stream_end = {"type": "stream_end"}
if execution_id in self._log_subscribers:
    for queue in self._log_subscribers[execution_id]:
        try:
            queue.put_nowait(stream_end)
        except asyncio.QueueFull:
            pass
    del self._log_subscribers[execution_id]

if execution_id in self._log_buffers:
    self._log_buffers[execution_id].append(stream_end)
    self._log_buffers[execution_id] = self._log_buffers[execution_id][-self._max_buffer_size:]
    self._completed_buffer_times[execution_id] = time.time()
    self._cleanup_completed_buffers_locked()
```

5. In `get_buffered_logs()`, call cleanup and return a copy of retained buffers.

- [ ] **Step 4: Run tests to verify pass**

Run:

```bash
python -m pytest tests/unit/test_process_registry_streaming.py -v
```

Expected: all tests pass.

---

## Task 4: OpenCode runtime live stdout/stderr streaming

**Files:**
- Modify: `docker/base-image/agent_server/services/opencode_runtime.py`
- Modify: `tests/unit/test_opencode_runtime.py`

- [ ] **Step 1: Add failing OpenCode streaming tests**

Append to `tests/unit/test_opencode_runtime.py`:

```python
@pytest.mark.asyncio
async def test_opencode_runtime_publishes_stdout_events_live(monkeypatch):
    from agent_server.services import opencode_runtime

    published = []

    class FakeRegistry:
        def register(self, execution_id, process, metadata=None):
            self.execution_id = execution_id

        def publish_log_entry_threadsafe(self, execution_id, entry):
            published.append((execution_id, entry))

        def unregister(self, execution_id):
            self.unregistered = execution_id

    class FakePipe:
        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return ''

    class FakeProcess:
        pid = 4444
        returncode = 0

        def __init__(self):
            self.stdout = FakePipe([
                '{"type":"text","part":{"id":"p1","type":"text","text":"hello"}}\n',
            ])
            self.stderr = FakePipe([])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return self.returncode


    fake_registry = FakeRegistry()
    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: fake_registry)
    monkeypatch.setattr(opencode_runtime.OpenCodeRuntime, "is_available", lambda self: True)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    runtime = opencode_runtime.OpenCodeRuntime()
    text, raw_messages, metadata, session_id = await runtime.execute_headless(
        prompt="hello",
        model="deepseek-openai/deepseek-v4-flash",
        execution_id="exec-live",
    )

    assert text == "hello"
    assert raw_messages[0]["part"]["text"] == "hello"
    assert published[0][0] == "exec-live"
    assert published[0][1]["part"]["text"] == "hello"


@pytest.mark.asyncio
async def test_opencode_runtime_drains_stderr_concurrently(monkeypatch):
    from agent_server.services import opencode_runtime

    stderr_read = []

    class FakeRegistry:
        def register(self, execution_id, process, metadata=None):
            pass
        def publish_log_entry_threadsafe(self, execution_id, entry):
            pass
        def unregister(self, execution_id):
            pass

    class FakePipe:
        def __init__(self, lines, marker=None):
            self._lines = list(lines)
            self._marker = marker
        def readline(self):
            if self._lines:
                line = self._lines.pop(0)
                if self._marker is not None:
                    self._marker.append(line)
                return line
            return ''

    class FakeProcess:
        pid = 5555
        returncode = 0
        def __init__(self):
            self.stdout = FakePipe(['{"type":"text","part":{"type":"text","text":"ok"}}\n'])
            self.stderr = FakePipe(['warn 1\n', 'warn 2\n'], stderr_read)
        def wait(self, timeout=None):
            return 0
        def poll(self):
            return self.returncode

    monkeypatch.setattr(opencode_runtime, "get_process_registry", lambda: FakeRegistry())
    monkeypatch.setattr(opencode_runtime.OpenCodeRuntime, "is_available", lambda self: True)
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(opencode_runtime, "kill_cgroup_orphans", lambda *args, **kwargs: 0)

    runtime = opencode_runtime.OpenCodeRuntime()
    text, _raw_messages, _metadata, _session_id = await runtime.execute_headless(
        prompt="hello",
        model="deepseek-openai/deepseek-v4-flash",
        execution_id="exec-stderr",
    )
    assert text == "ok"
    assert stderr_read == ['warn 1\n', 'warn 2\n']
```

- [ ] **Step 2: Run tests to verify fail**

Run:

```bash
python -m pytest tests/unit/test_opencode_runtime.py::test_opencode_runtime_publishes_stdout_events_live tests/unit/test_opencode_runtime.py::test_opencode_runtime_drains_stderr_concurrently -v
```

Expected: FAIL because OpenCode uses `communicate()` and does not publish events.

- [ ] **Step 3: Implement line-by-line OpenCode execution**

Modify `docker/base-image/agent_server/services/opencode_runtime.py` in `_run_opencode()`:

- keep command construction unchanged,
- keep process registry registration unchanged except use the updated registry,
- replace `process.communicate()` with stdout/stderr reader functions executed in worker threads inside `run_subprocess()`.

Implementation pattern:

```python
stdout_lines: List[str] = []
stderr_lines: List[str] = []
raw_messages: List[Dict] = []
stderr_limit = 200

def read_stdout():
    for line in iter(process.stdout.readline, ''):
        stdout_lines.append(line)
        try:
            event = json.loads(line.strip())
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event = sanitize_dict(event)
        raw_messages.append(event)
        try:
            registry.publish_log_entry_threadsafe(execution_id, event)
        except Exception as pub_err:
            logger.warning("[OpenCode] publish_log_entry failed: %s", pub_err)

def read_stderr():
    for line in iter(process.stderr.readline, ''):
        if len(stderr_lines) < stderr_limit:
            stderr_lines.append(line)
```

Start both threads, wait for the process with timeout, join threads briefly, classify non-zero/timeout as before, and parse `"".join(stdout_lines)` at the end. If `raw_messages` is already populated, the final returned raw messages should come from `parse_opencode_events()` output to preserve current sanitization and metadata behavior.

- [ ] **Step 4: Run OpenCode tests**

Run:

```bash
python -m pytest tests/unit/test_opencode_runtime.py -v
```

Expected: all OpenCode runtime tests pass.

---

## Task 5: ChatPanel live assistant draft

**Files:**
- Modify: `src/frontend/src/components/ChatPanel.vue`
- Reuse: `src/frontend/src/composables/useExecutionStream.js`
- Reuse: `src/frontend/src/utils/executionEventNormalizer.js`

- [ ] **Step 1: Add a manual test script case for streamed draft semantics**

Append to `src/frontend/scripts/test-execution-streaming.mjs`:

```js
function applyAssistantDraft(content, event) {
  if (event.kind !== 'assistant_text') return content
  return event.mode === 'snapshot' ? event.text : content + event.text
}

assert.equal(applyAssistantDraft('', { kind: 'assistant_text', text: 'Hel', mode: 'delta' }), 'Hel')
assert.equal(applyAssistantDraft('Hel', { kind: 'assistant_text', text: 'lo', mode: 'delta' }), 'Hello')
assert.equal(applyAssistantDraft('HelloHello', { kind: 'assistant_text', text: 'Hello', mode: 'snapshot' }), 'Hello')
```

- [ ] **Step 2: Run script to verify pass before component changes**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
```

Expected: pass.

- [ ] **Step 3: Modify ChatPanel imports**

Add imports near existing imports:

```js
import { useExecutionStream } from '@/composables/useExecutionStream'
import { normalizeExecutionEvent } from '@/utils/executionEventNormalizer'
```

- [ ] **Step 4: Replace local stream reader state**

Replace `let streamReader = null` and `closeSSE()` implementation with:

```js
let executionStream = null

const closeSSE = () => {
  if (executionStream) {
    executionStream.cancel()
    executionStream = null
  }
  clearHeartbeat()
}
```

- [ ] **Step 5: Replace `subscribeToStream()`**

Replace `subscribeToStream(executionId)` with:

```js
const subscribeToStream = (executionId, draftMessage) => {
  closeSSE()
  lastLabelTime = 0
  resetHeartbeat()
  let sequence = 0

  executionStream = useExecutionStream({
    agentName: props.agentName,
    executionId,
    token: authStore.token,
    onEvent(raw) {
      sequence += 1
      const events = normalizeExecutionEvent(raw, { sequence, executionId })
      for (const event of events) {
        if (event.kind === 'assistant_text') {
          if (event.mode === 'snapshot') {
            draftMessage.content = event.text
          } else {
            draftMessage.content += event.text
          }
          draftMessage.streaming = true
        } else if (event.kind === 'tool_start') {
          draftMessage.tools.push({ id: event.id, name: event.name, status: 'running' })
          updateLoadingText(`Using ${event.name}...`)
        } else if (event.kind === 'tool_result') {
          const tool = draftMessage.tools.find((item) => item.id === event.id)
          if (tool) tool.status = event.success === false ? 'failed' : 'done'
          updateLoadingText('Processing results...')
        } else if (event.kind === 'status' && event.label) {
          updateLoadingText(event.label)
        }
      }
    },
    onError(errorEvent) {
      console.warn('SSE stream error:', errorEvent.message)
    },
    onEnd() {
      clearHeartbeat()
    },
  })
  executionStream.start()
}
```

- [ ] **Step 6: Create assistant draft before subscribing**

After receiving `executionId`, before `subscribeToStream`, add:

```js
const assistantDraft = {
  role: 'assistant',
  content: '',
  timestamp: new Date().toISOString(),
  streaming: true,
  tools: [],
}
messages.value.push(assistantDraft)
subscribeToStream(executionId, assistantDraft)
```

Remove the old `subscribeToStream(executionId)` call.

- [ ] **Step 7: Reconcile final response without duplicate assistant message**

Change final success handling to update the draft:

```js
if (execution.status === 'success' && execution.response) {
  assistantDraft.content = execution.response
  assistantDraft.streaming = false
  assistantDraft.timestamp = new Date().toISOString()
} else if (execution.status === 'failed') {
  assistantDraft.streaming = false
  if (!assistantDraft.content) {
    messages.value = messages.value.filter((item) => item !== assistantDraft)
  }
  error.value = execution.error || 'Task execution failed'
} else if (execution.status === 'cancelled') {
  assistantDraft.streaming = false
  if (!assistantDraft.content) {
    messages.value = messages.value.filter((item) => item !== assistantDraft)
  }
  error.value = 'Task was cancelled'
}
```

- [ ] **Step 8: Run frontend build**

Run:

```bash
npm run build --prefix src/frontend
```

Expected: build succeeds with only existing chunk-size warnings.

---

## Task 6: TasksPanel async streaming rows

**Files:**
- Modify: `src/frontend/src/components/TasksPanel.vue`
- Reuse: `src/frontend/src/composables/useExecutionStream.js`
- Reuse: `src/frontend/src/utils/executionEventNormalizer.js`
- Reuse: `src/frontend/src/utils/executionLogParser.js`

- [ ] **Step 1: Modify imports**

Add:

```js
import { useExecutionStream } from '@/composables/useExecutionStream'
import { normalizeExecutionEvent } from '@/utils/executionEventNormalizer'
import { parseExecutionLog as parseSharedExecutionLog } from '@/utils/executionLogParser'
```

- [ ] **Step 2: Add stream registry state**

Near component state:

```js
const taskStreams = new Map()

const stopTaskStream = (executionId) => {
  const stream = taskStreams.get(executionId)
  if (stream) {
    stream.cancel()
    taskStreams.delete(executionId)
  }
}
```

- [ ] **Step 3: Add row update helper**

Add:

```js
const updateLocalTaskFromEvent = (task, event) => {
  task.latest_event_at = event.timestamp || new Date().toISOString()
  if (event.kind === 'assistant_text') {
    const current = task.response_preview || ''
    task.response_preview = event.mode === 'snapshot' ? event.text : current + event.text
  } else if (event.kind === 'tool_start') {
    task.current_tool = event.name
    task.tool_count = (task.tool_count || 0) + 1
    task.status_label = `Using ${event.name}...`
  } else if (event.kind === 'tool_result') {
    task.status_label = 'Processing results...'
  } else if (event.kind === 'status' && event.label) {
    task.status_label = event.label
  }
}
```

- [ ] **Step 4: Add stream subscription helper**

Add:

```js
const subscribeTaskStream = (task) => {
  if (!task.execution_id) return
  stopTaskStream(task.execution_id)
  let sequence = 0
  const stream = useExecutionStream({
    agentName: props.agentName,
    executionId: task.execution_id,
    token: authStore.token,
    onEvent(raw) {
      sequence += 1
      for (const event of normalizeExecutionEvent(raw, { sequence, executionId: task.execution_id })) {
        updateLocalTaskFromEvent(task, event)
      }
    },
    onError(errorEvent) {
      task.stream_warning = errorEvent.message
    },
    onEnd() {
      stopTaskStream(task.execution_id)
    },
  })
  taskStreams.set(task.execution_id, stream)
  stream.start()
}
```

- [ ] **Step 5: Change new task submit payload**

In `runNewTask()`, change payload to include:

```js
async_mode: true,
```

and treat the POST response as an accepted async execution:

```js
const executionId = response.data.execution_id || response.data.task_execution_id
localTask.execution_id = executionId
localTask.status = 'running'
localTask.status_label = 'Starting...'
subscribeTaskStream(localTask)
```

- [ ] **Step 6: Add completion polling for local async task**

Add:

```js
const pollTaskUntilTerminal = async (task) => {
  for (let attempt = 0; attempt < 360; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5000))
    const response = await axios.get(`/api/agents/${props.agentName}/executions/${task.execution_id}`, {
      headers: authStore.authHeader,
    })
    const execution = response.data
    if (['success', 'failed', 'cancelled'].includes(execution.status)) {
      task.status = execution.status
      task.response = execution.response
      task.response_preview = execution.response || task.response_preview
      task.error = execution.error
      stopTaskStream(task.execution_id)
      await loadTasks()
      return
    }
  }
}
```

Call it after `subscribeTaskStream(localTask)` without awaiting in the submit handler:

```js
pollTaskUntilTerminal(localTask).catch((err) => {
  localTask.stream_warning = err?.message || 'Task polling failed'
})
```

- [ ] **Step 7: Replace local static parser usage**

Where `parseExecutionLog()` is used for static logs, delegate to shared parser:

```js
const parseExecutionLog = (rawLog) => parseSharedExecutionLog(rawLog, { runtime: selectedRuntime.value })
```

If `selectedRuntime` is not available in this component, pass `{}`; the normalizer handles unknown runtime.

- [ ] **Step 8: Run frontend build**

Run:

```bash
npm run build --prefix src/frontend
```

Expected: build succeeds with only existing chunk-size warnings.

---

## Task 7: ExecutionDetail shared live transcript

**Files:**
- Modify: `src/frontend/src/views/ExecutionDetail.vue`
- Reuse: `src/frontend/src/composables/useExecutionStream.js`
- Reuse: `src/frontend/src/utils/executionEventNormalizer.js`
- Reuse: `src/frontend/src/utils/executionLogParser.js`

- [ ] **Step 1: Update imports**

Add:

```js
import { useExecutionStream } from '@/composables/useExecutionStream'
import { normalizeExecutionEvent } from '@/utils/executionEventNormalizer'
import { parseExecutionLog as parseSharedExecutionLog } from '@/utils/executionLogParser'
```

- [ ] **Step 2: Replace local stream reader with shared composable**

Replace local `streamReader` cancellation with:

```js
let executionStream = null

const stopStreaming = () => {
  if (executionStream) {
    executionStream.cancel()
    executionStream = null
  }
  isStreaming.value = false
}
```

- [ ] **Step 3: Replace `startStreaming()` event loop**

Use:

```js
const startStreaming = () => {
  if (!executionId.value || !agentName.value) return
  stopStreaming()
  isStreaming.value = true
  streamError.value = null
  streamingEntries.value = []
  let sequence = 0

  executionStream = useExecutionStream({
    agentName: agentName.value,
    executionId: executionId.value,
    token: authStore.token,
    onEvent(raw) {
      sequence += 1
      const events = normalizeExecutionEvent(raw, { sequence, executionId: executionId.value })
      streamingEntries.value.push(...events)
    },
    onError(errorEvent) {
      streamError.value = errorEvent.message || 'Stream error occurred'
    },
    onEnd() {
      isStreaming.value = false
      loadExecution()
    },
  })
  executionStream.start()
}
```

- [ ] **Step 4: Update computed transcript parser**

If currently streaming normalized entries, render them directly. For persisted raw logs, use shared parser:

```js
const logEntries = computed(() => {
  if (isStreaming.value && streamingEntries.value.length > 0) {
    return streamingEntries.value.map((event) => ({
      id: event.eventId,
      type: event.kind === 'assistant_text' ? 'assistant_text' : event.kind,
      content: event.text,
      tool: event.name,
      input: event.input,
      output: event.output,
      label: event.label,
      timestamp: event.timestamp,
      runtime: event.sourceRuntime,
    }))
  }
  return parseSharedExecutionLog(execution.value?.execution_log || [], { runtime: execution.value?.runtime })
})
```

- [ ] **Step 5: Remove hard-coded Claude labels**

Replace user-visible labels like `Claude` in the transcript with `Assistant` or `entry.runtime || 'Assistant'`.

- [ ] **Step 6: Run frontend build**

Run:

```bash
npm run build --prefix src/frontend
```

Expected: build succeeds with only existing chunk-size warnings.

---

## Task 8: Integration verification and deployment

**Files:**
- No new source files unless fixes are discovered during verification.

- [ ] **Step 1: Run local focused backend/agent tests**

Run:

```bash
python -m pytest tests/unit/test_process_registry_streaming.py tests/unit/test_opencode_runtime.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run frontend streaming tests**

Run:

```bash
node src/frontend/scripts/test-execution-streaming.mjs
npm run build --prefix src/frontend
```

Expected: script passes and build succeeds.

- [ ] **Step 3: Deploy changed files to remote**

Run from worktree root:

```bash
rsync -avR \
  docker/base-image/agent_server/services/process_registry.py \
  docker/base-image/agent_server/services/opencode_runtime.py \
  docker/base-image/agent_server/routers/chat.py \
  src/frontend/src/composables/useExecutionStream.js \
  src/frontend/src/utils/executionEventNormalizer.js \
  src/frontend/src/utils/executionLogParser.js \
  src/frontend/src/components/ChatPanel.vue \
  src/frontend/src/components/TasksPanel.vue \
  src/frontend/src/views/ExecutionDetail.vue \
  src/frontend/scripts/test-execution-streaming.mjs \
  tests/unit/test_process_registry_streaming.py \
  tests/unit/test_opencode_runtime.py \
  ubuntu-server:/home/sun/trinity/
```

Expected: files transfer without printing secrets.

- [ ] **Step 4: Rebuild remote frontend and base image**

Run:

```bash
ssh ubuntu-server 'cd /home/sun/trinity && docker compose up -d --build frontend && docker build -t trinity-agent-base:latest docker/base-image'
```

Expected: frontend rebuild succeeds; base image rebuild succeeds.

- [ ] **Step 5: Recreate OpenCode test agent container without printing secrets**

If a DB-managed OpenCode agent is available, restart it through the normal backend/UI lifecycle so the recreated container picks up `trinity-agent-base:latest`. For the current remote `agent-mo` test container, use this no-secret Docker-inspect based script on the remote host. It copies the existing container environment inside the remote shell and does not print env values:

```bash
ssh ubuntu-server 'python3 - <<'\''PY'\''
import json, os, subprocess, sys

source_name = "agent-mo"
inspect = subprocess.run(
    ["docker", "inspect", source_name],
    check=False,
    capture_output=True,
    text=True,
)
if inspect.returncode != 0:
    print("agent-mo does not exist; recreate a DB-managed OpenCode agent instead", file=sys.stderr)
    sys.exit(1)

src = json.loads(inspect.stdout)[0]
envs = src["Config"].get("Env", [])
labels = dict(src["Config"].get("Labels") or {})
ports = src["HostConfig"].get("PortBindings") or {}
host_port = ports.get("22/tcp", [{}])[0].get("HostPort", "2225")
network = src["HostConfig"].get("NetworkMode") or "trinity-agent-network"

subprocess.run(["docker", "rm", "-f", source_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

cmd = [
    "docker", "run", "-d",
    "--name", source_name,
    "--hostname", source_name,
    "--user", src["Config"].get("User") or "developer",
    "--workdir", src["Config"].get("WorkingDir") or "/home/developer",
    "--network", network,
    "-p", f"{host_port}:22",
]
for env in envs:
    cmd += ["-e", env]
for key, value in labels.items():
    cmd += ["--label", f"{key}={value}"]

for mount in src.get("Mounts", []):
    destination = mount.get("Destination")
    if mount.get("Type") == "volume" and destination in {"/home/developer", "/data"}:
        cmd += ["-v", f"{mount['Name']}:{destination}"]
    elif mount.get("Type") == "bind" and os.path.exists(mount.get("Source", "")):
        mode = ":ro" if not mount.get("RW", True) else ""
        cmd += ["-v", f"{mount['Source']}:{destination}{mode}"]

cmd += ["trinity-agent-base:latest", "/app/startup.sh"]
subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
print("agent-mo recreated with trinity-agent-base:latest")
PY'
```

Confirm:

```bash
ssh ubuntu-server 'docker ps --filter name=agent-mo --format "{{.Names}} {{.Status}} {{.Image}}" && docker exec agent-mo curl -fsS http://localhost:8000/api/model'
```

Expected: `agent-mo` is up on `trinity-agent-base:latest` and model is `deepseek-openai/deepseek-v4-flash`.

- [ ] **Step 6: Verify remote streaming path with deterministic command or real smoke**

Run a long-enough OpenCode task from the frontend that emits at least two text/tool events. Verify manually:

- ChatPanel shows assistant draft text before final completion.
- TasksPanel task row shows live preview/status/tool activity.
- ExecutionDetail shows live transcript while the execution is running.
- Final execution DB row has non-empty `response`, normalized `model_used`, and non-empty `execution_log`.

- [ ] **Step 7: Check remote errors**

Run:

```bash
ssh ubuntu-server 'docker logs trinity-backend --since 10m 2>&1 | python3 -c '\''import sys
for line in sys.stdin:
    if any(p in line for p in ("ProviderModelNotFoundError", "Traceback", "stream failed", "OpenCode execution failed")): print(line,end="")
'\'''
```

Expected: no new relevant errors.

---

## Self-Review Notes

- Spec coverage: plan covers shared frontend stream reader, normalizer/parser, ChatPanel, TasksPanel, ExecutionDetail, OpenCode live publishing, ProcessRegistry retention, thread-safe publishing, stderr draining, and remote verification.
- Gemini: explicitly scoped to parser compatibility in Phase 1; live publish is a follow-up task per revised spec.
- Correctness: final persisted response remains authoritative and polling remains in every UI path.
- Risk: OpenCode subprocess changes are the highest-risk part; implement after ProcessRegistry tests and before UI rollout.
