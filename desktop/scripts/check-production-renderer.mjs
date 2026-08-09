#!/usr/bin/env node
// 生产渲染器冒烟：以真实 file:// 加载 frontend/dist/index.html，校验 Vue 应用
// 在文件协议下挂载并命中 hash 路由（登录/初始化页）。模拟桌面壳的渲染进程边界：
// 真实 preload.cjs + contextIsolation + sandbox + 关闭 nodeIntegration，API 走
// 127.0.0.1:28090 的最小回环桩（与桌面壳同端口同 CORS 语义），auth 引导成功后再
// 判断文件协议路由是否真的命中 —— 避免“路由未命中”被“接口失败重定向”掩盖。
import { createServer } from "node:http"
import { access } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import { app, BrowserWindow, ipcMain } from "electron"

const PROXY_PORT = 28_090
const AUTH_STATUS = { setupRequired: true, authenticated: false, user: null }
const BACKEND_STATUS = {
  mode: null,
  phase: "unconfigured",
  serverUrl: null,
  serverVersion: null,
  protocolVersion: null,
  message: "请选择单机模式或连接服务器",
  progress: null,
}
// 无 DOM 变更的静默窗口：Vue 挂载后若再无变化即视为终态（红色用例快速失败）。
const SETTLE_MS = 5_000
// 绝对兜底：仅作为失败保护，正常路径在首次渲染后即通过 MutationObserver 返回。
const DEADLINE_MS = 20_000

const notes = []
const note = (message) => {
  notes.push(String(message))
  console.log(`[renderer-smoke] ${message}`)
}

function corsHeaders(request) {
  const origin = request.headers.origin
  const allowed = origin === undefined || origin === "null" || origin.startsWith("file://")
  if (!allowed) {
    return null
  }
  const headers = {
    "access-control-allow-origin": origin ?? "null",
    "access-control-allow-credentials": "true",
    "vary": "Origin",
  }
  if (request.method === "OPTIONS") {
    headers["access-control-allow-methods"] = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
    headers["access-control-allow-headers"] =
      request.headers["access-control-request-headers"]
      ?? "content-type,authorization,x-requested-with,accept"
  }
  return headers
}

function startStub() {
  const server = createServer((request, response) => {
    if (request.method === "OPTIONS") {
      const headers = corsHeaders(request)
      if (headers === null) {
        response.writeHead(403, { "content-type": "application/json; charset=utf-8" })
        response.end(JSON.stringify({ error: "origin not allowed" }))
        return
      }
      response.writeHead(204, headers)
      response.end()
      return
    }
    if (request.method === "GET" && request.url === "/api/auth/status") {
      const headers = corsHeaders(request)
      response.writeHead(200, {
        "content-type": "application/json; charset=utf-8",
        ...(headers ?? {}),
      })
      response.end(JSON.stringify(AUTH_STATUS))
      return
    }
    response.writeHead(404, { "content-type": "application/json; charset=utf-8" })
    response.end(JSON.stringify({ error: "smoke stub: not found" }))
  })
  return new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(PROXY_PORT, "127.0.0.1", () => resolve(server))
  })
}

function closeStub(server) {
  return new Promise((resolve) => {
    if (!server.listening) {
      resolve()
      return
    }
    server.close(() => resolve())
  })
}

// 渲染进程侧：等待 #app 出现真实子元素且 URL 进入 hash 路由。等待的是 DOM 变更
// 事件本身；setTimeout/setInterval 只作为失败保护，不做固定睡眠。
const OBSERVER_SCRIPT = `
new Promise((resolve) => {
  const settleMs = ${SETTLE_MS}
  const deadlineMs = ${DEADLINE_MS}
  const snapshot = () => {
    const app = document.getElementById("app")
    return {
      hash: location.hash,
      url: location.href,
      appElementCount: app === null ? -1 : app.children.length,
      appText: app === null ? "" : (app.textContent ?? "").trim().slice(0, 200),
    }
  }
  const isReady = (state) =>
    state.hash.startsWith("#/connection") && state.appElementCount > 0
  const startedAt = Date.now()
  let lastMutationAt = startedAt
  let settleTimer = undefined
  let deadlineTimer = undefined
  let cleanup = () => {}
  const finish = (ok, state) => {
    cleanup()
    resolve({ ok, ...state })
  }
  const observer = new MutationObserver(() => {
    lastMutationAt = Date.now()
    const state = snapshot()
    if (isReady(state)) {
      finish(true, state)
    }
  })
  cleanup = () => {
    observer.disconnect()
    clearTimeout(settleTimer)
    clearInterval(deadlineTimer)
  }
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  })
  let state = snapshot()
  if (isReady(state)) {
    finish(true, state)
    return
  }
  settleTimer = setTimeout(() => {
    const final = snapshot()
    finish(isReady(final), final)
  }, settleMs)
  deadlineTimer = setInterval(() => {
    const final = snapshot()
    if (isReady(final)) {
      finish(true, final)
      return
    }
    if (Date.now() - lastMutationAt > settleMs || Date.now() - startedAt > deadlineMs) {
      finish(false, final)
    }
  }, 250)
})
`

