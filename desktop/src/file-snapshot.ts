import { createHash } from "node:crypto"
import { constants } from "node:fs"
import { lstat, mkdir, mkdtemp, open, realpath, rmdir, stat, unlink } from "node:fs/promises"
import type { FileHandle } from "node:fs/promises"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import type { ScannedFile } from "./scanner.js"

export type StableFileSnapshot = {
  readonly path: string
  readonly name: string
  readonly size: number
  readonly sha256: string
}

export type SnapshotResult =
  | { readonly kind: "ready"; readonly snapshot: StableFileSnapshot }
  | { readonly kind: "waiting"; readonly reason: "recent" | "changed" | "busy" }

export type SnapshotOptions = {
  readonly userData: string
  readonly attemptId: string
  readonly nowMs?: () => number
  readonly minimumAgeMs?: number
  readonly afterCopy?: () => Promise<void>
}

const NO_FOLLOW = process.platform === "win32" ? 0 : constants.O_NOFOLLOW

export class SnapshotPathError extends Error {
  override readonly name = "SnapshotPathError"
}

function stagingRoot(userData: string): string {
  return resolve(userData, "automation-staging")
}

export function isStableSnapshotPath(userData: string, path: string): boolean {
  const root = stagingRoot(userData)
  const candidate = resolve(path)
  const suffix = relative(root, candidate)
  return suffix.length > 0 && suffix !== ".." && !suffix.startsWith(`..${sep}`) && !isAbsolute(suffix)
}

async function requireCanonicalStagingRoot(userData: string): Promise<string> {
  const userDataRoot = resolve(userData)
  const root = stagingRoot(userData)
  await mkdir(userDataRoot, { recursive: true })
  await mkdir(root, { recursive: true, mode: 0o700 })
  const rootMetadata = await lstat(root)
  if (!rootMetadata.isDirectory() || rootMetadata.isSymbolicLink()) {
    throw new SnapshotPathError("Automation staging root is not a real directory")
  }
  const [userDataRealPath, rootRealPath] = await Promise.all([
    realpath(userDataRoot),
    realpath(root),
  ])
  const suffix = relative(userDataRealPath, rootRealPath)
  if (!suffix || suffix === ".." || suffix.startsWith(`..${sep}`) || isAbsolute(suffix)) {
    throw new SnapshotPathError("Automation staging root resolved outside user data")
  }
  return root
}

export async function assertStableSnapshotFile(userData: string, path: string): Promise<void> {
  if (!isStableSnapshotPath(userData, path)) {
    throw new SnapshotPathError("Automation snapshot escaped the staging directory")
  }
  const metadata = await lstat(path)
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new SnapshotPathError("Automation snapshot is not a regular file")
  }
  const root = await requireCanonicalStagingRoot(userData)
  const [rootRealPath, fileRealPath] = await Promise.all([
    realpath(root),
    realpath(path),
  ])
  const suffix = relative(rootRealPath, fileRealPath)
  if (!suffix || suffix === ".." || suffix.startsWith(`..${sep}`) || isAbsolute(suffix)) {
    throw new SnapshotPathError("Automation snapshot resolved outside staging")
  }
}

export async function openStableSnapshotFile(
  userData: string,
  path: string,
): Promise<FileHandle> {
  const handle = await open(path, constants.O_RDONLY | NO_FOLLOW)
  try {
    await assertStableSnapshotFile(userData, path)
    const [opened, current] = await Promise.all([handle.stat(), lstat(path)])
    if (
      !current.isFile()
      || current.isSymbolicLink()
      || opened.dev !== current.dev
      || opened.ino !== current.ino
      || opened.size !== current.size
    ) {
      throw new SnapshotPathError("Automation snapshot changed while opening")
    }
    return handle
  } catch (error) {
    await handle.close()
    throw error
  }
}

async function removeAttemptRoot(userData: string, attemptRoot: string): Promise<void> {
  if (!isStableSnapshotPath(userData, attemptRoot)) {
    throw new SnapshotPathError("Refusing to remove an attempt outside staging")
  }
  const metadata = await lstat(attemptRoot)
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new SnapshotPathError("Automation attempt root is not a real directory")
  }
  const root = await requireCanonicalStagingRoot(userData)
  const [rootRealPath, attemptRealPath] = await Promise.all([
    realpath(root),
    realpath(attemptRoot),
  ])
  const suffix = relative(rootRealPath, attemptRealPath)
  if (!suffix || suffix === ".." || suffix.startsWith(`..${sep}`) || isAbsolute(suffix)) {
    throw new SnapshotPathError("Automation attempt root resolved outside staging")
  }
  await rmdir(attemptRoot)
}

