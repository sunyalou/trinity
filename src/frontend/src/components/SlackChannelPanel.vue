<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Slack Channel</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Bind this agent to a Slack channel so users can interact via @mentions.
    </p>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
      <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
      </svg>
      Loading...
    </div>

    <!-- Access Denied -->
    <div v-else-if="accessDenied" class="text-sm text-gray-500 dark:text-gray-400">
      Only the agent owner can manage Slack channel bindings.
    </div>

    <!-- Bound State -->
    <div v-else-if="channel.bound" class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <span class="inline-block w-2.5 h-2.5 rounded-full bg-green-500"></span>
          <div>
            <p class="text-sm font-medium text-gray-900 dark:text-white">
              #{{ channel.channel_name }}
            </p>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              {{ channel.workspace_name }}
              <span v-if="channel.is_dm_default" class="ml-1 text-indigo-600 dark:text-indigo-400">(DM default)</span>
            </p>
          </div>
        </div>
        <button
          @click="unbindChannel"
          :disabled="unbinding"
          class="text-sm text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 disabled:opacity-50"
        >
          {{ unbinding ? 'Removing...' : 'Unbind' }}
        </button>
      </div>
    </div>

    <!-- Unbound State -->
    <div v-else>
      <button
        @click="createChannel"
        :disabled="creating"
        class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <svg v-if="creating" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
        </svg>
        {{ creating ? 'Creating...' : 'Create Slack Channel' }}
      </button>
    </div>

    <!-- Messages -->
    <div v-if="message" :class="[
      'mt-3 p-3 rounded-lg text-sm',
      message.type === 'success' ? 'bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300'
    ]">
      {{ message.text }}
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import axios from 'axios'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const loading = ref(true)
const creating = ref(false)
const unbinding = ref(false)
const accessDenied = ref(false)
const channel = ref({ bound: false })
const message = ref(null)

async function loadChannel() {
  loading.value = true
  message.value = null
  accessDenied.value = false
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/slack/channel`)
    channel.value = response.data
  } catch (e) {
    if (e.response?.status === 403) {
      accessDenied.value = true
    } else {
      console.error('Failed to load Slack channel:', e)
    }
    channel.value = { bound: false }
  } finally {
    loading.value = false
  }
}

async function createChannel() {
  creating.value = true
  message.value = null
  try {
    const response = await axios.post(`/api/agents/${props.agentName}/slack/channel`)
    const data = response.data

    if (data.status === 'already_bound') {
      message.value = { type: 'success', text: `Already bound to #${data.channel_name} in ${data.workspace_name}` }
    } else {
      message.value = { type: 'success', text: `Channel #${data.channel_name} created in ${data.workspace_name}` }
    }

    await loadChannel()
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to create Slack channel'
    message.value = { type: 'error', text: detail }
  } finally {
    creating.value = false
  }
}

async function unbindChannel() {
  unbinding.value = true
  message.value = null
  try {
    await axios.delete(`/api/agents/${props.agentName}/slack/channel`)
    message.value = { type: 'success', text: 'Channel unbound' }
    await loadChannel()
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to unbind channel'
    message.value = { type: 'error', text: detail }
  } finally {
    unbinding.value = false
  }
}

watch(() => props.agentName, () => loadChannel())
onMounted(() => loadChannel())
</script>
