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
  const blockIndex = context.blockIndex
  const idSuffix = blockIndex === undefined ? '' : `:${blockIndex}`
  const eventId =
    (raw?.part?.id && `${raw.part.id}${idSuffix}`) ||
    (raw?.id && `${raw.id}${idSuffix}`) ||
    (raw?.message?.id && `${raw.message.id}${idSuffix}`) ||
    (raw?.messageID && `${raw.messageID}${idSuffix}`) ||
    raw?.sessionID && `${raw.sessionID}:${sequence}${idSuffix}` ||
    `${context.executionId || 'execution'}:${sequence}${idSuffix}`
  const event = {
    eventId,
    sequence,
    timestamp: pickTimestamp(raw),
    sourceRuntime: context.runtime || raw?.runtime || raw?.sourceRuntime || 'unknown',
    rawType: getRawType(raw),
  }
  if (blockIndex !== undefined) event.blockIndex = blockIndex
  return event
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

function collectedTexts(raw) {
  const texts = collectText(raw).filter((text) => typeof text === 'string' && text)
  if (texts.length === 2 && raw?.text === raw?.part?.text && texts[0] === raw.text && texts[1] === raw.text) {
    return [raw.text]
  }
  return texts
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
  for (const [blockIndex, block] of asArray(content).entries()) {
    if (!block || typeof block !== 'object') continue
    const blockContext = { ...context, blockIndex }
    if (block.type === 'text' && block.text) {
      output.push({ kind: 'assistant_text', text: block.text, mode: 'delta', ...baseEvent({ ...raw, id: block.id }, blockContext) })
    } else if (block.type === 'tool_use') {
      output.push({
        kind: 'tool_start',
        id: String(block.id || `${context.executionId || 'tool'}:${context.sequence}`),
        name: String(block.name || 'unknown'),
        input: block.input || {},
        ...baseEvent({ ...raw, id: block.id }, blockContext),
      })
    } else if (block.type === 'tool_result') {
      output.push({
        kind: 'tool_result',
        id: String(block.tool_use_id || block.id || `${context.executionId || 'tool'}:${context.sequence}`),
        name: String(block.name || 'unknown'),
        output: block.content,
        success: block.is_error === undefined ? undefined : !block.is_error,
        ...baseEvent({ ...raw, id: block.tool_use_id || block.id }, blockContext),
      })
    } else if (block.type === 'thinking') {
      output.push({ kind: 'status', label: 'Thinking...', ...baseEvent(raw, blockContext) })
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
      cost: usage.cost_usd ?? raw.cost_usd ?? raw.total_cost_usd,
      duration: usage.duration_ms ?? raw.duration_ms,
      message: raw.message || raw.result || raw.summary,
      ...baseEvent(raw, context),
    }]
  }

  const texts = collectedTexts(raw)
  if (texts.length > 0) {
    const mode = raw.snapshot === true || raw.is_snapshot === true || raw.part?.snapshot === true || raw.part?.is_snapshot === true ? 'snapshot' : 'delta'
    return texts.map((text, blockIndex) => {
      const textContext = texts.length > 1 ? { ...context, blockIndex } : context
      return { kind: 'assistant_text', text, mode, ...baseEvent(raw, textContext) }
    })
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
