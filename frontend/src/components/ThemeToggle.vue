<script setup lang="ts">
import { computed } from 'vue'
import { Moon, Sun } from '@lucide/vue'

import { useTheme } from '../theme'

withDefaults(defineProps<{
  variant?: 'shell' | 'login'
}>(), {
  variant: 'shell',
})

const { isDark, isThemeTransitioning, toggleTheme } = useTheme()
const actionLabel = computed(() => (isDark.value ? '切换到亮色模式' : '切换到夜间模式'))

/**
 * 从主题按钮中心触发全页主题动画。
 * 为什么这么做：鼠标点击与键盘触发都应从同一稳定位置扩散，不能依赖可能为零的事件坐标；
 * 好处：桌面顶栏和移动端登录页会得到一致、可预测的视觉反馈。
 *
 * @param event 主题按钮点击事件。
 */
function handleThemeToggle(event: MouseEvent) {
  const button = event.currentTarget as HTMLElement
  const bounds = button.getBoundingClientRect()
  toggleTheme({
    x: bounds.left + bounds.width / 2,
    y: bounds.top + bounds.height / 2,
  })
}
</script>

<template>
  <el-tooltip :content="actionLabel" placement="bottom">
    <button
      class="theme-toggle-button"
      :class="`theme-toggle-button--${variant}`"
      type="button"
      :aria-label="actionLabel"
      :aria-busy="isThemeTransitioning"
      :aria-disabled="isThemeTransitioning"
      :title="actionLabel"
      @click="handleThemeToggle"
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

.theme-toggle-button[aria-busy="true"] {
  cursor: wait;
}
</style>
