import { createHash } from "node:crypto"
import { createServer } from "node:http"
import type { Server } from "node:http"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { ApiProxy, resolveApiTarget } from "./proxy.js"

async function listen(server: Server): Promise<number> {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (address === null || typeof address === "string") {
    throw new Error("test server did not expose a TCP address")
  }
  return address.port
}

async function close(server: Server): Promise<void> {
  if (!server.listening) {
    return
  }
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error === undefined ? resolve() : reject(error)))
  })
}

describe("ApiProxy", () => {
  let upstream: Server
  let proxy: ApiProxy
  let upstreamPort = 0

  beforeEach(async () => {
    upstream = createServer((request, response) => {
      if (request.url === "/api/login") {
        response.setHeader("set-cookie", [
          "session=secret; HttpOnly; SameSite=Lax",
          "workspace=alpha; Path=/",
        ])
        response.end("logged-in")
        return
      }
      if (request.url === "/api/whoami") {
        response.end(request.headers.cookie ?? "")
        return
      }

      const hash = createHash("sha256")
      let size = 0
      request.on("data", (chunk: Buffer) => {
        hash.update(chunk)
        size += chunk.length
      })
      request.on("end", () => {
        response.setHeader("content-type", "application/json")
        response.end(
          JSON.stringify({
            method: request.method,
            url: request.url,
            contentType: request.headers["content-type"],
            size,
            hash: hash.digest("hex"),
          }),
        )
      })
    })
    upstreamPort = await listen(upstream)
    proxy = new ApiProxy({
      target: new URL(`http://127.0.0.1:${upstreamPort}`),
      port: 0,
      dev: true,
      timeoutMs: 5_000,
    })
    await proxy.listen()
  })

  afterEach(async () => {
    await proxy.close()
    await close(upstream)
  })

  it("forwards method, query, headers, and a streamed request body", async () => {
    // Given
    const body = Buffer.alloc(2 * 1024 * 1024, 7)
    const expectedHash = createHash("sha256").update(body).digest("hex")

    // When
    const response = await fetch(`${proxy.url}/api/upload?sheet=1`, {
      method: "POST",
      headers: { "content-type": "multipart/form-data; boundary=test" },
      body,
    })

    // Then
    await expect(response.json()).resolves.toEqual({
      method: "POST",
      url: "/api/upload?sheet=1",
      contentType: "multipart/form-data; boundary=test",
      size: body.length,
      hash: expectedHash,
    })
  })

  it("captures upstream cookies and reuses them without exposing Set-Cookie", async () => {
    // Given
    const login = await fetch(`${proxy.url}/api/login`)

    // When
    const response = await fetch(`${proxy.url}/api/whoami`)

    // Then
    expect(login.headers.get("set-cookie")).toBeNull()
    await expect(response.text()).resolves.toBe("session=secret; workspace=alpha")
  })

  it("answers approved credentialed CORS preflights", async () => {
    // Given
    const origin = "http://127.0.0.1:5173"

    // When
    const response = await fetch(`${proxy.url}/api/data-sources`, {
      method: "OPTIONS",
      headers: {
        origin,
        "access-control-request-headers": "content-type,x-requested-with",
      },
    })

    // Then
    expect(response.status).toBe(204)
    expect(response.headers.get("access-control-allow-origin")).toBe(origin)
    expect(response.headers.get("access-control-allow-credentials")).toBe("true")
    expect(response.headers.get("access-control-allow-headers")).toBe(
      "content-type,x-requested-with",
    )
  })

  it("does not add permissive CORS headers for a rejected origin", async () => {
    // Given
    const origin = "https://attacker.example"

    // When
    const response = await fetch(`${proxy.url}/api/data-sources`, { headers: { origin } })

    // Then
    expect(response.headers.get("access-control-allow-origin")).toBeNull()
    expect(response.headers.get("access-control-allow-credentials")).toBeNull()
  })

  it("returns sanitized JSON when upstream is unavailable", async () => {
    // Given
    await close(upstream)
    upstream = createServer()

    // When
    const response = await fetch(`${proxy.url}/api/data-sources`)

    // Then
    expect(response.status).toBe(502)
    await expect(response.json()).resolves.toEqual({ error: "upstream unavailable" })
  })
})

describe("resolveApiTarget", () => {
  it("defaults to the local Rust development server", () => {
    // Given
    const configuredTarget = undefined

    // When
    const target = resolveApiTarget(configuredTarget)

    // Then
    expect(target.href).toBe("http://127.0.0.1:8080/")
  })
})
