<script setup>
/**
 * Enterprise Audit Log dashboard (#941).
 *
 * Admin-facing read view over the platform audit log. Frontend ships
 * in the OSS bundle; route is gated by `requiresEntitlement: 'audit'`
 * in `router/index.js` so OSS-only deploys (where the entitlement
 * service does not register `'audit'`) bounce to the catalogue.
 *
 * v1 scope (#941): list + filter + detail panel. Out of scope here:
 * CSV/JSON export UI, hash-chain verify button, stats tiles, SIEM
 * push. The backend endpoints supporting those already exist; the UI
 * lands in follow-up issues.
 */
import { computed, onMounted, watch } from 'vue'
import { useAuditLogStore } from '../../stores/auditLog'

const store = useAuditLogStore()
// Direct reactive access — Pinia state is already reactive, and the
// template's v-model writes pass through to the store unchanged.
const filters = store.filters

onMounted(async () => {
  await store.loadDistinct()
  await store.loadList()
})

// Re-load list when offset changes (pagination clicks).
watch(
  () => store.offset,
  () => {
    store.loadList()
  }
)

function applyFilters() {
  store.offset = 0
  store.loadList()
}

function resetFilters() {
  store.resetFilters()
  store.loadDistinct(true)
  store.loadList()
}

async function openDetail(entry) {
  // Prefer the in-list payload, but refresh from the detail endpoint to
  // pick up any field the list response truncated. The server returns
  // the same row in both cases today, but keeping the detail call lets
  // us add lazy-loaded fields (e.g. raw `details` JSON) without
  // changing list payload shape later.
  store.selectEntry(entry)
  await store.loadDetail(entry.event_id)
}

function closeDetail() {
  store.clearSelection()
}

function formatTimestamp(ts) {
  if (!ts) return ''
  // Drop the millisecond + trailing Z for tighter table rows.
  return ts.replace('T', ' ').replace(/\.\d+/, '').replace(/Z$/, ' UTC')
}

function actorLabel(entry) {
  return entry.actor_email || entry.actor_id || `(${entry.actor_type})`
}

function targetLabel(entry) {
  if (!entry.target_type && !entry.target_id) return '—'
  if (!entry.target_id) return entry.target_type
  if (!entry.target_type) return entry.target_id
  return `${entry.target_type}/${entry.target_id}`
}

const detailsJson = computed(() => {
  const e = store.selectedEntry
  if (!e || !e.details) return ''
  if (typeof e.details === 'string') return e.details
  try {
    return JSON.stringify(e.details, null, 2)
  } catch {
    return String(e.details)
  }
})
</script>

