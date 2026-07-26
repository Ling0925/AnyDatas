try {
  const savedTheme = localStorage.getItem('anydatas.theme')
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const isDark = savedTheme === 'dark' || (savedTheme !== 'light' && prefersDark)
  const theme = isDark ? 'dark' : 'light'

  // 在样式和 Vue 应用加载前写入主题，避免夜间模式首屏短暂闪成亮色。
  document.documentElement.dataset.theme = theme
  document.documentElement.classList.toggle('dark', isDark)
  document.documentElement.style.colorScheme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    'content',
    isDark ? '#0b1210' : '#147d64',
  )
} catch {
  // 浏览器禁用存储时安全回退到亮色，保证主题初始化不会阻断应用启动。
  document.documentElement.dataset.theme = 'light'
  document.documentElement.style.colorScheme = 'light'
}
