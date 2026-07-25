<script setup lang="ts">
import { computed, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowRight,
  Building2,
  FileSpreadsheet,
  LockKeyhole,
  Mail,
  ShieldCheck,
  UserRound,
} from '@lucide/vue'

import { errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const form = reactive({
  email: '',
  name: '',
  workspaceName: '我的工作区',
  password: '',
  passwordConfirmation: '',
})

const isSetup = computed(() => auth.setupRequired)
const submitLabel = computed(() => (isSetup.value ? '创建并进入工作区' : '登录'))

async function submit() {
  if (!form.email.trim() || !form.password) {
    ElMessage.warning('请输入邮箱和密码')
    return
  }
  if (isSetup.value) {
    if (!form.name.trim() || !form.workspaceName.trim()) {
      ElMessage.warning('请完整填写管理员与工作区信息')
      return
    }
    if (form.password.length < 12) {
      ElMessage.warning('密码至少需要 12 位')
      return
    }
    if (form.password !== form.passwordConfirmation) {
      ElMessage.warning('两次输入的密码不一致')
      return
    }
  }

  try {
    if (isSetup.value) {
      await auth.setup({
        email: form.email.trim(),
        name: form.name.trim(),
        workspaceName: form.workspaceName.trim(),
        password: form.password,
      })
      ElMessage.success('工作区初始化完成')
    } else {
      await auth.login({ email: form.email.trim(), password: form.password })
    }
    const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/')
      ? route.query.redirect
      : '/workbench'
    await router.replace(redirect)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}
</script>

<template>
  <div class="auth-page">
    <header class="auth-header">
      <div class="auth-brand">
        <span class="brand-mark"><FileSpreadsheet :size="19" /></span>
        <strong>AnyDatas</strong>
      </div>
      <span class="auth-deployment"><ShieldCheck :size="15" /> 本机安全工作区</span>
    </header>

    <main class="auth-stage">
      <section class="auth-panel" aria-labelledby="auth-title">
        <div class="auth-panel-heading">
          <span v-if="isSetup" class="auth-mode">首次初始化</span>
          <h1 id="auth-title">{{ isSetup ? '创建管理员账户' : '登录工作区' }}</h1>
          <p>{{ isSetup ? '设置此服务器的管理员和默认工作区' : '使用管理员分配的账户继续' }}</p>
        </div>

        <el-alert
          v-if="auth.bootstrapError"
          class="auth-alert"
          :title="auth.bootstrapError"
          type="error"
          :closable="false"
          show-icon
        />

        <form class="auth-form" @submit.prevent="submit">
          <label v-if="isSetup" class="auth-field">
            <span>管理员姓名</span>
            <el-input v-model="form.name" size="large" autocomplete="name" maxlength="80">
              <template #prefix><UserRound :size="16" /></template>
            </el-input>
          </label>

          <label v-if="isSetup" class="auth-field">
            <span>工作区名称</span>
            <el-input v-model="form.workspaceName" size="large" autocomplete="organization" maxlength="80">
              <template #prefix><Building2 :size="16" /></template>
            </el-input>
          </label>

          <label class="auth-field">
            <span>邮箱</span>
            <el-input v-model="form.email" size="large" type="email" autocomplete="email">
              <template #prefix><Mail :size="16" /></template>
            </el-input>
          </label>

          <label class="auth-field">
            <span>密码</span>
            <el-input
              v-model="form.password"
              size="large"
              type="password"
              :autocomplete="isSetup ? 'new-password' : 'current-password'"
              show-password
            >
              <template #prefix><LockKeyhole :size="16" /></template>
            </el-input>
            <small v-if="isSetup">至少 12 位</small>
          </label>

          <label v-if="isSetup" class="auth-field">
            <span>确认密码</span>
            <el-input
              v-model="form.passwordConfirmation"
              size="large"
              type="password"
              autocomplete="new-password"
              show-password
            >
              <template #prefix><LockKeyhole :size="16" /></template>
            </el-input>
          </label>

          <el-button class="auth-submit" type="primary" size="large" native-type="submit" :loading="auth.loading">
            {{ submitLabel }}
            <ArrowRight :size="16" />
          </el-button>
        </form>
      </section>
    </main>
  </div>
</template>
