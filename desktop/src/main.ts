import { fileURLToPath, pathToFileURL } from "node:url"
import { app, BrowserWindow, dialog, ipcMain } from "electron"
import { LocalApiClient } from "./api-client.js"
import { Collector } from "./collector.js"
import { emitFileSourceEvent, registerFileSourceIpc } from "./ipc.js"
import type { IpcRegistrar } from "./ipc.js"
import { isNavigationAllowed } from "./navigation.js"
import type { NavigationPolicy } from "./navigation.js"
import { ApiProxy, resolveApiTarget } from "./proxy.js"
import { FileSourceScheduler, NativeSchedulerTimer } from "./scheduler.js"
import { FileSourceStore } from "./store.js"

const DEV_RENDERER_URL = "http://127.0.0.1:5173"
const isDev = process.env["ANYDATAS_ELECTRON_DEV"] === "1"
const isSmoke = process.env["ANYDATAS_ELECTRON_SMOKE"] === "1"
let mainWindow: BrowserWindow | null = null
let proxy: ApiProxy | undefined
let scheduler: FileSourceScheduler | undefined
let removeIpcHandlers: (() => void) | undefined

function createWindow(): BrowserWindow {
  const productionFrontend = fileURLToPath(
    new URL("../../frontend/dist/index.html", import.meta.url),
  )
  const navigationPolicy: NavigationPolicy = isDev
    ? { kind: "dev", origin: DEV_RENDERER_URL }
    : { kind: "production", fileUrl: pathToFileURL(productionFrontend).href }
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: fileURLToPath(new URL("preload.cjs", import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  })
  window.once("ready-to-show", () => {
    window.show()
    if (isSmoke) {
      app.quit()
    }
  })
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }))
  window.webContents.on("will-attach-webview", (event) => event.preventDefault())
  window.webContents.on("will-navigate", (event, url) => {
    if (!isNavigationAllowed(url, navigationPolicy)) {
      event.preventDefault()
    }
  })
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(false)
  })
  window.webContents.session.setPermissionCheckHandler(() => false)
  window.once("closed", () => {
    if (mainWindow === window) {
      mainWindow = null
    }
  })
  if (isDev) {
    void window.loadURL(DEV_RENDERER_URL)
  } else {
    void window.loadFile(productionFrontend)
  }
  mainWindow = window
  return window
}

function registrar(): IpcRegistrar {
  return {
    handle: (channel, handler) => {
      ipcMain.handle(channel, (_event, ...args: unknown[]) => handler(...args))
    },
    removeHandler: (channel) => ipcMain.removeHandler(channel),
  }
}

async function startRuntime(): Promise<void> {
  const target = resolveApiTarget(process.env["ANYDATAS_API_TARGET"])
  proxy = new ApiProxy({ target, port: 28_090, dev: isDev })
  await proxy.listen()

  const store = new FileSourceStore(app.getPath("userData"))
  const api = new LocalApiClient({ baseUrl: new URL(proxy.url) })
  const collector = new Collector(store, api, {
    emit: (event) => {
      const window = mainWindow
      if (window !== null && !window.isDestroyed()) {
        emitFileSourceEvent(
          { send: (channel, payload) => window.webContents.send(channel, payload) },
          event,
        )
      }
    },
  })
  removeIpcHandlers = registerFileSourceIpc({
    registrar: registrar(),
    store,
    runner: collector,
    dialog: {
      showOpenDialog: async () => dialog.showOpenDialog({ properties: ["openDirectory"] }),
    },
    apiTarget: target.href,
  })
  scheduler = new FileSourceScheduler(store, collector, {
    now: () => new Date(),
    timer: new NativeSchedulerTimer(),
    onError: (error) => console.error("desktop.scheduler.tick_failed", error),
  })
  scheduler.start()
  createWindow()
  console.info("desktop.runtime.ready")
}

app.whenReady().then(startRuntime).catch((error: unknown) => {
  console.error("desktop.runtime.start_failed", error)
  app.quit()
})

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0 && proxy !== undefined) {
    createWindow()
  }
})

app.on("before-quit", () => {
  scheduler?.stop()
  removeIpcHandlers?.()
  void proxy?.close()
})

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit()
  }
})
