<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowRight,
  Braces,
  Building2,
  Clock3,
  Database,
  FileSpreadsheet,
  LockKeyhole,
  Mail,
  ShieldCheck,
  UserRound,
} from '@lucide/vue'

import { errorMessage } from '../api'
import PixelOcean from '../components/PixelOcean.vue'
import ThemeToggle from '../components/ThemeToggle.vue'
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
type ValidationField = 'email' | 'name' | 'workspaceName' | 'password' | 'passwordConfirmation'
const validationError = ref('')
const validationField = ref<ValidationField | null>(null)

/**
 * 根据服务端状态切换“首次初始化”和“普通登录”模式。
 * 为什么这么做：两种流程共用同一页面，但字段与提交接口不同；
 * 好处：保留现有认证逻辑的同时，可以让界面文案和表单准确响应当前状态。
 */
const isSetup = computed(() => auth.setupRequired)

/**
 * 为当前认证模式提供明确的主操作文案。
 * 为什么这么做：初始化会同时创建账户与工作区，仅显示“登录”容易产生误解；
 * 好处：用户在提交前就能确认操作结果，减少首次部署时的认知成本。
 */
const submitLabel = computed(() => (isSetup.value ? '创建并进入工作区' : '登录'))

/**
 * 持久展示客户端校验错误，并记录需要标红的字段。
 * 为什么这么做：顶部 Toast 容易被用户错过；好处：按钮点击后错误会留在表单内，直到用户开始修正。
 */
function setValidationError(field: ValidationField, message: string) {
  validationField.value = field
  validationError.value = message
}

/**
 * 用户修改任意字段时清除旧提示。
 * 为什么这么做：错误对应的是上一次提交快照；好处：修正后不会继续显示已经过期的红色状态。
 */
function clearValidationError() {
  validationField.value = null
  validationError.value = ''
}

/**
 * 按页面展示顺序校验认证表单，并把第一个问题固定显示在对应字段。
 * 为什么这么做：浏览器原生校验可能直接拦截 submit 且提示不明显；好处：网页和 Electron 都获得一致的中文反馈。
 */
function validateForm() {
  if (isSetup.value && !form.name.trim()) {
    setValidationError('name', '请输入管理员姓名')
    return false
  }
  if (isSetup.value && !form.workspaceName.trim()) {
    setValidationError('workspaceName', '请输入工作区名称')
    return false
  }
  if (!form.email.trim()) {
    setValidationError('email', '请输入邮箱')
    return false
  }
  if (!/^\S+@\S+\.\S+$/u.test(form.email.trim())) {
    setValidationError('email', '请输入有效的邮箱地址')
    return false
  }
  if (!form.password) {
    setValidationError('password', '请输入密码')
    return false
  }
  if (isSetup.value && form.password.length < 12) {
    setValidationError('password', `密码至少需要 12 位，当前为 ${form.password.length} 位`)
    return false
  }
  if (isSetup.value && form.password !== form.passwordConfirmation) {
    setValidationError('passwordConfirmation', '两次输入的密码不一致')
    return false
  }
  clearValidationError()
  return true
}

/**
 * 校验并提交登录或首次初始化表单。
 * 为什么这么做：沿用既有校验、接口和安全重定向规则，避免视觉重构改变认证行为；
 * 好处：新页面只承担展示升级，登录、初始化和错误反馈仍保持原有可靠路径。
 */
async function submit() {
  if (!validateForm()) return

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
    validationField.value = null
    validationError.value = errorMessage(error)
    ElMessage.error(validationError.value)
  }
}
</script>

