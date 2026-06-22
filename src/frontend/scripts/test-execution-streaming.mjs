import assert from 'node:assert/strict'
import {
  normalizeExecutionEvent,
  normalizeExecutionEvents,
} from '../src/utils/executionEventNormalizer.js'
import { parseExecutionLog } from '../src/utils/executionLogParser.js'
import { parseSseFrames, useExecutionStream } from '../src/composables/useExecutionStream.js'

function kinds(events) {
  return events.map((event) => event.kind)
}

function applyAssistantDraft(content, event) {
  if (event.kind !== 'assistant_text') return content
  return event.mode === 'snapshot' ? event.text : content + event.text
}

function coalesceAssistantEntries(entries, event) {
  if (event.kind !== 'assistant_text') return entries
  const last = entries[entries.length - 1]
  if (last?.type === 'assistant_text') {
    if (event.mode === 'snapshot') {
      last.content = event.text
      last.mode = event.mode
    } else if (last.mode !== 'snapshot' || event.eventId === last.id) {
      last.content += event.text
      last.mode = event.mode
    } else {
      entries.push({ id: event.eventId, type: 'assistant_text', content: event.text, mode: event.mode })
    }
  } else {
    entries.push({ id: event.eventId, type: 'assistant_text', content: event.text, mode: event.mode })
  }
  return entries
}

assert.equal(applyAssistantDraft('', { kind: 'assistant_text', text: 'Hel', mode: 'delta' }), 'Hel')
assert.equal(applyAssistantDraft('Hel', { kind: 'assistant_text', text: 'lo', mode: 'delta' }), 'Hello')
assert.equal(applyAssistantDraft('HelloHello', { kind: 'assistant_text', text: 'Hello', mode: 'snapshot' }), 'Hello')
assert.equal(applyAssistantDraft('Hello', { kind: 'status', label: 'Working...' }), 'Hello')
let coalesced = []
coalesceAssistantEntries(coalesced, { kind: 'assistant_text', eventId: 'snap-1', text: 'Hello', mode: 'snapshot' })
coalesceAssistantEntries(coalesced, { kind: 'assistant_text', eventId: 'snap-1', text: 'Hello world', mode: 'snapshot' })
assert.equal(coalesced.length, 1)
assert.equal(coalesced[0].content, 'Hello world')
assert.equal(coalesced[0].mode, 'snapshot')
coalesced = []
coalesceAssistantEntries(coalesced, { kind: 'assistant_text', eventId: 'delta-1', text: 'Hel', mode: 'delta' })
coalesceAssistantEntries(coalesced, { kind: 'assistant_text', eventId: 'delta-2', text: 'lo', mode: 'delta' })
assert.equal(coalesced.length, 1)
assert.equal(coalesced[0].content, 'Hello')

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

const repeatedTextBlocks = {
  type: 'message',
  content: [
    { type: 'text', text: 'again' },
    { type: 'text', text: 'again' },
  ],
}

const nestedSnapshotText = {
  type: 'text',
  part: { id: 'prt_snapshot', type: 'text', text: 'Full snapshot', is_snapshot: true },
}

