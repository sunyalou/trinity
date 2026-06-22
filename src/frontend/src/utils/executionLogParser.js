import { normalizeExecutionEvents } from './executionEventNormalizer.js'

function fallbackEntry(type, content) {
  return [{
    id: `${type}:0`,
    type,
    content,
    timestamp: new Date().toISOString(),
    runtime: 'unknown',
  }]
}

export function parseExecutionLog(rawLog, options = {}) {
  let rawEvents = rawLog

  if (typeof rawLog === 'string') {
    try {
      const parsed = JSON.parse(rawLog)
      if (Array.isArray(parsed)) {
        rawEvents = parsed
      } else if (parsed && typeof parsed === 'object') {
        return fallbackEntry('raw_json', JSON.stringify(parsed, null, 2))
      } else {
        return fallbackEntry('raw_text', rawLog)
      }
    } catch (_err) {
      return fallbackEntry('raw_text', rawLog)
    }
  }

  if (rawEvents && typeof rawEvents === 'object' && !Array.isArray(rawEvents)) {
    return fallbackEntry('raw_json', JSON.stringify(rawEvents, null, 2))
  }

  rawEvents = Array.isArray(rawEvents) ? rawEvents : []
  const normalized = normalizeExecutionEvents(rawEvents, options)
  const entries = []
  for (const event of normalized) {
    if (event.kind === 'assistant_text') {
      appendAssistantTextEntry(entries, {
        id: event.eventId,
        type: 'assistant_text',
        content: event.text,
        mode: event.mode,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      })
      continue
    }
    if (event.kind === 'tool_start') {
      entries.push({
        id: event.eventId,
        type: 'tool_start',
        tool: event.name,
        input: event.input,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      })
      continue
    }
    if (event.kind === 'tool_result') {
      entries.push({
        id: event.eventId,
        type: 'tool_result',
        tool: event.name,
        output: event.output,
        success: event.success,
        timestamp: event.timestamp,
        runtime: event.sourceRuntime,
      })
      continue
    }
    entries.push({
      id: event.eventId,
      type: event.kind,
      label: event.label,
      model: event.model,
      tokens: event.tokens,
      cost: event.cost,
      duration: event.duration,
      message: event.message,
      timestamp: event.timestamp,
      runtime: event.sourceRuntime,
      rawType: event.rawType,
    })
  }
  return entries
}

function appendAssistantTextEntry(entries, entry) {
  const last = entries[entries.length - 1]
  if (last?.type !== 'assistant_text') {
    entries.push(entry)
    return
  }

  if (entry.mode === 'snapshot') {
    entries[entries.length - 1] = { ...last, ...entry, content: entry.content }
    return
  }

  entries[entries.length - 1] = {
    ...last,
    content: `${last.content || ''}${entry.content || ''}`,
    mode: entry.mode,
    timestamp: entry.timestamp,
    runtime: entry.runtime,
  }
}
