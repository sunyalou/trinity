<template>
  <div class="fixed z-10 inset-0 overflow-y-auto">
    <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
      <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity"></div>

      <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>

      <div class="inline-block align-bottom bg-white dark:bg-gray-800 rounded-lg text-left shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full max-h-[90vh] overflow-y-auto">
        <form @submit.prevent="createAgent">
          <div class="bg-white dark:bg-gray-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
            <h3 class="text-lg leading-6 font-medium text-gray-900 dark:text-white mb-4">Create New Agent</h3>

            <div class="space-y-4">
              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Agent Name</label>
                <input
                  v-model="form.name"
                  type="text"
                  required
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="my-agent"
                />
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Template</label>

                <!-- Loading state -->
                <div v-if="templatesLoading" class="mt-2 flex items-center justify-center py-4">
                  <svg class="animate-spin h-5 w-5 text-action-primary-500 mr-2" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <span class="text-sm text-gray-500 dark:text-gray-400">Loading templates...</span>
                </div>

                <!-- Error state -->
                <div v-else-if="templatesError" class="mt-2 p-3 bg-status-danger-50 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
                  <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ templatesError }}</p>
                  <button @click="fetchTemplates" type="button" class="mt-1 text-sm text-status-danger-700 dark:text-status-danger-300 underline">
                    Try again
                  </button>
                </div>

                <div v-else class="mt-1 space-y-2 max-h-80 overflow-y-auto">
                  <!-- Blank agent option -->
                  <div
                    @click="form.template = ''"
                    :class="[
                      'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                      form.template === '' ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                    ]"
                  >
                    <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-200 dark:bg-gray-600 flex items-center justify-center">
                      <svg class="w-4 h-4 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
                      </svg>
                    </div>
                    <div class="ml-3 flex-1">
                      <p class="text-sm font-medium text-gray-900 dark:text-white">Blank Agent (Claude Code)</p>
                      <p class="text-xs text-gray-500 dark:text-gray-400">Start with empty config using Claude Code runtime</p>
                    </div>
                    <div v-if="form.template === ''" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                    </div>
                  </div>

                  <!-- GitHub Repository URL option -->
                  <div
                    @click="form.template = 'github-custom'"
                    :class="[
                      'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                      form.template === 'github-custom' ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                    ]"
                  >
                    <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-900 dark:bg-gray-700 flex items-center justify-center">
                      <svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                      </svg>
                    </div>
                    <div class="ml-3 flex-1">
                      <p class="text-sm font-medium text-gray-900 dark:text-white">GitHub Repository</p>
                      <p class="text-xs text-gray-500 dark:text-gray-400">Create from any GitHub repository URL</p>
                    </div>
                    <div v-if="form.template === 'github-custom'" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                    </div>
                  </div>
                  <!-- GitHub repo URL input (shown when GitHub Repository is selected) -->
                  <div v-if="form.template === 'github-custom'" class="pl-11">
                    <input
                      v-model="githubRepoUrl"
                      type="text"
                      ref="githubRepoInput"
                      class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                      placeholder="owner/repo or https://github.com/owner/repo"
                      @click.stop
                    />
                    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Enter a GitHub repository in <code>owner/repo</code> format</p>
                  </div>

                  <!-- Local templates section (shown first after Blank Agent) -->
                  <div v-if="localTemplates.length > 0" class="pt-2">
                    <p class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2 flex items-center">
                      <svg class="w-4 h-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
                      </svg>
                      Local Templates
                    </p>
                    <div
                      v-for="template in localTemplates"
                      :key="template.id"
                      @click="form.template = template.id"
                      :class="[
                        'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                        form.template === template.id ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex-shrink-0 w-8 h-8 rounded-full bg-action-primary-100 dark:bg-action-primary-900/50 flex items-center justify-center">
                        <svg class="w-4 h-4 text-action-primary-600 dark:text-action-primary-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                      </div>
                      <div class="ml-3 flex-1">
                        <p class="text-sm font-medium text-gray-900 dark:text-white">{{ template.display_name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ truncateDescription(template.description) }}</p>
                      </div>
                      <div v-if="form.template === template.id" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                  </div>

                  <!-- GitHub templates section -->
                  <div v-if="githubTemplates.length > 0" class="pt-2">
                    <p class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2 flex items-center">
                      <svg class="w-4 h-4 mr-1" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                      </svg>
                      GitHub Templates
                    </p>
                    <div
                      v-for="template in githubTemplates"
                      :key="template.id"
                      @click="form.template = template.id"
                      :class="[
                        'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                        form.template === template.id ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                      ]"
                    >
                      <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-900 dark:bg-gray-700 flex items-center justify-center">
                        <svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24">
                          <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                        </svg>
                      </div>
                      <div class="ml-3 flex-1">
                        <p class="text-sm font-medium text-gray-900 dark:text-white">{{ template.display_name }}</p>
                        <p class="text-xs text-gray-500 dark:text-gray-400">{{ template.github_repo }}</p>
                      </div>
                      <div v-if="form.template === template.id" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                          <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                        </svg>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Selected template description -->
                <div v-if="selectedTemplate" class="mt-3 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                  <p class="text-sm text-gray-700 dark:text-gray-300">{{ selectedTemplate.description }}</p>
                  <div v-if="selectedTemplate.mcp_servers && selectedTemplate.mcp_servers.length > 0" class="mt-2">
                    <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">MCP Servers:</p>
                    <div class="flex flex-wrap gap-1">
                      <span v-for="server in selectedTemplate.mcp_servers" :key="typeof server === 'string' ? server : server.name" class="px-2 py-0.5 text-xs bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-300 rounded">
                        {{ typeof server === 'string' ? server : server.name }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div>
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Runtime</label>
                <div class="mt-1 space-y-2">
                  <div
                    v-for="option in runtimeOptions"
                    :key="option.value"
                    @click="form.runtime = option.value"
                    :class="[
                      'relative flex items-center p-3 border rounded-lg cursor-pointer transition-all',
                      form.runtime === option.value ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
                    ]"
                  >
                    <div class="ml-1 flex-1">
                      <p class="text-sm font-medium text-gray-900 dark:text-white">{{ option.label }}</p>
                      <p class="text-xs text-gray-500 dark:text-gray-400">{{ option.description }}</p>
                    </div>
                    <div v-if="form.runtime === option.value" class="flex-shrink-0 text-action-primary-500 dark:text-action-primary-400">
                      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                      </svg>
                    </div>
                  </div>
                </div>
              </div>

              <div v-if="runtimeProviderModelOptions.length > 0" class="space-y-2">
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Provider & Model</label>
                <select
                  v-model="selectedRuntimeProviderModel"
                  @change="selectRuntimeProviderModel(selectedRuntimeProviderModel)"
                  class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                >
                  <option value="">Use runtime default</option>
                  <option v-for="option in runtimeProviderModelOptions" :key="option.value" :value="option.value">
                    {{ option.label }}
                  </option>
                </select>
                <p class="text-xs text-gray-500 dark:text-gray-400">Only providers that work with {{ selectedRuntimeLabel }} are shown.</p>
              </div>

              <div v-if="form.runtime === 'opencode'" class="space-y-2">
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">OpenCode Provider & Model</label>
                <select
                  v-if="runtimeProviderModelOptions.length === 0"
                  v-model="opencodeProvider"
                  class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                >
                  <option v-for="provider in opencodeProviderOptions" :key="provider.value" :value="provider.value">
                    {{ provider.label }}
                  </option>
                </select>
                <input
                  v-if="runtimeProviderModelOptions.length === 0 && opencodeProvider === customProviderValue"
                  v-model="opencodeCustomProvider"
                  type="text"
                  required
                  class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="provider"
                />
                <input
                  v-if="runtimeProviderModelOptions.length === 0"
                  v-model="opencodeModel"
                  type="text"
                  required
                  class="block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
                  placeholder="claude-sonnet-4-5"
                />
                <p v-if="runtimeProviderModelOptions.length === 0" class="text-xs text-gray-500 dark:text-gray-400">Saved custom providers are available without exposing API keys.</p>
              </div>

              <div v-if="form.runtime === 'opencode'">
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Permission Profile</label>
                <select
                  v-model="form.runtime_permission"
                  class="mt-1 block w-full border border-gray-300 dark:border-gray-600 rounded-md shadow-sm px-3 py-2 focus:ring-action-primary-500 focus:border-action-primary-500 sm:text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                >
                  <option v-for="profile in permissionProfiles" :key="profile" :value="profile">
                    {{ profile }}
                  </option>
                </select>
              </div>
            </div>

            <div v-if="error" class="mt-4 text-status-danger-600 dark:text-status-danger-400 text-sm">
              {{ error }}
            </div>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 px-4 py-3 sm:px-6 sm:flex sm:flex-row-reverse">
            <button
              type="submit"
              :disabled="loading"
              class="w-full inline-flex justify-center rounded-md border border-transparent shadow-sm px-4 py-2 bg-action-primary-600 text-base font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:ml-3 sm:w-auto sm:text-sm disabled:opacity-50"
            >
              <svg v-if="loading" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              {{ loading ? 'Creating...' : 'Create Agent' }}
            </button>
            <button
              type="button"
              @click="$emit('close')"
              class="mt-3 w-full inline-flex justify-center rounded-md border border-gray-300 dark:border-gray-600 shadow-sm px-4 py-2 bg-white dark:bg-gray-700 text-base font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 sm:mt-0 sm:ml-3 sm:w-auto sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, computed, watch, nextTick } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useSettingsStore } from '../stores/settings'
