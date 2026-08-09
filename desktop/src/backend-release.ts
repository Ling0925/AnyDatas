import { createHash, randomUUID } from "node:crypto"
import { chmod, mkdir, open, readFile, rename, rm } from "node:fs/promises"
import { createReadStream } from "node:fs"
import { join } from "node:path"
import * as z from "zod"
import { DESKTOP_PROTOCOL_VERSION } from "./backend-types.js"

const MAX_MANIFEST_BYTES = 1024 * 1024
const MAX_SERVER_BINARY_BYTES = 1024 * 1024 * 1024
const MANIFEST_NAME = "anydatas-server-manifest.json"

// GitHub 的公开 API 会持续增加元数据字段；这里只提取安装链需要的字段，避免上游新增字段阻断启动。
const githubAssetSchema = z.object({
  name: z.string().min(1),
  browser_download_url: z.url(),
  size: z.number().int().nonnegative(),
  digest: z.string().nullable().optional(),
})
const githubReleaseSchema = z.object({
  tag_name: z.string().min(1),
  assets: z.array(githubAssetSchema),
})
const manifestAssetSchema = z.strictObject({
  name: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/u),
  size: z.number().int().positive(),
})
const releaseManifestSchema = z.strictObject({
  schemaVersion: z.literal(1),
  serverVersion: z.string().min(1),
  protocolVersion: z.number().int().positive(),
  tag: z.string().min(1),
  assets: z.record(z.string(), manifestAssetSchema),
})
const installedRuntimeSchema = z.strictObject({
  schemaVersion: z.literal(1),
  tag: z.string().min(1),
  serverVersion: z.string().min(1),
  protocolVersion: z.number().int().positive(),
  platformKey: z.string().min(1),
  binaryName: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/u),
  size: z.number().int().positive(),
})

type GithubAsset = z.infer<typeof githubAssetSchema>
type ReleaseManifest = z.infer<typeof releaseManifestSchema>
type InstalledRuntimeRecord = z.infer<typeof installedRuntimeSchema>

export type InstalledServerRuntime = {
  readonly binaryPath: string
  readonly serverVersion: string
  readonly protocolVersion: number
}

export type ServerReleaseInstallerOptions = {
  readonly userData: string
  readonly metadataUrl: URL
  readonly tag: string
  readonly githubToken?: string
  readonly platform?: NodeJS.Platform
  readonly arch?: NodeJS.Architecture
  readonly request?: typeof fetch
}

export class ServerReleaseError extends Error {
  override readonly name = "ServerReleaseError"

  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
  }
}

/**
 * 把 Electron 平台和架构映射为发行清单中的稳定键。
 *
 * 显式枚举可以在未发布的平台上立即失败，避免错误下载后直到执行阶段才出现格式错误。
 */
export function serverPlatformKey(
  platform: NodeJS.Platform = process.platform,
  arch: NodeJS.Architecture = process.arch,
): string {
  const key = `${platform}-${arch}`
  switch (key) {
    case "darwin-arm64":
      return "macos-arm64"
    case "darwin-x64":
      return "macos-x64"
    case "win32-x64":
      return "windows-x64"
    case "linux-x64":
      return "linux-x64"
    default:
      throw new ServerReleaseError(`当前平台尚无服务端发行物：${key}`)
  }
}

/**
 * 计算本地文件摘要，缓存命中时仍重新验证字节而不是只相信元数据文件。
 *
 * 这样可以发现磁盘损坏或本机篡改，并保证所有可执行文件都经过与首次下载相同的校验。
 */
async function fileSha256(path: string): Promise<string> {
  const hash = createHash("sha256")
  for await (const chunk of createReadStream(path)) {
    hash.update(chunk)
  }
  return hash.digest("hex")
}

/**
 * 为 GitHub 元数据和资产请求构造最小请求头，私有仓库令牌只保留在当前进程内存。
 *
 * 统一构造请求头可以避免重定向或重试路径意外把凭据写入配置文件和日志。
 */
function requestHeaders(token: string | undefined, accept: string): Record<string, string> {
  const headers: Record<string, string> = {
    accept,
    "user-agent": "AnyDatas-Desktop",
  }
  if (token !== undefined && token.length > 0) {
    headers["authorization"] = `Bearer ${token}`
  }
  return headers
}

/**
 * 下载有严格上限的小型 JSON，并在解析前返回真实字节摘要。
 *
 * 对清单单独限流可以阻止错误地址返回超大页面，同时允许用 GitHub 提供的摘要验证清单来源字节。
 */