async function cleanupAttempt(
  userData: string,
  attemptRoot: string,
  stagedPath: string,
): Promise<void> {
  if (!isStableSnapshotPath(userData, stagedPath)) {
    throw new SnapshotPathError("Refusing to clean a snapshot outside staging")
  }
  try {
    const metadata = await lstat(stagedPath)
    if (metadata.isFile() || metadata.isSymbolicLink()) {
      await unlink(stagedPath)
    } else {
      throw new SnapshotPathError("Unexpected entry in automation attempt directory")
    }
  } catch (error) {
    if (!(error instanceof Error) || !("code" in error) || error.code !== "ENOENT") {
      throw error
    }
  }
  await removeAttemptRoot(userData, attemptRoot)
}

function requireSafeAttemptId(value: string): void {
  if (!/^[A-Za-z0-9._-]{1,128}$/u.test(value)) {
    throw new SnapshotPathError("Invalid automation attempt id")
  }
}

function isTransientFileError(error: unknown): boolean {
  if (!(error instanceof Error) || !("code" in error)) return false
  return error.code === "EBUSY" || error.code === "EPERM" || error.code === "EACCES"
}

async function copyIntoExclusiveSnapshot(
  sourcePath: string,
  destinationPath: string,
  userData: string,
): Promise<{ readonly size: number; readonly sha256: string }> {
  const destination = await open(
    destinationPath,
    constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | NO_FOLLOW,
    0o600,
  )
  try {
    // Validate the canonical destination before writing any source bytes. If an attacker replaced an
    // ancestor with a symlink, O_EXCL cannot overwrite an existing target and this check rejects the
    // newly created empty file before content leaves the staging boundary.
    await assertStableSnapshotFile(userData, destinationPath)
    const source = await open(sourcePath, constants.O_RDONLY | NO_FOLLOW)
    const digest = createHash("sha256")
    const buffer = Buffer.allocUnsafe(1024 * 1024)
    let size = 0
    try {
      while (true) {
        const { bytesRead } = await source.read(buffer, 0, buffer.length, size)
        if (bytesRead === 0) break
        await destination.write(buffer, 0, bytesRead, size)
        digest.update(buffer.subarray(0, bytesRead))
        size += bytesRead
      }
    } finally {
      await source.close()
    }
    await destination.sync()
    return { size, sha256: digest.digest("hex") }
  } finally {
    await destination.close()
  }
}

export async function createStableFileSnapshot(
  file: ScannedFile,
  options: SnapshotOptions,
): Promise<SnapshotResult> {
  const nowMs = (options.nowMs ?? Date.now)()
  const minimumAgeMs = options.minimumAgeMs ?? 5_000
  if (nowMs - file.mtimeMs < minimumAgeMs) {
    return { kind: "waiting", reason: "recent" }
  }

  requireSafeAttemptId(options.attemptId)
  const root = await requireCanonicalStagingRoot(options.userData)
  const attemptRoot = await mkdtemp(join(root, `${options.attemptId}-`))
  const stagedPath = join(attemptRoot, file.name)
  if (!isStableSnapshotPath(options.userData, stagedPath)) {
    await removeAttemptRoot(options.userData, attemptRoot)
    throw new SnapshotPathError("Automation snapshot escaped the staging directory")
  }
  try {
    const before = await stat(file.path)
    if (!before.isFile()) {
      await removeAttemptRoot(options.userData, attemptRoot)
      return { kind: "waiting", reason: "changed" }
    }
    const copied = await copyIntoExclusiveSnapshot(file.path, stagedPath, options.userData)
    await options.afterCopy?.()
    const after = await stat(file.path)
    if (
      !after.isFile()
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
      || copied.size !== after.size
    ) {
      await cleanupAttempt(options.userData, attemptRoot, stagedPath)
      return { kind: "waiting", reason: "changed" }
    }
    await assertStableSnapshotFile(options.userData, stagedPath)
    return {
      kind: "ready",
      snapshot: {
        path: stagedPath,
        name: file.name,
        size: copied.size,
        sha256: copied.sha256,
      },
    }
  } catch (error) {
    await cleanupAttempt(options.userData, attemptRoot, stagedPath).catch(() => undefined)
    if (isTransientFileError(error)) {
      return { kind: "waiting", reason: "busy" }
    }
    throw error
  }
}

export async function removeStableFileSnapshot(
  userData: string,
  path: string | null,
): Promise<void> {
  if (path === null) return
  if (!isStableSnapshotPath(userData, path)) {
    throw new SnapshotPathError("Refusing to remove a path outside automation staging")
  }
  await assertStableSnapshotFile(userData, path)
  const attemptRoot = dirname(path)
  if (!isStableSnapshotPath(userData, attemptRoot)) {
    throw new SnapshotPathError("Refusing to remove an invalid snapshot directory")
  }
  await cleanupAttempt(userData, attemptRoot, path)
}