import { CUSTOM_PROVIDER_VALUE, buildRuntimeProviderModelOptions } from '../utils/runtimeModelPresets'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const props = defineProps({
  initialTemplate: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['close', 'created'])
const agentsStore = useAgentsStore()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()

const form = reactive({
  name: '',
  template: props.initialTemplate || '',
  runtime: 'claude-code',
  runtime_model: 'anthropic/claude-sonnet-4-5',
  runtime_provider_id: '',
  runtime_model_id: '',
  runtime_permission: 'restricted'
})

const githubRepoUrl = ref('')
const githubRepoInput = ref(null)
const customProviderValue = CUSTOM_PROVIDER_VALUE
const opencodeProvider = ref('anthropic')
const opencodeCustomProvider = ref('')
const opencodeModel = ref('claude-sonnet-4-5')
const customProviderConfigs = ref({})
const providerConfigs = ref({})
const selectedRuntimeProviderModel = ref('')

// Watch for initialTemplate changes (in case modal is reused)
watch(() => props.initialTemplate, (newVal) => {
  form.template = newVal || ''
})

// Auto-focus GitHub repo input when selected
watch(() => form.template, (newVal) => {
  if (newVal === 'github-custom') {
    nextTick(() => {
      githubRepoInput.value?.focus()
    })
  }
})

const templates = ref([])
const loading = ref(false)
const error = ref('')
const templatesLoading = ref(true)
const templatesError = ref('')

const runtimeOptions = [
  { value: 'claude-code', label: 'Claude Code', description: 'Run this agent with Claude Code' },
  { value: 'gemini-cli', label: 'Gemini CLI', description: 'Run this agent with Gemini CLI' },
  { value: 'opencode', label: 'OpenCode', description: 'Run this agent with OpenCode CLI' }
]

const selectedRuntimeLabel = computed(() => {
  return runtimeOptions.find(option => option.value === form.runtime)?.label || 'this runtime'
})

const permissionProfiles = ['restricted', 'standard', 'dangerous']

const builtinOpencodeProviders = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'google', label: 'Google' }
]

