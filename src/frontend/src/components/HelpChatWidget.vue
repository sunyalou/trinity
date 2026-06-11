<template>
  <!-- Floating help button -->
  <button
    v-if="!isOpen"
    @click="openChat"
    class="fixed bottom-6 right-6 w-14 h-14 bg-action-primary-600 hover:bg-action-primary-700 text-white rounded-full shadow-lg flex items-center justify-center transition-all hover:scale-105 focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 z-50"
    aria-label="Open help chat"
  >
    <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  </button>

  <!-- Chat panel -->
  <Transition
    enter-active-class="transition ease-out duration-200"
    enter-from-class="opacity-0 translate-y-4"
    enter-to-class="opacity-100 translate-y-0"
    leave-active-class="transition ease-in duration-150"
    leave-from-class="opacity-100 translate-y-0"
    leave-to-class="opacity-0 translate-y-4"
  >
    <div
      v-if="isOpen"
      ref="panelRef"
      data-bugreport-exclude
      class="fixed bottom-6 right-6 w-96 max-w-[calc(100vw-3rem)] h-[32rem] max-h-[calc(100vh-6rem)] bg-white dark:bg-gray-800 rounded-xl shadow-2xl flex flex-col z-50 border border-gray-200 dark:border-gray-700"
      role="dialog"
      aria-label="Help chat"
      @keydown.escape="closeChat"
    >
      <!-- Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 bg-action-primary-600 rounded-t-xl">
        <div class="flex items-center space-x-2">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span class="font-semibold text-white">Trinity Help</span>
        </div>
        <div class="flex items-center space-x-1">
          <!-- New conversation button (Ask mode only) -->
          <button
            v-if="mode === 'ask' && messages.length > 0"
            @click="startNewConversation"
            class="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            title="Start new conversation"
            aria-label="Start new conversation"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <!-- Close button -->
          <button
            @click="closeChat"
            class="p-1.5 text-white/80 hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            aria-label="Close help chat"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Mode tabs (only when bug reporting is enabled) -->
      <div v-if="bugReportEnabled" class="flex border-b border-gray-200 dark:border-gray-700 shrink-0">
        <button
          @click="mode = 'ask'"
          :class="mode === 'ask' ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400' : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          class="flex-1 px-4 py-2 text-sm font-medium border-b-2 transition-colors"
        >
          Ask
        </button>
        <button
          @click="mode = 'bug'"
          :class="mode === 'bug' ? 'border-action-primary-500 text-action-primary-600 dark:text-action-primary-400' : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          class="flex-1 px-4 py-2 text-sm font-medium border-b-2 transition-colors"
        >
          Report a bug
        </button>
      </div>

      <!-- ============================ ASK MODE ============================ -->
      <template v-if="mode === 'ask'">
        <div
          ref="messagesRef"
          class="flex-1 overflow-y-auto p-4 space-y-4"
          aria-live="polite"
          aria-atomic="false"
        >
          <div v-if="messages.length === 0 && !loading" class="text-center py-8">
            <div class="w-12 h-12 bg-action-primary-100 dark:bg-action-primary-900/30 rounded-full flex items-center justify-center mx-auto mb-3">
              <svg class="w-6 h-6 text-action-primary-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-1">How can I help?</h3>
            <p class="text-xs text-gray-500 dark:text-gray-400 max-w-xs mx-auto">
              Ask me anything about Trinity - agents, credentials, scheduling, and more.
            </p>
          </div>

          <ChatBubble
            v-for="(msg, idx) in messages"
            :key="idx"
            :role="msg.role"
            :content="msg.content"
          />

          <div v-if="loading" class="flex items-center space-x-2 text-gray-500 dark:text-gray-400">
            <div class="flex space-x-1">
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 0ms"></div>
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 150ms"></div>
              <div class="w-2 h-2 bg-action-primary-500 rounded-full animate-bounce" style="animation-delay: 300ms"></div>
            </div>
            <span class="text-sm">Thinking...</span>
          </div>
        </div>

        <div v-if="error" class="mx-4 mb-2 p-3 bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
          <div class="flex items-center justify-between">
            <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
            <button
              @click="retryLastMessage"
              class="ml-2 text-xs text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-700 dark:hover:text-status-danger-300 underline"
            >
              Retry
            </button>
          </div>
        </div>

        <div class="p-3 border-t border-gray-200 dark:border-gray-700">
          <form @submit.prevent="sendMessage" class="flex items-end space-x-2">
            <textarea
              ref="inputRef"
              v-model="inputMessage"
              rows="1"
              :maxlength="2000"
              placeholder="Ask a question..."
              class="flex-1 resize-none border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              :disabled="loading"
              @keydown.enter.exact.prevent="sendMessage"
              @input="autoResize"
            ></textarea>
            <button
              type="submit"
              :disabled="loading || !inputMessage.trim()"
              class="p-2 bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-action-primary-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors shrink-0"
              aria-label="Send message"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            </button>
          </form>
        </div>
      </template>

      <!-- ========================= REPORT A BUG MODE ===================== -->
      <template v-else>
        <div class="flex-1 overflow-y-auto p-4 space-y-3">
          <!-- Success state -->
          <div v-if="bugSuccess" class="text-center py-6">
            <div class="w-12 h-12 bg-status-success-100 dark:bg-status-success-900/30 rounded-full flex items-center justify-center mx-auto mb-3">
              <svg class="w-6 h-6 text-status-success-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-1">Thanks — report submitted</h3>
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">Your bug report was filed as a public GitHub issue.</p>
            <a
              v-if="bugSuccess.url"
              :href="bugSuccess.url"
              target="_blank"
              rel="noopener noreferrer"
              class="inline-block text-sm text-action-primary-600 dark:text-action-primary-400 underline break-all"
            >{{ bugSuccess.url }}</a>
            <p v-else-if="bugSuccess.reference" class="text-xs text-gray-600 dark:text-gray-300">
              Tracking reference: <code>{{ bugSuccess.reference }}</code>
            </p>
            <div class="mt-4">
              <button @click="resetBugForm" class="text-xs text-gray-500 dark:text-gray-400 underline">Report another</button>
            </div>
          </div>

          <!-- Form -->
          <template v-else>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              Found something broken? Send us a report. We'll attach some diagnostics so we can reproduce it.
            </p>

            <!-- Title -->
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Title <span class="text-status-danger-500">*</span></label>
              <input
                v-model="bugTitle"
                type="text"
                :maxlength="TITLE_MAX"
                placeholder="Short summary of the problem"
                class="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              />
              <div class="flex justify-between mt-0.5">
                <span v-if="titleError" class="text-xs text-status-danger-500">{{ titleError }}</span><span v-else></span>
                <span class="text-xs text-gray-400">{{ bugTitle.length }}/{{ TITLE_MAX }}</span>
              </div>
            </div>

            <!-- Description -->
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description <span class="text-status-danger-500">*</span></label>
              <textarea
                v-model="bugDescription"
                rows="4"
                :maxlength="DESC_MAX"
                placeholder="What happened? What did you expect? Steps to reproduce?"
                class="w-full resize-none border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:ring-2 focus:ring-action-primary-500 focus:border-transparent text-sm"
              ></textarea>
              <div class="flex justify-between mt-0.5">
                <span v-if="descError" class="text-xs text-status-danger-500">{{ descError }}</span><span v-else></span>
                <span class="text-xs text-gray-400">{{ bugDescription.length }}/{{ DESC_MAX }}</span>
              </div>
            </div>

            <!-- Screenshot -->
            <div>
              <div class="flex items-center justify-between">
                <label class="text-xs font-medium text-gray-700 dark:text-gray-300">Screenshot (optional)</label>
                <button
                  v-if="!screenshot"
                  @click="captureScreenshot"
                  :disabled="capturing"
                  class="text-xs text-action-primary-600 dark:text-action-primary-400 underline disabled:opacity-50"
                >{{ capturing ? 'Capturing…' : 'Capture current view' }}</button>
                <button v-else @click="screenshot = null" class="text-xs text-status-danger-500 underline">Remove</button>
              </div>
              <img
                v-if="screenshot"
                :src="screenshot"
                alt="Screenshot preview"
                class="mt-2 w-full rounded border border-gray-200 dark:border-gray-700"
              />
              <p v-if="screenshotError" class="text-xs text-status-danger-500 mt-1">{{ screenshotError }}</p>
            </div>

            <!-- Diagnostics preview (collapsible) -->
            <div class="border border-gray-200 dark:border-gray-700 rounded-lg">
              <button
                @click="showDiagnostics = !showDiagnostics"
                class="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-gray-700 dark:text-gray-300"
              >
                <span>What will be sent (diagnostics)</span>
                <svg class="w-4 h-4 transition-transform" :class="showDiagnostics ? 'rotate-180' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <pre
                v-if="showDiagnostics"
                class="px-3 pb-3 text-[11px] leading-snug text-gray-600 dark:text-gray-400 overflow-x-auto whitespace-pre-wrap break-words max-h-48 overflow-y-auto"
              >{{ diagnosticsPreview }}</pre>
            </div>

            <!-- Public notice + confirm -->
            <div class="p-2 bg-status-warning-50 dark:bg-status-warning-900/20 border border-status-warning-200 dark:border-status-warning-800 rounded-lg">
              <p class="text-xs text-status-warning-700 dark:text-status-warning-300">
                ⚠️ This creates a <strong>public</strong> GitHub issue in <code>abilityai/trinity</code>, visible to anyone. Don't include passwords or secrets.
              </p>
            </div>
            <label class="flex items-start space-x-2 text-xs text-gray-700 dark:text-gray-300 cursor-pointer">
              <input v-model="bugConfirm" type="checkbox" class="mt-0.5 rounded border-gray-300 dark:border-gray-600" />
              <span>I understand this report (including the diagnostics and screenshot above) will be posted publicly.</span>
            </label>

            <div v-if="bugError" class="p-2 bg-status-danger-50 dark:bg-status-danger-900/20 border border-status-danger-200 dark:border-status-danger-800 rounded-lg flex items-center justify-between">
              <p class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ bugError }}</p>
              <button @click="submitBug" class="ml-2 text-xs text-status-danger-600 dark:text-status-danger-400 underline">Retry</button>
            </div>
          </template>
        </div>

        <!-- Submit footer -->
        <div v-if="!bugSuccess" class="p-3 border-t border-gray-200 dark:border-gray-700">
          <button
            @click="submitBug"
            :disabled="!canSubmitBug || submitting"
            class="w-full py-2 bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-action-primary-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors text-sm font-medium"
          >
            {{ submitting ? 'Submitting…' : 'Submit bug report' }}
          </button>
        </div>
      </template>
    </div>
  </Transition>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import ChatBubble from './chat/ChatBubble.vue'
