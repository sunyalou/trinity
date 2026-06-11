/**
 * Lightweight capped ring buffer of recent console errors/warnings and
 * uncaught errors, for the in-app bug reporter (#1116).
 *
 * Installed once, early in app bootstrap, so the Help widget can read the
 * last N entries at report time. Entries are stored RAW here and scrubbed at
 * read time (`getRecentLogs`) so the scrub logic lives in one place
 * (`utils/scrub.js`) and a future viewer can choose its own policy.
 *
 * Deliberately minimal: no deps, bounded memory, never throws into the app's
 * own console path (a logging buffer must not break the thing it observes).
 */
import { scrubText } from './scrub'

const MAX_ENTRIES = 50
const MAX_LEN = 1000 // per-entry char cap — stack traces can be huge

const buffer = []
let installed = false

function push(level, parts) {
  try {
    const text = parts
      .map((p) => {
        if (p instanceof Error) return `${p.name}: ${p.message}\n${p.stack || ''}`
        if (typeof p === 'string') return p
        try {
          return JSON.stringify(p)
        } catch {
          return String(p)
        }
      })
      .join(' ')
      .slice(0, MAX_LEN)
    buffer.push({ level, ts: new Date().toISOString(), text })
    if (buffer.length > MAX_ENTRIES) buffer.shift()
  } catch {
    /* never let capture break the caller */
  }
}

/**
 * Install console + window error hooks. Idempotent. Call once at bootstrap,
 * before app.mount, so early errors are captured too.
 */
export function installConsoleBuffer() {
  if (installed || typeof window === 'undefined') return
  installed = true

  for (const level of ['error', 'warn']) {
    const original = console[level]
    console[level] = function (...args) {
      push(level, args)
      return original.apply(this, args)
    }
  }

  window.addEventListener('error', (e) => {
    push('error', [e.message, e.error || `${e.filename}:${e.lineno}:${e.colno}`])
  })
  window.addEventListener('unhandledrejection', (e) => {
    push('error', ['Unhandled promise rejection:', e.reason])
  })
}

/**
 * Return the most recent captured entries, newest last, each scrubbed.
 * @param {number} limit
 * @returns {Array<{level: string, ts: string, text: string}>}
 */
export function getRecentLogs(limit = MAX_ENTRIES) {
  return buffer.slice(-limit).map((e) => ({ ...e, text: scrubText(e.text) }))
}
