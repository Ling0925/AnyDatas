import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vite.dev/config/
export default defineConfig({
  // 相对 base：Electron 通过 loadFile 以 file:// 打开 dist/index.html，根绝对路径会变成 file:///... 而失效。
  base: './',
  plugins: [
    vue(),
    // 模板级按需解析避免把整套 Element Plus 注入入口包，各路由只加载实际组件。
    Components({
      dts: 'src/components.d.ts',
      directives: true,
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
    }),
  ],
  build: {
    manifest: true,
    // Monaco 是显式延迟加载的功能分块；入口预算由构建后的依赖图单独强制检查。
    chunkSizeWarningLimit: 4_000,
    rollupOptions: {
      output: {
        // Vue 核心保持稳定缓存；Monaco 与图表由功能级动态 import 自然形成延迟分块。
        manualChunks(id) {
          if (id.includes('/node_modules/vue/') || id.includes('/node_modules/vue-router/')) {
            return 'vue-runtime'
          }
          return undefined
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
