<script setup>
/**
 * Enterprise User & Organization Management (#995, Phase 1a).
 *
 * Org CRUD + membership UI. Backend logic lives in the private
 * trinity-enterprise submodule; this view is part of the OSS bundle,
 * reachable only when `user_management` is entitled (route guard +
 * Index.vue card gate it). No algorithmic IP here — pure CRUD glue.
 */
import { ref, onMounted } from 'vue'
import { useOrgManagementStore } from '../../stores/orgManagement'

const store = useOrgManagementStore()

const orgs = ref([])
const selected = ref(null)
const members = ref([])
const error = ref('')
const busy = ref(false)

// Create-org form
const newOrg = ref({ name: '', slug: '', max_seats: null })
// Add-member form
const newMember = ref({ user_id: null, org_role: 'member' })

async function refresh() {
  error.value = ''
  try {
    orgs.value = await store.listOrganizations()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  }
}

async function createOrg() {
  if (!newOrg.value.name) return
  busy.value = true
  error.value = ''
  try {
    const payload = { name: newOrg.value.name }
    if (newOrg.value.slug) payload.slug = newOrg.value.slug
    if (newOrg.value.max_seats) payload.max_seats = Number(newOrg.value.max_seats)
    await store.createOrganization(payload)
    newOrg.value = { name: '', slug: '', max_seats: null }
    orgs.value = store.organizations
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function selectOrg(org) {
  selected.value = org
  members.value = []
  try {
    members.value = await store.listMembers(org.id)
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  }
}

async function deleteOrg(org) {
  if (!confirm(`Delete organization "${org.name}"? Members are removed (users are not deleted).`)) return
  busy.value = true
  error.value = ''
  try {
    await store.deleteOrganization(org.id)
    if (selected.value?.id === org.id) selected.value = null
    orgs.value = store.organizations
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function addMember() {
  if (!selected.value || !newMember.value.user_id) return
  busy.value = true
  error.value = ''
  try {
    members.value = await store.addMember(selected.value.id, {
      user_id: Number(newMember.value.user_id),
      org_role: newMember.value.org_role || 'member',
    })
    newMember.value = { user_id: null, org_role: 'member' }
    await refresh()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function removeMember(m) {
  busy.value = true
  error.value = ''
  try {
    await store.removeMember(selected.value.id, m.user_id)
    members.value = await store.listMembers(selected.value.id)
    await refresh()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="p-6 max-w-6xl mx-auto">
    <header class="mb-6">
      <div class="flex items-center gap-3 mb-1">
        <h1 class="text-2xl font-semibold text-gray-900 dark:text-white">User &amp; Organization Management</h1>
        <span class="px-2 py-0.5 text-xs font-bold rounded bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200">PRO</span>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Multi-tenant organizations and membership. Teams and custom-role permission matrices land in later phases (#995).
      </p>
    </header>

    <div v-if="error" class="mb-4 px-4 py-3 rounded-lg bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300 text-sm">
      {{ error }}
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Organizations -->
      <section class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-5">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white mb-3">Organizations</h2>

        <form @submit.prevent="createOrg" class="space-y-2 mb-4">
          <div class="flex flex-wrap gap-2">
            <input v-model="newOrg.name" placeholder="Name" :disabled="busy"
              class="flex-1 min-w-[140px] px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <input v-model="newOrg.slug" placeholder="slug (optional)" :disabled="busy"
              class="flex-1 min-w-[120px] px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <input v-model="newOrg.max_seats" type="number" min="1" placeholder="seats" :disabled="busy"
              class="w-24 px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
          </div>
          <button type="submit" :disabled="busy || !newOrg.name"
            class="px-4 py-2 text-sm font-medium rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50">
            Create organization
          </button>
        </form>

        <div v-if="store.loading" class="text-sm text-gray-500 dark:text-gray-400">Loading…</div>
        <ul v-else-if="orgs.length" class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
          <li v-for="org in orgs" :key="org.id"
            class="px-4 py-3 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer"
            :class="{ 'bg-gray-50 dark:bg-gray-700/50': selected?.id === org.id }"
            @click="selectOrg(org)">
            <div>
              <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ org.name }}</span>
              <span class="ml-2 text-xs text-gray-500 dark:text-gray-400">{{ org.slug }}</span>
            </div>
            <div class="flex items-center gap-3">
              <span class="text-xs text-gray-500 dark:text-gray-400">
                {{ org.member_count }}<template v-if="org.max_seats"> / {{ org.max_seats }}</template> members
              </span>
              <button @click.stop="deleteOrg(org)" :disabled="busy"
                class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:underline disabled:opacity-50">Delete</button>
            </div>
          </li>
        </ul>
        <p v-else class="text-sm text-gray-500 dark:text-gray-400">No organizations yet.</p>
      </section>

      <!-- Members -->
      <section class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-5">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white mb-3">
          Members<template v-if="selected"> — {{ selected.name }}</template>
        </h2>

        <p v-if="!selected" class="text-sm text-gray-500 dark:text-gray-400">Select an organization to manage its members.</p>

        <template v-else>
          <form @submit.prevent="addMember" class="flex flex-wrap gap-2 mb-4">
            <input v-model="newMember.user_id" type="number" min="1" placeholder="User ID" :disabled="busy"
              class="w-28 px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <input v-model="newMember.org_role" placeholder="role (e.g. member)" :disabled="busy"
              class="flex-1 min-w-[120px] px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <button type="submit" :disabled="busy || !newMember.user_id"
              class="px-4 py-2 text-sm font-medium rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50">Add</button>
          </form>

          <ul v-if="members.length" class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
            <li v-for="m in members" :key="m.user_id" class="px-4 py-3 flex items-center justify-between">
              <div>
                <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ m.username || ('user #' + m.user_id) }}</span>
                <span v-if="m.email" class="ml-2 text-xs text-gray-500 dark:text-gray-400">{{ m.email }}</span>
              </div>
              <div class="flex items-center gap-3">
                <span class="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">{{ m.org_role }}</span>
                <button @click="removeMember(m)" :disabled="busy"
                  class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:underline disabled:opacity-50">Remove</button>
              </div>
            </li>
          </ul>
          <p v-else class="text-sm text-gray-500 dark:text-gray-400">No members yet.</p>
        </template>
      </section>
    </div>
  </div>
</template>
