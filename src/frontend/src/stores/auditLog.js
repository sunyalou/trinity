/**
 * Audit log dashboard store (#941).
 *
 * Domain state for `views/enterprise/Audit.vue` — the admin-facing
 * dashboard over the platform audit log. Kept separate from the
 * cross-cutting `enterprise.js` store (which only caches the
 * `enterprise_features` flag list) so domain concerns and entitlement
 * lookup don't get tangled.
 *
 * Endpoints consumed (all admin-only, public repo):
 *   GET /api/audit-log                        — filtered + paginated list
 *   GET /api/audit-log/{event_id}             — single entry detail
 *   GET /api/audit-log/distinct/event-types   — filter dropdown values
 *   GET /api/audit-log/distinct/actor-types   — filter dropdown values
 *
 * Default time window: last 24h (per #941 acceptance criteria).
 *
 * Backend endpoints are not enterprise-gated — they were shipped OSS
 * via SEC-001 / #20. Only the OSS-side dashboard route is
 * entitlement-gated (via the router guard's `requiresEntitlement: 'audit'`).
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

const DEFAULT_LIMIT = 50

function isoMinusHours(hours) {
  const d = new Date(Date.now() - hours * 3600 * 1000)
  return d.toISOString()
}

function emptyFilters() {
  return {
    event_type: '',
    actor_type: '',
    actor_id: '',
    target_type: '',
    start_time: isoMinusHours(24),
    end_time: '',
  }
}

export const useAuditLogStore = defineStore('auditLog', {
  state: () => ({
    entries: [],
    total: 0,
    limit: DEFAULT_LIMIT,
    offset: 0,
    filters: emptyFilters(),
    selectedEntry: null,
    distinctEventTypes: [],
    distinctActorTypes: [],
    distinctLoaded: false,
    loading: false,
    detailLoading: false,
    error: '',
  }),

  getters: {
    page: (state) => Math.floor(state.offset / state.limit) + 1,
    pageCount: (state) =>
      state.total === 0 ? 1 : Math.ceil(state.total / state.limit),
    hasNext: (state) => state.offset + state.limit < state.total,
    hasPrev: (state) => state.offset > 0,
    rangeLabel: (state) => {
      if (state.total === 0) return 'No entries'
      const start = state.offset + 1
      const end = Math.min(state.offset + state.entries.length, state.total)
      return `Showing ${start}–${end} of ${state.total}`
    },
  },

  actions: {
    _params() {
      const f = this.filters
      const p = { limit: this.limit, offset: this.offset }
      for (const k of [
        'event_type',
        'actor_type',
        'actor_id',
        'target_type',
        'start_time',
        'end_time',
      ]) {
        if (f[k]) p[k] = f[k]
      }
      return p
    },

    async loadList() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) {
        this.entries = []
        this.total = 0
        return
      }
      this.loading = true
      this.error = ''
      try {
        const r = await axios.get('/api/audit-log', {
          headers: authStore.authHeader,
          params: this._params(),
        })
        this.entries = Array.isArray(r.data?.entries) ? r.data.entries : []
        this.total = Number(r.data?.total) || 0
      } catch (e) {
        this.entries = []
        this.total = 0
        this.error =
          e?.response?.data?.detail ||
          e?.message ||
          'Failed to load audit entries'
      } finally {
        this.loading = false
      }
    },

    async loadDetail(eventId) {
      if (!eventId) {
        this.selectedEntry = null
        return
      }
      const authStore = useAuthStore()
      this.detailLoading = true
      try {
        const r = await axios.get(
          `/api/audit-log/${encodeURIComponent(eventId)}`,
          { headers: authStore.authHeader }
        )
        if (r.data) this.selectedEntry = r.data
      } catch (e) {
        // Leave `selectedEntry` intact — the caller usually set it from
        // the in-list row before calling loadDetail (optimistic display).
        // Wiping it on a transient detail-fetch failure would hide the
        // row the user just clicked.
        this.error =
          e?.response?.data?.detail || e?.message || 'Failed to load detail'
      } finally {
        this.detailLoading = false
      }
    },

    async loadDistinct(force = false) {
      if (this.distinctLoaded && !force) return
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      try {
        const [evts, actors] = await Promise.all([
          axios.get('/api/audit-log/distinct/event-types', {
            headers: authStore.authHeader,
          }),
          axios.get('/api/audit-log/distinct/actor-types', {
            headers: authStore.authHeader,
          }),
        ])
        this.distinctEventTypes = Array.isArray(evts.data) ? evts.data : []
        this.distinctActorTypes = Array.isArray(actors.data) ? actors.data : []
        this.distinctLoaded = true
      } catch (e) {
        // Distinct endpoints failing shouldn't break the dashboard —
        // fall back to free-text input (dropdowns render empty).
        this.distinctEventTypes = []
        this.distinctActorTypes = []
        this.distinctLoaded = true
      }
    },

    setFilter(key, value) {
      if (!(key in this.filters)) return
      this.filters[key] = value
      this.offset = 0
    },

    resetFilters() {
      this.filters = emptyFilters()
      this.offset = 0
      this.selectedEntry = null
    },

    setPage(page) {
      const target = Math.max(1, Math.min(page, this.pageCount))
      this.offset = (target - 1) * this.limit
    },

    nextPage() {
      if (this.hasNext) this.offset += this.limit
    },

    prevPage() {
      if (this.hasPrev) this.offset = Math.max(0, this.offset - this.limit)
    },

    selectEntry(entry) {
      this.selectedEntry = entry
    },

    clearSelection() {
      this.selectedEntry = null
    },
  },
})
