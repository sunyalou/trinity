<script setup>
/**
 * Enterprise SSO view (#847 PoC).
 *
 * Lists configured SSO providers from `GET /api/enterprise/sso/providers`.
 * In the PoC the list is always empty — the backend returns []. Real
 * provider management (add/remove OIDC/SAML) lands in Phase 4.
 *
 * Route guard: the OSS shell's router already checks `meta.requiresEntitlement`
 * against the auth store's `enterpriseFeatures` list. If 'sso' isn't entitled
 * the user never reaches this component.
 */
import { ref, onMounted } from 'vue'
import api from '../../api'  // public repo's shared Axios instance

const providers = ref([])
const loading = ref(true)
const error = ref(null)

async function loadProviders() {
  loading.value = true
  error.value = null
  try {
    const { data } = await api.get('/api/enterprise/sso/providers')
    providers.value = data || []
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

onMounted(loadProviders)
</script>

<template>
  <div class="enterprise-sso p-6 max-w-4xl mx-auto">
    <header class="mb-6">
      <h1 class="text-2xl font-semibold">Enterprise SSO</h1>
      <p class="text-sm text-gray-500 mt-1">
        Single sign-on providers configured for this instance. Issue
        <a href="https://github.com/abilityai/trinity/issues/847" class="underline" target="_blank">#847</a>
        — Phase 0 PoC: provider management is a stub. No real OIDC/SAML flow yet.
      </p>
    </header>

    <div v-if="loading" class="text-gray-500 text-sm">Loading…</div>

    <div v-else-if="error" class="bg-red-50 border border-red-200 rounded p-4 text-sm text-red-700">
      <strong class="block">Failed to load providers</strong>
      <span>{{ error }}</span>
    </div>

    <div v-else-if="providers.length === 0" class="bg-gray-50 border border-gray-200 rounded p-6 text-center">
      <p class="text-gray-600 mb-2">No SSO providers configured.</p>
      <p class="text-xs text-gray-400">
        Provider configuration UI will be added in Phase 4. In the PoC
        the backend registry is empty by default.
      </p>
    </div>

    <ul v-else class="divide-y divide-gray-200 border rounded">
      <li v-for="p in providers" :key="p.provider_id" class="p-4 flex items-center justify-between">
        <div>
          <div class="font-medium">{{ p.display_name }}</div>
          <div class="text-xs text-gray-500">
            {{ p.provider_id }} · {{ p.protocol.toUpperCase() }}
          </div>
        </div>
        <button
          class="px-3 py-1 text-sm border rounded text-gray-500 cursor-not-allowed"
          disabled
          title="PoC stub — backend returns 501"
        >
          Login (stub)
        </button>
      </li>
    </ul>
  </div>
</template>
