<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  CheckCircle2,
  CircleAlert,
  KeyRound,
  PlugZap,
  Plus,
  Server,
  ShieldCheck,
  Sparkles,
  Wifi,
  WifiOff,
} from '@lucide/vue'

import { api, errorMessage } from '../api'
import type { AiMcpServerConfig, AiSettings, AiSkillSummary } from '../types'

interface McpDraft {
  name: string
  command: string
  argsText: string
  envText: string
  enabled: boolean
}

interface ProviderPreset {
  id: string
  name: string
  hint: string
  url: string
  models: string[]
  keyOptional: boolean
}

const PROVIDERS: ProviderPreset[] = [
  {
    id: 'openai',
    name: 'OpenAI',
    hint: '官方 Chat Completions',
    url: 'https://api.openai.com/v1',
    models: ['gpt-4.1', 'gpt-4o', 'gpt-4o-mini'],
    keyOptional: false,
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    hint: 'deepseek-chat / reasoner',
    url: 'https://api.deepseek.com/v1',
    models: ['deepseek-chat', 'deepseek-reasoner'],
    keyOptional: false,
  },
  {
    id: 'moonshot',
    name: 'Kimi',
    hint: '月之暗面兼容接口',
    url: 'https://api.moonshot.cn/v1',
    models: ['kimi-k2-0905-preview', 'moonshot-v1-auto'],
    keyOptional: false,
  },
  {
    id: 'zhipu',
    name: '智谱 GLM',
    hint: 'BigModel 兼容模式',
    url: 'https://open.bigmodel.cn/api/paas/v4',
    models: ['glm-4-plus', 'glm-4-flash'],
    keyOptional: false,
  },
  {
    id: 'qwen',
    name: '通义千问',
    hint: 'DashScope 兼容模式',
    url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: ['qwen-plus', 'qwen-turbo'],
    keyOptional: false,
  },
  {
    id: 'ollama',
    name: 'Ollama',
    hint: '本机，密钥可留空',
    url: 'http://127.0.0.1:11434/v1',
    models: ['llama3.1', 'qwen2.5', 'deepseek-r1'],
    keyOptional: true,
  },
  {
    id: 'custom',
    name: '自定义',
    hint: '任意 OpenAI 兼容地址',
    url: '',
    models: [],
    keyOptional: true,
  },
]

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const current = ref<AiSettings | null>(null)
const feedback = ref<{ type: 'success' | 'warning' | 'error'; text: string } | null>(null)
const lastTest = ref<{ ok: boolean; text: string } | null>(null)
const selectedPreset = ref('custom')
const form = reactive({
  enabled: false,
  baseUrl: 'https://api.openai.com/v1',
  model: '',
  apiKey: '',
  clearApiKey: false,
})
const mcpDrafts = ref<McpDraft[]>([])
const skills = ref<AiSkillSummary[]>([])
const savedSnapshot = ref('')

const activePreset = computed(() => (
  PROVIDERS.find((item) => item.id === selectedPreset.value) ?? PROVIDERS[PROVIDERS.length - 1]!
))
const isLocalEndpoint = computed(() => isLocalUrl(form.baseUrl))
const keyNeeded = computed(() => (
  form.enabled && !activePreset.value.keyOptional && !isLocalEndpoint.value
))
const modelOptions = computed(() => {
  const options = [...activePreset.value.models]
  if (form.model.trim() && !options.includes(form.model.trim())) {
    options.unshift(form.model.trim())
  }
  return options
})
const isDirty = computed(() => snapshotOf(form, mcpDrafts.value) !== savedSnapshot.value)
const statusLabel = computed(() => {
  if (!current.value) return '尚未配置'
  if (!form.enabled) return '已关闭'
  if (!form.model.trim()) return '未填写模型'
  if (keyNeeded.value && !current.value.apiKeyConfigured && !form.apiKey.trim()) return '缺少密钥'
  return '可以使用'
})
const statusKind = computed(() => {
  if (!form.enabled) return 'off'
  if (statusLabel.value === '可以使用') return 'ready'
  return 'warn'
})

watch(visible, (opened) => {
  if (opened) {
    feedback.value = null
    lastTest.value = null
    void loadSettings()
  }
})

function snapshotOf(value: typeof form, servers: McpDraft[]) {
  return JSON.stringify({
    enabled: value.enabled,
    baseUrl: value.baseUrl.trim(),
    model: value.model.trim(),
    apiKey: value.apiKey.trim(),
    clearApiKey: value.clearApiKey,
    mcp: servers,
  })
}