<template>
  <div class="auth-page" :class="{ 'auth-page--setup': isSetup }">
    <PixelOcean class="auth-ocean" />

    <header class="auth-header">
      <div class="auth-brand">
        <span class="brand-mark"><FileSpreadsheet :size="19" /></span>
        <span class="auth-brand-name">
          <strong>AnyDatas</strong>
          <small>DATA WORKSPACE</small>
        </span>
      </div>
      <div class="auth-header-meta">
        <ThemeToggle variant="login" />
        <span class="auth-system-code">ANALYSIS SYSTEM / 01</span>
        <span class="auth-deployment">
          <span class="auth-status-dot" aria-hidden="true" />
          <ShieldCheck :size="15" />
          本机安全工作区
        </span>
      </div>
    </header>

    <main class="auth-stage">
      <section class="auth-story" aria-labelledby="auth-story-title">
        <div class="auth-kicker">
          <span>LOCAL-FIRST</span>
          <i aria-hidden="true" />
          DATA ANALYSIS WORKBENCH
        </div>
        <h1 id="auth-story-title">
          让每一份数据
          <span>流向答案。</span>
        </h1>
        <p>
          把 Excel / CSV 导入、多表 SQL 分析、定时任务与结果导出，
          收进同一个可靠的工作区。
        </p>

        <div class="auth-capabilities" aria-label="核心能力">
          <article>
            <span class="auth-capability-icon"><Database :size="17" /></span>
            <span>
              <strong>导入</strong>
              <small>Excel / CSV</small>
            </span>
          </article>
          <article>
            <span class="auth-capability-icon"><Braces :size="17" /></span>
            <span>
              <strong>查询</strong>
              <small>多表 SQL</small>
            </span>
          </article>
          <article>
            <span class="auth-capability-icon"><Clock3 :size="17" /></span>
            <span>
              <strong>调度</strong>
              <small>后台任务与追踪</small>
            </span>
          </article>
        </div>
      </section>

      <section class="auth-panel" aria-labelledby="auth-title">
        <span class="auth-panel-corner" aria-hidden="true" />
        <div class="auth-panel-meta">
          <span class="auth-mode">{{ isSetup ? '首次初始化' : '安全访问' }}</span>
          <span>ACCESS / 001</span>
        </div>

        <div class="auth-panel-heading">
          <h2 id="auth-title">{{ isSetup ? '创建管理员账户' : '欢迎回来' }}</h2>
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

        <form class="auth-form" novalidate @input="clearValidationError" @submit.prevent="submit">
          <el-alert
            v-if="validationError"
            class="auth-alert auth-validation-alert"
            :title="validationError"
            type="error"
            :closable="false"
            show-icon
          />

          <label v-if="isSetup" class="auth-field" :class="{ 'is-invalid': validationField === 'name' }">
            <span>管理员姓名</span>
            <el-input v-model="form.name" size="large" autocomplete="name" maxlength="80" :aria-invalid="validationField === 'name'">
              <template #prefix><UserRound :size="16" /></template>
            </el-input>
          </label>

          <label v-if="isSetup" class="auth-field" :class="{ 'is-invalid': validationField === 'workspaceName' }">
            <span>工作区名称</span>
            <el-input v-model="form.workspaceName" size="large" autocomplete="organization" maxlength="80" :aria-invalid="validationField === 'workspaceName'">
              <template #prefix><Building2 :size="16" /></template>
            </el-input>
          </label>

          <label class="auth-field" :class="{ 'is-invalid': validationField === 'email' }">
            <span>邮箱</span>
            <el-input v-model="form.email" size="large" type="email" autocomplete="email" :aria-invalid="validationField === 'email'">
              <template #prefix><Mail :size="16" /></template>
            </el-input>
          </label>

          <label class="auth-field" :class="{ 'is-invalid': validationField === 'password' }">
            <span>密码</span>
            <el-input
              v-model="form.password"
              size="large"
              type="password"
              :minlength="isSetup ? 12 : undefined"
              :autocomplete="isSetup ? 'new-password' : 'current-password'"
              :aria-invalid="validationField === 'password'"
              show-password
            >
              <template #prefix><LockKeyhole :size="16" /></template>
            </el-input>
            <small v-if="isSetup">至少 12 位</small>
          </label>

          <label v-if="isSetup" class="auth-field" :class="{ 'is-invalid': validationField === 'passwordConfirmation' }">
            <span>确认密码</span>
            <el-input
              v-model="form.passwordConfirmation"
              size="large"
              type="password"
              autocomplete="new-password"
              :aria-invalid="validationField === 'passwordConfirmation'"
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

        <div class="auth-panel-footer" aria-hidden="true">
          <span><i /> SYSTEM READY</span>
          <span>ENCRYPTED SESSION</span>
        </div>
      </section>
    </main>

    <footer class="auth-footer" aria-hidden="true">
      <span>ANYDATAS / LOCAL-FIRST ANALYTICS</span>
      <span class="auth-tide-status">
        <i />
        <span class="auth-tide-live">PIXEL OCEAN / 60 FPS</span>
        <span class="auth-tide-static">PIXEL OCEAN / STATIC</span>
      </span>
    </footer>
  </div>
</template>

