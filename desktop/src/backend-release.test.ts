import { createHash } from "node:crypto"
import { createServer } from "node:http"
import type { Server } from "node:http"
import { mkdtemp, readFile, rm, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { ServerReleaseError, ServerReleaseInstaller, serverPlatformKey } from "./backend-release.js"

const TAG = "server-v0.1.0"
const ASSET_NAME = "anydatas-server-linux-x64"

/**
 * 在回环随机端口启动测试 Release 服务，覆盖 GitHub 元数据、清单和二进制三个请求。
 *
 * 真实 HTTP 流可以验证重定向无关的流式写入与摘要行为，同时不依赖外部 GitHub 状态。
 */
async function releaseServer(binary: Buffer, servedBinary: Buffer = binary): Promise<{
  readonly server: Server
  readonly metadataUrl: URL
  readonly requests: () => number
}> {
  const binarySha256 = createHash("sha256").update(binary).digest("hex")
  const manifest = Buffer.from(`${JSON.stringify({
    schemaVersion: 1,
    serverVersion: "0.1.0",
    protocolVersion: 1,
    tag: TAG,
    assets: {
      "linux-x64": { name: ASSET_NAME, sha256: binarySha256, size: binary.byteLength },
    },
  })}\n`)
  const manifestSha256 = createHash("sha256").update(manifest).digest("hex")
  let requestCount = 0
  const server = createServer((request, response) => {
    requestCount += 1
    const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`
    if (request.url === "/release") {
      response.setHeader("content-type", "application/json")
      response.end(JSON.stringify({
        tag_name: TAG,
        assets: [
          {
            name: "anydatas-server-manifest.json",
            browser_download_url: `${origin}/manifest`,
            size: manifest.byteLength,
            digest: `sha256:${manifestSha256}`,
          },
          {
            name: ASSET_NAME,
            browser_download_url: `${origin}/binary`,
            size: binary.byteLength,
            digest: `sha256:${binarySha256}`,
          },
        ],
      }))
      return
    }
    if (request.url === "/manifest") {
      response.end(manifest)
      return
    }
    if (request.url === "/binary") {
      response.end(servedBinary)
      return
    }
    response.writeHead(404).end()
  })
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (address === null || typeof address === "string") throw new Error("missing test address")
  return {
    server,
    metadataUrl: new URL(`http://127.0.0.1:${address.port}/release`),
    requests: () => requestCount,
  }
}

/**
 * 关闭测试 HTTP 服务并等待监听句柄释放。
 *
 * 每个用例独占随机端口可以防止并行测试相互污染下载计数或响应内容。
 */
async function close(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => error === undefined ? resolve() : reject(error))
  })
}

describe("serverPlatformKey", () => {
  it("maps every published desktop target", () => {
    expect(serverPlatformKey("darwin", "arm64")).toBe("macos-arm64")
    expect(serverPlatformKey("darwin", "x64")).toBe("macos-x64")
    expect(serverPlatformKey("win32", "x64")).toBe("windows-x64")
    expect(serverPlatformKey("linux", "x64")).toBe("linux-x64")
  })

  it("rejects an unpublished architecture", () => {
    expect(() => serverPlatformKey("linux", "arm64")).toThrow(ServerReleaseError)
  })
})

describe("ServerReleaseInstaller", () => {
  const roots: string[] = []
  const servers: Server[] = []

  afterEach(async () => {
    await Promise.all(servers.splice(0).map(close))
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
  })

  it("downloads, verifies, installs, and reuses the runtime offline", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-runtime-"))
    roots.push(root)
    const binary = Buffer.from("verified-linux-server")
    const release = await releaseServer(binary)
    servers.push(release.server)
    const installer = new ServerReleaseInstaller({
      userData: root,
      metadataUrl: release.metadataUrl,
      tag: TAG,
      platform: "linux",
      arch: "x64",
    })
    const progress: Array<number | null> = []

    const installed = await installer.install((value) => progress.push(value))

    expect(await readFile(installed.binaryPath)).toEqual(binary)
    expect((await stat(installed.binaryPath)).mode & 0o777).toBe(0o700)
    expect(installed).toMatchObject({ serverVersion: "0.1.0", protocolVersion: 1 })
    expect(progress.at(-1)).toBe(1)
    expect(release.requests()).toBe(3)

    await close(release.server)
    servers.splice(servers.indexOf(release.server), 1)
    const cached = await installer.install(() => undefined)
    expect(cached).toEqual(installed)
    expect(release.requests()).toBe(3)
  })

  it("does not install bytes that fail the manifest digest", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-runtime-"))
    roots.push(root)
    const release = await releaseServer(
      Buffer.from("expected-server"),
      Buffer.from("tampered-server"),
    )
    servers.push(release.server)
    const installer = new ServerReleaseInstaller({
      userData: root,
      metadataUrl: release.metadataUrl,
      tag: TAG,
      platform: "linux",
      arch: "x64",
    })

    await expect(installer.install(() => undefined)).rejects.toThrow(/校验失败|大小/u)
  })
})
