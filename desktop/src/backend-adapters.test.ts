import { createServer } from "node:http"
import type { Server } from "node:http"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import {
  BackendConnectionError,
  normalizeRemoteServerUrl,
  readBackendHandshake,
  StandaloneBackendAdapter,
} from "./backend-adapters.js"

/**
 * 启动返回指定协议号的握手服务，验证真实 HTTP 状态和桌面令牌请求头。
 *
 * 用随机回环端口可以覆盖 URL 拼接和请求超时路径，不依赖任何生产服务器。
 */
async function handshakeServer(protocolVersion = 1): Promise<{
  readonly server: Server
  readonly url: URL
  readonly token: () => string | undefined
}> {
  let token: string | undefined
  const server = createServer((request, response) => {
    token = request.headers["x-anydatas-desktop-token"] as string | undefined
    response.setHeader("content-type", "application/json")
    response.end(JSON.stringify({
      service: "anydatas-server",
      serverVersion: "0.1.0",
      protocolVersion,
      capabilities: ["agent"],
    }))
  })
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (address === null || typeof address === "string") throw new Error("missing test address")
  return {
    server,
    url: new URL(`http://127.0.0.1:${address.port}`),
    token: () => token,
  }
}

describe("normalizeRemoteServerUrl", () => {
  it("accepts a LAN address and adds the HTTP scheme", () => {
    expect(normalizeRemoteServerUrl("192.168.8.108:8080").href).toBe("http://192.168.8.108:8080/")
  })

  it.each([
    "ftp://example.com",
    "https://user:pass@example.com",
    "https://example.com/anydatas",
    "https://example.com?token=value",
  ])("rejects an unsafe or ambiguous server URL: %s", (url) => {
    expect(() => normalizeRemoteServerUrl(url)).toThrow(BackendConnectionError)
  })
})

describe("readBackendHandshake", () => {
  const servers: Server[] = []

  afterEach(async () => {
    await Promise.all(servers.splice(0).map((server) => new Promise<void>((resolve) => {
      server.close(() => resolve())
    })))
  })

  it("validates the service and forwards the local desktop token", async () => {
    const target = await handshakeServer()
    servers.push(target.server)

    await expect(readBackendHandshake(target.url, "local-secret")).resolves.toMatchObject({
      serverVersion: "0.1.0",
      protocolVersion: 1,
    })
    expect(target.token()).toBe("local-secret")
  })

  it("rejects an incompatible protocol before login", async () => {
    const target = await handshakeServer(2)
    servers.push(target.server)

    await expect(readBackendHandshake(target.url)).rejects.toThrow(/不兼容/u)
  })
})

const integrationBinary = process.env["ANYDATAS_TEST_SERVER_BINARY"]

describe.skipIf(integrationBinary === undefined)("StandaloneBackendAdapter integration", () => {
  it("starts the real Rust binary with a protected random loopback port", async () => {
    if (integrationBinary === undefined) throw new Error("integration binary is required")
    const root = await mkdtemp(join(tmpdir(), "anydatas-standalone-"))
    const adapter = new StandaloneBackendAdapter({
      userData: root,
      webDirectory: join(root, "web"),
      installer: {
        install: async () => ({
          binaryPath: integrationBinary,
          serverVersion: "0.1.0",
          protocolVersion: 1,
        }),
      },
    })
    let connection
    try {
      connection = await adapter.connect(() => undefined)
      expect(connection.baseUrl.hostname).toBe("127.0.0.1")
      expect(connection.baseUrl.port).not.toBe("8080")
      expect(connection.handshake).toMatchObject({ protocolVersion: 1, serverVersion: "0.1.0" })

      const unauthorized = await fetch(new URL("/api/readyz", connection.baseUrl))
      expect(unauthorized.status).toBe(401)
      const ready = await fetch(new URL("/api/readyz", connection.baseUrl), {
        headers: { "x-anydatas-desktop-token": connection.desktopToken ?? "" },
      })
      expect(ready.status).toBe(200)
    } finally {
      await connection?.stop()
      await rm(root, { recursive: true, force: true })
    }
  }, 15_000)
})
