<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import {
  Building2,
  ChevronDown,
  Database,
  FileSpreadsheet,
  ListChecks,
  LogOut,
  Settings2,
  Sparkles,
} from '@lucide/vue'

import { errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'
import type { WorkspaceRole } from '../types'
import AiSettingsDialog from './AiSettingsDialog.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const loggingOut = ref(false)
const aiSettingsVisible = ref(false)
const activePath = computed(() => {
  if (route.path.startsWith('/tasks')) return '/tasks'
  if (route.path.startsWith('/agent')) return '/agent'
  return '/workbench'
})
const initials = computed(() => auth.user?.name.trim().slice(0, 2).toUpperCase() || 'U')
const canManageAi = computed(() => auth.user?.role === 'owner' || auth.user?.role === 'admin')
const roleLabels: Record<WorkspaceRole, string> = {
  owner: '所有者',
  admin: '管理员',
  analyst: '分析员',
  viewer: '查看者',
}

async function handleUserCommand(command: string) {
  if (command !== 'logout' || loggingOut.value) return
  loggingOut.value = true
  try {
    await auth.logout()
    await router.replace('/login')
    ElMessage.success('已退出登录')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <button class="brand" type="button" @click="router.push('/workbench')">
        <span class="brand-mark"><FileSpreadsheet :size="19" /></span>
        <span>AnyDatas</span>
      </button>

      <nav class="workspace-switch" aria-label="工作区">
        <button
          type="button"
          :class="{ active: activePath === '/workbench' }"
          @click="router.push('/workbench')"
        >
          <Database :size="16" />
          数据分析
        </button>
        <button
          type="button"
          :class="{ active: activePath === '/agent' }"
          @click="router.push('/agent')"
        >
          <Sparkles :size="16" />
          AI Agent
        </button>
        <button
          type="button"
          :class="{ active: activePath === '/tasks' }"
          @click="router.push('/tasks')"
        >
          <ListChecks :size="16" />
          后台任务
        </button>
      </nav>

      <div class="topbar-meta">
        <span class="workspace-identity">
          <Building2 :size="15" />
          {{ auth.user?.workspaceName }}
        </span>
        <el-tooltip v-if="canManageAi" content="工作区 AI 设置" placement="bottom">
          <button
            class="topbar-icon-button"
            type="button"
            aria-label="工作区 AI 设置"
            @click="aiSettingsVisible = true"
          >
            <Settings2 :size="16" />
          </button>
        </el-tooltip>
        <span class="health-dot" title="服务正常" />
        <el-dropdown trigger="click" placement="bottom-end" @command="handleUserCommand">
          <button class="user-menu-button" type="button" :aria-label="`${auth.user?.name}的账户菜单`">
            <span class="user-avatar">{{ initials }}</span>
            <span class="user-menu-copy">
              <strong>{{ auth.user?.name }}</strong>
              <small>{{ auth.user ? roleLabels[auth.user.role] : '' }}</small>
            </span>
            <ChevronDown :size="14" />
          </button>
          <template #dropdown>
            <el-dropdown-menu class="user-dropdown-menu">
              <li class="user-dropdown-context">
                <strong>{{ auth.user?.name }}</strong>
                <span>{{ auth.user?.email }}</span>
              </li>
              <el-dropdown-item command="logout" divided :disabled="loggingOut">
                <LogOut :size="15" />
                退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>
    <main class="shell-content">
      <router-view />
    </main>
    <AiSettingsDialog v-model="aiSettingsVisible" />
  </div>
</template>
