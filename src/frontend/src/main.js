import { createApp } from 'vue'
import { createPinia } from 'pinia'
import axios from 'axios'
import router from './router'
import App from './App.vue'
import './style.css'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// Initialize auth state from localStorage/cookies on app startup
const authStore = useAuthStore()
authStore.initializeAuth()

// Setup axios interceptor to handle token expiration
axios.interceptors.response.use(
  response => response,
  error => {
    // If we get a 401 Unauthorized, token is expired or invalid
    if (error.response?.status === 401) {
      // Get the current route
      const currentPath = router.currentRoute.value.path

      // Don't redirect if already on login or setup page
      if (currentPath !== '/login' && currentPath !== '/setup' && currentPath !== '/m') {
        console.log('🔐 Session expired - redirecting to login')

        // Clear auth state
        authStore.logout()

        // Redirect to login
        router.push('/login')
      }
    }
    return Promise.reject(error)
  }
)

// #847 Phase 0 — load enterprise frontend module if present. The
// submodule at `src/frontend/src/enterprise/` is OPTIONAL — OSS-only
// builds clone without it and the glob below returns an empty object,
// so this block silently no-ops. When the submodule IS mounted, its
// `frontend/index.js` exports `registerEnterprise(router, app)` which
// calls `router.addRoute(...)` for each enterprise view. Routes carry
// `meta.requiresEntitlement` so the nav bar can hide them when the
// auth store reports the feature is not entitled.
//
// `eager: false` keeps the module out of the initial bundle — it's
// fetched only when the import is called. The route guard double-
// checks entitlement (defense in depth against a mounted-but-not-
// licensed scenario).
const enterpriseModules = import.meta.glob('./enterprise/frontend/index.js', { eager: false })
const enterpriseEntry = Object.values(enterpriseModules)[0]
if (enterpriseEntry) {
  enterpriseEntry().then((mod) => {
    if (typeof mod.registerEnterprise === 'function') {
      mod.registerEnterprise(router, app)
      console.info('[enterprise] frontend module loaded')
    } else {
      console.warn('[enterprise] frontend/index.js did not export registerEnterprise')
    }
  }).catch((err) => {
    console.error('[enterprise] failed to load frontend module:', err)
  })
} else {
  console.debug('[enterprise] submodule not present (OSS-only build)')
}

app.mount('#app')
