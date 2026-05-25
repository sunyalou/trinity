import { ref } from 'vue'
import api from '@/api'

/**
 * #926 build-info composable.
 *
 * One-shot fetch of `GET /api/version` cached in module scope: build
 * metadata can't change at runtime, so every consumer (NavBar chip,
 * Settings panel, future "About" dialog) reads the same singleton.
 *
 * Returned refs are the cache itself — mutating them would corrupt
 * the singleton. Treat as read-only. `load()` is idempotent; calling
 * it more than once returns the same in-flight Promise.
 */

const info = ref(null)
const loading = ref(false)
const error = ref(null)
let inFlight = null

async function load() {
  if (info.value) return info.value
  if (inFlight) return inFlight
  loading.value = true
  error.value = null
  inFlight = (async () => {
    try {
      const { data } = await api.get('/api/version')
      info.value = data
      return data
    } catch (err) {
      error.value = err
      throw err
    } finally {
      loading.value = false
      inFlight = null
    }
  })()
  return inFlight
}

export function useBuildInfo() {
  return { info, loading, error, load }
}
