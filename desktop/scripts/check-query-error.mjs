#!/usr/bin/env node

import { createServer } from "node:http"
import { fileURLToPath } from "node:url"
import { app, BrowserWindow, ipcMain } from "electron"

const PROXY_PORT = 28_090
const DEADLINE_MS = 25_000
const QUERY_ERROR = "SQL 字段 missing_column 不存在"
const BACKEND_STATUS = {
  mode: "standalone",
  phase: "ready",
  serverUrl: null,
  serverVersion: "0.1.1",
  protocolVersion: 1,
  message: "单机服务已就绪",
  progress: null,
}
const USER = {
  userId: "user-1",
  email: "owner@example.com",
  name: "测试用户",
  workspaceId: "workspace-1",
  workspaceName: "测试工作区",
  role: "owner",
}
const SOURCE = {
  id: "source-1",
  name: "测试数据",
  originalFilename: "test.csv",
  mediaType: "text/csv",
  fileKind: "csv",
  sizeBytes: 128,
  selectedSheet: "data",
  startCell: "A1",
  firstRowAsHeader: true,
  sheetNames: ["data"],
  rowCount: 1,
  columnCount: 1,
  sqlTableName: "data",
  createdAt: "2026-08-09T00:00:00Z",
  updatedAt: "2026-08-09T00:00:00Z",
}
const TABLE = {
  id: "table-1",
  sourceId: SOURCE.id,
  sourceName: SOURCE.name,
  originalFilename: SOURCE.originalFilename,
  fileKind: SOURCE.fileKind,
  name: "data",
  sheetName: "data",
  startCell: "A1",
  endCell: null,
  firstRowAsHeader: true,
  rowCount: 1,
  columnCount: 1,
  fields: [{ name: "name", dataType: "文本", nullable: false }],
  configVersion: 1,
  cacheStatus: "ready",
  cacheError: null,
  isDefault: true,
  createdAt: "2026-08-09T00:00:00Z",
  updatedAt: "2026-08-09T00:00:00Z",
}

/**
 * 返回生产工作台启动所需的最小 API，并让查询端点稳定失败。
 *
 * 为什么这么做：错误必须穿过真实 Axios、Pinia 和 Vue 渲染链路；好处是同时验证服务端结构化消息没有在界面层丢失。
 */
function startWorkspaceStub() {
  let queryRequests = 0
  const server = createServer((request, response) => {
    const origin = request.headers.origin ?? "null"
    const cors = {
      "access-control-allow-origin": origin,
      "access-control-allow-credentials": "true",
      vary: "Origin",
    }
    const json = (status, body) => {
      response.writeHead(status, { ...cors, "content-type": "application/json" })
      response.end(JSON.stringify(body))
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
      json(200, { setupRequired: false, authenticated: true, user: USER })
      return
    }
    if (request.method === "GET" && request.url === "/api/data-sources") {
      json(200, [SOURCE])
      return
    }
    if (request.method === "GET" && request.url === "/api/source-tables") {
      json(200, [TABLE])
      return
    }
    if (request.method === "GET" && request.url === "/api/source-tables/table-1/preview?limit=200") {
      json(200, {
        columns: TABLE.fields,
        rows: [["Alice"]],
        totalRows: 1,
        truncated: false,
        sheet: "data",
        startCell: "A1",
        endCell: null,
      })
      return
    }
    if (request.method === "GET" && request.url === "/api/saved-queries") {
      json(200, [])
      return
    }
    if (request.method === "POST" && request.url === "/api/query") {
      queryRequests += 1
      json(422, { error: { message: QUERY_ERROR } })
      return
    }
    json(404, { error: { message: `query smoke: ${request.method} ${request.url}` } })
  })
  return new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(PROXY_PORT, "127.0.0.1", () => {
      resolve({ server, queryRequests: () => queryRequests })
    })
  })
}

/**
 * 等待工作台的运行按钮可点击，随后验证结果区存在持久错误节点。
 *
 * 为什么这么做：全局 toast 会自动消失且不属于查询结果区；好处是断言直接对应用户可见的失败位置和文本。
 */
const QUERY_SCRIPT = `
new Promise((resolve) => {
  const deadline = Date.now() + ${DEADLINE_MS}
  let clicked = false
  const snapshot = () => {
    const button = document.querySelector('button[aria-label="运行查询"]')
    const alert = document.querySelector('.result-pane .query-error-panel[role="alert"]')
    return {
      hash: location.hash,
      buttonReady: button instanceof HTMLButtonElement && !button.disabled,
      alertText: alert?.textContent?.trim() ?? "",
    }
  }
  const act = () => {
    const state = snapshot()
    if (!clicked && state.buttonReady) {
      clicked = true
      document.querySelector('button[aria-label="运行查询"]')?.click()
    }
    const next = snapshot()
    if (next.alertText.includes(${JSON.stringify(QUERY_ERROR)}) || Date.now() >= deadline) {
      observer.disconnect()
      clearInterval(timer)
      resolve(next)
    }
  }
  const observer = new MutationObserver(act)
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true })
  const timer = setInterval(act, 50)
  act()
})
`

/**
 * 在 production file:// 页面中执行一次失败查询。
 *
 * 为什么这么做：这与最终 Windows 安装包使用相同的 preload 与安全隔离配置；好处是构建产物层面的回归不会被开发服务器掩盖。
 */
async function main() {
  await app.whenReady()
  const stub = await startWorkspaceStub()
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

  let outcome
  try {
    const indexHtml = fileURLToPath(new URL("../../frontend/dist/index.html", import.meta.url))
    await window.loadFile(indexHtml, { hash: "/workbench" })
    outcome = await window.webContents.executeJavaScript(QUERY_SCRIPT)
  } finally {
    window.destroy()
    ipcMain.removeHandler("desktop:backend:status")
    await new Promise((resolve) => stub.server.close(() => resolve()))
  }

  const passed = outcome.hash.startsWith("#/workbench")
    && outcome.alertText.includes(QUERY_ERROR)
    && stub.queryRequests() === 1
  if (!passed) {
    throw new Error(
      `query error was not rendered: outcome=${JSON.stringify(outcome)} queryRequests=${stub.queryRequests()}`,
    )
  }
  process.stdout.write(`RESULT: PASS\nerror: ${outcome.alertText}\nqueryRequests: 1\n`)
}

main()
  .then(() => app.exit(0))
  .catch((error) => {
    console.error(error)
    app.exit(1)
  })
