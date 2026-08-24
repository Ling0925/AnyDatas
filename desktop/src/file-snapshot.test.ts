import { createHash } from "node:crypto"
import { mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import {
  SnapshotPathError,
  createStableFileSnapshot,
  removeStableFileSnapshot,
} from "./file-snapshot.js"
import type { ScannedFile } from "./scanner.js"

function scanned(path: string, name: string, size: number, mtimeMs: number): ScannedFile {
  return { path, name, size, mtimeMs, sha256: "ignored-live-hash" }
}

describe("createStableFileSnapshot", () => {
  let root = ""

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "anydatas-file-snapshot-"))
  })

  afterEach(async () => {
    await rm(root, { recursive: true, force: true })
  })

  it("copies, fsyncs, and hashes the exact staged bytes", async () => {
    const path = join(root, "daily.csv")
    const bytes = Buffer.from("id,value\n1,10\n")
    await writeFile(path, bytes)
    const metadata = await stat(path)

    const result = await createStableFileSnapshot(
      scanned(path, "daily.csv", metadata.size, metadata.mtimeMs),
      { userData: root, attemptId: "attempt-1", nowMs: () => metadata.mtimeMs + 10_000 },
    )

    expect(result.kind).toBe("ready")
    if (result.kind !== "ready") return
    expect(await readFile(result.snapshot.path)).toEqual(bytes)
    expect(result.snapshot.sha256).toBe(createHash("sha256").update(bytes).digest("hex"))
  })

  it("defers files newer than the settle window without creating a snapshot", async () => {
    const path = join(root, "daily.csv")
    await writeFile(path, "writing")
    const metadata = await stat(path)

    const result = await createStableFileSnapshot(
      scanned(path, "daily.csv", metadata.size, metadata.mtimeMs),
      { userData: root, attemptId: "attempt-2", nowMs: () => metadata.mtimeMs + 1_000 },
    )

    expect(result).toEqual({ kind: "waiting", reason: "recent" })
  })

  it("discards the staged copy when the producer changes the source during copy", async () => {
    const path = join(root, "daily.csv")
    await writeFile(path, "first")
    const metadata = await stat(path)

    const result = await createStableFileSnapshot(
      scanned(path, "daily.csv", metadata.size, metadata.mtimeMs),
      {
        userData: root,
        attemptId: "attempt-3",
        nowMs: () => metadata.mtimeMs + 10_000,
        afterCopy: async () => writeFile(path, "changed-after-copy"),
      },
    )

    expect(result).toEqual({ kind: "waiting", reason: "changed" })
  })

  it("rejects a staging-root symlink before creating destination bytes", async () => {
    const source = join(root, "daily.csv")
    const outside = join(root, "outside")
    await writeFile(source, "source")
    await mkdir(outside)
    await symlink(outside, join(root, "automation-staging"))
    const metadata = await stat(source)

    await expect(createStableFileSnapshot(
      scanned(source, "daily.csv", metadata.size, metadata.mtimeMs),
      { userData: root, attemptId: "attempt-root", nowMs: () => metadata.mtimeMs + 10_000 },
    )).rejects.toBeInstanceOf(SnapshotPathError)
    await expect(readFile(join(outside, "daily.csv"))).rejects.toMatchObject({ code: "ENOENT" })
  })

  it("rejects a persisted snapshot symlink that resolves outside staging", async () => {
    const outside = join(root, "outside.csv")
    await writeFile(outside, "do-not-overwrite")
    const attemptRoot = join(root, "automation-staging", "attempt-existing")
    await mkdir(attemptRoot, { recursive: true })
    const linked = join(attemptRoot, "daily.csv")
    await symlink(outside, linked)

    await expect(removeStableFileSnapshot(root, linked)).rejects.toBeInstanceOf(SnapshotPathError)
    await expect(readFile(outside, "utf8")).resolves.toBe("do-not-overwrite")
  })

  it("rejects tampered attempt ids and refuses cleanup outside staging", async () => {
    const path = join(root, "daily.csv")
    await writeFile(path, "safe")
    const metadata = await stat(path)

    await expect(createStableFileSnapshot(
      scanned(path, "daily.csv", metadata.size, metadata.mtimeMs),
      { userData: root, attemptId: "../escape", nowMs: () => metadata.mtimeMs + 10_000 },
    )).rejects.toBeInstanceOf(SnapshotPathError)
    await expect(removeStableFileSnapshot(root, path)).rejects.toBeInstanceOf(SnapshotPathError)
    await expect(readFile(path, "utf8")).resolves.toBe("safe")
  })
})
