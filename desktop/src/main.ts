import { join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, Tray } from "electron"
import { LocalApiClient } from "./api-client.js"
import { RemoteBackendAdapter, StandaloneBackendAdapter } from "./backend-adapters.js"
import { emitBackendStatus, registerBackendIpc } from "./backend-ipc.js"
import { ServerReleaseInstaller } from "./backend-release.js"
import { BackendRuntimeManager } from "./backend-runtime.js"
import type { BackendSelection } from "./backend-types.js"
import { Collector } from "./collector.js"
import { emitFileSourceEvent, registerFileSourceIpc } from "./ipc.js"
import type { IpcRegistrar } from "./ipc.js"
import { isNavigationAllowed } from "./navigation.js"
import type { NavigationPolicy } from "./navigation.js"
import { ApiProxy, resolveApiTarget } from "./proxy.js"
import { productionFrontendDirectory } from "./production-paths.js"
import { FileSourceScheduler, NativeSchedulerTimer } from "./scheduler.js"
import { FileSourceStore } from "./store.js"
import { createTrayMenuTemplate, TrayWindowLifecycle } from "./tray-lifecycle.js"

const DEV_RENDERER_URL = "http://127.0.0.1:5173"
const isDev = process.env["ANYDATAS_ELECTRON_DEV"] === "1"
const isSmoke = process.env["ANYDATAS_ELECTRON_SMOKE"] === "1"
let mainWindow: BrowserWindow | null = null
let proxy: ApiProxy | undefined
let scheduler: FileSourceScheduler | undefined
let removeIpcHandlers: (() => void) | undefined
let removeBackendIpcHandlers: (() => void) | undefined
let runtime: BackendRuntimeManager | undefined
let tray: Tray | undefined
let shutdownStarted = false

const windowLifecycle = new TrayWindowLifecycle({
  getWindow: () => mainWindow,
  createWindow: () => {
    if (proxy !== undefined && (mainWindow === null || mainWindow.isDestroyed())) {
      createWindow()
    }
  },
  quit: () => app.quit(),
})

if (!app.requestSingleInstanceLock()) {
  app.quit()
}

app.on("second-instance", () => {
  windowLifecycle.showMainWindow()
})

/**
 * 返回桌面端锁定的 GitHub Release 元数据地址，仓库拆分后只需覆盖环境变量。
 *
 * Tag 与 Cargo 版本固定匹配可以阻止已发布桌面端在未来无提示追随不兼容的 latest。
 */
function serverReleaseMetadataUrl(tag: string): URL {
  const configured = process.env["ANYDATAS_SERVER_RELEASE_METADATA_URL"]
  if (configured !== undefined) {
    return new URL(configured)
  }
  const repository = process.env["ANYDATAS_SERVER_REPOSITORY"] ?? "Ling0925/AnyDatas"
  return new URL(`https://api.github.com/repos/${repository}/releases/tags/${tag}`)
}

/**
 * 根据当前运行形态定位随桌面端发布的 Vue 资源。
 *
 * 打包后不再依赖 app.asar 的内部层级，开发态则保留当前仓库布局，便于本地 smoke 复用。
 */
function frontendDirectory(): string {
  return productionFrontendDirectory({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    moduleUrl: import.meta.url,
  })
}

/**
 * 返回开发态与安装包中都存在的托盘品牌图标路径。
 *
 * 为什么这么做：开发态的 Vite 不保证提前生成 frontend/dist，安装包又不能依赖源码目录；
 * 好处：本地调试直接复用 public 资源，正式安装则只读取随包发布的只读前端资源。
 */
function trayIconPath(): string {
  if (app.isPackaged) {
    return join(frontendDirectory(), "anydatas-logo.png")
  }
  return fileURLToPath(new URL("../../frontend/public/anydatas-logo.png", import.meta.url))
}

/**
 * 创建系统托盘以及统一的打开、退出入口。
 *
 * 为什么这么做：窗口隐藏后必须始终留有可发现的恢复和退出入口；
 * 好处：Windows、macOS 与 Linux 共用一套行为，第二实例和托盘也复用同一窗口恢复逻辑。
 */
