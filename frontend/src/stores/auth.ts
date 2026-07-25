import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api, errorMessage } from '../api'
import type { AuthUser, LoginPayload, SetupPayload } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const setupRequired = ref(false)
  const initialized = ref(false)
  const loading = ref(false)
  const bootstrapError = ref('')

  const authenticated = computed(() => user.value !== null)

  async function bootstrap(force = false) {
    if (initialized.value && !force) return
    loading.value = true
    try {
      const status = await api.authStatus()
      setupRequired.value = status.setupRequired
      user.value = status.user
      bootstrapError.value = ''
    } catch (error) {
      user.value = null
      bootstrapError.value = errorMessage(error)
      throw error
    } finally {
      initialized.value = true
      loading.value = false
    }
  }

  async function setup(payload: SetupPayload) {
    loading.value = true
    try {
      user.value = await api.setup(payload)
      setupRequired.value = false
      bootstrapError.value = ''
      return user.value
    } finally {
      loading.value = false
    }
  }

  async function login(payload: LoginPayload) {
    loading.value = true
    try {
      user.value = await api.login(payload)
      setupRequired.value = false
      bootstrapError.value = ''
      return user.value
    } finally {
      loading.value = false
    }
  }

  async function logout() {
    loading.value = true
    try {
      await api.logout()
      clearSession()
    } finally {
      loading.value = false
    }
  }

  function clearSession() {
    user.value = null
  }

  return {
    user,
    setupRequired,
    initialized,
    loading,
    bootstrapError,
    authenticated,
    bootstrap,
    setup,
    login,
    logout,
    clearSession,
  }
})