<style scoped>
.auth-page {
  --auth-bg: #fbfdfc;
  --auth-header-bg: rgb(255 255 255 / 88%);
  --auth-header-line: rgb(203 214 209 / 78%);
  --auth-halo-strong: rgb(251 253 252 / 98%);
  --auth-halo-medium: rgb(251 253 252 / 91%);
  --auth-halo-soft: rgb(251 253 252 / 66%);
  --auth-halo-clear: rgb(251 253 252 / 0%);
  --auth-body-copy: #506059;
  --auth-text-shadow: rgb(251 253 252 / 92%);
  --auth-capability-line: rgb(203 214 209 / 74%);
  --auth-capability-bg: rgb(255 255 255 / 88%);
  --auth-panel-bg: #ffffff;
  --auth-field-text: #405048;
  --auth-input-bg: #fbfdfc;
  --auth-input-focus-bg: #ffffff;
  --auth-footer-text: rgb(64 80 72 / 72%);
  position: relative;
  isolation: isolate;
  height: 100%;
  min-height: 620px;
  display: grid;
  grid-template-rows: 64px minmax(0, 1fr) 38px;
  overflow: hidden;
  color: var(--text);
  background: var(--auth-bg);
}

:global(html[data-theme="dark"] .auth-page) {
  --auth-bg: #09110e;
  --auth-header-bg: rgb(11 24 19 / 88%);
  --auth-header-line: rgb(59 77 70 / 78%);
  --auth-halo-strong: rgb(9 17 14 / 98%);
  --auth-halo-medium: rgb(9 17 14 / 91%);
  --auth-halo-soft: rgb(9 17 14 / 70%);
  --auth-halo-clear: rgb(9 17 14 / 0%);
  --auth-body-copy: var(--muted);
  --auth-text-shadow: rgb(9 17 14 / 94%);
  --auth-capability-line: rgb(59 77 70 / 78%);
  --auth-capability-bg: rgb(17 27 23 / 88%);
  --auth-panel-bg: var(--panel);
  --auth-field-text: var(--text-secondary);
  --auth-input-bg: var(--panel-muted);
  --auth-input-focus-bg: var(--panel-elevated);
  --auth-footer-text: rgb(167 183 175 / 72%);
}

.auth-ocean {
  position: absolute;
  inset: 0;
  z-index: 0;
  width: 100%;
  height: 100%;
}

.auth-header {
  position: relative;
  z-index: 3;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 30px;
  background: var(--auth-header-bg);
  border-bottom: 1px solid var(--auth-header-line);
  backdrop-filter: blur(12px);
}

.auth-brand {
  display: flex;
  align-items: center;
  gap: 11px;
}

.auth-brand-name {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.auth-brand-name strong {
  font-size: 18px;
  line-height: 1.1;
  font-weight: 760;
  letter-spacing: -0.02em;
}

.auth-brand-name small {
  color: var(--muted);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 8px;
  line-height: 1.1;
  letter-spacing: 0.14em;
}

.auth-header-meta,
.auth-deployment {
  display: flex;
  align-items: center;
}

.auth-header-meta {
  gap: 20px;
}

.auth-system-code {
  color: var(--subtle);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
}

.auth-deployment {
  position: relative;
  gap: 7px;
  color: var(--muted);
  font-size: 12px;
}

.auth-deployment::before {
  content: "";
  width: 1px;
  height: 18px;
  margin-right: 13px;
  background: var(--line);
}

.auth-status-dot {
  width: 6px;
  height: 6px;
  background: var(--primary);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--primary-text) 12%, transparent);
}

.auth-stage {
  position: relative;
  z-index: 2;
  min-height: 0;
  width: min(1180px, calc(100% - 88px));
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 440px;
  align-items: center;
  gap: clamp(72px, 9vw, 148px);
  padding: 36px 0 44px;
  overflow-y: auto;
}

.auth-story {
  position: relative;
  isolation: isolate;
  align-self: center;
  max-width: 620px;
  padding-bottom: 26px;
}

.auth-story::before {
  content: "";
  position: absolute;
  z-index: -1;
  inset: -54px -76px -48px -58px;
  background: radial-gradient(
    ellipse at 42% 48%,
    var(--auth-halo-strong) 0%,
    var(--auth-halo-medium) 48%,
    var(--auth-halo-soft) 70%,
    var(--auth-halo-clear) 100%
  );
  pointer-events: none;
}

.auth-kicker {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 25px;
  color: var(--muted);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 9px;
  font-weight: 650;
  letter-spacing: 0.14em;
}

