export interface TrayManagedWindow {
  readonly isDestroyed: () => boolean
  readonly isMinimized: () => boolean
  readonly restore: () => void
  readonly show: () => void
  readonly focus: () => void
  readonly hide: () => void
}

export interface TrayWindowCloseEvent {
  readonly preventDefault: () => void
}

export type TrayMenuItem =
  | {
      readonly label: string
      readonly enabled?: boolean
      readonly click?: () => void
    }
  | { readonly type: "separator" }

type TrayLifecycleDependencies = {
  readonly getWindow: () => TrayManagedWindow | null
  readonly createWindow: () => void
  readonly quit: () => void
}

type TrayMenuActions = {
  readonly open: () => void
  readonly quit: () => void
}

/**
 * 统一管理托盘模式下的窗口恢复与真正退出状态。
 *
 * 为什么这么做：托盘单击、菜单、Dock 激活和第二实例都需要完全相同的窗口恢复行为；
 * 好处：关闭窗口只隐藏，只有明确退出才放行 Electron 的 close 流程，后台运行时不会被误停。
 */
export class TrayWindowLifecycle {
  #quitting = false

  constructor(private readonly dependencies: TrayLifecycleDependencies) {}

  /**
   * 显示并聚焦现有主窗口，窗口已销毁时请求创建新窗口。
   *
   * 为什么这么做：隐藏、最小化和意外销毁是三个不同状态；
   * 好处：所有重新打开入口都能恢复同一个窗口，同时避免重复创建 BrowserWindow。
   */
  showMainWindow(): void {
    const window = this.dependencies.getWindow()
    if (window === null || window.isDestroyed()) {
      this.dependencies.createWindow()
      return
    }
    if (window.isMinimized()) {
      window.restore()
    }
    window.show()
    window.focus()
  }

  /**
   * 将普通窗口关闭转换为隐藏到托盘。
   *
   * 为什么这么做：BrowserWindow 的 close 默认会销毁窗口并触发整个应用退出；
   * 好处：本地后端、代理、采集器和定时任务继续运行，渲染窗口也能快速恢复。
   *
   * @param event Electron 窗口关闭事件。
   * @param window 正在关闭的主窗口。
   */
  handleWindowClose(event: TrayWindowCloseEvent, window: TrayManagedWindow): void {
    if (this.#quitting) return

    event.preventDefault()
    window.hide()
  }

  /**
   * 标记操作系统已经开始真正退出应用。
   *
   * 为什么这么做：系统退出也会依次触发 before-quit 与窗口 close；
   * 好处：close 处理器不会再次阻止退出，既有异步清理流程可以完整执行。
   */
  beginQuit(): void {
    this.#quitting = true
  }

  /**
   * 从托盘发起真正退出。
   *
   * 为什么这么做：必须在调用 app.quit 前同步退出状态；
   * 好处：窗口关闭不会被托盘隐藏逻辑拦截，最终仍统一进入 before-quit 的优雅清理路径。
   */
  quitApplication(): void {
    this.beginQuit()
    this.dependencies.quit()
  }
}

/**
 * 创建稳定、可测试的托盘菜单定义。
 *
 * 为什么这么做：菜单文案和动作若散落在 Electron 初始化代码中，很难覆盖回归测试；
 * 好处：首版始终提供明确的“打开”和“退出”，以后增加服务状态时也有单一扩展点。
 *
 * @param actions 托盘菜单需要调用的窗口与退出动作。
 * @returns 可直接交给 Electron Menu.buildFromTemplate 的菜单项。
 */
export function createTrayMenuTemplate(actions: TrayMenuActions): TrayMenuItem[] {
  return [
    { label: "AnyDatas", enabled: false },
    { type: "separator" },
    { label: "打开 AnyDatas", click: actions.open },
    { type: "separator" },
    { label: "退出 AnyDatas", click: actions.quit },
  ]
}