function createTray(): void {
  if (tray !== undefined && !tray.isDestroyed()) return

  const sourceImage = nativeImage.createFromPath(trayIconPath())
  if (sourceImage.isEmpty()) {
    throw new Error("AnyDatas tray icon could not be loaded")
  }
  const iconSize = process.platform === "darwin" ? 20 : 16
  tray = new Tray(sourceImage.resize({ width: iconSize, height: iconSize, quality: "best" }))
  tray.setToolTip("AnyDatas")
  tray.setContextMenu(Menu.buildFromTemplate(createTrayMenuTemplate({
    open: () => windowLifecycle.showMainWindow(),
    quit: () => windowLifecycle.quitApplication(),
  })))
  tray.on("click", () => windowLifecycle.showMainWindow())
  tray.on("double-click", () => windowLifecycle.showMainWindow())
}

/**
 * 销毁托盘资源并清空引用。
 *
 * 为什么这么做：真正退出时系统状态栏不应短暂保留失效图标；
 * 好处：异步停止后端期间用户不会误点已经进入退出流程的菜单。
 */
function destroyTray(): void {
  tray?.destroy()
  tray = undefined
}

function createWindow(): BrowserWindow {
  const productionFrontend = join(frontendDirectory(), "index.html")
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
  window.on("close", (event) => windowLifecycle.handleWindowClose(event, window))
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
  proxy = new ApiProxy({ port: 28_090, dev: isDev })
  await proxy.listen()

  const userData = app.getPath("userData")
  const releaseTag = process.env["ANYDATAS_SERVER_RELEASE_TAG"] ?? "server-v0.1.2"
  const installer = new ServerReleaseInstaller({
    userData,
    metadataUrl: serverReleaseMetadataUrl(releaseTag),
    tag: releaseTag,
    ...(process.env["ANYDATAS_GITHUB_TOKEN"] === undefined
      ? {}
      : { githubToken: process.env["ANYDATAS_GITHUB_TOKEN"] }),
  })
  runtime = new BackendRuntimeManager({
    userData,
    proxy,
    adapterFor: (selection: BackendSelection) => {
      if (selection.mode === "standalone") {
        return new StandaloneBackendAdapter({
          installer,
          userData,
          webDirectory: frontendDirectory(),
        })
      }
      return new RemoteBackendAdapter({ serverUrl: resolveApiTarget(selection.serverUrl) })
    },
  })
  removeBackendIpcHandlers = registerBackendIpc({ registrar: registrar(), runtime })
  runtime.subscribe((status) => {
    const window = mainWindow
    if (window !== null && !window.isDestroyed()) {
      emitBackendStatus(
        { send: (channel, payload) => window.webContents.send(channel, payload) },
        status,
      )
    }
  })

  const store = new FileSourceStore(userData)
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
    apiTarget: () => proxy?.targetUrl() ?? null,
  })
  // 未配置或后端失联时暂停本地 cron，避免把“尚未选择服务器”记成一次真实采集失败。
  // 运行时恢复 ready 后下一次 30 秒 tick 会自动重新读取配置，无需重建 Scheduler。
  scheduler = new FileSourceScheduler({
    list: async () => runtime?.status().phase === "ready" ? store.list() : [],
  }, collector, {
    now: () => new Date(),
    timer: new NativeSchedulerTimer(),
    onError: (error) => console.error("desktop.scheduler.tick_failed", error),
  })
  scheduler.start()
  createTray()
  windowLifecycle.showMainWindow()
  const configuredTarget = process.env["ANYDATAS_API_TARGET"]
  const initialize = configuredTarget === undefined
    ? runtime.initialize()
    : runtime.configure({ mode: "remote", serverUrl: configuredTarget })
  void initialize.catch((error: unknown) => {
    console.error("desktop.backend.initialize_failed", error)
  })
  console.info("desktop.runtime.ready")
}

app.whenReady().then(startRuntime).catch((error: unknown) => {
  console.error("desktop.runtime.start_failed", error)
  app.quit()
})

app.on("activate", () => {
  windowLifecycle.showMainWindow()
})

app.on("before-quit", (event) => {
  windowLifecycle.beginQuit()
  if (shutdownStarted) {
    return
  }
  event.preventDefault()
  shutdownStarted = true
  scheduler?.stop()
  removeIpcHandlers?.()
  removeBackendIpcHandlers?.()
  destroyTray()
  // 等待 Rust 处理 SIGTERM 和 SQLite 收尾后再退出 Electron，防止子进程成为孤儿。
  // allSettled 保证某个清理动作失败时其他资源仍会释放，随后由操作系统结束当前应用。
  void Promise.allSettled([
    runtime?.stop() ?? Promise.resolve(),
    proxy?.close() ?? Promise.resolve(),
  ]).finally(() => app.quit())
})
