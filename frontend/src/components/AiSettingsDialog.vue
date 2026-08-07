<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Bot, CheckCircle2, KeyRound, PlugZap } from '@lucide/vue'

import { api, errorMessage } from '../api'
import type { AiSettings } from '../types'

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
/** 对话框内固定反馈区，避免 toast 被遮罩挡住或一闪而过。 */
const feedback = ref<{ type: 'success' | 'warning' | 'error'; text: string } | null>(null)
const form = reactive({
  enabled: false,
  baseUrl: 'https://api.openai.com/v1',
  model: '',
  apiKey: '',
  clearApiKey: false,
})

watch(visible, (opened) => {
  if (opened) {
    feedback.value = null
    void loadSettings()
  }
})

function setFeedback(type: 'success' | 'warning' | 'error', text: string) {
  feedback.value = { type, text }
  if (type === 'success') ElMessage.success(text)
  else if (type === 'warning') ElMessage.warning(text)
  else ElMessage.error(text)
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

/** 保存工作区配置；API Key 留空时保留原密钥，显式勾选清除才会删除。 */
async function saveSettings(showMessage = true) {
  if (!form.baseUrl.trim()) {
    setFeedback('warning', '请输入 Base URL')
    return null
  }
  if (form.enabled && !form.model.trim()) {
    setFeedback('warning', '启用 AI 前必须填写模型名称')
    return null
  }
  saving.value = true
  feedback.value = null
  try {
    const settings = await api.updateAiSettings({
      enabled: form.enabled,
      baseUrl: form.baseUrl.trim(),
      model: form.model.trim(),
      apiKey: form.apiKey.trim() || undefined,
      clearApiKey: form.clearApiKey,
    })
    applySettings(settings)
    if (showMessage) {
      setFeedback(
        'success',
        settings.enabled
          ? `AI 设置已保存并启用 · ${settings.model || '未命名模型'}`
          : 'AI 设置已保存（当前未启用）',
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

/** 先保存表单再发起最小 Chat 请求，测试结果对应的就是当前可见配置。 */
async function saveAndTest() {
  if (!form.model.trim()) {
    setFeedback('warning', '请输入模型名称')
    return
  }
  const settings = await saveSettings(false)
  if (!settings) return
  testing.value = true
  try {
    const result = await api.testAiSettings()
    setFeedback('success', `连接成功 · ${result.model}`)
  } catch (error) {
    setFeedback('error', `连接测试失败：${errorMessage(error)}`)
  } finally {
    testing.value = false
  }
}

/** 用服务端摘要覆盖表单但主动清空密钥输入，避免浏览器长期保留敏感值。 */
function applySettings(settings: AiSettings) {
  current.value = settings
  form.enabled = settings.enabled
  form.baseUrl = settings.baseUrl
  form.model = settings.model
  form.apiKey = ''
  form.clearApiKey = false
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="工作区 AI 设置"
    width="560px"
    append-to-body
    align-center
    :close-on-click-modal="false"
  >
    <div class="ai-settings" v-loading="loading">
      <div class="ai-provider-row">
        <span class="ai-provider-icon"><Bot :size="19" /></span>
        <div>
          <strong>OpenAI Chat Completions</strong>
          <span>/chat/completions</span>
        </div>
        <el-switch v-model="form.enabled" inline-prompt active-text="开" inactive-text="关" />
      </div>

      <el-alert
        v-if="feedback"
        class="ai-settings-feedback"
        :type="feedback.type"
        :title="feedback.text"
        show-icon
        :closable="true"
        @close="feedback = null"
      />

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="Base URL">
          <el-input v-model="form.baseUrl" placeholder="https://api.openai.com/v1" maxlength="500">
            <template #prefix><PlugZap :size="15" /></template>
          </el-input>
        </el-form-item>
        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="填写接口支持的模型名称" maxlength="160" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.apiKey"
            type="password"
            show-password
            maxlength="4096"
            :placeholder="current?.apiKeyConfigured ? '已安全保存，留空保持不变' : '可选，本地兼容接口可以留空'"
          >
            <template #prefix><KeyRound :size="15" /></template>
          </el-input>
        </el-form-item>
        <el-checkbox v-if="current?.apiKeyConfigured" v-model="form.clearApiKey">
          清除已保存的 API Key
        </el-checkbox>
      </el-form>

      <div v-if="current?.apiKeyConfigured" class="ai-key-status">
        <CheckCircle2 :size="15" />
        API Key 已加密保存
      </div>

      <p class="ai-settings-hint">
        启用后地址会做网络校验：默认拒绝本机/私网；本地模型请部署时设置
        <code>ANYDATAS_AI_ALLOW_PRIVATE_NETWORK=1</code>。
      </p>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button aria-label="保存并测试 AI 设置" :loading="testing" :disabled="saving" @click="saveAndTest">
        保存并测试
      </el-button>
      <el-button
        type="primary"
        aria-label="保存 AI 设置"
        :loading="saving"
        :disabled="testing"
        @click="saveSettings()"
      >
        保存
      </el-button>
    </template>
  </el-dialog>
</template>
