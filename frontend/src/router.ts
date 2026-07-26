import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from './stores/auth'

// 页面级动态导入让登录、工作台、Agent 和任务管理形成独立加载边界。
const AppShell = () => import('./components/AppShell.vue')
const AgentView = () => import('./views/AgentView.vue')
const LoginView = () => import('./views/LoginView.vue')
const TasksView = () => import('./views/TasksView.vue')
const WorkbenchView = () => import('./views/WorkbenchView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
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
      ],
    },
  ],
})

router.beforeEach(async (to) => {
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
  return true
})

export default router
