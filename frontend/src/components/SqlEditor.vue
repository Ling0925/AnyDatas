<script setup lang="ts">
import { computed, onMounted, shallowRef, type Component } from 'vue'

import { loadSqlEditor } from '../monaco'
import { useTheme } from '../theme'

const props = withDefaults(defineProps<{
  modelValue: string
  language?: string
  theme?: string
  options?: Record<string, unknown>
}>(), {
  language: 'sql',
  options: () => ({}),
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'before-mount': [monaco: unknown]
}>()

const editorComponent = shallowRef<Component | null>(null)
const loadFailed = shallowRef(false)
const { isDark } = useTheme()

/**
 * 解析 Monaco 当前应使用的主题。
 * 为什么这么做：少数组件仍可能需要显式覆盖主题，而常规编辑器应跟随全局明暗模式；
 * 好处：保留原有扩展能力的同时，主题切换会由响应式状态立即传递给 Monaco。
 */
const resolvedEditorTheme = computed(() => (
  props.theme ?? (isDark.value ? 'vs-dark' : 'vs')
))

/** 组件挂载后再请求编辑器分块，加载失败时保留稳定占位而不破坏整个页面。 */
onMounted(async () => {
  try {
    editorComponent.value = await loadSqlEditor()
  } catch {
    loadFailed.value = true
  }
})
</script>

<template>
  <component
    :is="editorComponent"
    v-if="editorComponent"
    :value="props.modelValue"
    :language="props.language"
    :theme="resolvedEditorTheme"
    :options="props.options"
    @update:value="emit('update:modelValue', $event)"
    @before-mount="emit('before-mount', $event)"
  />
  <div v-else class="editor-loading">
    {{ loadFailed ? '编辑器加载失败' : '正在加载编辑器' }}
  </div>
</template>
