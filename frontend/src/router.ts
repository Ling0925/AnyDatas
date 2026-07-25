import { createRouter, createWebHistory } from 'vue-router'

import AppShell from './components/AppShell.vue'
import { useAuthStore } from './stores/auth'
import AgentView from './views/AgentView.vue'
import LoginView from './views/LoginView.vue'
import TasksView from './views/TasksView.vue'
import WorkbenchView from './views/WorkbenchView.vue'

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
