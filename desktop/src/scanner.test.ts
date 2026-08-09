import { createHash } from "node:crypto"
import { mkdir, mkdtemp, rm, utimes, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { ScannerAccessError, scanNewestFile } from "./scanner.js"

describe("scanNewestFile", () => {
  let directory = ""

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "anydatas-scanner-"))
  })

  afterEach(async () => {
    await rm(directory, { recursive: true, force: true })
  })

  it("matches star and question-mark globs on direct regular files only", async () => {
    // Given
    const directPath = join(directory, "daily-01.csv")
    await writeFile(directPath, "direct")
    await mkdir(join(directory, "nested"))
    await writeFile(join(directory, "nested", "daily-02.csv"), "nested")
    await mkdir(join(directory, "daily-03.csv"))

    // When
    const result = await scanNewestFile(directory, "daily-??.csv")

    // Then
    expect(result).toMatchObject({ kind: "found", file: { name: "daily-01.csv" } })
  })

  it("chooses newest mtime and uses ascending filename as a deterministic tie-break", async () => {
    // Given
    const olderPath = join(directory, "report-z.csv")
    const tiedPath = join(directory, "report-b.csv")
    const selectedPath = join(directory, "report-a.csv")
    await Promise.all([
      writeFile(olderPath, "older"),
      writeFile(tiedPath, "newer-b"),
      writeFile(selectedPath, "newer-a"),
    ])
    await utimes(olderPath, new Date(1_000), new Date(1_000))
    await Promise.all([
      utimes(tiedPath, new Date(2_000), new Date(2_000)),
      utimes(selectedPath, new Date(2_000), new Date(2_000)),
    ])

    // When
    const result = await scanNewestFile(directory, "report-*.csv")

    // Then
    expect(result).toMatchObject({ kind: "found", file: { name: "report-a.csv" } })
  })

  it("computes the selected file SHA-256", async () => {
    // Given
    const content = "large-file-content".repeat(65_536)
    await writeFile(join(directory, "export.bin"), content)
    const expectedHash = createHash("sha256").update(content).digest("hex")

    // When
    const result = await scanNewestFile(directory, "*.bin")

    // Then
    expect(result).toMatchObject({ kind: "found", file: { sha256: expectedHash } })
  })

  it("returns a typed no-match outcome", async () => {
    // Given
    await writeFile(join(directory, "notes.txt"), "notes")

    // When
    const result = await scanNewestFile(directory, "*.csv")

    // Then
    expect(result).toEqual({ kind: "no_match" })
  })

  it("returns a typed unreadable outcome when the directory cannot be read", async () => {
    // Given
    const missingDirectory = join(directory, "missing")

    // When
    const result = await scanNewestFile(missingDirectory, "*.csv")

    // Then
    expect(result).toEqual({ kind: "unreadable", error: expect.any(ScannerAccessError) })
  })
})
