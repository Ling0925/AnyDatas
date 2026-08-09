import { randomBytes } from "node:crypto"
import { createWriteStream } from "node:fs"
import { mkdir } from "node:fs/promises"
import { join } from "node:path"
import { createInterface } from "node:readline"
import { spawn } from "node:child_process"
import type { ChildProcessByStdio } from "node:child_process"
import type { Readable } from "node:stream"
import * as z from "zod"
import type { InstalledServerRuntime } from "./backend-release.js"
import {
  DESKTOP_PROTOCOL_VERSION,
} from "./backend-types.js"
import type {
  BackendAdapter,
  BackendConnection,
  BackendHandshake,
  BackendProgress,
} from "./backend-types.js"

const HANDSHAKE_TIMEOUT_MS = 10_000
const STARTUP_TIMEOUT_MS = 30_000
const SHUTDOWN_TIMEOUT_MS = 5_000
const READY_PREFIX = "ANYDATAS_READY "
const DESKTOP_TOKEN_HEADER = "x-anydatas-desktop-token"

const handshakeSchema = z.strictObject({
  service: z.literal("anydatas-server"),
  serverVersion: z.string().min(1),
  protocolVersion: z.number().int().positive(),
  capabilities: z.array(z.string().min(1)),
})
const readySchema = z.strictObject({
  address: z.string().min(1),
  serverVersion: z.string().min(1),
  protocolVersion: z.number().int().positive(),
})

type ServerChild = ChildProcessByStdio<null, Readable, Readable>

export class BackendConnectionError extends Error {
  override readonly name = "BackendConnectionError"

  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
  }
}

/**
 * 规范化用户输入的服务器地址，并限制为当前代理能够无歧义转发的根 HTTP(S) 地址。
 *
 * 自动补全 http:// 方便输入局域网地址；拒绝凭据、查询和子路径可以避免登录请求被转发到意外位置。
 */
export function normalizeRemoteServerUrl(input: string): URL {
  const trimmed = input.trim()
  if (trimmed.length === 0) {
    throw new BackendConnectionError("请输入服务器地址")
  }
  const withProtocol = /^[a-z][a-z0-9+.-]*:\/\//iu.test(trimmed)
    ? trimmed
    : `http://${trimmed}`
  let url: URL
  try {
    url = new URL(withProtocol)
  } catch (error) {
    throw new BackendConnectionError("服务器地址格式不正确", { cause: error })
  }
  if (
    !["http:", "https:"].includes(url.protocol)
    || url.username !== ""
    || url.password !== ""
    || url.search !== ""
    || url.hash !== ""
    || !["", "/"].includes(url.pathname)
  ) {
    throw new BackendConnectionError("服务器地址必须是 HTTP(S) 根地址，且不能包含凭据、路径或参数")
  }
  url.pathname = "/"
  return url
}

/**
 * 在登录前读取服务端握手并验证协议号，单机令牌只在本机子进程场景下注入。
 *
 * 把远端和单机连接都走同一握手函数，可以确保两个 Adapter 对兼容性的判断完全一致。
 */
export async function readBackendHandshake(
  baseUrl: URL,
  desktopToken?: string,
  request: typeof fetch = fetch,
): Promise<BackendHandshake> {
  const endpoint = new URL("/api/desktop-handshake", baseUrl)
  const headers: Record<string, string> = { accept: "application/json" }
  if (desktopToken !== undefined) {
    headers[DESKTOP_TOKEN_HEADER] = desktopToken
  }
  let response: Response
  try {
    response = await request(endpoint, {
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(HANDSHAKE_TIMEOUT_MS),
    })
  } catch (error) {
    throw new BackendConnectionError(`无法连接服务器：${endpoint.origin}`, { cause: error })
  }
  if (!response.ok) {
    throw new BackendConnectionError(`服务器握手失败：HTTP ${response.status}`)
  }
  const handshake = handshakeSchema.parse(await response.json())
  if (handshake.protocolVersion !== DESKTOP_PROTOCOL_VERSION) {
    throw new BackendConnectionError(
      `服务端协议 ${handshake.protocolVersion} 与桌面端协议 ${DESKTOP_PROTOCOL_VERSION} 不兼容`,
    )
  }
  return handshake
}

/**
 * 等待子进程输出机器可读就绪行，同时把完整 stdout/stderr 留到本地诊断日志。
 *
 * 使用显式就绪协议比轮询固定端口更可靠，也能安全使用端口 0 消除多实例和端口占用冲突。
 */
async function waitForReady(
  child: ServerChild,
  logPath: string,
): Promise<z.infer<typeof readySchema>> {
  const log = createWriteStream(logPath, { flags: "a", mode: 0o600 })
  child.stdout.on("data", (chunk: Buffer) => log.write(chunk))
  child.stderr.on("data", (chunk: Buffer) => log.write(chunk))
  const lines = createInterface({ input: child.stdout })

  child.once("exit", () => log.end())
  return new Promise<z.infer<typeof readySchema>>((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup()
      reject(new BackendConnectionError("本地服务端启动超时"))
    }, STARTUP_TIMEOUT_MS)

    const cleanup = (): void => {
      clearTimeout(timeout)
      lines.removeAllListeners()
      child.removeListener("error", onError)
      child.removeListener("exit", onExit)
    }
    const onError = (error: Error): void => {
      cleanup()
      reject(new BackendConnectionError("无法启动本地服务端进程", { cause: error }))
    }
    const onExit = (code: number | null): void => {
      cleanup()
      reject(new BackendConnectionError(`本地服务端在就绪前退出：${code ?? "signal"}`))
    }
    child.once("error", onError)
    child.once("exit", onExit)
    lines.on("line", (line) => {
      if (!line.startsWith(READY_PREFIX)) {
        return
      }
      try {
        const ready = readySchema.parse(JSON.parse(line.slice(READY_PREFIX.length)))
        cleanup()
        resolve(ready)
      } catch (error) {
        cleanup()
        reject(new BackendConnectionError("本地服务端返回了无效的就绪消息", { cause: error }))
      }
    })
  }).finally(() => lines.close())
}

