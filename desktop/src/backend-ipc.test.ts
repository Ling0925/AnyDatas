import { describe, expect, it } from "vitest"
import { BACKEND_CHANNELS, emitBackendStatus, registerBackendIpc } from "./backend-ipc.js"
import type { BackendSelection, BackendStatus } from "./backend-types.js"
import type { IpcHandler, IpcRegistrar } from "./ipc.js"

const READY: BackendStatus = {
  mode: "remote",
  phase: "ready",
  serverUrl: "https://example.com/",
  serverVersion: "0.1.0",
  protocolVersion: 1,
  message: "服务器连接成功",
  progress: 1,
}

class FakeRegistrar implements IpcRegistrar {
  readonly handlers = new Map<string, IpcHandler>()

  /** 注册固定通道，重复注册会覆盖以贴近 Electron ipcMain.handle 的唯一键语义。 */
  handle(channel: string, handler: IpcHandler): void {
    this.handlers.set(channel, handler)
  }

  /** 清理窗口生命周期内注册的固定通道，便于断言清理函数完整性。 */
  removeHandler(channel: string): void {
    this.handlers.delete(channel)
  }

  /** 通过已注册 interface 调用处理器，测试不会越过 IPC seam 访问实现状态。 */
  async invoke(channel: string, ...args: readonly unknown[]): Promise<unknown> {
    const handler = this.handlers.get(channel)
    if (handler === undefined) throw new Error(`missing handler ${channel}`)
    return handler(...args)
  }
}

describe("registerBackendIpc", () => {
  it("validates selections and delegates fixed methods", async () => {
    const registrar = new FakeRegistrar()
    const selections: BackendSelection[] = []
    registerBackendIpc({
      registrar,
      runtime: {
        status: () => READY,
        configure: async (selection) => {
          selections.push(selection)
          return READY
        },
        reset: async () => ({ ...READY, phase: "unconfigured" }),
      },
    })

    await expect(registrar.invoke(BACKEND_CHANNELS.status)).resolves.toEqual(READY)
    await expect(registrar.invoke(BACKEND_CHANNELS.configure, {
      mode: "remote",
      serverUrl: "https://example.com",
    })).resolves.toEqual(READY)
    expect(selections).toEqual([{ mode: "remote", serverUrl: "https://example.com" }])
    await expect(registrar.invoke(BACKEND_CHANNELS.configure, {
      mode: "remote",
      serverUrl: "",
    })).rejects.toBeDefined()
  })

  it("removes every registered request channel", () => {
    const registrar = new FakeRegistrar()
    const remove = registerBackendIpc({
      registrar,
      runtime: {
        status: () => READY,
        configure: async () => READY,
        reset: async () => READY,
      },
    })

    remove()

    expect(registrar.handlers.size).toBe(0)
  })
})

describe("emitBackendStatus", () => {
  it("sends only the fixed backend event channel", () => {
    const calls: unknown[][] = []

    emitBackendStatus({ send: (...args) => calls.push(args) }, READY)

    expect(calls).toEqual([[BACKEND_CHANNELS.event, READY]])
  })
})
