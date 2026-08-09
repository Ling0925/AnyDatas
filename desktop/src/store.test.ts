import { mkdtemp, readdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import {
  FileSourceDataError,
  FileSourceStore,
  FileSourceValidationError,
} from "./store.js"
import type { DesktopFileSourceConfig, DesktopFileSourceRun } from "./types.js"

const CREATED_AT = "2026-08-09T00:00:00.000Z"
const UPDATED_AT = "2026-08-09T01:00:00.000Z"

function validConfig(
  overrides: Partial<DesktopFileSourceConfig> = {},
): DesktopFileSourceConfig {
  return {
    name: "Daily export",
    directory: "/tmp/exports",
    pattern: "daily-*.csv",
    targetSourceId: "source-1",
    cron: "0 8 * * *",
    timezone: "Asia/Shanghai",
    triggerScheduleIds: ["schedule-1"],
    ...overrides,
  }
}

describe("FileSourceStore", () => {
  let userData = ""
  let now = CREATED_AT
  let store: FileSourceStore

  beforeEach(async () => {
    userData = await mkdtemp(join(tmpdir(), "anydatas-store-"))
    store = new FileSourceStore(userData, {
      createId: () => "file-source-1",
      now: () => new Date(now),
    })
  })

  afterEach(async () => {
    await rm(userData, { recursive: true, force: true })
  })

  it("creates and lists a source through the atomic JSON file", async () => {
    // Given
    const config = validConfig()

    // When
    const created = await store.create(config)

    // Then
    await expect(store.list()).resolves.toEqual([created])
    await expect(readdir(userData)).resolves.toEqual(["file-sources.json"])
  })

  it("updates config while preserving createdAt and changing updatedAt", async () => {
    // Given
    const created = await store.create(validConfig())
    now = UPDATED_AT

    // When
    const updated = await store.update(created.id, validConfig({ name: "Renamed" }))

    // Then
    expect(updated).toMatchObject({
      name: "Renamed",
      createdAt: CREATED_AT,
      updatedAt: UPDATED_AT,
    })
  })

  it("deletes an existing source", async () => {
    // Given
    const created = await store.create(validConfig())

    // When
    await store.delete(created.id)

    // Then
    await expect(store.list()).resolves.toEqual([])
  })

  it("toggles enabled state and updates updatedAt", async () => {
    // Given
    const created = await store.create(validConfig())
    now = UPDATED_AT

    // When
    const toggled = await store.toggle(created.id, false)

    // Then
    expect(toggled).toMatchObject({ enabled: false, updatedAt: UPDATED_AT })
  })

  it("caps runs at twenty and derives lastRun from the appended run", async () => {
    // Given
    const created = await store.create(validConfig())
    let latest = created

    // When
    for (let index = 0; index < 25; index += 1) {
      const run: DesktopFileSourceRun = {
        at: new Date(Date.UTC(2026, 7, 9, 0, index)).toISOString(),
        status: "success",
        file: `daily-${index}.csv`,
        error: null,
        rowsImported: index,
      }
      latest = await store.appendRun(created.id, { run, fileHash: `hash-${index}` })
    }

    // Then
    expect(latest.runs).toHaveLength(20)
    expect(latest.runs.at(0)?.file).toBe("daily-5.csv")
    expect(latest.lastRun).toEqual({
      status: "success",
      at: "2026-08-09T00:24:00.000Z",
      file: "daily-24.csv",
      fileHash: "hash-24",
      rowsImported: 24,
      error: null,
    })
  })

  it("rejects malformed JSON with a typed data error", async () => {
    // Given
    await writeFile(join(userData, "file-sources.json"), "{broken")

    // When
    const list = store.list()

    // Then
    await expect(list).rejects.toBeInstanceOf(FileSourceDataError)
  })

  it("rejects malformed persisted entries with a typed data error", async () => {
    // Given
    await writeFile(join(userData, "file-sources.json"), JSON.stringify([{ id: "broken" }]))

    // When
    const list = store.list()

    // Then
    await expect(list).rejects.toBeInstanceOf(FileSourceDataError)
  })

  it.each([
    validConfig({ name: " " }),
    validConfig({ directory: "" }),
    validConfig({ pattern: "" }),
    validConfig({ targetSourceId: "" }),
    validConfig({ cron: "99 * * * *" }),
    validConfig({ timezone: "Mars/Olympus" }),
    { ...validConfig(), triggerScheduleIds: "schedule-1" },
  ])("rejects invalid source config %#", async (config: unknown) => {
    // Given
    const invalidConfig = config

    // When
    const create = store.create(invalidConfig)

    // Then
    await expect(create).rejects.toBeInstanceOf(FileSourceValidationError)
  })

  it("preserves every concurrent create mutation", async () => {
    // Given
    let nextId = 0
    const concurrentStore = new FileSourceStore(userData, {
      createId: () => `file-source-${nextId++}`,
      now: () => new Date(CREATED_AT),
    })
    const configs = Array.from({ length: 8 }, (_, index) =>
      validConfig({ name: `Source ${index}` }),
    )

    // When
    await Promise.all(configs.map((sourceConfig) => concurrentStore.create(sourceConfig)))

    // Then
    await expect(concurrentStore.list()).resolves.toHaveLength(8)
  })

  it("preserves concurrent appends and a toggle mutation", async () => {
    // Given
    const created = await store.create(validConfig())
    const appends = Array.from({ length: 10 }, (_, index) =>
      store.appendRun(created.id, {
        fileHash: `hash-${index}`,
        run: {
          at: new Date(Date.UTC(2026, 7, 9, 0, index)).toISOString(),
          status: "success",
          file: `daily-${index}.csv`,
          error: null,
          rowsImported: index,
        },
      }),
    )

    // When
    await Promise.all([...appends, store.toggle(created.id, false)])

    // Then
    const [updated] = await store.list()
    expect(updated?.enabled).toBe(false)
    expect(updated?.runs).toHaveLength(10)
  })
})