const opencodeProviderOptions = computed(() => {
  const options = [...builtinOpencodeProviders]
  const builtinValues = new Set(options.map(option => option.value))
  for (const provider of Object.keys(customProviderConfigs.value || {}).sort()) {
    if (!provider || builtinValues.has(provider)) continue
    options.push({ value: provider, label: provider })
  }
  options.push({ value: CUSTOM_PROVIDER_VALUE, label: 'Custom' })
  return options
})

const runtimeProviderModelOptions = computed(() => {
  return buildRuntimeProviderModelOptions(form.runtime, providerConfigs.value)
})

const selectedRuntimeProviderModelIds = computed(() => {
  const selectedValue = String(selectedRuntimeProviderModel.value || '').trim()
  if (!selectedValue) return { providerId: '', modelId: '' }
  if (!runtimeProviderModelOptions.value.some(option => option.value === selectedValue)) {
    return { providerId: '', modelId: '' }
  }
  return parseRuntimeProviderModelValue(selectedValue)
})

const legacyOpencodeRuntimeModel = () => {
  const provider = opencodeProvider.value === CUSTOM_PROVIDER_VALUE
    ? opencodeCustomProvider.value.trim()
    : opencodeProvider.value.trim()
  const model = opencodeModel.value.trim()
  return provider && model ? `${provider}/${model}` : ''
}