const claudeAssistantWithoutBlockIds = {
  type: 'assistant',
  id: 'msg_same',
  message: {
    content: [
      { type: 'text', text: 'First' },
      { type: 'text', text: 'Second' },
      { type: 'tool_use', name: 'Write', input: { file_path: 'note.md' } },
    ],
  },
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

events = normalizeExecutionEvent(repeatedTextBlocks, { sequence: 6, runtime: 'gemini-cli' })
assert.deepEqual(kinds(events), ['assistant_text', 'assistant_text'])
assert.equal(events[0].text, 'again')
assert.equal(events[1].text, 'again')

events = normalizeExecutionEvent(nestedSnapshotText, { sequence: 7, runtime: 'opencode' })
assert.equal(events[0].kind, 'assistant_text')
assert.equal(events[0].mode, 'snapshot')

events = normalizeExecutionEvent(claudeAssistantWithoutBlockIds, { sequence: 8, runtime: 'claude-code' })
assert.deepEqual(kinds(events), ['assistant_text', 'assistant_text', 'tool_start'])
assert.equal(new Set(events.map((event) => event.eventId)).size, events.length)
assert.deepEqual(events.map((event) => event.blockIndex), [0, 1, 2])

const repeatedIdEvents = normalizeExecutionEvents([claudeAssistantWithoutBlockIds, claudeAssistantWithoutBlockIds], { runtime: 'claude-code' })
assert.equal(new Set(repeatedIdEvents.map((event) => `${event.sequence}:${event.eventId}`)).size, repeatedIdEvents.length)

const normalized = normalizeExecutionEvents([openCodeText, openCodeTool], { runtime: 'opencode' })
assert.deepEqual(kinds(normalized), ['assistant_text', 'tool_start'])

const transcript = parseExecutionLog([openCodeText, openCodeTool], { runtime: 'opencode' })
assert.equal(transcript[0].type, 'assistant_text')
assert.equal(transcript[0].content, 'Hello live stream')
assert.equal(transcript[1].type, 'tool_start')
assert.equal(transcript[1].tool, 'bash')

const metadataTranscript = parseExecutionLog([{
  type: 'result',
  model: 'claude-sonnet-4-6',
  usage: { input_tokens: 123, output_tokens: 45 },
  cost_usd: 0.0123,
  duration_ms: 4567,
  message: 'completed',
}], { runtime: 'claude-code' })
assert.equal(metadataTranscript[0].type, 'metadata')
assert.equal(metadataTranscript[0].model, 'claude-sonnet-4-6')
assert.deepEqual(metadataTranscript[0].tokens, { input: 123, output: 45 })
assert.equal(metadataTranscript[0].cost, 0.0123)
assert.equal(metadataTranscript[0].duration, 4567)
assert.equal(metadataTranscript[0].rawType, 'result')
assert.equal(metadataTranscript[0].message, 'completed')

const jsonStringTranscript = parseExecutionLog(JSON.stringify([openCodeText]), { runtime: 'opencode' })
assert.equal(jsonStringTranscript[0].type, 'assistant_text')
assert.equal(jsonStringTranscript[0].content, 'Hello live stream')

const staticSnapshotTranscript = parseExecutionLog([
  { type: 'text', part: { id: 'snap-static', type: 'text', text: 'Hello', is_snapshot: true } },
  { type: 'text', part: { id: 'snap-static', type: 'text', text: 'Hello world', is_snapshot: true } },
  { type: 'text', part: { id: 'delta-static-1', type: 'text', text: '!' } },
], { runtime: 'opencode' })
assert.equal(staticSnapshotTranscript.length, 1)
assert.equal(staticSnapshotTranscript[0].type, 'assistant_text')
assert.equal(staticSnapshotTranscript[0].content, 'Hello world!')
assert.equal(staticSnapshotTranscript[0].mode, 'delta')

const rawStringTranscript = parseExecutionLog('plain log text')
assert.equal(rawStringTranscript.length, 1)
assert.equal(rawStringTranscript[0].type, 'raw_text')
assert.equal(rawStringTranscript[0].content, 'plain log text')

const objectTranscript = parseExecutionLog({ type: 'legacy', value: 42 })
assert.equal(objectTranscript.length, 1)
assert.equal(objectTranscript[0].type, 'raw_json')
assert.equal(objectTranscript[0].content, JSON.stringify({ type: 'legacy', value: 42 }, null, 2))

assert.deepEqual(normalizeExecutionEvent('malformed', { sequence: 9 }), [])
assert.deepEqual(parseExecutionLog(null), [])

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

parsed = parseSseFrames('data: {"type":"one"}\r\n\r\ndata: {"type":"two"}\r\n\r\n')
assert.deepEqual(parsed.events.map((event) => event.type), ['one', 'two'])
assert.equal(parsed.buffer, '')

parsed = parseSseFrames('data:\t{"type":"tab-preserved"}\n\n')
assert.equal(parsed.events[0].type, 'tab-preserved')

parsed = parseSseFrames('data: {bad json}\n\n')
assert.deepEqual(parsed.events[0], { type: 'error', message: 'Malformed stream event', retryable: false })

parsed = parseSseFrames('dat', '')
parsed = parseSseFrames('a: {"type":"split-line"}\n\n', parsed.buffer)
assert.equal(parsed.events[0].type, 'split-line')

parsed = parseSseFrames(': keepalive\n\n\n\ndata: {"type":"after-empty"}\n\n')
assert.deepEqual(parsed.events.map((event) => event.type), ['after-empty'])

function sseChunk(event) {
  return new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)
}