import { useBuildInfo } from '../composables/useBuildInfo'
import { gatherDiagnostics } from '../utils/diagnostics'

const ENDPOINT = 'https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity'
const SESSION_KEY = 'trinity_help_session_id'

// #1116: hosted intake endpoint (sibling Cloud Function holding the
// server-side GitHub token). Operators can repoint or disable via build env.
const BUG_ENDPOINT =
  import.meta.env.VITE_BUG_REPORT_ENDPOINT ||
  'https://us-central1-mcp-server-project-455215.cloudfunctions.net/report-bug'
const bugReportEnabled =
  import.meta.env.VITE_BUG_REPORT_ENABLED !== 'false' && !!BUG_ENDPOINT

const TITLE_MAX = 120
const TITLE_MIN = 8
const DESC_MAX = 4000
const DESC_MIN = 20

const route = useRoute()
const buildInfo = useBuildInfo()

const isOpen = ref(false)
const mode = ref('ask') // 'ask' | 'bug'

// --- Ask mode state ---
const messages = ref([])
const inputMessage = ref('')
const loading = ref(false)
const error = ref(null)
const sessionId = ref(null)
const lastUserMessage = ref('')

// --- Bug mode state ---
const bugTitle = ref('')
const bugDescription = ref('')
const bugConfirm = ref(false)
const showDiagnostics = ref(false)
const screenshot = ref(null)
const capturing = ref(false)
const screenshotError = ref(null)
const submitting = ref(false)
const bugError = ref(null)
const bugSuccess = ref(null)
const diagnostics = ref(null)

