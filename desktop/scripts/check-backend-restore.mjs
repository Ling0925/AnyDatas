#!/usr/bin/env node

import { createServer } from "node:http"
import { fileURLToPath } from "node:url"
import { app, BrowserWindow, ipcMain } from "electron"

const PROXY_PORT = 28_090
const DEADLINE_MS = 15_000
const BACKEND_EVENT = "desktop:backend:event"
const STARTING_STATUS = {
  mode: "standalone",
  phase: "starting",
  serverUrl: null,
  serverVersion: null,
  protocolVersion: null,
  message: "正在恢复单机服务",
  progress: null,
}
const READY_STATUS = {
  mode: "standalone",
  phase: "ready",
  serverUrl: null,
  serverVersion: "0.1.1",
  protocolVersion: 1,
  message: "单机服务已就绪",
  progress: null,
}

/**
 * 为恢复后的登录页提供最小认证响应。
 *
 * 为什么这么做：路由只有在服务就绪后完成认证引导才会稳定落到登录页；好处是回归覆盖真实 file://、代理和路由守卫边界。
 */
function startAuthStub() {
  const server = createServer((request, response) => {
    const origin = request.headers.origin ?? "null"
    const cors = {
      "access-control-allow-origin": origin,
      "access-control-allow-credentials": "true",
      vary: "Origin",
    }
    if (request.method === "OPTIONS") {
      response.writeHead(204, {
        ...cors,
        "access-control-allow-methods": "GET, OPTIONS",
        "access-control-allow-headers": "content-type,accept",
      })
      response.end()
      return
    }
    if (request.method === "GET" && request.url === "/api/auth/status") {
      response.writeHead(200, { ...cors, "content-type": "application/json" })
      response.end(JSON.stringify({ setupRequired: true, authenticated: false, user: null }))
      return
    }
    response.writeHead(404, { ...cors, "content-type": "application/json" })
    response.end(JSON.stringify({ error: { message: "restore smoke: not found" } }))
  })
  return new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(PROXY_PORT, "127.0.0.1", () => resolve(server))
  })
}

/**
 * 等待服务桩释放端口。
 *
 * 为什么这么做：渲染器检查会连续运行并共用固定代理端口；好处是避免后一条用例被端口残留干扰。
 */
function close(server) {
  return new Promise((resolve) => server.close(() => resolve()))
}

/**
 * 等待页面满足断言并返回最后一次页面快照。
 *
 * 为什么这么做：Vue 路由、认证请求和 DOM 提交都是异步的；好处是用状态收敛替代固定睡眠，CI 较慢时也稳定。
 */
function waitForPage(window, predicateSource) {
  return window.webContents.executeJavaScript(`
    new Promise((resolve) => {
      const deadline = Date.now() + ${DEADLINE_MS}
      const snapshot = () => ({
        hash: location.hash,
        text: document.getElementById("app")?.textContent?.trim().slice(0, 300) ?? "",
      })
      const predicate = ${predicateSource}
      const finishIfReady = () => {
        const state = snapshot()
        if (predicate(state) || Date.now() >= deadline) {
          observer.disconnect()
          clearInterval(timer)
          resolve(state)
          return true
        }
        return false
      }
      const observer = new MutationObserver(finishIfReady)
      observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true })
      const timer = setInterval(finishIfReady, 50)
      finishIfReady()
    })
  `)
}

/**
 * 模拟桌面端先渲染 starting、随后已保存的单机服务恢复为 ready。
 *
 * 为什么这么做：真实启动时窗口创建早于后端恢复完成；好处是能防止模式配置明明已保存却每次仍停在选择页。
 */
async function main() {
  await app.whenReady()
  let backendStatus = STARTING_STATUS
  ipcMain.handle("desktop:backend:status", () => backendStatus)
  const server = await startAuthStub()
  const window = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: fileURLToPath(new URL("../dist/preload.cjs", import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  })

  let initial
  let outcome
  try {
    const indexHtml = fileURLToPath(new URL("../../frontend/dist/index.html", import.meta.url))
    await window.loadFile(indexHtml)
    initial = await waitForPage(window, `(state) => state.hash.startsWith("#/connection")`)
    if (!initial.hash.startsWith("#/connection")) {
      throw new Error(`did not enter startup connection route: ${JSON.stringify(initial)}`)
    }
    backendStatus = READY_STATUS
    window.webContents.send(BACKEND_EVENT, READY_STATUS)
    outcome = await waitForPage(window, `(state) => state.hash.startsWith("#/login")`)
  } finally {
    window.destroy()
    ipcMain.removeHandler("desktop:backend:status")
    await close(server)
  }

  if (!outcome.hash.startsWith("#/login")) {
    throw new Error(
      `saved backend did not continue to login: initial=${JSON.stringify(initial)} outcome=${JSON.stringify(outcome)}`,
    )
  }
  process.stdout.write(`RESULT: PASS\ninitial: ${initial.hash}\nrestored: ${outcome.hash}\n`)
}

main()
  .then(() => app.exit(0))
  .catch((error) => {
    console.error(error)
    app.exit(1)
  })
