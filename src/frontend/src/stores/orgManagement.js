/**
 * Enterprise Org/Team management store (#995, Phase 1a).
 *
 * Thin API client over the private enterprise backend at
 * `/api/enterprise/user-management/*` (mounted only when the
 * trinity-enterprise submodule is present AND the `user_management`
 * feature is entitled — otherwise these calls 404/403 and the UI is
 * hidden by the route guard + Index.vue gating).
 *
 * Domain-per-store (Invariant #6); all calls go through the shared
 * axios instance with the auth header (Invariant #7).
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

const BASE = '/api/enterprise/user-management'

export const useOrgManagementStore = defineStore('orgManagement', {
  state: () => ({
    organizations: [],
    loading: false,
    error: null,
  }),

  actions: {
    _auth() {
      return { headers: useAuthStore().authHeader }
    },

    async listOrganizations() {
      this.loading = true
      this.error = null
      try {
        const r = await axios.get(`${BASE}/organizations`, this._auth())
        this.organizations = r.data
        return r.data
      } catch (e) {
        this.error = e.response?.data?.detail || e.message
        throw e
      } finally {
        this.loading = false
      }
    },

    async createOrganization(payload) {
      const r = await axios.post(`${BASE}/organizations`, payload, this._auth())
      await this.listOrganizations()
      return r.data
    },

    async updateOrganization(orgId, payload) {
      const r = await axios.patch(`${BASE}/organizations/${orgId}`, payload, this._auth())
      await this.listOrganizations()
      return r.data
    },

    async deleteOrganization(orgId) {
      await axios.delete(`${BASE}/organizations/${orgId}`, this._auth())
      await this.listOrganizations()
    },

    async listMembers(orgId) {
      const r = await axios.get(`${BASE}/organizations/${orgId}/members`, this._auth())
      return r.data
    },

    async addMember(orgId, payload) {
      const r = await axios.post(`${BASE}/organizations/${orgId}/members`, payload, this._auth())
      return r.data
    },

    async removeMember(orgId, userId) {
      await axios.delete(`${BASE}/organizations/${orgId}/members/${userId}`, this._auth())
    },
  },
})