const panelRef = ref(null)
const messagesRef = ref(null)
const inputRef = ref(null)

const titleError = computed(() => {
  if (!bugTitle.value) return ''
  if (bugTitle.value.trim().length < TITLE_MIN) return `At least ${TITLE_MIN} characters`
  return ''
})
const descError = computed(() => {
  if (!bugDescription.value) return ''
  if (bugDescription.value.trim().length < DESC_MIN) return `At least ${DESC_MIN} characters`
  return ''
})
const canSubmitBug = computed(() =>
  bugTitle.value.trim().length >= TITLE_MIN &&
  bugDescription.value.trim().length >= DESC_MIN &&
  bugConfirm.value
)
const diagnosticsPreview = computed(() =>
  diagnostics.value ? JSON.stringify(diagnostics.value, null, 2) : 'Gathering…'
)

const openChat = () => {
  isOpen.value = true
  nextTick(() => inputRef.value?.focus())
}
const closeChat = () => { isOpen.value = false }

const startNewConversation = () => {
  messages.value = []
  sessionId.value = null
  localStorage.removeItem(SESSION_KEY)
  error.value = null
  nextTick(() => inputRef.value?.focus())
}

const autoResize = (event) => {
  const textarea = event.target
  textarea.style.height = 'auto'
  textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px'
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight
  })
}