<template>
  <div class="audit-dashboard p-6 max-w-7xl mx-auto">
    <header class="mb-6">
      <div class="flex items-center gap-3 mb-2">
        <router-link
          to="/enterprise"
          class="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
        >
          ← Enterprise
        </router-link>
      </div>
      <div class="flex items-center gap-3 mb-2">
        <h1 class="text-3xl font-semibold text-gray-900 dark:text-white">Audit Log</h1>
        <span class="px-2 py-0.5 text-xs font-bold rounded bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200">
          PRO
        </span>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Tamper-evident record of administrative actions. Default filter
        shows the last 24 hours.
      </p>
    </header>

    <!-- Filter form -->
    <section
      class="mb-4 p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
    >
      <h2 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-3">
        Filters
      </h2>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Event type</label
          >
          <select
            v-model="filters.event_type"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          >
            <option value="">All</option>
            <option v-for="t in store.distinctEventTypes" :key="t" :value="t">
              {{ t }}
            </option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Actor type</label
          >
          <select
            v-model="filters.actor_type"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          >
            <option value="">All</option>
            <option v-for="t in store.distinctActorTypes" :key="t" :value="t">
              {{ t }}
            </option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Actor ID</label
          >
          <input
            v-model="filters.actor_id"
            type="text"
            placeholder="user.id or agent_name"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Target type</label
          >
          <input
            v-model="filters.target_type"
            type="text"
            placeholder="agent / user / schedule…"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Start (ISO 8601 UTC)</label
          >
          <input
            v-model="filters.start_time"
            type="text"
            placeholder="2026-05-25T00:00:00Z"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >End (ISO 8601 UTC)</label
          >
          <input
            v-model="filters.end_time"
            type="text"
            placeholder="leave blank for now"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
      </div>
      <div class="mt-3 flex gap-2">
        <button
          class="px-3 py-1.5 text-sm font-medium rounded bg-blue-600 text-white hover:bg-blue-700"
          @click="applyFilters"
        >
          Apply
        </button>
        <button
          class="px-3 py-1.5 text-sm font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
          @click="resetFilters"
        >
          Reset
        </button>
        <span
          v-if="store.error"
          class="ml-auto text-xs text-red-600 dark:text-red-400 self-center"
        >
          {{ store.error }}
        </span>
      </div>
    </section>

    <!-- Table + detail layout -->
    <section class="flex gap-4">
      <div
        class="flex-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden"
        :class="store.selectedEntry ? 'lg:max-w-3xl' : ''"
      >
        <div v-if="store.loading" class="p-6 text-sm text-gray-500 text-center">
          Loading…
        </div>
        <div
          v-else-if="!store.entries.length"
          class="p-6 text-sm text-gray-500 text-center"
        >
          {{
            store.total === 0
              ? 'No audit entries match these filters.'
              : 'No results on this page.'
          }}
        </div>
        <table v-else class="w-full text-sm">
          <thead class="bg-gray-50 dark:bg-gray-900 text-left">
            <tr class="text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400">
              <th class="px-3 py-2 font-medium">Timestamp</th>
              <th class="px-3 py-2 font-medium">Event</th>
              <th class="px-3 py-2 font-medium">Actor</th>
              <th class="px-3 py-2 font-medium">Target</th>
              <th class="px-3 py-2 font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in store.entries"
              :key="entry.event_id"
              class="border-t border-gray-100 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700"
              :class="
                store.selectedEntry?.event_id === entry.event_id
                  ? 'bg-blue-50 dark:bg-blue-900/20'
                  : ''
              "
              @click="openDetail(entry)"
            >
              <td class="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-300 whitespace-nowrap">
                {{ formatTimestamp(entry.timestamp) }}
              </td>
              <td class="px-3 py-2 text-gray-900 dark:text-white">
                <span class="font-medium">{{ entry.event_type }}</span>
                <span class="ml-1 text-xs text-gray-500 dark:text-gray-400"
                  >· {{ entry.event_action }}</span
                >
              </td>
              <td class="px-3 py-2 text-gray-700 dark:text-gray-200">
                {{ actorLabel(entry) }}
              </td>
              <td class="px-3 py-2 text-gray-700 dark:text-gray-200">
                {{ targetLabel(entry) }}
              </td>
              <td class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
                {{ entry.source }}
              </td>
            </tr>
          </tbody>
        </table>
        <footer
          class="flex items-center justify-between px-4 py-2 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400"
        >
          <span>{{ store.rangeLabel }}</span>
          <span class="flex items-center gap-2">
            <button
              class="px-2 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-40"
              :disabled="!store.hasPrev || store.loading"
              @click="store.prevPage()"
            >
              ← Prev
            </button>
            <span>Page {{ store.page }} of {{ store.pageCount }}</span>
            <button
              class="px-2 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-40"
              :disabled="!store.hasNext || store.loading"
              @click="store.nextPage()"
            >
              Next →
            </button>
          </span>
        </footer>
      </div>

      <!-- Side detail panel -->
      <aside
        v-if="store.selectedEntry"
        class="hidden lg:block flex-1 max-w-lg rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 overflow-y-auto self-start"
      >
        <header class="flex items-start justify-between mb-3">
          <div>
            <h3 class="text-base font-medium text-gray-900 dark:text-white">
              {{ store.selectedEntry.event_type }} ·
              {{ store.selectedEntry.event_action }}
            </h3>
            <p class="text-xs text-gray-500 dark:text-gray-400 font-mono mt-0.5">
              {{ store.selectedEntry.event_id }}
            </p>
          </div>
          <button
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            aria-label="Close detail"
            @click="closeDetail"
          >
            ✕
          </button>
        </header>

        <dl class="grid grid-cols-3 gap-x-3 gap-y-1 text-xs mb-3">
          <dt class="text-gray-500 dark:text-gray-400">Timestamp</dt>
          <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
            {{ store.selectedEntry.timestamp }}
          </dd>

          <dt class="text-gray-500 dark:text-gray-400">Actor</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ store.selectedEntry.actor_type }} ·
            {{ actorLabel(store.selectedEntry) }}
          </dd>

          <template v-if="store.selectedEntry.actor_ip">
            <dt class="text-gray-500 dark:text-gray-400">Actor IP</dt>
            <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
              {{ store.selectedEntry.actor_ip }}
            </dd>
          </template>

          <template v-if="store.selectedEntry.mcp_key_name">
            <dt class="text-gray-500 dark:text-gray-400">MCP key</dt>
            <dd class="col-span-2 text-gray-900 dark:text-white">
              {{ store.selectedEntry.mcp_key_name }} ({{ store.selectedEntry.mcp_scope || 'unknown scope' }})
            </dd>
          </template>

          <dt class="text-gray-500 dark:text-gray-400">Target</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ targetLabel(store.selectedEntry) }}
          </dd>

          <dt class="text-gray-500 dark:text-gray-400">Source</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ store.selectedEntry.source }}
            <span
              v-if="store.selectedEntry.endpoint"
              class="text-gray-500 dark:text-gray-400 font-mono"
            >
              ({{ store.selectedEntry.endpoint }})
            </span>
          </dd>

          <template v-if="store.selectedEntry.request_id">
            <dt class="text-gray-500 dark:text-gray-400">Request</dt>
            <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
              {{ store.selectedEntry.request_id }}
            </dd>
          </template>
        </dl>

        <details class="mb-3" open>
          <summary class="text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer">
            Details JSON
          </summary>
          <pre
            class="mt-2 p-2 rounded bg-gray-50 dark:bg-gray-900 text-xs text-gray-800 dark:text-gray-200 overflow-x-auto"
          >{{ detailsJson || '(none)' }}</pre>
        </details>

        <details>
          <summary class="text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer">
            Hash chain
          </summary>
          <dl class="mt-2 text-xs grid grid-cols-3 gap-x-3 gap-y-1">
            <dt class="text-gray-500 dark:text-gray-400">previous_hash</dt>
            <dd class="col-span-2 font-mono break-all text-gray-900 dark:text-white">
              {{ store.selectedEntry.previous_hash || '(none)' }}
            </dd>
            <dt class="text-gray-500 dark:text-gray-400">entry_hash</dt>
            <dd class="col-span-2 font-mono break-all text-gray-900 dark:text-white">
              {{ store.selectedEntry.entry_hash || '(none)' }}
            </dd>
          </dl>
        </details>
      </aside>
    </section>
  </div>
</template>
