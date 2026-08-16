import { describe, expect, it, vi } from "vitest"
import {
  createTrayMenuTemplate,
  TrayWindowLifecycle,
} from "./tray-lifecycle.js"
import type { TrayManagedWindow } from "./tray-lifecycle.js"

describe("TrayWindowLifecycle", () => {
  it("从托盘恢复已隐藏或最小化的窗口", () => {
    const window: TrayManagedWindow = {
      isDestroyed: vi.fn(() => false),
      isMinimized: vi.fn(() => true),
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
      hide: vi.fn(),
    }
    const createWindow = vi.fn()
    const lifecycle = new TrayWindowLifecycle({
      getWindow: () => window,
      createWindow,
      quit: vi.fn(),
    })

    lifecycle.showMainWindow()

    expect(window.restore).toHaveBeenCalledOnce()
    expect(window.show).toHaveBeenCalledOnce()
    expect(window.focus).toHaveBeenCalledOnce()
    expect(createWindow).not.toHaveBeenCalled()
  })

  it("窗口不存在或已销毁时只创建一个新窗口", () => {
    const createWindow = vi.fn()
    const lifecycle = new TrayWindowLifecycle({
      getWindow: () => null,
      createWindow,
      quit: vi.fn(),
    })

    lifecycle.showMainWindow()

    expect(createWindow).toHaveBeenCalledOnce()
  })

  it("普通关闭只隐藏窗口，不进入退出动作", () => {
    const event = { preventDefault: vi.fn() }
    const window: TrayManagedWindow = {
      isDestroyed: vi.fn(() => false),
      isMinimized: vi.fn(() => false),
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
      hide: vi.fn(),
    }
    const quit = vi.fn()
    const lifecycle = new TrayWindowLifecycle({
      getWindow: () => window,
      createWindow: vi.fn(),
      quit,
    })

    lifecycle.handleWindowClose(event, window)

    expect(event.preventDefault).toHaveBeenCalledOnce()
    expect(window.hide).toHaveBeenCalledOnce()
    expect(quit).not.toHaveBeenCalled()
  })

  it("托盘退出后放行窗口关闭并调用真正退出", () => {
    const event = { preventDefault: vi.fn() }
    const window: TrayManagedWindow = {
      isDestroyed: vi.fn(() => false),
      isMinimized: vi.fn(() => false),
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
      hide: vi.fn(),
    }
    const quit = vi.fn()
    const lifecycle = new TrayWindowLifecycle({
      getWindow: () => window,
      createWindow: vi.fn(),
      quit,
    })

    lifecycle.quitApplication()
    lifecycle.handleWindowClose(event, window)

    expect(quit).toHaveBeenCalledOnce()
    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(window.hide).not.toHaveBeenCalled()
  })

  it("操作系统退出同样会放行窗口关闭", () => {
    const event = { preventDefault: vi.fn() }
    const window: TrayManagedWindow = {
      isDestroyed: vi.fn(() => false),
      isMinimized: vi.fn(() => false),
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
      hide: vi.fn(),
    }
    const lifecycle = new TrayWindowLifecycle({
      getWindow: () => window,
      createWindow: vi.fn(),
      quit: vi.fn(),
    })

    lifecycle.beginQuit()
    lifecycle.handleWindowClose(event, window)

    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(window.hide).not.toHaveBeenCalled()
  })
})

describe("createTrayMenuTemplate", () => {
  it("提供固定的打开和退出入口并绑定正确动作", () => {
    const open = vi.fn()
    const quit = vi.fn()
    const menu = createTrayMenuTemplate({ open, quit })

    expect(menu.map((item) => "type" in item ? item.type : item.label)).toEqual([
      "AnyDatas",
      "separator",
      "打开 AnyDatas",
      "separator",
      "退出 AnyDatas",
    ])
    const openItem = menu[2]
    const quitItem = menu[4]
    if (openItem !== undefined && "click" in openItem) openItem.click?.()
    if (quitItem !== undefined && "click" in quitItem) quitItem.click?.()
    expect(open).toHaveBeenCalledOnce()
    expect(quit).toHaveBeenCalledOnce()
  })
})