async function downloadJsonBytes(
  request: typeof fetch,
  url: URL,
  headers: Record<string, string>,
): Promise<{ readonly bytes: Buffer; readonly sha256: string }> {
  const response = await request(url, { headers, redirect: "follow" })
  if (!response.ok) {
    throw new ServerReleaseError(`下载发行信息失败：HTTP ${response.status}`)
  }
  const bytes = Buffer.from(await response.arrayBuffer())
  if (bytes.byteLength > MAX_MANIFEST_BYTES) {
    throw new ServerReleaseError("服务端发行清单超过 1 MiB 安全上限")
  }
  return {
    bytes,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  }
}

/**
 * 从 GitHub 的 sha256 摘要字段提取十六进制值，拒绝缺失或未知算法。
 *
 * 强制要求 Release 元数据摘要可以让清单本身也进入校验链，而不是只校验清单描述的二进制。
 */
function githubSha256(asset: GithubAsset): string {
  const digest = asset.digest ?? ""
  const match = /^sha256:([a-f0-9]{64})$/u.exec(digest)
  if (match?.[1] === undefined) {
    throw new ServerReleaseError(`GitHub 资产 ${asset.name} 缺少 SHA-256 摘要`)
  }
  return match[1]
}

/**
 * 流式下载原生二进制到同目录临时文件，并在落盘过程中同步计算摘要和大小。
 *
 * 临时文件加原子改名保证应用崩溃或网络中断时不会留下看似完整、实际截断的可执行文件。
 */
async function downloadBinary(
  request: typeof fetch,
  url: URL,
  headers: Record<string, string>,
  temporaryPath: string,
  expectedSize: number,
  expectedSha256: string,
  onProgress: (progress: number | null) => void,
): Promise<void> {
  const response = await request(url, { headers, redirect: "follow" })
  if (!response.ok || response.body === null) {
    throw new ServerReleaseError(`下载服务端二进制失败：HTTP ${response.status}`)
  }
  if (expectedSize > MAX_SERVER_BINARY_BYTES) {
    throw new ServerReleaseError("服务端二进制超过 1 GiB 安全上限")
  }

  const handle = await open(temporaryPath, "wx", 0o600)
  const hash = createHash("sha256")
  let received = 0
  try {
    for await (const chunk of response.body) {
      const bytes = Buffer.from(chunk)
      received += bytes.byteLength
      if (received > expectedSize || received > MAX_SERVER_BINARY_BYTES) {
        throw new ServerReleaseError("服务端二进制大小与发行清单不一致")
      }
      hash.update(bytes)
      await handle.write(bytes)
      onProgress(expectedSize === 0 ? null : received / expectedSize)
    }
    await handle.sync()
  } finally {
    await handle.close()
  }
  if (received !== expectedSize || hash.digest("hex") !== expectedSha256) {
    throw new ServerReleaseError("服务端二进制 SHA-256 或大小校验失败")
  }
}

/**
 * 持久化已验证运行时记录，后续离线启动只需复算本地二进制摘要。
 *
 * 记录与二进制放在同一版本目录可以让多个版本并存，为升级失败回退保留完整证据。
 */
async function writeInstalledRecord(path: string, record: InstalledRuntimeRecord): Promise<void> {
  const temporaryPath = `${path}.${process.pid}.${randomUUID()}.tmp`
  const handle = await open(temporaryPath, "wx", 0o600)
  try {
    await handle.writeFile(`${JSON.stringify(record, null, 2)}\n`, "utf8")
    await handle.sync()
  } finally {
    await handle.close()
  }
  await rename(temporaryPath, path)
}

export class ServerReleaseInstaller {
  readonly #request: typeof fetch
  readonly #platformKey: string

  constructor(private readonly options: ServerReleaseInstallerOptions) {
    this.#request = options.request ?? fetch
    this.#platformKey = serverPlatformKey(options.platform, options.arch)
  }

