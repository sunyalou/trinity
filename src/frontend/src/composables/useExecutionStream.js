export function parseSseFrames(chunk, existingBuffer = '') {
  const combined = `${existingBuffer}${chunk || ''}`
  const frames = combined.split(/\r?\n\r?\n/)
  const buffer = frames.pop() || ''
  const events = []

  for (const frame of frames) {
    const dataLines = []
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue
      if (line.startsWith('data:')) {
        const value = line.slice(5)
        dataLines.push(value.startsWith(' ') ? value.slice(1) : value)
      }
    }
    if (dataLines.length === 0) continue

    try {
      events.push(JSON.parse(dataLines.join('\n')))
    } catch (_err) {
      events.push({ type: 'error', message: 'Malformed stream event', retryable: false })
    }
  }

  return { events, buffer }
}

function delay(ms, signal) {
  if (signal?.aborted) return Promise.resolve(false)

  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener?.('abort', onAbort)
      resolve(true)
    }, ms)

    function onAbort() {
      clearTimeout(timer)
      resolve(false)
    }

    signal?.addEventListener?.('abort', onAbort, { once: true })
  })
}

function retryDelay(attempt) {
  return Math.min(2000, 250 * 2 ** attempt)
}

export function useExecutionStream({ agentName, executionId, token, onEvent, onError, onEnd }) {
  let cancelled = false
  let ended = false
  let reader = null
  let running = false
  let abortController = null
  let currentPromise = null
  const maxRetries = 5

  function handleCallbackPromise(result, onReject) {
    if (!result || typeof result.then !== 'function') return
    result.catch((err) => {
      try {
        onReject?.(err)
      } catch (_err) {
        // Callback rejection handlers must not create unhandled failures.
      }
    })
  }

  function safeOnError(error) {
    try {
      handleCallbackPromise(onError?.(error))
    } catch (_err) {
      // Callback exceptions should not create unhandled stream rejections.
    }
  }

  function safeOnEvent(event) {
    try {
      handleCallbackPromise(onEvent?.(event), (err) => {
        safeOnError({ message: err?.message || 'Stream event callback failed', retryable: false })
      })
    } catch (err) {
      safeOnError({ message: err?.message || 'Stream event callback failed', retryable: false })
    }
  }

  function finish() {
    if (cancelled || ended) return
    ended = true
    try {
      handleCallbackPromise(onEnd?.(), (err) => {
        safeOnError({ message: err?.message || 'Stream end callback failed', retryable: false })
      })
    } catch (err) {
      safeOnError({ message: err?.message || 'Stream end callback failed', retryable: false })
    }
  }

  async function closeReader() {
    const activeReader = reader
    reader = null
    if (!activeReader) return
    try {
      await activeReader.cancel?.()
    } catch (_err) {
      // Ignore cancellation failures.
    }
    try {
      activeReader.releaseLock?.()
    } catch (_err) {
      // Ignore release failures.
    }
  }

  async function connect() {
    let retryCount = 0

    try {
      while (!cancelled) {
        try {
          abortController = new AbortController()
          const response = await fetch(
            `/api/agents/${encodeURIComponent(agentName)}/executions/${encodeURIComponent(executionId)}/stream`,
            {
              signal: abortController.signal,
              headers: {
                Authorization: `Bearer ${token}`,
                Accept: 'text/event-stream',
              },
            },
          )

          if (!response.ok) {
            const retryable = response.status === 404
            safeOnError({ message: `Stream failed with HTTP ${response.status}`, retryable })
            if (!retryable || retryCount >= maxRetries) break
            const waited = await delay(retryDelay(retryCount), abortController.signal)
            retryCount += 1
            if (!waited || cancelled) break
            continue
          }

          if (!response.body) {
            safeOnError({ message: 'Stream response has no body', retryable: false })
            break
          }

          reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          let shouldRetry = false

          while (!cancelled) {
            const { done, value } = await reader.read()
            if (done) break
            if (cancelled) break

            const parsed = parseSseFrames(decoder.decode(value, { stream: true }), buffer)
            buffer = parsed.buffer

            for (const event of parsed.events) {
              if (cancelled) break
              if (event?.type === 'stream_end') {
                if (cancelled) break
                finish()
                return
              }

              if (cancelled) break
              if (event?.type === 'error') {
                if (cancelled) break
                safeOnError(event)
                if (cancelled) break
                if (event.retryable && retryCount < maxRetries) {
                  await closeReader()
                  const waited = await delay(retryDelay(retryCount), abortController.signal)
                  retryCount += 1
                  if (!waited || cancelled) break
                  shouldRetry = true
                  break
                }
                continue
              }

              if (cancelled) break
              safeOnEvent(event)
              if (cancelled) break
            }

            if (shouldRetry) break
          }

          if (shouldRetry) continue
          break
        } catch (err) {
          if (cancelled) break
          const retryable = retryCount < maxRetries
          safeOnError({ message: err?.message || 'Stream connection failed', retryable })
          if (!retryable) break
          const waited = await delay(retryDelay(retryCount), abortController?.signal)
          retryCount += 1
          if (!waited || cancelled) break
        } finally {
          try {
            reader?.releaseLock?.()
          } catch (_err) {
            // Ignore release failures.
          }
          reader = null
          abortController = null
        }
      }
    } finally {
      running = false
      finish()
    }
  }

  return {
    start() {
      if (running) return currentPromise
      cancelled = false
      ended = false
      running = true
      currentPromise = connect()
      return currentPromise
    },
    cancel() {
      cancelled = true
      abortController?.abort()
      if (reader) reader.cancel().catch(() => {})
    },
  }
}
