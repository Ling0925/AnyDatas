import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'

import { useAuthStore } from './stores/auth'

// 页面级动态导入让登录、工作台、Agent 和任务管理形成独立加载边界。
const AppShell = () => import('./components/AppShell.vue')
const ConnectionView = () => import('./views/ConnectionView.vue')
const AgentView = () => import('./views/AgentView.vue')
const FileSourcesView = () => import('./views/FileSourcesView.vue')
const LoginView = () => import('./views/LoginView.vue')
const TasksView = () => import('./views/TasksView.vue')
const WorkbenchView = () => import('./views/WorkbenchView.vue')

// Electron 以 file:// 加载生产包：HTML5 history 会把整条文件路径当作路由，导致无匹配而白屏，
// 官方文档明确 file/宿主为空时用 hash 模式；HTTP(S) 网页部署仍保留 HTML5 history。
const history = window.location.protocol === 'file:'
  ? createWebHashHistory()
  : createWebHistory()

const router = createRouter({
  history,
  routes: [
    {
      path: '/connection',
      component: ConnectionView,
      meta: { desktopRuntime: true },
    },
    {
      path: '/login',
      component: LoginView,
      meta: { public: true },
    },
    {
      path: '/',
      component: AppShell,
      meta: { requiresAuth: true },
      children: [
        { path: '', redirect: '/workbench' },
        { path: 'workbench', component: WorkbenchView },
        { path: 'agent', component: AgentView },
        { path: 'tasks', component: TasksView },
        { path: 'file-sources', component: FileSourcesView },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  if (window.desktop) {
    const backend = await window.desktop.getBackendStatus()
    if (to.meta.desktopRuntime) {
      if (backend.phase === 'ready' && to.query.change !== '1') {
        return { path: '/login' }
      }
      return true
    }
    if (backend.phase !== 'ready') {
      return { path: '/connection' }
    }
  }

  const auth = useAuthStore()
  if (!auth.initialized) {
    try {
      await auth.bootstrap()
    } catch {
      if (to.path !== '/login') return { path: '/login', query: { redirect: to.fullPath } }
    }
  }

  if (to.meta.public) {
    return auth.authenticated ? { path: '/workbench' } : true
  }
  if (to.meta.requiresAuth && !auth.authenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // 文件采集仅存在于 Electron 桌面壳；网页浏览器中 window.desktop 为 undefined，直接退回工作台。
  if (to.path.startsWith('/file-sources') && !window.desktop) {
    return { path: '/workbench' }
  }
  return true
})

export default router
