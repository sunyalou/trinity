/**
 * Assemble the diagnostic payload attached to an in-app bug report (#1116).
 *
 * Everything here is shown to the user BEFORE sending (the widget renders a
 * collapsible preview) and is scrubbed: console logs via the ring buffer's
 * read-time scrub, and the route/URL via `scrubText` (a path or query string
 * can carry an email or token). The version block comes from `GET /api/version`
 * (build provenance, #926) which the widget already has via `useBuildInfo`.
 */
import { getRecentLogs } from './consoleBuffer'
import { scrubText } from './scrub'

/**
 * @param {object} opts
 * @param {object|null} opts.versionInfo  result of GET /api/version (or null)
 * @param {object} opts.route             vue-router route (uses fullPath/name)
 * @param {number} [opts.logLimit=20]
 * @returns {object} plain JSON-serializable diagnostics
 */
export function gatherDiagnostics({ versionInfo, route, logLimit = 20 }) {
  const v = versionInfo || {}
  const nav = typeof navigator !== 'undefined' ? navigator : {}
  const win = typeof window !== 'undefined' ? window : {}

  return {
    app: {
      version: v.version || 'unknown',
      git_commit: v.git_commit_short || v.git_commit || 'unknown',
      git_branch: v.git_branch || 'unknown',
      build_date: v.build_date || 'unknown',
    },
    location: {
      // route.fullPath is the in-app route; href is the absolute URL. Both
      // scrubbed — a query param can carry a token or email.
      route: scrubText(route?.fullPath || ''),
      route_name: route?.name ? String(route.name) : '',
      href: scrubText(win.location?.href || ''),
    },
    browser: {
      user_agent: nav.userAgent || 'unknown',
      language: nav.language || 'unknown',
      platform: nav.userAgentData?.platform || nav.platform || 'unknown',
      viewport: {
        width: win.innerWidth || 0,
        height: win.innerHeight || 0,
        dpr: win.devicePixelRatio || 1,
      },
    },
    console_logs: getRecentLogs(logLimit),
    captured_at: new Date().toISOString(),
  }
}
