import { randomUUID } from "node:crypto"
import { mkdir, open, readFile, rename, rm } from "node:fs/promises"
import { join } from "node:path"
import * as z from "zod"
import { normalizeRemoteServerUrl } from "./backend-adapters.js"
import type {
  BackendAdapter,
  BackendConnection,
  BackendProgress,
  BackendSelection,
  BackendStatus,
} from "./backend-types.js"

const runtimeConfigSchema = z.strictObject({
  schemaVersion: z.literal(1),
  selection: z.discriminatedUnion("mode", [
    z.strictObject({ mode: z.literal("standalone") }),
    z.strictObject({ mode: z.literal("remote"), serverUrl: z.string().min(1) }),
  ]),
})

type RuntimeConfig = z.infer<typeof runtimeConfigSchema>

export type RuntimeProxy = {
  readonly setTarget: (target: URL | undefined, desktopToken?: string) => void
}

export type BackendRuntimeManagerOptions = {
  readonly userData: string
  readonly proxy: RuntimeProxy
  readonly adapterFor: (selection: BackendSelection) => BackendAdapter
}

export class BackendRuntimeConfigError extends Error {
  override readonly name = "BackendRuntimeConfigError"

  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
  }
}

function initialStatus(): BackendStatus {
  return {
    mode: null,
    phase: "unconfigured",
    serverUrl: null,
    serverVersion: null,
    protocolVersion: null,
    message: "请选择单机模式或连接服务器",
    progress: null,
  }
}

/**
 * 把未知异常转成可以展示的简短文本，不把下载响应或进程环境泄露到渲染层。
 *
 * 状态界面获得稳定错误消息的同时，完整诊断仍保留在主进程和服务端日志中。
 */
function publicErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "后端运行时启动失败"
}

/**
 * 规范化并复制运行模式配置，防止调用方后续修改对象影响持久化和 Adapter 选择。
 *
 * 远端地址在进入配置文件前统一处理，后续启动就不会因同一地址的不同写法产生重复会话。
 */
function normalizeSelection(selection: BackendSelection): BackendSelection {
  if (selection.mode === "standalone") {
    return { mode: "standalone" }
  }
  return { mode: "remote", serverUrl: normalizeRemoteServerUrl(selection.serverUrl).href }
}

export class BackendRuntimeManager {
  readonly #configPath: string
  readonly #subscribers = new Set<(status: BackendStatus) => void>()
  #status: BackendStatus = initialStatus()
  #connection: BackendConnection | undefined
  #generation = 0
  #operation: Promise<BackendStatus> = Promise.resolve(this.#status)

  constructor(private readonly options: BackendRuntimeManagerOptions) {
    this.#configPath = join(options.userData, "backend-runtime.json")
  }

  /**
   * 返回当前不可变状态快照，渲染层不接触运行时内部连接和进程对象。
   *
   * 统一快照让首次路由、进度界面和测试都通过同一个 interface 观察行为。
   */
  status(): BackendStatus {
    return { ...this.#status }
  }

  /**
   * 订阅运行时状态并返回精确解绑函数，窗口重载时不会累积 IPC 推送。
   *
   * 订阅者只接收状态快照，下载器和进程实现可以自由重构而不扩大渲染层接口。
   */
  subscribe(callback: (status: BackendStatus) => void): () => void {
    this.#subscribers.add(callback)
    return () => this.#subscribers.delete(callback)
  }

  /**
   * 在串行队列中读取上次成功配置并恢复连接；首次使用则保持未配置状态。
   *
   * 串行化初始化和用户点击可以避免两个本地服务端同时下载、迁移或抢占数据目录。
   */
  initialize(): Promise<BackendStatus> {
    return this.#enqueue(async () => {
      let config: RuntimeConfig
      try {
        config = runtimeConfigSchema.parse(JSON.parse(await readFile(this.#configPath, "utf8")))
      } catch (error) {
        if (error instanceof Error && "code" in error && error.code === "ENOENT") {
          this.#publish(initialStatus())
          return this.status()
        }
        this.#publish({
          ...initialStatus(),
          phase: "failed",
          message: "桌面后端配置已损坏，请重新选择运行模式",
        })
        return this.status()
      }
      return this.#connect(normalizeSelection(config.selection), false)
    })
  }

  /**
   * 验证并切换运行模式，只有握手成功后才原子保存新配置。
   *
   * 失败配置不会覆盖最后一次可用选择，用户修正地址或恢复网络后可以安全重试。
   */
  configure(selection: BackendSelection): Promise<BackendStatus> {
    return this.#enqueue(() => this.#connect(normalizeSelection(selection), true))
  }