  /**
   * 读取并验证版本化缓存；任何缺失、格式错误或摘要变化都按缓存未命中处理。
   *
   * 缓存问题不阻止在线修复的好处是用户无需手工清理目录，同时损坏文件绝不会被执行。
   */
  async #cached(directory: string): Promise<InstalledServerRuntime | null> {
    const recordPath = join(directory, "installed.json")
    try {
      const record = installedRuntimeSchema.parse(JSON.parse(await readFile(recordPath, "utf8")))
      if (record.tag !== this.options.tag || record.platformKey !== this.#platformKey) {
        return null
      }
      const binaryPath = join(directory, record.binaryName)
      if ((await fileSha256(binaryPath)) !== record.sha256) {
        return null
      }
      return {
        binaryPath,
        serverVersion: record.serverVersion,
        protocolVersion: record.protocolVersion,
      }
    } catch {
      return null
    }
  }

  /**
   * 安装桌面端锁定 Tag 对应的平台二进制，并在执行前完成元数据、清单和文件三级校验。
   *
   * 对外只有一个 install 接口，调用方无需了解 GitHub、缓存布局或原子替换细节。
   */
  async install(onProgress: (progress: number | null) => void): Promise<InstalledServerRuntime> {
    const directory = join(this.options.userData, "server-runtime", this.options.tag, this.#platformKey)
    await mkdir(directory, { recursive: true })
    const cached = await this.#cached(directory)
    if (cached !== null) {
      return cached
    }

    const metadataHeaders = requestHeaders(this.options.githubToken, "application/vnd.github+json")
    const releaseDownload = await downloadJsonBytes(
      this.#request,
      this.options.metadataUrl,
      metadataHeaders,
    )
    const release = githubReleaseSchema.parse(JSON.parse(releaseDownload.bytes.toString("utf8")))
    if (release.tag_name !== this.options.tag) {
      throw new ServerReleaseError(`GitHub Release Tag 不匹配：${release.tag_name}`)
    }
    const manifestAsset = release.assets.find((asset) => asset.name === MANIFEST_NAME)
    if (manifestAsset === undefined) {
      throw new ServerReleaseError(`GitHub Release 缺少 ${MANIFEST_NAME}`)
    }

    const manifestDownload = await downloadJsonBytes(
      this.#request,
      new URL(manifestAsset.browser_download_url),
      requestHeaders(this.options.githubToken, "application/octet-stream"),
    )
    if (manifestDownload.sha256 !== githubSha256(manifestAsset)) {
      throw new ServerReleaseError("服务端发行清单 SHA-256 校验失败")
    }
    const manifest = releaseManifestSchema.parse(JSON.parse(manifestDownload.bytes.toString("utf8")))
    this.#validateManifest(manifest)
    const expected = manifest.assets[this.#platformKey]
    if (expected === undefined) {
      throw new ServerReleaseError(`发行清单缺少平台 ${this.#platformKey}`)
    }
    const githubBinary = release.assets.find((asset) => asset.name === expected.name)
    if (githubBinary === undefined) {
      throw new ServerReleaseError(`GitHub Release 缺少 ${expected.name}`)
    }
    if (githubBinary.size !== expected.size || githubSha256(githubBinary) !== expected.sha256) {
      throw new ServerReleaseError("GitHub 资产元数据与发行清单不一致")
    }

    const binaryPath = join(directory, expected.name)
    const temporaryPath = `${binaryPath}.${process.pid}.${randomUUID()}.download`
    try {
      await downloadBinary(
        this.#request,
        new URL(githubBinary.browser_download_url),
        requestHeaders(this.options.githubToken, "application/octet-stream"),
        temporaryPath,
        expected.size,
        expected.sha256,
        onProgress,
      )
      await chmod(temporaryPath, 0o700)
      await rm(binaryPath, { force: true })
      await rename(temporaryPath, binaryPath)
    } catch (error) {
      await rm(temporaryPath, { force: true })
      throw error
    }

    const record: InstalledRuntimeRecord = {
      schemaVersion: 1,
      tag: manifest.tag,
      serverVersion: manifest.serverVersion,
      protocolVersion: manifest.protocolVersion,
      platformKey: this.#platformKey,
      binaryName: expected.name,
      sha256: expected.sha256,
      size: expected.size,
    }
    await writeInstalledRecord(join(directory, "installed.json"), record)
    return {
      binaryPath,
      serverVersion: manifest.serverVersion,
      protocolVersion: manifest.protocolVersion,
    }
  }

  /**
   * 校验清单与桌面端锁定版本及协议完全一致。
   *
   * 精确匹配而非追随 latest 可以保证桌面前端升级前不会被新的服务端接口提前破坏。
   */
  #validateManifest(manifest: ReleaseManifest): void {
    if (manifest.tag !== this.options.tag) {
      throw new ServerReleaseError(`发行清单 Tag 不匹配：${manifest.tag}`)
    }
    if (manifest.protocolVersion !== DESKTOP_PROTOCOL_VERSION) {
      throw new ServerReleaseError(
        `服务端协议 ${manifest.protocolVersion} 与桌面端协议 ${DESKTOP_PROTOCOL_VERSION} 不兼容`,
      )
    }
  }
}
