import { createApp } from 'vue'
import { createPinia } from 'pinia'

import './style.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'
import router from './router'
import { useAuthStore } from './stores/auth'

const pinia = createPinia()

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
