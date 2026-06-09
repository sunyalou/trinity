import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

// Sequential agent loops UI store (#1106 / #740 Phase 2).
//
// Domain-scoped, agent-at-a-time: LoopsPanel mounts per agent and calls
// setAgent(name) — the store tracks that name so the GLOBAL WebSocket handler
// (loop events are broadcast fleet-wide, unfiltered) only reacts to events for
// the agent currently on screen. All HTTP goes through the shared api.js client
// (Invariant #7) so auth + 401→/login handling is inherited.
const ACTIVE_STATUSES = ['queued', 'running']
const POLL_INTERVAL_MS = 12000

export const useLoopsStore = defineStore('loops', () => {
  // --- state ---
  const loops = ref([])            // LoopStatusResponse[] for the mounted agent
  const agentName = ref(null)      // the agent LoopsPanel is currently showing
  const loading = ref(false)
  const starting = ref(false)
  const error = ref(null)
  const expandedLoopId = ref(null) // kept in the store so it survives tab remount
  const stoppingIds = ref([])      // loop_ids with an in-flight stop request

  let _pollTimer = null
  const _loadInFlight = new Set()  // per-loop loadLoop() guard

  // --- getters ---
  const hasActiveLoops = computed(() =>
    loops.value.some((l) => ACTIVE_STATUSES.includes(l.status))
  )

  // --- helpers ---
  function _upsertLoop(loop) {
    const idx = loops.value.findIndex((l) => l.loop_id === loop.loop_id)
    if (idx === -1) {
      loops.value = [loop, ...loops.value]
    } else {
      loops.value.splice(idx, 1, loop)
    }
  }

  function _ensurePolling() {
    // Backstop for a missed loop_completed event: while any loop is active,
    // poll the list so a dropped WS event can't leave the UI spinning forever.
    if (hasActiveLoops.value) {
      if (!_pollTimer) _pollTimer = setInterval(fetchLoops, POLL_INTERVAL_MS)
    } else {
      stopPolling()
    }
  }

  // --- actions ---
  function setAgent(name) {
    if (agentName.value !== name) {
      agentName.value = name
      loops.value = []
      error.value = null
    }
  }

  async function fetchLoops() {
    if (!agentName.value || loading.value) return
    loading.value = true
    error.value = null
    try {
      const res = await api.get(`/api/agents/${agentName.value}/loops`, {
        params: { limit: 50 },
      })
      loops.value = res.data
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      loading.value = false
      _ensurePolling()
    }
  }

  async function startLoop(payload) {
    if (!agentName.value) return
    starting.value = true
    error.value = null
    try {
      await api.post(`/api/agents/${agentName.value}/loops`, payload)
      await fetchLoops()
      return true
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
      return false
    } finally {
      starting.value = false
    }
  }

  // Targeted single-loop refresh — cheaper than refetching the whole list and
  // avoids list reorder flicker. Used by the WS handler and after stop.
  async function loadLoop(loopId) {
    if (_loadInFlight.has(loopId)) return
    _loadInFlight.add(loopId)
    try {
      const res = await api.get(`/api/loops/${loopId}`)
      _upsertLoop(res.data)
    } catch {
      // A 403/404 here (e.g. loop gone) shouldn't wedge the panel — the next
      // list poll reconciles. Swallow.
    } finally {
      _loadInFlight.delete(loopId)
      _ensurePolling()
    }
  }

  async function stopLoop(loopId) {
    if (stoppingIds.value.includes(loopId)) return
    stoppingIds.value = [...stoppingIds.value, loopId]
    error.value = null
    try {
      await api.post(`/api/loops/${loopId}/stop`)
      // Cooperative stop: the loop finishes its current iteration, then emits
      // loop_completed. Reconcile now and let WS/poll catch the terminal state.
      await loadLoop(loopId)
    } catch (err) {
      error.value = err.response?.data?.detail || err.message
    } finally {
      stoppingIds.value = stoppingIds.value.filter((id) => id !== loopId)
    }
  }

  function toggleExpanded(loopId) {
    expandedLoopId.value = expandedLoopId.value === loopId ? null : loopId
  }

  // Loop events are broadcast fleet-wide and keyed by `type`; only react to
  // those for the agent currently on screen.
  function handleWebSocketEvent(data) {
    if (!agentName.value) return
    if (data.type !== 'loop_run_completed' && data.type !== 'loop_completed') return
    if (data.agent_name !== agentName.value) return
    if (!data.loop_id) return
    // Targeted reconcile of just the affected loop.
    loadLoop(data.loop_id)
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
  }

  // Called on panel unmount: stop the timer and drop the agent filter so the
  // global WS handler becomes a no-op while no panel is showing.
  // expandedLoopId is intentionally preserved (it's keyed by a globally-unique
  // loop_id) so an expanded loop survives a v-if tab remount; a stale id from a
  // different agent simply matches no row and expands nothing.
  function clear() {
    stopPolling()
    agentName.value = null
    loops.value = []
    error.value = null
  }

  return {
    loops,
    agentName,
    loading,
    starting,
    error,
    expandedLoopId,
    stoppingIds,
    hasActiveLoops,
    setAgent,
    fetchLoops,
    startLoop,
    loadLoop,
    stopLoop,
    toggleExpanded,
    handleWebSocketEvent,
    stopPolling,
    clear,
  }
})
