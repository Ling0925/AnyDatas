import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { loader } from '@guolao/vue-monaco-editor'

import './style.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'
import router from './router'
import { useAuthStore } from './stores/auth'

;(self as typeof self & { MonacoEnvironment: { getWorker: () => Worker } }).MonacoEnvironment = {
  getWorker() {
    return new editorWorker()
  },
}

loader.config({ monaco })

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
  .use(ElementPlus, { locale: zhCn })
  .mount('#app')