const syncLegacyOpencodeRuntimeModel = () => {
  form.runtime_model = legacyOpencodeRuntimeModel()
}

const parseRuntimeProviderModelValue = (selectedValue) => {
  const value = String(selectedValue || '').trim()
  if (!value) return { providerId: '', modelId: '' }

  const [providerId, ...modelParts] = value.split('/')
  return {
    providerId: providerId || '',
    modelId: modelParts.join('/'),
  }
}

const clearRuntimeProviderModelSelection = () => {
  selectedRuntimeProviderModel.value = ''
  form.runtime_provider_id = ''
  form.runtime_model_id = ''

  if (form.runtime === 'opencode') {
    syncLegacyOpencodeRuntimeModel()
  } else {
    form.runtime_model = ''
  }
}

const selectRuntimeProviderModel = (selectedValue) => {
  const value = String(selectedValue || '').trim()
  selectedRuntimeProviderModel.value = value

  if (!value) {
    clearRuntimeProviderModelSelection()
    return
  }

  const { providerId, modelId } = parseRuntimeProviderModelValue(value)
  form.runtime_provider_id = providerId
  form.runtime_model_id = modelId

  if (form.runtime === 'opencode') {
    form.runtime_model = form.runtime_provider_id && form.runtime_model_id
      ? `${form.runtime_provider_id}/${form.runtime_model_id}`
      : ''
  } else {
    form.runtime_model = ''
  }
}

watch([() => form.runtime, runtimeProviderModelOptions], () => {
  const options = runtimeProviderModelOptions.value
  const currentValue = form.runtime_provider_id && form.runtime_model_id
    ? `${form.runtime_provider_id}/${form.runtime_model_id}`
    : ''
  const currentIsCompatible = currentValue && options.some(option => option.value === currentValue)

  if (currentIsCompatible) {
    selectedRuntimeProviderModel.value = currentValue
    return
  }

  if (currentValue) {
    clearRuntimeProviderModelSelection()
    return
  }

  selectedRuntimeProviderModel.value = ''
  if (form.runtime !== 'opencode') form.runtime_model = ''
}, { immediate: true })

watch([opencodeProvider, opencodeCustomProvider, opencodeModel], () => {
  if (form.runtime !== 'opencode') return
  if (form.runtime === 'opencode' && form.runtime_provider_id && form.runtime_model_id) return
  syncLegacyOpencodeRuntimeModel()
}, { immediate: true })

