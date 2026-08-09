<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  ArrowRight,
  DownloadCloud,
  FileSpreadsheet,
  HardDrive,
  Server,
  ShieldCheck,
  Wifi,
} from '@lucide/vue'

import PixelOcean from '../components/PixelOcean.vue'
import ThemeToggle from '../components/ThemeToggle.vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const selectedMode = ref<'standalone' | 'remote'>('standalone')
const serverUrl = ref('')
const status = ref<DesktopBackendStatus>({
  mode: null,
  phase: 'unconfigured',
  serverUrl: null,
  serverVersion: null,
  protocolVersion: null,
  message: '请选择运行模式',
  progress: null,
})
let unsubscribe: (() => void) | undefined

const busy = computed(() => status.value.phase === 'starting' || status.value.phase === 'downloading')
const progressPercentage = computed(() => {
  if (status.value.progress === null) return undefined
  return Math.round(Math.max(0, Math.min(1, status.value.progress)) * 100)
})

/**
 * 读取主进程状态并订阅下载、启动和崩溃事件。
 *
 * 为什么这么做：服务端运行时完全位于 Electron 主进程；好处是页面重载后仍能恢复真实进度，而不是重新发起下载。
 */
onMounted(async () => {
  status.value = await window.desktop.getBackendStatus()
  if (status.value.mode !== null) selectedMode.value = status.value.mode
  if (status.value.mode === 'remote' && status.value.serverUrl) {
    serverUrl.value = status.value.serverUrl
  }
  unsubscribe = window.desktop.onBackendStatus((next) => {
    status.value = next
  })
})

/**
 * 窗口离开模式页时解除 IPC 监听。
 *
 * 为什么这么做：路由可以多次进入该页面；好处是避免重复状态通知和已经卸载页面上的内存泄漏。
 */
onBeforeUnmount(() => unsubscribe?.())

/**
 * 提交用户选择并只在服务端握手成功后进入登录页。
 *
 * 为什么这么做：登录页面依赖确定的 API 目标；好处是下载失败、地址错误和版本不兼容都会留在当前页给出明确反馈。
 */
async function connect() {
  if (busy.value) return
  if (selectedMode.value === 'remote' && !serverUrl.value.trim()) {
    ElMessage.warning('请输入服务器地址')
    return
  }
  try {
    await window.desktop.configureBackend(
      selectedMode.value === 'standalone'
        ? { mode: 'standalone' }
        : { mode: 'remote', serverUrl: serverUrl.value.trim() },
    )
    auth.resetForBackendChange()
    ElMessage.success(selectedMode.value === 'standalone' ? '单机服务已启动' : '服务器连接成功')
    await router.replace('/login')
  } catch {
    // 主进程已经把经过清理的错误写入 status，避免在渲染层展示下载响应或进程细节。
  }
}
</script>

<template>
  <div class="connection-page">
    <PixelOcean class="connection-ocean" />

    <header class="connection-header">
      <div class="connection-brand">
        <span class="brand-mark"><FileSpreadsheet :size="19" /></span>
        <span><strong>AnyDatas</strong><small>DESKTOP RUNTIME</small></span>
      </div>
      <ThemeToggle variant="login" />
    </header>

    <main class="connection-stage">
      <section class="connection-intro">
        <span class="connection-kicker">RUNTIME / SELECT</span>
        <h1>选择数据运行的地方</h1>
        <p>单机模式把数据留在当前电脑；连接服务器适合团队共享同一个工作区。</p>
        <div class="connection-trust">
          <ShieldCheck :size="17" />
          <span>服务端启动或握手成功后才会进入登录页面</span>
        </div>
      </section>

      <section class="connection-panel" aria-labelledby="connection-title">
        <div class="connection-panel-heading">
          <span>CONNECTION / 001</span>
          <h2 id="connection-title">运行模式</h2>
        </div>

        <div class="mode-grid">
          <button
            type="button"
            class="mode-card"
            :class="{ active: selectedMode === 'standalone' }"
            :disabled="busy"
            @click="selectedMode = 'standalone'"
          >
            <span class="mode-icon"><HardDrive :size="22" /></span>
            <span class="mode-copy">
              <strong>单机模式</strong>
              <small>自动下载并随桌面端启动服务</small>
            </span>
            <span class="mode-radio" aria-hidden="true" />
          </button>

          <button
            type="button"
            class="mode-card"
            :class="{ active: selectedMode === 'remote' }"
            :disabled="busy"
            @click="selectedMode = 'remote'"
          >
            <span class="mode-icon"><Server :size="22" /></span>
            <span class="mode-copy">
              <strong>连接服务器</strong>
              <small>使用团队或已部署的 AnyDatas</small>
            </span>
            <span class="mode-radio" aria-hidden="true" />
          </button>
        </div>

        <label v-if="selectedMode === 'remote'" class="server-field">
          <span>服务器地址</span>
          <el-input
            v-model="serverUrl"
            size="large"
            placeholder="例如 https://data.example.com 或 192.168.8.108:8080"
            :disabled="busy"
            @keyup.enter="connect"
          >
            <template #prefix><Wifi :size="16" /></template>
          </el-input>
          <small>非本机地址建议使用 HTTPS，地址验证成功后才会保存。</small>
        </label>

        <div v-if="status.phase !== 'unconfigured'" class="runtime-status" :class="`is-${status.phase}`">
          <DownloadCloud v-if="status.phase === 'downloading'" :size="17" />
          <ShieldCheck v-else :size="17" />
          <span>
            <strong>{{ status.message }}</strong>
            <small v-if="status.serverVersion">服务端 {{ status.serverVersion }} · 协议 {{ status.protocolVersion }}</small>
          </span>
        </div>

        <el-progress
          v-if="status.phase === 'downloading' && progressPercentage !== undefined"
          class="runtime-progress"
          :percentage="progressPercentage"
          :stroke-width="7"
        />

        <button class="connection-submit" type="button" :disabled="busy" @click="connect">
          <span>{{ busy ? status.message : selectedMode === 'standalone' ? '启动单机服务' : '验证并连接' }}</span>
          <ArrowRight v-if="!busy" :size="18" />
        </button>
      </section>
    </main>
  </div>