function isLocalUrl(value: string) {
  try {
    const host = new URL(value).hostname
    return host === 'localhost' || host === '127.0.0.1' || host === '::1'
  } catch {
    return false
  }
}

function matchPreset(url: string) {
  const normalized = url.trim().replace(/\/+$/u, '')
  const found = PROVIDERS.find((item) => (
    item.url && normalized.startsWith(item.url.replace(/\/+$/u, ''))
  ))
  return found?.id ?? 'custom'
}

function applyPreset(id: string) {
  const preset = PROVIDERS.find((item) => item.id === id)
  if (!preset) return
  selectedPreset.value = id
  if (preset.url) form.baseUrl = preset.url
  if (preset.models.length && !form.model.trim()) {
    form.model = preset.models[0] ?? ''
  }
}

function setFeedback(type: 'success' | 'warning' | 'error', text: string) {
  feedback.value = { type, text }
}

/** 读取只包含密钥状态的配置摘要，服务端不会把已保存 API Key 回传浏览器。 */
async function loadSettings() {
  loading.value = true
  try {
    const settings = await api.getAiSettings()
    applySettings(settings)
  } catch (error) {
    setFeedback('error', errorMessage(error))
  } finally {
    loading.value = false
  }
}

function applySettings(settings: AiSettings) {
  current.value = settings
  form.enabled = settings.enabled
  form.baseUrl = settings.baseUrl
  form.model = settings.model
  form.apiKey = ''
  form.clearApiKey = false
  mcpDrafts.value = (settings.mcpServers ?? []).map(toMcpDraft)
  skills.value = settings.skills ?? []
  selectedPreset.value = matchPreset(settings.baseUrl)
  savedSnapshot.value = snapshotOf(form, mcpDrafts.value)
}

function toMcpDraft(server: AiMcpServerConfig): McpDraft {
  return {
    name: server.name,
    command: server.command,
    argsText: (server.args ?? []).join('\n'),
    envText: Object.entries(server.env ?? {})
      .map(([key, value]) => `${key}=${value}`)
      .join('\n'),
    enabled: server.enabled !== false,
  }
}

function emptyMcpDraft(): McpDraft {
  return {
    name: '',
    command: '',
    argsText: '',
    envText: '',
    enabled: true,
  }
}

function addMcpServer() {
  if (mcpDrafts.value.length >= 8) {
    setFeedback('warning', '最多配置 8 个 MCP 服务器')
    return
  }
  mcpDrafts.value = [...mcpDrafts.value, emptyMcpDraft()]
}

function removeMcpServer(index: number) {
  mcpDrafts.value = mcpDrafts.value.filter((_, itemIndex) => itemIndex !== index)
}

function compileMcpServers(): AiMcpServerConfig[] | null {
  const servers: AiMcpServerConfig[] = []
  for (const draft of mcpDrafts.value) {
    const name = draft.name.trim()
    const command = draft.command.trim()
    if (!name && !command && !draft.argsText.trim() && !draft.envText.trim()) continue
    if (!/^[A-Za-z0-9_-]{1,32}$/.test(name)) {
      setFeedback('warning', 'MCP 名称仅允许字母、数字、下划线和短横线')
      return null
    }
    if (!command) {
      setFeedback('warning', `MCP “${name}” 需要填写启动命令`)
      return null
    }
    const env: Record<string, string> = {}
    for (const line of draft.envText.split('\n').map((item) => item.trim()).filter(Boolean)) {
      const index = line.indexOf('=')
      if (index <= 0) {
        setFeedback('warning', 'MCP 环境变量请使用 KEY=VALUE，每行一条')
        return null
      }
      env[line.slice(0, index)] = line.slice(index + 1)
    }
    servers.push({
      name,
      command,
      args: draft.argsText.split('\n').map((item) => item.trim()).filter(Boolean),
      env,
      enabled: draft.enabled,
    })
  }
  return servers
}

function validateForm() {
  if (!form.baseUrl.trim()) {
    setFeedback('warning', '请填写服务地址')
    return false
  }
  try {
    const parsed = new URL(form.baseUrl.trim())
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      setFeedback('warning', '服务地址必须是 http 或 https')
      return false
    }
  } catch {
    setFeedback('warning', '服务地址格式不正确')
    return false
  }
  if (form.enabled && !form.model.trim()) {
    setFeedback('warning', '启用 AI 前必须填写模型名称')
    return false
  }
  if (keyNeeded.value && !current.value?.apiKeyConfigured && !form.apiKey.trim()) {
    setFeedback('warning', '这个接口需要 API Key')
    return false
  }
  return true
}

