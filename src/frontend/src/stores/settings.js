import { defineStore } from 'pinia'
import axios from 'axios'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: {},
    loading: false,
    saving: false,
    error: null
  }),

  getters: {
    trinityPrompt() {
      return this.settings.trinity_prompt || ''
    }
  },

  actions: {
    /**
     * Fetch all system settings from the backend.
     * Admin-only endpoint.
     */
    async fetchSettings() {
      this.loading = true
      this.error = null

      try {
        const response = await axios.get('/api/settings')
        // Convert array to object for easier access
        const settingsObj = {}
        for (const setting of response.data) {
          if (setting.key === 'custom_provider_configs') continue
          settingsObj[setting.key] = setting.value
        }
        this.settings = settingsObj
        return this.settings
      } catch (error) {
        console.error('Failed to fetch settings:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch settings'
        throw error
      } finally {
        this.loading = false
      }
    },

    /**
     * #1129: Fleet-wide default access policy for new agents.
     * Returns { require_email, require_email_default, note }. Admin-only.
     */
    async getAgentDefaultAccessPolicy() {
      const response = await axios.get('/api/settings/agent-defaults/access-policy')
      return response.data
    },

    /**
     * #1129: Set the fleet-wide default `require_email` for new agents.
     * Applies to newly created agents only. Admin-only.
     */
    async setAgentDefaultRequireEmail(requireEmail) {
      const response = await axios.put('/api/settings/agent-defaults/access-policy', {
        require_email: requireEmail,
      })
      return response.data
    },

    /**
     * Get a specific setting by key.
     */
    async getSetting(key) {
      try {
        const response = await axios.get(`/api/settings/${key}`)
        this.settings[key] = response.data.value
        return response.data.value
      } catch (error) {
        if (error.response?.status === 404) {
          return null
        }
        console.error(`Failed to get setting ${key}:`, error)
        throw error
      }
    },

    /**
     * Update a system setting.
     * Admin-only endpoint.
     */
    async updateSetting(key, value) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put(`/api/settings/${key}`, { value })
        this.settings[key] = response.data.value
        return response.data
      } catch (error) {
        console.error(`Failed to update setting ${key}:`, error)
        this.error = error.response?.data?.detail || 'Failed to update setting'
        throw error
      } finally {
        this.saving = false
      }
    },

    /**
     * Delete a system setting.
     * Admin-only endpoint.
     */
    async deleteSetting(key) {
      this.saving = true
      this.error = null

      try {
        await axios.delete(`/api/settings/${key}`)
        delete this.settings[key]
        return true
      } catch (error) {
        console.error(`Failed to delete setting ${key}:`, error)
        this.error = error.response?.data?.detail || 'Failed to delete setting'
        throw error
      } finally {
        this.saving = false
      }
    },

    async fetchRuntimeDefaultModels() {
      try {
        const response = await axios.get('/api/settings/runtime-default-models')
        this.settings.runtime_default_models = response.data.runtime_default_models
        return response.data
      } catch (error) {
        console.error('Failed to fetch runtime default models:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch runtime default models'
        throw error
      }
    },

    async updateRuntimeDefaultModels(runtimeDefaultModels) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put('/api/settings/runtime-default-models', {
          runtime_default_models: runtimeDefaultModels,
        })
        this.settings.runtime_default_models = response.data.runtime_default_models
        this.settings.platform_default_model = response.data.runtime_default_models?.['claude-code']?.model
        return response.data
      } catch (error) {
        console.error('Failed to update runtime default models:', error)
        this.error = error.response?.data?.detail || 'Failed to update runtime default models'
        throw error
      } finally {
        this.saving = false
      }
    },

    async fetchCustomProviderConfigs() {
      try {
        const response = await axios.get('/api/settings/custom-provider-configs')
        this.settings.custom_provider_configs = response.data.custom_provider_configs || {}
        return response.data
      } catch (error) {
        console.error('Failed to fetch custom provider configs:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch custom provider configs'
        throw error
      }
    },

    async fetchProviderConfigs() {
      try {
        const response = await axios.get('/api/settings/provider-configs')
        return response.data.providers || {}
      } catch (error) {
        console.error('Failed to fetch provider configs:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch provider configs'
        throw error
      }
    },

    async updateProviderConfigs(providers) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put('/api/settings/provider-configs', { providers })
        return response.data.providers || {}
      } catch (error) {
        console.error('Failed to update provider configs:', error)
        this.error = error.response?.data?.detail || 'Failed to update provider configs'
        throw error
      } finally {
        this.saving = false
      }
    },

    async discoverCustomProviders() {
      try {
        const response = await axios.get('/api/settings/custom-provider-configs/discovery')
        return response.data
      } catch (error) {
        console.error('Failed to discover custom providers:', {
          status: error.response?.status,
          message: error.message,
        })
        this.error = error.response?.data?.detail || 'Failed to discover custom providers'
        throw error
      }
    },

    async updateCustomProviderConfigs(customProviderConfigs) {
      this.saving = true
      this.error = null

      try {
        const response = await axios.put('/api/settings/custom-provider-configs', {
          custom_provider_configs: customProviderConfigs,
        })
        this.settings.custom_provider_configs = response.data.custom_provider_configs || {}
        return response.data
      } catch (error) {
        console.error('Failed to update custom provider configs:', {
          status: error.response?.status,
          message: error.message,
        })
        this.error = error.response?.data?.detail || 'Failed to update custom provider configs'
        throw error
      } finally {
        this.saving = false
      }
    },

    async testProviderConnection(payload) {
      const response = await axios.post('/api/settings/provider-connection-test', payload)
      return response.data
    },

    /**
     * Clear any error state.
     */
    clearError() {
      this.error = null
    }
  }
})