/**
 * 优先发送温和终止信号并限时等待，超时后才强制结束进程。
 *
 * 给予 SQLite 和后台 Worker 清理时间可以减少 WAL 恢复和“服务重启导致任务中断”记录。
 */
async function stopChild(child: ServerChild): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }
  const exitPromise = new Promise<boolean>((resolve) => child.once("exit", () => resolve(true)))
  child.kill("SIGTERM")
  const exited = await Promise.race([
    exitPromise,
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), SHUTDOWN_TIMEOUT_MS)),
  ])
  if (!exited && child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL")
  }
}

export type StandaloneBackendAdapterOptions = {
  readonly installer: {
    readonly install: (onProgress: (progress: number | null) => void) => Promise<InstalledServerRuntime>
  }
  readonly userData: string
  readonly webDirectory: string
  readonly spawnProcess?: typeof spawn
  readonly request?: typeof fetch
}

export class StandaloneBackendAdapter implements BackendAdapter {
  constructor(private readonly options: StandaloneBackendAdapterOptions) {}

  /**
   * 安装并启动锁定版本的本地服务端，成功后返回可由运行时管理器统一托管的连接。
   *
   * 下载、进程、握手和资源配置全部隐藏在 Adapter 内，渲染层只观察标准状态而不会接触可执行路径或令牌。
   */
  async connect(progress: (event: BackendProgress) => void): Promise<BackendConnection> {
    progress({ phase: "downloading", message: "正在检查本地服务端…", progress: null })
    const installed = await this.options.installer.install((value) => {
      progress({ phase: "downloading", message: "正在下载并校验服务端…", progress: value })
    })
    if (installed.protocolVersion !== DESKTOP_PROTOCOL_VERSION) {
      throw new BackendConnectionError("已安装服务端协议与桌面端不兼容")
    }

    progress({ phase: "starting", message: "正在启动本地服务端…", progress: null })
    const dataDirectory = join(this.options.userData, "standalone-data")
    const logDirectory = join(this.options.userData, "logs")
    await mkdir(dataDirectory, { recursive: true })
    await mkdir(logDirectory, { recursive: true })
    const desktopToken = randomBytes(32).toString("base64url")
    const spawnProcess = this.options.spawnProcess ?? spawn
    const child = spawnProcess(installed.binaryPath, [], {
      cwd: dataDirectory,
      env: {
        ...process.env,
        ANYDATAS_BIND: "127.0.0.1:0",
        ANYDATAS_DATA_DIR: dataDirectory,
        ANYDATAS_WEB_DIR: this.options.webDirectory,
        ANYDATAS_DESKTOP_TOKEN: desktopToken,
        ANYDATAS_COOKIE_SECURE: "0",
        ANYDATAS_DUCKDB_MEMORY_LIMIT_MB: "512",
        ANYDATAS_DUCKDB_THREADS: "2",
        ANYDATAS_DUCKDB_TEMP_LIMIT_MB: "4096",
        ANYDATAS_MIN_FREE_SPACE_MB: "256",
        ANYDATAS_JOB_RESULT_MAX_MB: "4096",
        RUST_LOG: "anydatas_api=info,tower_http=warn",
      },
      stdio: ["ignore", "pipe", "pipe"],
    })

    let expectedStop = false
    try {
      const ready = await waitForReady(child, join(logDirectory, "server.log"))
      if (
        ready.serverVersion !== installed.serverVersion
        || ready.protocolVersion !== installed.protocolVersion
      ) {
        throw new BackendConnectionError("本地服务端就绪版本与已校验发行物不一致")
      }
      const baseUrl = new URL(`http://${ready.address}`)
      const handshake = await readBackendHandshake(baseUrl, desktopToken, this.options.request)
      if (handshake.serverVersion !== installed.serverVersion) {
        throw new BackendConnectionError("本地服务端握手版本与发行清单不一致")
      }
      child.once("exit", (code) => {
        if (!expectedStop) {
          progress({
            phase: "failed",
            message: `本地服务端意外退出：${code ?? "signal"}`,
            progress: null,
          })
        }
      })
      return {
        baseUrl,
        desktopToken,
        handshake,
        stop: async () => {
          expectedStop = true
          await stopChild(child)
        },
      }
    } catch (error) {
      expectedStop = true
      await stopChild(child)
      throw error
    }
  }
}

export type RemoteBackendAdapterOptions = {
  readonly serverUrl: URL
  readonly request?: typeof fetch
}

export class RemoteBackendAdapter implements BackendAdapter {
  constructor(private readonly options: RemoteBackendAdapterOptions) {}

  /**
   * 验证远端 AnyDatas 服务端并返回与单机模式相同形状的连接。
   *
   * 远端连接也经过协议握手，运行时管理器因此无需为登录和后续请求维护另一套分支逻辑。
   */
  async connect(progress: (event: BackendProgress) => void): Promise<BackendConnection> {
    progress({ phase: "starting", message: "正在验证服务器地址…", progress: null })
    const handshake = await readBackendHandshake(
      this.options.serverUrl,
      undefined,
      this.options.request,
    )
    return {
      baseUrl: this.options.serverUrl,
      handshake,
      stop: async () => undefined,
    }
  }
}
