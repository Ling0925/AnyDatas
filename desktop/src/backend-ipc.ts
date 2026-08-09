import * as z from "zod"
import type { BackendStatus } from "./backend-types.js"
import type { IpcHandler, IpcRegistrar } from "./ipc.js"

export const BACKEND_CHANNELS = {
  status: "desktop:backend:status",
  configure: "desktop:backend:configure",
  reset: "desktop:backend:reset",
  event: "desktop:backend:event",
} as const

const emptyArgsSchema = z.tuple([])
const selectionSchema = z.discriminatedUnion("mode", [
  z.strictObject({ mode: z.literal("standalone") }),
  z.strictObject({ mode: z.literal("remote"), serverUrl: z.string().trim().min(1) }),
])

type BackendEventSender = {
  readonly send: (channel: string, payload: BackendStatus) => void
}

type BackendRuntimePort = {
  readonly status: () => BackendStatus
  readonly configure: (selection: { mode: "standalone" } | { mode: "remote"; serverUrl: string }) => Promise<BackendStatus>
  readonly reset: () => Promise<BackendStatus>
}

/**
 * 把运行时状态固定发送到只读事件通道，渲染层不能构造主进程内部事件。
 *
 * 集中发送函数让窗口生命周期检查留在 main.ts，IPC 模块保持可独立测试。
 */
export function emitBackendStatus(sender: BackendEventSender, status: BackendStatus): void {
  sender.send(BACKEND_CHANNELS.event, status)
}

/**
 * 注册模式查询、配置和重置方法，并返回完整清理函数。
 *
 * 所有输入先经过严格 schema，避免被篡改的渲染进程把任意对象或进程参数传入主进程。
 */
export function registerBackendIpc(options: {
  readonly registrar: IpcRegistrar
  readonly runtime: BackendRuntimePort
}): () => void {
  const registered: string[] = []
  const handle = (channel: string, handler: IpcHandler): void => {
    options.registrar.handle(channel, handler)
    registered.push(channel)
  }

  handle(BACKEND_CHANNELS.status, async (...args) => {
    emptyArgsSchema.parse(args)
    return options.runtime.status()
  })
  handle(BACKEND_CHANNELS.configure, async (...args) => {
    const [input] = z.tuple([z.unknown()]).parse(args)
    return options.runtime.configure(selectionSchema.parse(input))
  })
  handle(BACKEND_CHANNELS.reset, async (...args) => {
    emptyArgsSchema.parse(args)
    return options.runtime.reset()
  })

  return () => {
    for (const channel of registered) {
      options.registrar.removeHandler(channel)
    }
  }
}