.auth-kicker span {
  padding: 6px 8px;
  color: var(--primary-hover);
  background: var(--primary-soft);
  border: 1px solid color-mix(in srgb, var(--primary-text) 18%, transparent);
}

.auth-kicker i {
  width: 28px;
  height: 1px;
  background: var(--line-strong);
}

.auth-story h1 {
  max-width: 590px;
  font-size: clamp(45px, 4.4vw, 67px);
  line-height: 1.08;
  font-weight: 760;
  letter-spacing: -0.055em;
  text-shadow: 0 2px 18px var(--auth-text-shadow);
}

.auth-story h1 span {
  display: block;
  color: var(--primary);
}

.auth-story > p {
  max-width: 520px;
  margin-top: 24px;
  color: var(--auth-body-copy);
  font-size: 15px;
  line-height: 1.8;
  text-shadow: 0 1px 12px var(--auth-text-shadow);
}

.auth-capabilities {
  max-width: 580px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  margin-top: 42px;
  padding: 1px;
  background: var(--auth-capability-line);
  border: 1px solid var(--auth-capability-line);
}

.auth-capabilities article {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 13px 12px;
  background: var(--auth-capability-bg);
  backdrop-filter: blur(5px);
}

.auth-capability-icon {
  flex: 0 0 auto;
  display: inline-grid;
  place-items: center;
  width: 33px;
  height: 33px;
  color: var(--primary);
  background: var(--primary-soft);
}

.auth-capabilities article > span:last-child {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.auth-capabilities strong {
  font-size: 12px;
  font-weight: 700;
}

.auth-capabilities small {
  overflow: hidden;
  color: var(--muted);
  font-size: 9px;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.auth-panel {
  position: relative;
  width: 440px;
  padding: 27px 30px 20px;
  overflow: hidden;
  background: var(--auth-panel-bg);
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  box-shadow:
    var(--shadow-lg),
    var(--shadow-xs);
}

.auth-panel::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  width: 98px;
  height: 3px;
  background: var(--primary);
}

.auth-panel-corner {
  position: absolute;
  top: 13px;
  right: 13px;
  width: 15px;
  height: 15px;
  border-top: 1px solid var(--line-strong);
  border-right: 1px solid var(--line-strong);
}

.auth-panel-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 19px;
}

.auth-panel-meta > span:last-child {
  padding-right: 18px;
  color: var(--subtle);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 8px;
  letter-spacing: 0.12em;
}

.auth-mode {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 8px;
  color: var(--primary-hover);
  background: var(--primary-soft);
  border: 1px solid color-mix(in srgb, var(--primary-text) 18%, transparent);
  font-size: 10px;
  font-weight: 700;
}

.auth-panel-heading {
  margin-bottom: 23px;
}

.auth-panel-heading h2 {
  margin: 0 0 7px;
  font-size: 25px;
  line-height: 1.25;
  font-weight: 750;
  letter-spacing: -0.025em;
}

.auth-panel-heading p {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}

.auth-alert {
  margin-bottom: 18px;
}

.auth-validation-alert {
  margin-bottom: 0;
}

.auth-form {
  display: grid;
  gap: 16px;
}

.auth-field {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: var(--auth-field-text);
  font-size: 12px;
  font-weight: 650;
}

.auth-field > small {
  color: var(--muted);
  font-size: 10px;
  font-weight: 500;
}

.auth-field.is-invalid > span {
  color: var(--el-color-danger);
}

.auth-field.is-invalid :deep(.el-input__wrapper) {
  border-color: var(--el-color-danger);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--el-color-danger) 14%, transparent);
}

.auth-field :deep(.el-input__wrapper) {
  min-height: 43px;
  background: var(--auth-input-bg);
  border: 1px solid var(--line);
  border-radius: 6px;
  box-shadow: none;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    background 160ms ease;
}

.auth-field :deep(.el-input__wrapper:hover) {
  border-color: var(--line-strong);
}

.auth-field :deep(.el-input__wrapper.is-focus) {
  background: var(--auth-input-focus-bg);
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-text) 14%, transparent);
}

.auth-field :deep(.el-input__prefix-inner) {
  color: var(--muted);
}

.auth-submit {
  width: 100%;
  min-height: 45px;
  margin-top: 5px;
  border-radius: 6px;
  box-shadow: 0 10px 24px color-mix(in srgb, var(--primary-solid) 26%, transparent);
}

