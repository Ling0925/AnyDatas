import { createApp } from 'vue'
import { createPinia } from 'pinia'

import 'element-plus/theme-chalk/dark/css-vars.css'
import './style.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'
import router from './router'
import { useAuthStore } from './stores/auth'
import { initializeTheme } from './theme'

const pinia = createPinia()

initializeTheme()

setUnauthorizedHandler(() => {
  const auth = useAuthStore(pinia)
  auth.clearSession()
  if (router.currentRoute.value.path !== '/login') {
    void router.replace({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

createApp(App)
  .use(pinia)
  .use(router)
  .mount('#app')