</template>

<style scoped>
.connection-page {
  --panel: rgb(252 254 253 / 92%);
  --line: rgb(20 125 100 / 20%);
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  color: var(--text);
  background: var(--app-bg);
}

.connection-ocean {
  position: fixed;
  inset: 0;
  z-index: 0;
}

.connection-header,
.connection-stage {
  position: relative;
  z-index: 1;
}

.connection-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 76px;
  padding: 0 clamp(24px, 5vw, 72px);
}

.connection-brand {
  display: flex;
  gap: 12px;
  align-items: center;
}

.connection-brand > span:last-child {
  display: grid;
  gap: 1px;
}

.connection-brand small,
.connection-kicker,
.connection-panel-heading > span {
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .16em;
}

.connection-stage {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(440px, 560px);
  gap: clamp(48px, 8vw, 128px);
  align-items: center;
  width: min(1180px, calc(100% - 48px));
  min-height: calc(100vh - 116px);
  margin: 0 auto 40px;
}

.connection-intro {
  max-width: 520px;
}

.connection-intro h1 {
  max-width: 500px;
  margin: 18px 0;
  font-size: clamp(38px, 5vw, 68px);
  line-height: 1.06;
  letter-spacing: -.045em;
}

.connection-intro > p {
  max-width: 470px;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1.8;
}

.connection-trust {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 30px;
  color: var(--primary-text);
  font-size: 13px;
}

.connection-panel {
  padding: 30px;
  border: 1px solid var(--line);
  border-radius: 22px;
  background: var(--panel);
  box-shadow: 0 28px 80px rgb(9 60 47 / 12%);
  backdrop-filter: blur(20px);
}

.connection-panel-heading {
  margin-bottom: 22px;
}

.connection-panel-heading h2 {
  margin: 7px 0 0;
  font-size: 28px;
}

.mode-grid {
  display: grid;
  gap: 12px;
}

.mode-card {
  display: grid;
  grid-template-columns: 44px 1fr 18px;
  gap: 13px;
  align-items: center;
  width: 100%;
  padding: 17px;
  color: inherit;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--panel-elevated);
  cursor: pointer;
  transition: border-color .2s ease, transform .2s ease, background .2s ease;
}

.mode-card:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: var(--primary-solid);
}

.mode-card.active {
  border-color: var(--primary-solid);
  background: rgb(20 125 100 / 8%);
}

.mode-icon {
  display: grid;
  width: 44px;
  height: 44px;
  color: var(--primary-text);
  place-items: center;
  border-radius: 12px;
  background: rgb(20 125 100 / 10%);
}

.mode-copy {
  display: grid;
  gap: 5px;
}

.mode-copy small,
.server-field small,
.runtime-status small {
  color: var(--muted);
  font-size: 12px;
}

.mode-radio {
  width: 16px;
  height: 16px;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
}

.mode-card.active .mode-radio {
  border: 5px solid var(--primary-solid);
}

.server-field {
  display: grid;
  gap: 8px;
  margin-top: 20px;
  font-size: 13px;
  font-weight: 650;
}

.runtime-status {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 18px;
  padding: 12px 14px;
  color: var(--primary-text);
  border-radius: 10px;
  background: rgb(20 125 100 / 8%);
}

.runtime-status.is-failed {
  color: var(--el-color-danger);
  background: rgb(220 38 38 / 8%);
}

.runtime-status > span {
  display: grid;
  gap: 3px;
}

.runtime-progress {
  margin-top: 12px;
}

.connection-submit {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 48px;
  margin-top: 22px;
  padding: 0 18px;
  color: white;
  font-weight: 700;
  border: 0;
  border-radius: 12px;
  background: var(--primary-solid);
  cursor: pointer;
}

.connection-submit:disabled {
  cursor: wait;
  opacity: .65;
}

:global(html[data-theme="dark"]) .connection-page {
  --panel: rgb(15 25 21 / 92%);
  --line: rgb(76 190 159 / 22%);
}

@media (max-width: 900px) {
  .connection-stage {
    grid-template-columns: 1fr;
    gap: 28px;
    align-items: start;
    padding-top: 30px;
  }

  .connection-intro h1 {
    font-size: 42px;
  }
}

@media (max-width: 560px) {
  .connection-header {
    padding-inline: 18px;
  }

  .connection-stage {
    width: calc(100% - 28px);
  }

  .connection-intro {
    display: none;
  }

  .connection-panel {
    padding: 20px;
    border-radius: 16px;
  }
}
</style>
