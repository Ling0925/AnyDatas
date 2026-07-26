import { computed, readonly, ref } from 'vue'

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'anydatas.theme'

const preference = ref<ThemePreference>('system')
const systemPrefersDark = ref(false)
const resolvedTheme = computed<ResolvedTheme>(() => (
  preference.value === 'system'
    ? systemPrefersDark.value ? 'dark' : 'light'
    : preference.value
))
const isDark = computed(() => resolvedTheme.value === 'dark')

let initialized = false
let systemThemeQuery: MediaQueryList | null = null

/**
 * 判断持久化值是否是受支持的显式主题。
 * 为什么这么做：localStorage 可能包含旧版本或被手工修改的任意字符串；
 * 好处：无效值会安全回退为跟随系统，不会让根节点进入未知主题状态。
 *
 * @param value 待判断的持久化字符串。
 * @returns 是否为亮色或暗色主题。
 */
function isStoredTheme(value: string | null): value is Exclude<ThemePreference, 'system'> {
  return value === 'light' || value === 'dark'
}

/**
 * 将当前解析主题同步到文档根节点。
 * 为什么这么做：Element Plus 的 Teleport 浮层不在业务页面 DOM 内，只给局部容器加类无法覆盖；
 * 好处：普通 CSS、Element Plus、Canvas 和编辑器都能从同一根主题状态读取结果。
 */
function applyResolvedTheme() {
  if (typeof document === 'undefined') return

  const nextTheme = resolvedTheme.value
  const root = document.documentElement
  const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  root.dataset.theme = nextTheme
  root.classList.toggle('dark', nextTheme === 'dark')
  root.style.colorScheme = nextTheme
  themeColor?.setAttribute('content', nextTheme === 'dark' ? '#0b1210' : '#147d64')
}

/**
 * 安全读取用户保存的主题偏好。
 * 为什么这么做：隐私模式或受限浏览器环境可能拒绝 localStorage 访问；
 * 好处：存储不可用时仍可正常跟随系统，不会阻断应用启动。
 *
 * @returns 已保存的显式主题；不存在或不可用时返回 null。
 */
function readStoredTheme(): Exclude<ThemePreference, 'system'> | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isStoredTheme(value) ? value : null
  } catch {
    return null
  }
}

/**
 * 安全保存或清除用户主题偏好。
 * 为什么这么做：主题切换不应因浏览器拒绝持久化而抛出异常；
 * 好处：支持存储时可以跨会话记忆，不支持时当前页面仍立即生效。
 *
 * @param nextPreference 要保存的主题偏好。
 */
function persistTheme(nextPreference: ThemePreference) {
  try {
    if (nextPreference === 'system') {
      window.localStorage.removeItem(THEME_STORAGE_KEY)
      return
    }
    window.localStorage.setItem(THEME_STORAGE_KEY, nextPreference)
  } catch {
    // 存储失败不影响当前页面的主题状态。
  }
}

/**
 * 响应操作系统主题变化。
 * 为什么这么做：未手动选择主题的用户期望应用实时跟随系统；
 * 好处：系统在日落或计划任务中切换外观时，无需刷新页面即可同步。
 *
 * @param event 系统颜色偏好媒体查询事件。
 */
function handleSystemThemeChange(event: MediaQueryListEvent) {
  systemPrefersDark.value = event.matches
  if (preference.value === 'system') applyResolvedTheme()
}

/**
 * 同步其他标签页中的主题选择。
 * 为什么这么做：同一应用可能同时打开工作台和任务页，两个标签页应保持一致；
 * 好处：用户在任一标签页切换后，其他页面无需刷新即可立即更新。
 *
 * @param event 浏览器存储变化事件。
 */
function handleStorageChange(event: StorageEvent) {
  if (event.key !== THEME_STORAGE_KEY) return

  preference.value = isStoredTheme(event.newValue) ? event.newValue : 'system'
  applyResolvedTheme()
}

/**
 * 初始化全局主题状态。
 * 为什么这么做：在 Vue 挂载前写入根主题可以减少首屏亮色闪烁；
 * 好处：首次访问跟随系统，之后优先恢复用户的手动选择。
 */
export function initializeTheme() {
  if (initialized || typeof window === 'undefined') return

  initialized = true
  systemThemeQuery = window.matchMedia('(prefers-color-scheme: dark)')
  systemPrefersDark.value = systemThemeQuery.matches
  preference.value = readStoredTheme() ?? 'system'
  applyResolvedTheme()
  systemThemeQuery.addEventListener('change', handleSystemThemeChange)
  window.addEventListener('storage', handleStorageChange)
}

/**
 * 设置全局主题偏好。
 * 为什么这么做：所有主题入口必须走同一更新路径，避免根节点和持久化状态不一致；
 * 好处：登录页与工作区中的切换按钮会立即同步，并可选择重新跟随系统。
 *
 * @param nextPreference 系统、亮色或暗色偏好。
 */
export function setThemePreference(nextPreference: ThemePreference) {
  preference.value = nextPreference
  persistTheme(nextPreference)
  applyResolvedTheme()
}

/**
 * 在当前解析主题的相反模式间切换。
 * 为什么这么做：按钮只需要提供一次点击即可完成明暗切换；
 * 好处：即使当前正在跟随系统，首次点击也会保存明确且可预测的用户选择。
 */
export function toggleTheme() {
  setThemePreference(isDark.value ? 'light' : 'dark')
}

/**
 * 暴露只读主题状态和统一操作。
 * 为什么这么做：Canvas、图表、编辑器与切换按钮都需要响应同一主题；
 * 好处：组件不直接操作 DOM 或 localStorage，主题行为可以保持一致。
 *
 * @returns 全局主题状态与操作函数。
 */
export function useTheme() {
  initializeTheme()
  return {
    preference: readonly(preference),
    resolvedTheme,
    isDark,
    setThemePreference,
    toggleTheme,
  }
}
