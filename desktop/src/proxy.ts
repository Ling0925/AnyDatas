import { request as httpRequest } from "node:http"
import { createServer } from "node:http"
import type {
  ClientRequest,
  IncomingHttpHeaders,
  IncomingMessage,
  OutgoingHttpHeaders,
  Server,
  ServerResponse,
} from "node:http"
import { request as httpsRequest } from "node:https"
import { CookieJar } from "./cookie-jar.js"

const DEFAULT_TARGET = "http://127.0.0.1:8080"
const ALLOWED_METHODS = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
const DEFAULT_ALLOWED_HEADERS = "content-type,authorization,x-requested-with,accept"
const FILTERED_RESPONSE_HEADERS = new Set([
  "set-cookie",
  "access-control-allow-origin",
  "access-control-allow-credentials",
  "access-control-allow-methods",
  "access-control-allow-headers",
])

export type ApiProxyOptions = {
  readonly target?: URL
  readonly port?: number
  readonly dev: boolean
  readonly timeoutMs?: number
}

export class ApiTargetError extends Error {
  override readonly name = "ApiTargetError"

  constructor(readonly configuredTarget: string, options?: ErrorOptions) {
    super("ANYDATAS_API_TARGET must be an HTTP or HTTPS URL", options)
  }
}

export class ApiProxyStateError extends Error {
  override readonly name = "ApiProxyStateError"

  constructor(message: string) {
    super(message)
  }
}

export function resolveApiTarget(configuredTarget: string | undefined): URL {
  const value = configuredTarget ?? DEFAULT_TARGET
  let target: URL
  try {
    target = new URL(value)
  } catch (error) {
    if (error instanceof TypeError) {
      throw new ApiTargetError(value, { cause: error })
    }
    throw error
  }
  const supportedProtocol = target.protocol === "http:" || target.protocol === "https:"
  if (!supportedProtocol || target.username !== "" || target.password !== "") {
    throw new ApiTargetError(value)
  }
  return target
}

function responseHeaders(headers: IncomingHttpHeaders): OutgoingHttpHeaders {
  const forwarded: OutgoingHttpHeaders = {}
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined && !FILTERED_RESPONSE_HEADERS.has(name.toLowerCase())) {
      forwarded[name] = value
    }
  }
  return forwarded
}

export class ApiProxy {
  readonly #server: Server
  readonly #jar = new CookieJar()
  #port: number | undefined
  #target: URL | undefined
  #desktopToken: string | undefined

  constructor(private readonly options: ApiProxyOptions) {
    this.#target = options.target
    this.#server = createServer((request, response) => this.#handle(request, response))
  }

  get url(): string {
    if (this.#port === undefined) {
      throw new ApiProxyStateError("API proxy is not listening")
    }
    return `http://127.0.0.1:${this.#port}`
  }

  #originAllowed(origin: string): boolean {
    if (this.options.dev) {
      return origin === "http://127.0.0.1:5173" || origin === "http://localhost:5173"
    }
    return origin === "null" || origin.startsWith("file://")
  }

  #applyCors(request: IncomingMessage, response: ServerResponse): boolean {
    const origin = request.headers.origin
    if (origin === undefined || !this.#originAllowed(origin)) {
      return false
    }
    response.setHeader("access-control-allow-origin", origin)
    response.setHeader("access-control-allow-credentials", "true")
    response.setHeader("vary", "Origin")
    return true
  }

  #preflight(request: IncomingMessage, response: ServerResponse): void {
    const approved = this.#applyCors(request, response)
    if (!approved) {
      response.writeHead(403, { "content-type": "application/json; charset=utf-8" })
      response.end(JSON.stringify({ error: "origin not allowed" }))
      return
    }
    response.setHeader("access-control-allow-methods", ALLOWED_METHODS)
    response.setHeader(
      "access-control-allow-headers",
      request.headers["access-control-request-headers"] ?? DEFAULT_ALLOWED_HEADERS,
    )
    response.writeHead(204)
    response.end()
  }

  #upstreamRequest(request: IncomingMessage, target: URL): ClientRequest {
    const cookie = this.#jar.header()
    const headers: OutgoingHttpHeaders = {
      ...request.headers,
      host: target.host,
    }
    delete headers["x-anydatas-desktop-token"]
    if (cookie !== undefined) {
      headers.cookie = cookie
    }
    if (this.#desktopToken !== undefined) {
      headers["x-anydatas-desktop-token"] = this.#desktopToken
    }
    const send = target.protocol === "https:" ? httpsRequest : httpRequest
    return send({
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port,
      path: request.url ?? "/",
      method: request.method,
      headers,
    })
  }

  #handle(request: IncomingMessage, response: ServerResponse): void {
    if (request.method === "OPTIONS") {
      this.#preflight(request, response)
      return
    }

    const target = this.#target
    if (target === undefined) {
      this.#applyCors(request, response)
      response.writeHead(503, { "content-type": "application/json; charset=utf-8" })
      response.end(JSON.stringify({ error: "backend runtime is not configured" }))
      return
    }

    const upstream = this.#upstreamRequest(request, target)
    upstream.setTimeout(this.options.timeoutMs ?? 300_000, () => upstream.destroy())
    upstream.once("response", (upstreamResponse) => {
      this.#jar.capture(upstreamResponse.headers["set-cookie"] ?? [])
      for (const [name, value] of Object.entries(responseHeaders(upstreamResponse.headers))) {
        if (value !== undefined) {
          response.setHeader(name, value)
        }
      }
      this.#applyCors(request, response)
      response.writeHead(upstreamResponse.statusCode ?? 502)
      upstreamResponse.pipe(response)
    })
    upstream.once("error", () => {
      if (response.headersSent) {
        response.destroy()
        return
      }
      this.#applyCors(request, response)
      response.writeHead(502, { "content-type": "application/json; charset=utf-8" })
      response.end(JSON.stringify({ error: "upstream unavailable" }))
    })
    request.once("aborted", () => upstream.destroy())
    request.pipe(upstream)
  }

  async listen(): Promise<number> {
    await new Promise<void>((resolve, reject) => {
      this.#server.once("error", reject)
      this.#server.listen(this.options.port ?? 28_090, "127.0.0.1", resolve)
    })
    const address = this.#server.address()
    if (address === null || typeof address === "string") {
      throw new ApiProxyStateError("API proxy did not expose a TCP address")
    }
    this.#port = address.port
    return address.port
  }

  /**
   * 原子切换代理上游和可选桌面令牌，并始终清空上一服务器的 Cookie。
   *
   * 单机与远端 Adapter 共用该 seam，渲染层的固定 API 地址无需随模式变化而重建。
   */
  setTarget(target: URL | undefined, desktopToken?: string): void {
    this.#target = target
    this.#desktopToken = desktopToken
    this.#jar.clear()
  }

  /**
   * 返回当前规范化上游地址，仅用于诊断界面，不包含桌面令牌或 Cookie。
   *
   * 通过只读字符串暴露目标可以保留原有文件采集诊断能力，而不会泄露主进程凭据。
   */
  targetUrl(): string | null {
    return this.#target?.href ?? null
  }

  async close(): Promise<void> {
    if (!this.#server.listening) {
      return
    }
    await new Promise<void>((resolve, reject) => {
      this.#server.close((error) => (error === undefined ? resolve() : reject(error)))
    })
  }
}