/** 保存工作区配置；API Key 留空时保留已有密钥，显式勾选清除才会删除。 */
async function saveSettings(showMessage = true) {
  if (!validateForm()) return null
  const mcpServers = compileMcpServers()
  if (!mcpServers) return null
  saving.value = true
  feedback.value = null
  try {
    const settings = await api.updateAiSettings({
      enabled: form.enabled,
      baseUrl: form.baseUrl.trim().replace(/\/+$/u, ''),
      model: form.model.trim(),
      apiKey: form.apiKey.trim() || undefined,
      clearApiKey: form.clearApiKey,
      mcpServers,
    })
    applySettings(settings)
    if (showMessage) {
      setFeedback(
        'success',
        settings.enabled
          ? `已保存并启用 · ${settings.model}`
          : '已保存。AI 仍关闭，打开开关后再用来分析。',
      )
    }
    return settings
  } catch (error) {
    setFeedback('error', `保存失败：${errorMessage(error)}`)
    return null
  } finally {
    saving.value = false
  }
}

/** 先保存当前表单再测连通，避免测到磁盘里的旧配置。 */
async function saveAndTest() {
  const settings = await saveSettings(false)
  if (!settings) return
  testing.value = true
  lastTest.value = null
  try {
    const result = await api.testAiSettings()
    lastTest.value = { ok: true, text: `已连通 · ${result.model}` }
    setFeedback('success', `连接成功，当前模型 ${result.model}`)
  } catch (error) {
    lastTest.value = { ok: false, text: errorMessage(error) }
    setFeedback('error', `连接失败：${errorMessage(error)}`)
  } finally {
    testing.value = false
  }
}

async function handleBeforeClose(done: (cancel?: boolean) => void) {
  if (!isDirty.value || loading.value) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('有未保存的修改，确定关闭？', '关闭设置', {
      type: 'warning',
      confirmButtonText: '关闭',
      cancelButtonText: '继续编辑',
      confirmButtonClass: 'el-button--danger',
    })
    done()
  } catch {
    // 继续留在对话框
  }
}

