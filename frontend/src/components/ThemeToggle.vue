<script setup lang="ts">
import { computed } from 'vue'
import { Moon, Sun } from '@lucide/vue'

import { useTheme } from '../theme'

withDefaults(defineProps<{
  variant?: 'shell' | 'login'
}>(), {
  variant: 'shell',
})

const { isDark, toggleTheme } = useTheme()
const actionLabel = computed(() => (isDark.value ? '切换到亮色模式' : '切换到夜间模式'))
</script>

<template>
  <el-tooltip :content="actionLabel" placement="bottom">
    <button
      class="theme-toggle-button"
      :class="`theme-toggle-button--${variant}`"
      type="button"
      :aria-label="actionLabel"
      :title="actionLabel"
      @click="toggleTheme"
    >
      <Sun v-if="isDark" :size="16" />
      <Moon v-else :size="16" />
    </button>
  </el-tooltip>
</template>

<style scoped>
.theme-toggle-button {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  color: var(--muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 5px;
  cursor: pointer;
  transition:
    color 160ms ease,
    border-color 160ms ease,
    background 160ms ease;
}

.theme-toggle-button:hover {
  color: var(--primary-text);
  background: var(--panel-muted);
  border-color: var(--line);
}

.theme-toggle-button--login {
  background: color-mix(in srgb, var(--panel) 78%, transparent);
  border-color: var(--line);
  backdrop-filter: blur(8px);
}
</style>
