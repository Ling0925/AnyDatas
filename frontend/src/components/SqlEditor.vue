<script setup lang="ts">
import { onMounted, shallowRef, type Component } from 'vue'

import { loadSqlEditor } from '../monaco'

withDefaults(defineProps<{
  modelValue: string
  language?: string
  theme?: string
  options?: Record<string, unknown>
}>(), {
  language: 'sql',
  theme: 'vs',
  options: () => ({}),
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'before-mount': [monaco: unknown]
}>()

const editorComponent = shallowRef<Component | null>(null)
const loadFailed = shallowRef(false)

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
    :value="modelValue"
    :language="language"
    :theme="theme"
    :options="options"
    @update:value="emit('update:modelValue', $event)"
    @before-mount="emit('before-mount', $event)"
  />
  <div v-else class="editor-loading">
    {{ loadFailed ? '编辑器加载失败' : '正在加载编辑器' }}
  </div>
</template>
