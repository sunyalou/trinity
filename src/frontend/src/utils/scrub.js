/**
 * Client-side secret/PII scrubber for in-app bug reports (#1116).
 *
 * Bug reports land as PUBLIC GitHub issues in abilityai/trinity, so console
 * logs and any free text must be scrubbed of credentials and PII BEFORE they
 * leave the browser. This is the first line of defense; the hosted intake
 * service runs a secondary server-side scrub (issues are public + indexed).
 *
 * Patterns deliberately err toward over-redaction — a redacted token is a
 * non-event, a leaked one is permanent and search-indexed.
 */

// Order matters: longer/more-specific token shapes first so a generic
// fallback can't partially mask a known prefix.
const PATTERNS = [
  // Authorization: Bearer <jwt/opaque> — keep the scheme, drop the value.
  [/\b(Bearer)\s+[A-Za-z0-9._\-+/=]+/gi, '$1 [REDACTED]'],
  // Trinity MCP API keys.
  [/\btrinity_mcp_[A-Za-z0-9._\-]+/g, 'trinity_mcp_[REDACTED]'],
  // Anthropic / OpenAI-style keys.
  [/\bsk-[A-Za-z0-9._\-]{8,}/g, 'sk-[REDACTED]'],
  // GitHub PATs (classic + fine-grained) and other gh* token prefixes.
  [/\bgh[pousr]_[A-Za-z0-9]{8,}/g, 'gh_[REDACTED]'],
  [/\bgithub_pat_[A-Za-z0-9_]{8,}/g, 'github_pat_[REDACTED]'],
  // Slack bot/user/app tokens.
  [/\bxox[baprs]-[A-Za-z0-9-]{8,}/g, 'xox_[REDACTED]'],
  // Google API keys.
  [/\bAIza[A-Za-z0-9._\-]{10,}/g, 'AIza[REDACTED]'],
  // JWT triplets (header.payload.signature) not already caught by Bearer.
  [/\beyJ[A-Za-z0-9._\-]+\.[A-Za-z0-9._\-]+\.[A-Za-z0-9._\-]+/g, '[REDACTED_JWT]'],
  // Email addresses (PII).
  [/\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b/g, '[REDACTED_EMAIL]'],
  // Private / internal IPv4 ranges (10/8, 172.16/12, 192.168/16, 127/8).
  [/\b(?:10|127)(?:\.\d{1,3}){3}\b/g, '[REDACTED_IP]'],
  [/\b192\.168(?:\.\d{1,3}){2}\b/g, '[REDACTED_IP]'],
  [/\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b/g, '[REDACTED_IP]'],
  // Generic key=value / "key": "value" secrets for common secret-y names.
  [/\b(api[_-]?key|token|secret|password|passwd|auth)["']?\s*[:=]\s*["']?[^\s"',&]{6,}/gi,
   '$1=[REDACTED]'],
]

/**
 * Scrub a string. Returns '' for nullish input.
 * @param {string} input
 * @returns {string}
 */
export function scrubText(input) {
  if (input == null) return ''
  let out = String(input)
  for (const [re, repl] of PATTERNS) {
    out = out.replace(re, repl)
  }
  return out
}

/**
 * Deep-scrub a JSON-serializable value (strings, arrays, plain objects).
 * Object KEYS are preserved; only string values are scrubbed.
 * @param {*} value
 * @returns {*}
 */
export function scrubDeep(value) {
  if (typeof value === 'string') return scrubText(value)
  if (Array.isArray(value)) return value.map(scrubDeep)
  if (value && typeof value === 'object') {
    const out = {}
    for (const k of Object.keys(value)) out[k] = scrubDeep(value[k])
    return out
  }
  return value
}