  /**
   * 停止当前连接并删除模式配置，供用户从设置页返回首次选择界面。
   *
   * 同时清空代理会话可以确保服务器切换时 Cookie 不会跨地址复用。
   */
  reset(): Promise<BackendStatus> {
    return this.#enqueue(async () => {
      await this.#disconnect()
      await rm(this.#configPath, { force: true })
      this.#publish(initialStatus())
      return this.status()
    })
  }

  /**
   * 应用退出时关闭本地子进程；远端 Adapter 的 stop 是无副作用实现。
   *
   * 关闭逻辑与模式无关，主进程无需知道当前连接究竟是本地进程还是远端地址。
   */
  stop(): Promise<void> {
    return this.#enqueue(async () => {
      await this.#disconnect()
      return this.status()
    }).then(() => undefined)
  }

  /**
   * 把所有状态变化集中广播为副本，避免订阅者修改管理器内部对象。
   *
   * 单点发布使下载进度、崩溃和握手结果具有一致的时序和测试表面。
   */
  #publish(status: BackendStatus): void {
    this.#status = status
    for (const subscriber of this.#subscribers) {
      subscriber(this.status())
    }
  }

  /**
   * 将异步切换加入恢复型队列，前一次失败不会阻塞后续重试。
   *
   * 这是管理器内部 seam，调用方仍只面对普通 Promise 而不需要自行做互斥。
   */
  #enqueue(operation: () => Promise<BackendStatus>): Promise<BackendStatus> {
    const result = this.#operation.then(operation, operation)
    this.#operation = result.catch(() => this.status())
    return result
  }

  /**
   * 停止当前 Adapter 并让代理进入不可用状态，切换服务器时不会复用旧 Cookie。
   *
   * 先撤销代理再停进程可以阻止退出窗口内的新请求继续进入正在关闭的 SQLite Worker。
   */
  async #disconnect(): Promise<void> {
    this.#generation += 1
    this.options.proxy.setTarget(undefined)
    const connection = this.#connection
    this.#connection = undefined
    await connection?.stop()
  }

  /**
   * 选择对应 Adapter、转发进度并在握手后切换统一代理目标。
   *
   * 运行模式的复杂实现被隐藏在该 seam 后，路由和登录只需判断 ready 状态。
   */
  async #connect(selection: BackendSelection, persist: boolean): Promise<BackendStatus> {
    await this.#disconnect()
    const generation = this.#generation
    this.#publish({
      mode: selection.mode,
      phase: "starting",
      serverUrl: selection.mode === "remote" ? selection.serverUrl : null,
      serverVersion: null,
      protocolVersion: null,
      message: selection.mode === "standalone" ? "正在准备单机服务…" : "正在连接服务器…",
      progress: null,
    })

    const onProgress = (event: BackendProgress): void => {
      if (generation !== this.#generation) {
        return
      }
      if (event.phase === "failed") {
        this.options.proxy.setTarget(undefined)
      }
      this.#publish({
        ...this.#status,
        phase: event.phase,
        message: event.message,
        progress: event.progress,
      })
    }

    try {
      const adapter = this.options.adapterFor(selection)
      const connection = await adapter.connect(onProgress)
      if (generation !== this.#generation) {
        await connection.stop()
        return this.status()
      }
      this.#connection = connection
      this.options.proxy.setTarget(connection.baseUrl, connection.desktopToken)
      if (persist) {
        await this.#writeConfig({ schemaVersion: 1, selection })
      }
      this.#publish({
        mode: selection.mode,
        phase: "ready",
        serverUrl: connection.baseUrl.href,
        serverVersion: connection.handshake.serverVersion,
        protocolVersion: connection.handshake.protocolVersion,
        message: selection.mode === "standalone" ? "单机服务已就绪" : "服务器连接成功",
        progress: 1,
      })
      return this.status()
    } catch (error) {
      this.options.proxy.setTarget(undefined)
      const connection = this.#connection
      this.#connection = undefined
      await connection?.stop()
      this.#publish({
        ...this.#status,
        phase: "failed",
        message: publicErrorMessage(error),
        progress: null,
      })
      throw error
    }
  }

  /**
   * 使用同目录临时文件和原子改名保存最后一次成功模式。
   *
   * 应用或系统在写入中途退出时，旧配置仍保持完整，不会让下次启动进入半配置状态。
   */
  async #writeConfig(config: RuntimeConfig): Promise<void> {
    await mkdir(this.options.userData, { recursive: true })
    const temporaryPath = `${this.#configPath}.${process.pid}.${randomUUID()}.tmp`
    const handle = await open(temporaryPath, "wx", 0o600)
    try {
      await handle.writeFile(`${JSON.stringify(config, null, 2)}\n`, "utf8")
      await handle.sync()
    } finally {
      await handle.close()
    }
    await rename(temporaryPath, this.#configPath)
  }
}