function sseChunks(events) {
  return new TextEncoder().encode(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''))
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function createReader(chunks, options = {}) {
  let index = 0
  let readCount = 0
  return {
    cancelled: false,
    released: false,
    async read() {
      if (options.throwOnRead !== undefined && readCount === options.throwOnRead) {
        readCount += 1
        throw new Error('read failed')
      }
      readCount += 1
      if (this.cancelled) return { done: true }
      if (index >= chunks.length) return { done: true }
      return { done: false, value: chunks[index++] }
    },
    async cancel() {
      this.cancelled = true
    },
    releaseLock() {
      this.released = true
    },
  }
}

function okResponse(reader) {
  return { ok: true, status: 200, body: { getReader: () => reader } }
}

function waitTick() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

async function withMockedFetch(mockFetch, fn) {
  const originalFetch = globalThis.fetch
  globalThis.fetch = mockFetch
  try {
    await fn()
  } finally {
    globalThis.fetch = originalFetch
  }
}

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'stream_end' })])), async () => {
  let endCount = 0
  const stream = useExecutionStream({ agentName: 'agent', executionId: 'exec', token: 'tok', onEnd: () => { endCount += 1 } })
  await stream.start()
  assert.equal(endCount, 1)
})

await withMockedFetch(async () => okResponse(createReader([sseChunks([
  { type: 'text', value: 'first' },
  { type: 'text', value: 'second' },
  { type: 'stream_end' },
])])), async () => {
  const received = []
  let endCount = 0
  let stream
  stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onEvent: (event) => {
      received.push(event.value)
      stream.cancel()
    },
    onEnd: () => { endCount += 1 },
  })
  await stream.start()
  assert.deepEqual(received, ['first'])
  assert.equal(endCount, 0)
})

await withMockedFetch(async (_url, options) => {
  return okResponse(createReader([sseChunk({ type: 'text' }), sseChunk({ type: 'stream_end' })], { signal: options.signal }))
}, async () => {
  const reader = createReader([sseChunk({ type: 'text' }), sseChunk({ type: 'stream_end' })])
  let aborted = false
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onEvent() {
      throw new Error('should not receive events after cancel')
    },
    onEnd() {
      throw new Error('should not end after cancel')
    },
  })
  globalThis.fetch = async (_url, options) => {
    options.signal.addEventListener('abort', () => { aborted = true })
    return okResponse(reader)
  }
  const promise = stream.start()
  stream.cancel()
  await promise
  assert.equal(aborted, true)
})

await withMockedFetch(async () => ({ ok: false, status: 404 }), async () => {
  let fetchCount = 0
  let errorCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    return { ok: false, status: 404 }
  }
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onError: () => { errorCount += 1 },
  })
  await stream.start()
  assert.equal(fetchCount, 6)
  assert.equal(errorCount, 6)
})

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'error', retryable: true, message: 'try again' })])), async () => {
  let fetchCount = 0
  let errorCount = 0
  const readers = []
  globalThis.fetch = async () => {
    fetchCount += 1
    const reader = fetchCount === 1
      ? createReader([sseChunk({ type: 'error', retryable: true, message: 'try again' })])
      : createReader([sseChunk({ type: 'stream_end' })])
    readers.push(reader)
    return okResponse(reader)
  }
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onError: () => { errorCount += 1 },
  })
  await stream.start()
  assert.equal(fetchCount, 2)
  assert.equal(errorCount, 1)
  assert.equal(readers[0].cancelled, true)
  assert.equal(readers[0].released, true)
})

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'stream_end' })])), async () => {
  let fetchCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    if (fetchCount === 1) throw new Error('network down')
    return okResponse(createReader([sseChunk({ type: 'stream_end' })]))
  }
  const errors = []
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onError: (error) => errors.push(error),
  })
  await stream.start()
  assert.equal(fetchCount, 2)
  assert.equal(errors[0].retryable, true)
})