async function main() {
  await app.whenReady()
  // 生产 smoke 不启动真实服务端；固定返回首次未配置状态，验证用户能看到模式选择界面。
  ipcMain.handle("desktop:backend:status", () => BACKEND_STATUS)

  let stub
  try {
    stub = await startStub()
    note(`stub listening on 127.0.0.1:${PROXY_PORT}`)
  } catch (error) {
    note(`failed to bind auth stub on 127.0.0.1:${PROXY_PORT}: ${error}`)
    app.exit(1)
    return
  }

  const indexHtml = fileURLToPath(new URL("../../frontend/dist/index.html", import.meta.url))
  const preload = fileURLToPath(new URL("../dist/preload.cjs", import.meta.url))
  const missing = []
  for (const [label, path] of [["index.html", indexHtml], ["preload.cjs", preload]]) {
    try {
      await access(path)
    } catch {
      missing.push(label)
    }
  }
  if (missing.length > 0) {
    note(`missing build artifacts (run pnpm --dir desktop test:renderer): ${missing.join(", ")}`)
    await closeStub(stub)
    ipcMain.removeHandler("desktop:backend:status")
    app.exit(1)
    return
  }

  const win = new BrowserWindow({
    show: false,
    webPreferences: {
      preload,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  })
  const loadNotes = []
  win.webContents.on("did-fail-load", (_event, code, description, url) => {
    loadNotes.push(`did-fail-load code=${code} description=${description} url=${url}`)
  })
  win.webContents.on("render-process-gone", (_event, details) => {
    loadNotes.push(`render-process-gone reason=${details.reason} exitCode=${details.exitCode}`)
  })
  win.webContents.on("console-message", (...args) => {
    // Electron 43 同时支持 (event, details) 与旧式位置参数，两种都兜底解析。
    const second = args[1]
    const level = typeof second === "object" && second !== null ? second.level : second
    const message = typeof second === "object" && second !== null ? second.message : args[2]
    if (level === 2 || level === 3 || level === "warning" || level === "error") {
      loadNotes.push(`console[${level}]: ${message}`)
    }
  })

  let outcome
  let loadError = null
  try {
    await Promise.race([
      win.loadFile(indexHtml),
      new Promise((_resolve, reject) => {
        setTimeout(() => reject(new Error("loadFile timed out")), DEADLINE_MS)
      }),
    ])
    note(`loaded ${indexHtml}`)
    outcome = await win.webContents.executeJavaScript(OBSERVER_SCRIPT)
  } catch (error) {
    loadError = error
  } finally {
    if (!win.isDestroyed()) {
      win.destroy()
    }
    await closeStub(stub)
    ipcMain.removeHandler("desktop:backend:status")
  }

  const lines = []
  if (loadError !== null) {
    lines.push("RESULT: FAIL")
    lines.push(`error: ${loadError}`)
  } else if (outcome.ok) {
    lines.push("RESULT: PASS")
    lines.push(`url: ${outcome.url}`)
    lines.push(`hash: ${outcome.hash}`)
    lines.push(`appElementCount: ${outcome.appElementCount}`)
  } else {
    lines.push("RESULT: FAIL")
    lines.push("renderer did not render the desktop connection surface at a hash route")
    lines.push(`url: ${outcome.url}`)
    lines.push(`hash: ${outcome.hash}`)
    lines.push(`appElementCount: ${outcome.appElementCount}`)
    lines.push(`appText: ${outcome.appText}`)
  }
  for (const entry of loadNotes) {
    lines.push(`diagnostics: ${entry}`)
  }
  process.stdout.write(`${lines.join("\n")}\n`, () => {
    ipcMain.removeHandler("desktop:backend:status")
    app.exit(outcome !== undefined && outcome.ok ? 0 : 1)
  })
}

main().catch((error) => {
  console.error(`[renderer-smoke] fatal: ${error}`)
  app.exit(1)
})