function formatUpdatedAt(value: string | null) {
  if (!value) return '从未保存'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <el-dialog
    v-model="visible"
    class="ai-settings-dialog"
    width="720px"
    append-to-body
    align-center
    :show-close="true"
    :close-on-click-modal="false"
    :before-close="handleBeforeClose"
  >
    <template #header>
      <div class="ai-settings-header">
        <span class="ai-settings-mark"><Sparkles :size="18" /></span>
        <div>
          <strong>AI 分析设置</strong>
          <span>工作区共用。密钥只保存在本机数据目录，接口不会回传明文。</span>
        </div>
      </div>
    </template>

    <div class="ai-settings" v-loading="loading">
      <section class="ai-settings-status" :class="statusKind">
        <div>
          <strong>{{ statusLabel }}</strong>
          <span>最近保存 {{ formatUpdatedAt(current?.updatedAt ?? null) }}</span>
        </div>
        <label class="ai-enable">
          <span>{{ form.enabled ? '已启用' : '已关闭' }}</span>
          <el-switch v-model="form.enabled" />
        </label>
      </section>

      <section class="ai-settings-block">
        <header>
          <h3>接口</h3>
          <p>选择一个兼容 OpenAI Chat Completions 的服务，或自己填地址。</p>
        </header>
        <div class="ai-provider-grid">
          <button
            v-for="preset in PROVIDERS"
            :key="preset.id"
            type="button"
            class="ai-provider-chip"
            :class="{ active: selectedPreset === preset.id }"
            @click="applyPreset(preset.id)"
          >
            <strong>{{ preset.name }}</strong>
            <span>{{ preset.hint }}</span>
          </button>
        </div>
      </section>

      <section class="ai-settings-block">
        <header>
          <h3>连接</h3>
          <p v-if="isLocalEndpoint">本机地址可以不填密钥。</p>
          <p v-else-if="keyNeeded">公网接口需要密钥，且应使用 HTTPS。</p>
          <p v-else>填好地址和模型后，先测连接再启用。</p>
        </header>

        <label class="ai-field">
          <span><PlugZap :size="14" /> 服务地址</span>
          <el-input
            v-model="form.baseUrl"
            placeholder="https://api.openai.com/v1"
            maxlength="500"
            @change="selectedPreset = matchPreset(form.baseUrl)"
          />
        </label>

        <label class="ai-field">
          <span><Server :size="14" /> 模型</span>
          <el-select
            v-if="modelOptions.length"
            v-model="form.model"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入模型名"
          >
            <el-option v-for="name in modelOptions" :key="name" :label="name" :value="name" />
          </el-select>
          <el-input
            v-else
            v-model="form.model"
            placeholder="例如 gpt-4o、deepseek-chat"
            maxlength="160"
          />
        </label>

        <label class="ai-field">
          <span><KeyRound :size="14" /> API Key</span>
          <el-input
            v-model="form.apiKey"
            type="password"
            show-password
            maxlength="4096"
            :disabled="form.clearApiKey"
            :placeholder="form.clearApiKey
              ? '将清除已保存的密钥'
              : (current?.apiKeyConfigured ? '已保存，留空则保持不变' : (activePreset.keyOptional ? '可选' : '粘贴密钥'))"
          />
        </label>

        <div class="ai-key-row">
          <span v-if="current?.apiKeyConfigured && !form.clearApiKey" class="ai-key-pill saved">
            <ShieldCheck :size="14" /> 密钥已加密保存在本机
          </span>
          <span v-else-if="form.clearApiKey" class="ai-key-pill warn">
            <CircleAlert :size="14" /> 保存后将删除密钥
          </span>
          <span v-else class="ai-key-pill">
            <KeyRound :size="14" /> {{ keyNeeded ? '尚未保存密钥' : '此接口可以不填密钥' }}
          </span>
          <el-checkbox
            v-if="current?.apiKeyConfigured"
            v-model="form.clearApiKey"
            :disabled="Boolean(form.apiKey.trim())"
          >
            清除已保存的密钥
          </el-checkbox>
        </div>
      </section>

      <section class="ai-settings-block">
        <header>
          <h3>MCP</h3>
          <p>stdio 进程，映射为 Agent 工具。最多 8 个。环境变量按 KEY=VALUE 分行填写。</p>
        </header>
        <div class="ai-mcp-list">
          <article v-for="(server, index) in mcpDrafts" :key="index" class="ai-mcp-row">
            <div class="ai-mcp-row-head">
              <el-switch v-model="server.enabled" />
              <el-input v-model="server.name" maxlength="32" placeholder="名称，如 filesystem" />
              <el-button text type="danger" @click="removeMcpServer(index)">移除</el-button>
            </div>
            <el-input v-model="server.command" maxlength="500" placeholder="启动命令，如 npx" />
            <el-input
              v-model="server.argsText"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 4 }"
              placeholder="参数，每行一个"
            />
            <el-input
              v-model="server.envText"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 4 }"
              placeholder="环境变量，每行 KEY=VALUE"
            />
          </article>
        </div>
        <el-button :disabled="mcpDrafts.length >= 8" @click="addMcpServer">
          <Plus :size="14" />添加 MCP 服务器
        </el-button>
      </section>

      <section class="ai-settings-block">
        <header>
          <h3>Skills</h3>
          <p>扫描数据目录 agent-skills/{'{name}'}/SKILL.md，此处只读。</p>
        </header>
        <ul v-if="skills.length" class="ai-skill-list">
          <li v-for="skill in skills" :key="skill.name">
            <strong>{{ skill.name }}</strong>
            <span>{{ skill.description || '无描述' }}</span>
          </li>
        </ul>
        <p v-else class="ai-settings-empty">还没有 Skill。放好文件后重新打开此对话框即可看到。</p>
      </section>

      <section v-if="lastTest || feedback" class="ai-settings-result" :class="lastTest?.ok === false || feedback?.type === 'error' ? 'error' : lastTest?.ok ? 'ok' : feedback?.type">
        <CheckCircle2 v-if="lastTest?.ok || feedback?.type === 'success'" :size="16" />
        <Wifi v-else-if="testing" :size="16" />
        <WifiOff v-else-if="lastTest?.ok === false || feedback?.type === 'error'" :size="16" />
        <CircleAlert v-else :size="16" />
        <p>{{ lastTest?.text || feedback?.text }}</p>
      </section>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button :loading="testing" :disabled="saving" @click="saveAndTest">
        保存并测试
      </el-button>
      <el-button type="primary" :loading="saving" :disabled="testing" @click="saveSettings()">
        {{ form.enabled ? '保存' : '保存（保持关闭）' }}
      </el-button>
    </template>
  </el-dialog>
</template>