// ---------------------------------------------------------------- Ask mode
const sendMessage = async () => {
  const question = inputMessage.value.trim()
  if (!question || loading.value) return

  error.value = null
  lastUserMessage.value = question
  inputMessage.value = ''
  if (inputRef.value) inputRef.value.style.height = 'auto'

  messages.value.push({ role: 'user', content: question })
  scrollToBottom()
  loading.value = true

  try {
    const payload = { question }
    if (sessionId.value) payload.session_id = sessionId.value

    const response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!response.ok) throw new Error(`Request failed: ${response.status}`)

    const data = await response.json()
    if (!data.answer) throw new Error('No answer received')

    if (data.session_id) {
      sessionId.value = data.session_id
      localStorage.setItem(SESSION_KEY, data.session_id)
    }
    messages.value.push({ role: 'assistant', content: data.answer })
    scrollToBottom()
  } catch (err) {
    console.error('Help chat error:', err)
    error.value = 'Failed to get response. Please try again.'
    messages.value.pop()
  } finally {
    loading.value = false
  }
}

const retryLastMessage = () => {
  if (lastUserMessage.value) {
    inputMessage.value = lastUserMessage.value
    error.value = null
    sendMessage()
  }
}

// ---------------------------------------------------------------- Bug mode
const refreshDiagnostics = () => {
  diagnostics.value = gatherDiagnostics({ versionInfo: buildInfo.info.value, route })
}

const captureScreenshot = async () => {
  capturing.value = true
  screenshotError.value = null
  try {
    // Dynamic import keeps html-to-image out of the main bundle until used.
    const { toPng } = await import('html-to-image')
    screenshot.value = await toPng(document.body, {
      pixelRatio: 0.7, // downscale — keeps the data URL payload modest
      cacheBust: true,
      // Exclude the widget panel itself so the report shows the page, not us.
      filter: (node) =>
        !(node?.hasAttribute && node.hasAttribute('data-bugreport-exclude')),
    })
  } catch (err) {
    console.error('Screenshot capture failed:', err)
    screenshotError.value = 'Could not capture a screenshot of this view.'
  } finally {
    capturing.value = false
  }
}

const resetBugForm = () => {
  bugTitle.value = ''
  bugDescription.value = ''
  bugConfirm.value = false
  screenshot.value = null
  screenshotError.value = null
  bugError.value = null
  bugSuccess.value = null
  showDiagnostics.value = false
  refreshDiagnostics()
}

const submitBug = async () => {
  if (!canSubmitBug.value || submitting.value) return
  bugError.value = null
  submitting.value = true

  // Re-gather diagnostics at submit time so console_logs reflect the moment
  // of report, and send EXACTLY what the preview showed (no silent edits).
  refreshDiagnostics()

  try {
    const payload = {
      title: bugTitle.value.trim(),
      description: bugDescription.value.trim(),
      diagnostics: diagnostics.value,
      screenshot: screenshot.value || null,
      source: 'in-app',
    }
    const response = await fetch(BUG_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!response.ok) {
      let detail = ''
      try { detail = (await response.json())?.error || '' } catch { /* ignore */ }
      throw new Error(detail || `Request failed: ${response.status}`)
    }
    const data = await response.json().catch(() => ({}))
    bugSuccess.value = {
      url: data.issue_url || data.html_url || data.url || null,
      reference: data.reference || data.id || null,
    }
  } catch (err) {
    console.error('Bug report submit error:', err)
    bugError.value =
      'Failed to submit the report. Check your connection and try again.'
  } finally {
    submitting.value = false
  }
}

// Focus trap
const handleKeydown = (event) => {
  if (!isOpen.value) return
  if (event.key === 'Tab') {
    const focusableElements = panelRef.value?.querySelectorAll(
      'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    )
    if (!focusableElements || focusableElements.length === 0) return
    const first = focusableElements[0]
    const last = focusableElements[focusableElements.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
}

onMounted(() => {
  const savedSession = localStorage.getItem(SESSION_KEY)
  if (savedSession) sessionId.value = savedSession
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})

watch(isOpen, (open) => {
  if (open && mode.value === 'ask') {
    nextTick(() => inputRef.value?.focus())
  }
})

// Lazily gather diagnostics when the user first switches to bug mode, and
// kick a build-info fetch so the version block is populated.
watch(mode, async (m) => {
  if (m === 'bug') {
    refreshDiagnostics() // immediate (version may still be 'unknown')
    try { await buildInfo.load() } catch { /* version stays 'unknown' */ }
    refreshDiagnostics() // re-gather once build info resolves
  }
})
</script>
