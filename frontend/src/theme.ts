import { computed, nextTick, readonly, ref } from 'vue'

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export interface ThemeTransitionOrigin {
  x: number
  y: number
}

const THEME_STORAGE_KEY = 'anydatas.theme'
const THEME_TRANSITION_DURATION = 360
const THEME_TRANSITION_EASING = 'cubic-bezier(0.4, 0, 0.2, 1)'
const THEME_TRANSITION_START_RADIUS = 12

const preference = ref<ThemePreference>('system')
const systemPrefersDark = ref(false)
const isThemeTransitioning = ref(false)
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
  if (typeof window === 'undefined') return

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
 * 解析主题揭示动画的圆心与最大覆盖半径。
 * 为什么这么做：按钮可能位于桌面顶栏或移动端登录区，固定圆心无法兼顾不同布局；
 * 好处：新主题总会从真实交互位置扩散，并完整覆盖任意尺寸的视口。
 *
 * @param origin 触发按钮在视口中的中心坐标。
 * @returns 经过视口边界约束的圆心与最大覆盖半径。
 */
function resolveThemeTransitionGeometry(origin?: ThemeTransitionOrigin) {
  const x = Math.min(Math.max(origin?.x ?? window.innerWidth / 2, 0), window.innerWidth)
  const y = Math.min(Math.max(origin?.y ?? window.innerHeight / 2, 0), window.innerHeight)
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  )

  return { x, y, radius }
}

/**
 * 判断当前用户是否要求减少动态效果。
 * 为什么这么做：大范围扩散动画可能让动态敏感用户感到不适；
 * 好处：尊重系统无障碍偏好，同时仍保留即时、可用的主题切换。
 *
 * @returns 是否应跳过主题动画。
 */
function shouldReduceThemeMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * 从用户触发位置播放根页面主题揭示动画。
 * 为什么这么做：根快照可以一次覆盖 Element Plus 浮层、Canvas、图表和编辑器，避免组件逐个变色造成闪烁；
 * 好处：旧画面保持稳定，新主题从按钮位置平滑扩散，并在 Vue 更新完成后统一呈现。
 *
 * @param nextPreference 即将应用的显式主题偏好。
 * @param origin 主题按钮在视口中的中心坐标。
 */
function runViewThemeTransition(
  nextPreference: Exclude<ThemePreference, 'system'>,
  origin?: ThemeTransitionOrigin,
) {
  const root = document.documentElement
  const geometry = resolveThemeTransitionGeometry(origin)
  let revealAnimation: Animation | null = null
  isThemeTransitioning.value = true

  try {
    const transition = document.startViewTransition(async () => {
      setThemePreference(nextPreference)
      await nextTick()
    })

    // 将按钮坐标直接写入伪元素并从 12px 慢启动，让首个可见帧贴着按钮而不是突然铺到页面中部。
    void transition.ready
      .then(() => {
        revealAnimation = root.animate(
          {
            clipPath: [
              `circle(${THEME_TRANSITION_START_RADIUS}px at ${geometry.x}px ${geometry.y}px)`,
              `circle(${geometry.radius}px at ${geometry.x}px ${geometry.y}px)`,
            ],
          },
          {
            duration: THEME_TRANSITION_DURATION,
            easing: THEME_TRANSITION_EASING,
            fill: 'both',
            pseudoElement: '::view-transition-new(root)',
          },
        )
      })
      .catch(() => undefined)

    void transition.finished
      .catch(() => undefined)
      .finally(() => {
        revealAnimation?.cancel()
        isThemeTransitioning.value = false
      })
  } catch {
    isThemeTransitioning.value = false
    setThemePreference(nextPreference)
  }
}

/**
 * 在当前解析主题的相反模式间切换。
 * 为什么这么做：主题按钮需要统一处理动画、持久化和快速连点，不能由组件各自直接改根节点；
 * 好处：支持新浏览器的定向扩散动画，也能在旧浏览器和减少动态效果模式下安全降级。
 *
 * @param origin 触发按钮在视口中的中心坐标；省略时从视口中心扩散。
 */
export function toggleTheme(origin?: ThemeTransitionOrigin) {
  if (isThemeTransitioning.value) return

  const nextPreference = isDark.value ? 'light' : 'dark'
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    setThemePreference(nextPreference)
    return
  }

  if (shouldReduceThemeMotion()) {
    setThemePreference(nextPreference)
    return
  }

  if (typeof document.startViewTransition !== 'function') {
    setThemePreference(nextPreference)
    return
  }

  runViewThemeTransition(nextPreference, origin)
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
    isThemeTransitioning: readonly(isThemeTransitioning),
    setThemePreference,
    toggleTheme,
  }
}
