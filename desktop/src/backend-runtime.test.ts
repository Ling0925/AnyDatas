import { mkdtemp, readFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { BackendRuntimeManager } from "./backend-runtime.js"
import type {
  BackendAdapter,
  BackendConnection,
  BackendProgress,
  BackendSelection,
} from "./backend-types.js"

/**
 * 创建只记录目标变化的代理替身，测试只通过运行时公开 interface 观察切换结果。
 *
 * 不启动真实 HTTP 代理可以让管理器测试聚焦配置持久化、Cookie 隔离时序和错误恢复。
 */
function fakeProxy(): {
  readonly targets: Array<{ readonly url: string | null; readonly token: string | null }>
  readonly setTarget: (target: URL | undefined, token?: string) => void
} {
  const targets: Array<{ readonly url: string | null; readonly token: string | null }> = []
  return {
    targets,
    setTarget: (target, token) => targets.push({ url: target?.href ?? null, token: token ?? null }),
  }
}

/**
 * 构造可控 Adapter，连接结果与进度完全由用例提供。
 *
 * Adapter 替身证明运行时 seam 足够小，单机下载和远端 HTTP 无需泄露进管理器测试。
 */
function adapter(
  connection: BackendConnection | Error,
  capture?: (progress: (event: BackendProgress) => void) => void,
): BackendAdapter {
  return {
    connect: async (progress) => {
      capture?.(progress)
      progress({ phase: "starting", message: "connecting", progress: null })
      if (connection instanceof Error) throw connection
      return connection
    },
  }
}

/**
 * 构造标准握手连接，stop 计数用于验证切换和退出确实释放旧 Adapter。
 *
 * 连接形状同时适用于单机和远端，使测试覆盖管理器对两种模式的一致处理。
 */
function connection(url: string, stopped: { value: number }, token?: string): BackendConnection {
  return {
    baseUrl: new URL(url),
    ...(token === undefined ? {} : { desktopToken: token }),
    handshake: {
      service: "anydatas-server",
      serverVersion: "0.1.0",
      protocolVersion: 1,
      capabilities: ["agent"],
    },
    stop: async () => {
      stopped.value += 1
    },
  }
}

describe("BackendRuntimeManager", () => {
  const roots: string[] = []

  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
  })

  it("persists a successful remote selection and restores it on startup", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-manager-"))
    roots.push(root)
    const proxy = fakeProxy()
    const stopped = { value: 0 }
    const factory = (_selection: BackendSelection): BackendAdapter =>
      adapter(connection("http://127.0.0.1:18080/", stopped))
    const runtime = new BackendRuntimeManager({ userData: root, proxy, adapterFor: factory })

    const ready = await runtime.configure({ mode: "remote", serverUrl: "127.0.0.1:18080" })

    expect(ready).toMatchObject({ mode: "remote", phase: "ready" })
    expect(proxy.targets.at(-1)).toEqual({ url: "http://127.0.0.1:18080/", token: null })
    const config = JSON.parse(await readFile(join(root, "backend-runtime.json"), "utf8"))
    expect(config.selection).toEqual({ mode: "remote", serverUrl: "http://127.0.0.1:18080/" })

    const restoredProxy = fakeProxy()
    const restored = new BackendRuntimeManager({
      userData: root,
      proxy: restoredProxy,
      adapterFor: factory,
    })
    await expect(restored.initialize()).resolves.toMatchObject({ phase: "ready" })
  })

  it("keeps a failed selection out of persistent configuration", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-manager-"))
    roots.push(root)
    const proxy = fakeProxy()
    const runtime = new BackendRuntimeManager({
      userData: root,
      proxy,
      adapterFor: () => adapter(new Error("handshake failed")),
    })

    await expect(runtime.configure({ mode: "remote", serverUrl: "server.invalid" })).rejects.toThrow(
      "handshake failed",
    )
    expect(runtime.status()).toMatchObject({ phase: "failed", message: "handshake failed" })
    await expect(readFile(join(root, "backend-runtime.json"), "utf8")).rejects.toMatchObject({
      code: "ENOENT",
    })
  })

  it("clears the proxy and stops the old connection when switching modes", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-manager-"))
    roots.push(root)
    const proxy = fakeProxy()
    const stopped = { value: 0 }
    const runtime = new BackendRuntimeManager({
      userData: root,
      proxy,
      adapterFor: (selection) => adapter(connection(
        selection.mode === "standalone" ? "http://127.0.0.1:19001/" : selection.serverUrl,
        stopped,
        selection.mode === "standalone" ? "desktop-token" : undefined,
      )),
    })
    await runtime.configure({ mode: "standalone" })

    await runtime.configure({ mode: "remote", serverUrl: "https://example.com" })

    expect(stopped.value).toBe(1)
    expect(proxy.targets).toContainEqual({ url: null, token: null })
    expect(proxy.targets.at(-1)).toEqual({ url: "https://example.com/", token: null })
  })

  it("turns an unexpected standalone exit into a failed public status", async () => {
    const root = await mkdtemp(join(tmpdir(), "anydatas-manager-"))
    roots.push(root)
    const proxy = fakeProxy()
    const stopped = { value: 0 }
    let report: ((event: BackendProgress) => void) | undefined
    const runtime = new BackendRuntimeManager({
      userData: root,
      proxy,
      adapterFor: () => adapter(connection("http://127.0.0.1:19001/", stopped), (value) => {
        report = value
      }),
    })
    await runtime.configure({ mode: "standalone" })

    report?.({ phase: "failed", message: "本地服务端意外退出：1", progress: null })

    expect(runtime.status()).toMatchObject({ phase: "failed" })
    expect(proxy.targets.at(-1)).toEqual({ url: null, token: null })
  })
})
