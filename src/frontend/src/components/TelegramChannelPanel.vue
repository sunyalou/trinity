<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Telegram Bot</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Connect a Telegram bot so users can chat with this agent via Telegram DMs.
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
      Only the agent owner can manage Telegram bot settings.
    </div>

    <!-- Connected State -->
    <div v-else-if="binding.configured" class="space-y-3">
      <div class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span class="inline-block w-2.5 h-2.5 rounded-full bg-green-500"></span>
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-white">
                @{{ binding.bot_username }}
              </p>
              <a
                v-if="binding.bot_link"
                :href="binding.bot_link"
                target="_blank"
                rel="noopener noreferrer"
                class="text-xs text-indigo-600 dark:text-indigo-400 hover:underline"
              >
                {{ binding.bot_link }}
              </a>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="verifyBot"
              :disabled="verifying"
              class="text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 disabled:opacity-50"
            >
              {{ verifying ? 'Verifying...' : 'Verify' }}
            </button>
            <button
              @click="disconnectBot"
              :disabled="disconnecting"
              class="text-sm text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 disabled:opacity-50"
            >
              {{ disconnecting ? 'Removing...' : 'Disconnect' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Webhook Warning -->
      <div v-if="!binding.webhook_url" class="p-3 rounded-lg text-sm bg-yellow-50 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300">
        Bot connected but webhook not registered. Set a public URL in Settings for Telegram messages to reach this agent.
      </div>
    </div>

    <!-- Disconnected State — Token Input -->
    <div v-else>
      <form @submit.prevent="connectBot" class="space-y-3">
        <div>
          <label for="telegram-token" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Bot Token
          </label>
          <input
            id="telegram-token"
            v-model="botToken"
            type="password"
            placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
            :disabled="connecting"
            class="w-full max-w-lg px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100 dark:disabled:bg-gray-900"
          />
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Get a token from <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer" class="text-indigo-600 dark:text-indigo-400 hover:underline">@BotFather</a> on Telegram.
          </p>
        </div>
        <button
          type="submit"
          :disabled="connecting || !botToken.trim()"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg v-if="connecting" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          {{ connecting ? 'Connecting...' : 'Connect Bot' }}
        </button>
      </form>
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
import api from '../api'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  }
})

const loading = ref(true)
const connecting = ref(false)
const disconnecting = ref(false)
const verifying = ref(false)
const accessDenied = ref(false)
const binding = ref({ configured: false })
const botToken = ref('')
const message = ref(null)

async function loadBinding() {
  loading.value = true
  message.value = null
  accessDenied.value = false
  try {
    const response = await api.get(`/api/agents/${props.agentName}/telegram`)
    binding.value = response.data
  } catch (e) {
    if (e.response?.status === 403) {
      accessDenied.value = true
    }
    binding.value = { configured: false }
  } finally {
    loading.value = false
  }
}

async function connectBot() {
  connecting.value = true
  message.value = null
  try {
    const response = await api.put(`/api/agents/${props.agentName}/telegram`, {
      bot_token: botToken.value.trim()
    })
    botToken.value = ''
    binding.value = response.data
    message.value = { type: 'success', text: `Bot @${response.data.bot_username} connected` }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to connect bot'
    message.value = { type: 'error', text: detail }
  } finally {
    connecting.value = false
  }
}

async function disconnectBot() {
  disconnecting.value = true
  message.value = null
  try {
    await api.delete(`/api/agents/${props.agentName}/telegram`)
    message.value = { type: 'success', text: 'Bot disconnected' }
    binding.value = { configured: false }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to disconnect bot'
    message.value = { type: 'error', text: detail }
  } finally {
    disconnecting.value = false
  }
}

async function verifyBot() {
  verifying.value = true
  message.value = null
  try {
    const response = await api.post(`/api/agents/${props.agentName}/telegram/test`, {
      message: 'Verification check'
    })
    if (response.data.ok) {
      message.value = { type: 'success', text: response.data.message }
    } else {
      message.value = { type: 'error', text: response.data.message || 'Verification failed' }
    }
    setTimeout(() => { message.value = null }, 3000)
  } catch (e) {
    const detail = e.response?.data?.detail || 'Failed to verify bot'
    message.value = { type: 'error', text: detail }
  } finally {
    verifying.value = false
  }
}

watch(() => props.agentName, () => loadBinding())
onMounted(() => loadBinding())
</script>