// Computed properties to separate GitHub and local templates
const githubTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'github')
})

const localTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'local' || !t.source)
})

const selectedTemplate = computed(() => {
  if (!form.template) return null
  return templates.value.find(t => t.id === form.template)
})

// Helper function to truncate long descriptions
const truncateDescription = (description) => {
  if (!description) return ''
  const firstLine = description.split('\n')[0]
  if (firstLine.length > 60) {
    return firstLine.substring(0, 57) + '...'
  }
  return firstLine
}

// Parse GitHub repo from various input formats into owner/repo
const parseGithubRepo = (input) => {
  if (!input) return null
  let repo = input.trim()
  // Handle full URLs: https://github.com/owner/repo(.git)
  const urlMatch = repo.match(/github\.com\/([^/]+\/[^/\s#?.]+)/)
  if (urlMatch) {
    repo = urlMatch[1].replace(/\.git$/, '')
  }
  // Validate owner/repo format
  if (/^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/.test(repo)) {
    return repo
  }
  return null
}

const onTemplateChange = () => {
  // Template selection changed - no action needed
  // All config comes from backend based on template
}

const fetchTemplates = async () => {
  templatesLoading.value = true
  templatesError.value = ''
  try {
    const response = await axios.get('/api/templates', {
      headers: authStore.authHeader
    })
    templates.value = response.data
  } catch (err) {
    console.error('Failed to fetch templates:', err)
    templatesError.value = 'Failed to load templates'
  } finally {
    templatesLoading.value = false
  }
}

const fetchCustomProviderConfigs = async () => {
  try {
    const response = await settingsStore.discoverCustomProviders()
    const configs = {}
    for (const entry of response.custom_providers || []) {
      const provider = String(entry.provider || '').trim()
      if (!provider || !entry.api_key_configured) continue
      configs[provider] = {
        protocol: entry.protocol,
        api_key_configured: true
      }
    }
    customProviderConfigs.value = configs
  } catch (err) {
    console.error('Failed to discover custom providers:', err)
  }
}

const fetchProviderConfigs = async () => {
  if (authStore.role !== 'admin') return

  try {
    providerConfigs.value = await settingsStore.fetchProviderConfigs()
  } catch (err) {
    console.error('Failed to fetch provider configs:', err)
  }
}

const createAgent = async () => {
  loading.value = true
  error.value = ''

  try {
    // Only send name and template - backend handles everything else
    const { providerId, modelId } = selectedRuntimeProviderModelIds.value
    const hasRuntimeProviderModel = Boolean(providerId && modelId)
    const runtimeModel = form.runtime === 'opencode'
      ? (hasRuntimeProviderModel ? `${providerId}/${modelId}` : legacyOpencodeRuntimeModel())
      : null

    const payload = {
      name: form.name,
      runtime: form.runtime,
      runtime_provider_id: hasRuntimeProviderModel ? providerId : null,
      runtime_model_id: hasRuntimeProviderModel ? modelId : null,
      runtime_model: runtimeModel,
      runtime_permission: form.runtime_permission
    }

    if (form.template === 'github-custom') {
      const repo = parseGithubRepo(githubRepoUrl.value)
      if (!repo) {
        error.value = 'Please enter a valid GitHub repository (e.g., owner/repo)'
        loading.value = false
        return
      }
      payload.template = `github:${repo}`
    } else if (form.template) {
      payload.template = form.template
    }

    const agent = await agentsStore.createAgent(payload)
    emit('created', agent)
    emit('close')
  } catch (err) {
    const detail = err.response?.data?.detail
    if (detail && typeof detail === 'object' && detail.code === 'QUOTA_EXCEEDED') {
      error.value = `${detail.error}`
    } else if (typeof detail === 'string') {
      error.value = detail
    } else {
      error.value = detail?.error || 'Failed to create agent'
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchTemplates()
  fetchProviderConfigs()
  fetchCustomProviderConfigs()
})
</script>