.auth-submit :deep(span) {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.auth-panel-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 20px;
  padding-top: 15px;
  color: var(--muted);
  border-top: 1px solid var(--line);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 8px;
  letter-spacing: 0.08em;
}

.auth-panel-footer span:first-child,
.auth-tide-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.auth-panel-footer i,
.auth-tide-status i {
  width: 5px;
  height: 5px;
  background: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-text) 12%, transparent);
}

.auth-tide-static {
  display: none;
}

.auth-footer {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 30px;
  color: var(--auth-footer-text);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 8px;
  letter-spacing: 0.11em;
}

@media (max-width: 1080px) {
  .auth-page {
    min-height: 100%;
    grid-template-rows: 64px max-content 38px;
    overflow-y: auto;
  }

  .auth-stage {
    min-height: max-content;
    width: min(620px, calc(100% - 48px));
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: max-content max-content;
    align-items: start;
    align-content: start;
    gap: 42px;
    padding: 54px 0 76px;
    overflow: visible;
  }

  .auth-story {
    align-self: start;
    padding: 0;
  }

  .auth-story h1 {
    max-width: 560px;
    font-size: clamp(44px, 8vw, 62px);
  }

  .auth-panel {
    width: 100%;
  }

  .auth-footer {
    position: relative;
    height: 38px;
  }
}

@media (max-width: 640px) {
  .auth-page {
    grid-template-rows: 60px max-content 38px;
  }

  .auth-ocean {
    inset: 0;
    height: 100%;
  }

  .auth-header {
    padding: 0 18px;
  }

  .auth-system-code {
    display: none;
  }

  .auth-deployment::before {
    display: none;
  }

  .auth-deployment {
    font-size: 0;
  }

  .auth-deployment svg {
    width: 17px;
    height: 17px;
  }

  .auth-stage {
    display: flex;
    flex-direction: column;
    width: calc(100% - 30px);
    gap: 0;
    padding: 22px 0 44px;
  }

  .auth-panel {
    order: 2;
    margin-bottom: 30px;
  }

  .auth-story {
    display: contents;
  }

  .auth-story::before {
    content: none;
  }

  .auth-kicker {
    order: 3;
    width: calc(100% - 8px);
    margin: 0 4px 13px;
    font-size: 8px;
    letter-spacing: 0.08em;
  }

  .auth-kicker i {
    width: 14px;
  }

  .auth-story h1 {
    order: 1;
    width: calc(100% - 8px);
    margin: 0 4px 18px;
    padding: 10px 0;
    font-size: clamp(31px, 9.5vw, 40px);
    background: radial-gradient(
      ellipse at 32% 50%,
      var(--auth-halo-strong) 0%,
      var(--auth-halo-soft) 58%,
      var(--auth-halo-clear) 100%
    );
  }

  .auth-story > p {
    order: 4;
    width: calc(100% - 8px);
    margin: 0 4px;
    font-size: 13px;
  }

  .auth-capabilities {
    display: none;
  }

  .auth-panel {
    padding: 24px 21px 18px;
  }

  .auth-panel-meta > span:last-child,
  .auth-panel-footer > span:last-child {
    display: none;
  }

  .auth-footer {
    padding: 0 18px;
  }

  .auth-footer > span:first-child {
    display: none;
  }
}

@media (max-height: 760px) and (min-width: 1081px) {
  .auth-stage {
    padding-top: 22px;
    padding-bottom: 28px;
  }

  .auth-story h1 {
    font-size: 50px;
  }

  .auth-story > p {
    margin-top: 17px;
  }

  .auth-capabilities {
    margin-top: 28px;
  }

  .auth-page--setup .auth-panel {
    padding-top: 20px;
    padding-bottom: 15px;
  }

  .auth-page--setup .auth-panel-meta {
    margin-bottom: 12px;
  }

  .auth-page--setup .auth-panel-heading {
    margin-bottom: 15px;
  }

  .auth-page--setup .auth-form {
    gap: 11px;
  }

  .auth-page--setup .auth-field {
    gap: 5px;
  }

  .auth-page--setup .auth-field :deep(.el-input__wrapper) {
    min-height: 38px;
  }

  .auth-page--setup .auth-panel-footer {
    margin-top: 13px;
    padding-top: 11px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .auth-field :deep(.el-input__wrapper) {
    transition: none;
  }

  .auth-tide-live {
    display: none;
  }

  .auth-tide-static {
    display: inline;
  }
}
</style>
