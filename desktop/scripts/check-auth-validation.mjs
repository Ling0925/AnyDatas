#!/usr/bin/env node

import { createServer } from "node:http"
import { fileURLToPath } from "node:url"
import { app, BrowserWindow, ipcMain } from "electron"

const PROXY_PORT = 28_090
const DEADLINE_MS = 10_000
const BACKEND_STATUS = {
  mode: "standalone",
  phase: "ready",
  serverUrl: null,
  serverVersion: "0.1.1",
  protocolVersion: 1,
  message: "单机服务已就绪",
  progress: null,
}

/**
 * 为生产登录页提供最小认证桩，并记录短密码是否错误地进入服务端。
 *
 * 使用真实 HTTP/CORS 边界可以证明校验发生在提交前，而不是靠后端拒绝后再显示提示。
 */
function startAuthStub() {
  let setupRequests = 0
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
        "access-control-allow-methods": "GET, POST, OPTIONS",
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
    if (request.method === "POST" && request.url === "/api/auth/setup") {
      setupRequests += 1
    }
    response.writeHead(500, { ...cors, "content-type": "application/json" })
    response.end(JSON.stringify({ error: { message: "validation smoke must not submit" } }))
  })
  return new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(PROXY_PORT, "127.0.0.1", () => {
      resolve({ server, setupRequests: () => setupRequests })
    })
  })
}

/**
 * 关闭认证桩并等待端口释放。
 *
 * 显式等待可以让本地连续运行和 CI 并行重试都不会误报端口占用。
 */
function close(server) {
  return new Promise((resolve) => server.close(() => resolve()))
}

// 渲染进程使用真实 Vue v-model 输入事件提交短密码，并等待持久错误区域出现。
const VALIDATION_SCRIPT = `
new Promise((resolve) => {
  const deadline = Date.now() + ${DEADLINE_MS}
  let submitted = false
  const snapshot = () => ({
    hash: location.hash,
    alertText: document.querySelector(".auth-validation-alert")?.textContent?.trim() ?? "",
    passwordInvalid: document.querySelectorAll(".auth-field.is-invalid input[type=password]").length > 0,
  })
  const finishIfReady = () => {
    const state = snapshot()
    if (state.alertText.length > 0 || Date.now() >= deadline) {
      observer.disconnect()
      clearInterval(timer)
      resolve(state)
      return true
    }
    return false
  }
  const submitWhenReady = () => {
    if (submitted) return
    const values = ["测试管理员", "测试工作区", "test@example.com", "short", "short"]
    const inputs = [...document.querySelectorAll(".auth-form input")]
    const button = document.querySelector("button.auth-submit")
    if (inputs.length !== values.length || !(button instanceof HTMLButtonElement)) return
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set
    if (!setter) return
    submitted = true
    inputs.forEach((input, index) => {
      setter.call(input, values[index])
      input.dispatchEvent(new Event("input", { bubbles: true }))
    })
    button.click()
  }
  const observer = new MutationObserver(() => {
    submitWhenReady()
    finishIfReady()
  })
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true })
  const timer = setInterval(() => {
    submitWhenReady()
    finishIfReady()
  }, 50)
  submitWhenReady()
})
`

/**
 * 在生产 file://、preload 隔离和真实 Vue 页面中验证注册错误反馈。
 *
 * 该入口覆盖用户实际点击路径，避免只测试校验函数却遗漏表单原生行为或提示样式绑定。
 */
async function main() {
  await app.whenReady()
  const authStub = await startAuthStub()
  ipcMain.handle("desktop:backend:status", () => BACKEND_STATUS)
  const window = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: fileURLToPath(new URL("../dist/preload.cjs", import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  })
  const diagnostics = []
  window.webContents.on("console-message", (...args) => {
    const second = args[1]
    const message = typeof second === "object" && second !== null ? second.message : args[2]
    diagnostics.push(String(message))
  })

  let outcome
  try {
    const indexHtml = fileURLToPath(new URL("../../frontend/dist/index.html", import.meta.url))
    await window.loadFile(indexHtml, { hash: "/login" })
    outcome = await window.webContents.executeJavaScript(VALIDATION_SCRIPT)
  } finally {
    window.destroy()
    ipcMain.removeHandler("desktop:backend:status")
    await close(authStub.server)
  }

  const passed = outcome.alertText.includes("密码至少需要 12 位")
    && outcome.passwordInvalid
    && authStub.setupRequests() === 0
  if (!passed) {
    throw new Error(
      `auth validation failed: outcome=${JSON.stringify(outcome)} setupRequests=${authStub.setupRequests()} diagnostics=${JSON.stringify(diagnostics)}`,
    )
  }
  process.stdout.write(`RESULT: PASS\nalert: ${outcome.alertText}\npasswordInvalid: true\nsetupRequests: 0\n`)
}

main()
  .then(() => app.exit(0))
  .catch((error) => {
    console.error(error)
    app.exit(1)
  })