await withMockedFetch(async () => okResponse(createReader([], { throwOnRead: 0 })), async () => {
  let fetchCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    if (fetchCount === 1) return okResponse(createReader([], { throwOnRead: 0 }))
    return okResponse(createReader([sseChunk({ type: 'stream_end' })]))
  }
  const stream = useExecutionStream({ agentName: 'agent', executionId: 'exec', token: 'tok' })
  await stream.start()
  assert.equal(fetchCount, 2)
})

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'error', retryable: true, message: 'try again' })])), async () => {
  let fetchCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    return okResponse(createReader([sseChunk({ type: 'error', retryable: true, message: 'try again' })]))
  }
  let endCount = 0
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onEnd: () => { endCount += 1 },
  })
  const promise = stream.start()
  await waitTick()
  const started = Date.now()
  stream.cancel()
  await promise
  assert.equal(fetchCount, 1)
  assert.equal(endCount, 0)
  assert.ok(Date.now() - started < 100)
})

await withMockedFetch(async () => okResponse(createReader([])), async () => {
  const pendingRead = deferred()
  const reader = {
    cancelled: false,
    released: false,
    read() {
      return pendingRead.promise
    },
    async cancel() {
      this.cancelled = true
      pendingRead.resolve({ done: true })
    },
    releaseLock() {
      this.released = true
    },
  }
  globalThis.fetch = async () => okResponse(reader)
  const stream = useExecutionStream({ agentName: 'agent', executionId: 'exec', token: 'tok' })
  const promise = stream.start()
  await waitTick()
  stream.cancel()
  await promise
  assert.equal(reader.cancelled, true)
  assert.equal(reader.released, true)
})

await withMockedFetch(async () => okResponse(createReader([], { throwOnRead: 0 })), async () => {
  let fetchCount = 0
  let errorCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    return okResponse(createReader([], { throwOnRead: 0 }))
  }
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onError: () => { errorCount += 1 },
  })
  await stream.start()
  assert.equal(fetchCount, 6)
  assert.equal(errorCount, 6)
})

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'text' })], { throwOnRead: 1 })), async () => {
  let fetchCount = 0
  let errorCount = 0
  let eventCount = 0
  globalThis.fetch = async () => {
    fetchCount += 1
    return okResponse(createReader([sseChunk({ type: 'text' })], { throwOnRead: 1 }))
  }
  const stream = useExecutionStream({
    agentName: 'agent',
    executionId: 'exec',
    token: 'tok',
    onEvent: () => { eventCount += 1 },
    onError: () => { errorCount += 1 },
  })
  await stream.start()
  assert.equal(fetchCount, 6)
  assert.equal(eventCount, 6)
  assert.equal(errorCount, 6)
})

await withMockedFetch(async () => okResponse(createReader([sseChunk({ type: 'text' }), sseChunk({ type: 'stream_end' })])), async () => {
  const unhandled = []
  function onUnhandled(reason) {
    unhandled.push(reason)
  }
  process.on('unhandledRejection', onUnhandled)
  try {
    const stream = useExecutionStream({
      agentName: 'agent',
      executionId: 'exec',
      token: 'tok',
      onEvent: async () => { throw new Error('async event failed') },
      onError: async () => { throw new Error('async error failed') },
      onEnd: async () => { throw new Error('async end failed') },
    })
    await stream.start()
    await waitTick()
    assert.deepEqual(unhandled, [])
  } finally {
    process.off('unhandledRejection', onUnhandled)
  }
})

await waitTick()

console.log('execution streaming tests passed')
