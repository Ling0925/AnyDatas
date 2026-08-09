import { createHash } from "node:crypto"
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { Collector } from "./collector.js"
import type { CollectorApi, FileSourceEvent } from "./collector.js"
import { FileSourceStore } from "./store.js"
import type { DesktopFileSource, DesktopFileSourceConfig } from "./types.js"

class TestApiError extends Error {
  override readonly name = "TestApiError"
}

class FakeApi implements CollectorApi {
  readonly calls: string[] = []
  readonly failingSchedules = new Set<string>()
  replaceGate: Promise<void> = Promise.resolve()
  replaceFailuresRemaining = 0

  async replaceSource(sourceId: string, filePath: string): Promise<{ readonly rowCount: number }> {
    this.calls.push(`replace:${sourceId}:${filePath}`)
    await this.replaceGate
    if (this.replaceFailuresRemaining > 0) {
      this.replaceFailuresRemaining -= 1
      throw new TestApiError("replacement failed")
    }
    return { rowCount: 12 }
  }

  async runSchedule(scheduleId: string): Promise<void> {
    this.calls.push(`schedule:${scheduleId}`)
    if (this.failingSchedules.has(scheduleId)) {
      throw new TestApiError(`schedule ${scheduleId} failed`)
    }
  }
}

function config(directory: string, overrides: Partial<DesktopFileSourceConfig> = {}): DesktopFileSourceConfig {
  return {
    name: "Daily export",
    directory,
    pattern: "daily-*.csv",
    targetSourceId: "target-1",
    cron: "* * * * *",
    timezone: "UTC",
    triggerScheduleIds: ["schedule-a", "schedule-b"],
    ...overrides,
  }
}

describe("Collector", () => {
  let root = ""
  let files = ""
  let store: FileSourceStore
  let api: FakeApi
  let collector: Collector
  let events: FileSourceEvent[]

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "anydatas-collector-"))
    files = join(root, "files")
    await mkdir(files)
    store = new FileSourceStore(root, {
      createId: () => "file-source-1",
      now: () => new Date("2026-08-09T08:00:00.000Z"),
    })
    api = new FakeApi()
    events = []
    collector = new Collector(store, api, {
      now: () => new Date("2026-08-09T08:00:00.000Z"),
      emit: (event) => events.push(event),
    })
  })

  afterEach(async () => {
    await rm(root, { recursive: true, force: true })
  })

  async function createSource(overrides: Partial<DesktopFileSourceConfig> = {}): Promise<DesktopFileSource> {
    return store.create(config(files, overrides))
  }

  it("replaces the newest file, triggers schedules sequentially, persists, and emits", async () => {
    // Given
    const source = await createSource()
    const filePath = join(files, "daily-1.csv")
    await writeFile(filePath, "one,two\n1,2")

    // When
    const updated = await collector.runNow(source.id)

    // Then
    expect(api.calls).toEqual([
      `replace:target-1:${filePath}`,
      "schedule:schedule-a",
      "schedule:schedule-b",
    ])
    expect(updated.lastRun).toMatchObject({ status: "success", rowsImported: 12 })
    expect(events).toEqual([{ id: source.id, lastRun: updated.lastRun, runs: updated.runs }])
  })

  it("skips replacement when the latest successful hash is unchanged", async () => {
    // Given
    const source = await createSource()
    const content = "unchanged"
    await writeFile(join(files, "daily-1.csv"), content)
    const fileHash = createHash("sha256").update(content).digest("hex")
    await store.appendRun(source.id, {
      fileHash,
      run: {
        at: "2026-08-09T07:00:00.000Z",
        status: "success",
        file: "daily-1.csv",
        error: null,
        rowsImported: 10,
      },
    })

    // When
    const updated = await collector.runNow(source.id)

    // Then
    expect(api.calls).toEqual([])
    expect(updated.lastRun).toMatchObject({ status: "skipped", fileHash })
  })

  it("persists a Chinese failed outcome when no file matches", async () => {
    // Given
    const source = await createSource()

    // When
    const updated = await collector.runNow(source.id)

    // Then
    expect(updated.lastRun).toMatchObject({ status: "failed", error: "未找到匹配文件" })
  })

  it("persists a Chinese failed outcome when the directory is unreadable", async () => {
    // Given
    const source = await createSource({ directory: join(root, "missing") })

    // When
    const updated = await collector.runNow(source.id)

    // Then
    expect(updated.lastRun).toMatchObject({ status: "failed", error: "无法读取目录或文件" })
  })

  it("attempts every schedule sequentially and reports any trigger failure", async () => {
    // Given
    const source = await createSource()
    await writeFile(join(files, "daily-1.csv"), "changed")
    api.failingSchedules.add("schedule-a")

    // When
    const updated = await collector.runNow(source.id)

    // Then
    expect(api.calls.map((call) => call.split(":").slice(0, 2).join(":"))).toEqual([
      "replace:target-1",
      "schedule:schedule-a",
      "schedule:schedule-b",
    ])
    expect(updated.lastRun).toMatchObject({ status: "failed", rowsImported: 12 })
  })

  it("retries schedules without replacing again after a partial success", async () => {
    // Given
    const source = await createSource()
    await writeFile(join(files, "daily-1.csv"), "changed")
    api.failingSchedules.add("schedule-a")
    const failed = await collector.runNow(source.id)
    api.failingSchedules.clear()

    // When
    const retried = await collector.runNow(source.id)

    // Then
    expect(failed.lastRun).toMatchObject({ status: "failed", rowsImported: 12 })
    expect(api.calls.filter((call) => call.startsWith("replace:"))).toHaveLength(1)
    expect(api.calls.filter((call) => call === "schedule:schedule-a")).toHaveLength(2)
    expect(api.calls.filter((call) => call === "schedule:schedule-b")).toHaveLength(2)
    expect(retried.lastRun).toMatchObject({ status: "success", rowsImported: 12 })
  })

  it("retries replacement when the prior replacement failed", async () => {
    // Given
    const source = await createSource({ triggerScheduleIds: [] })
    await writeFile(join(files, "daily-1.csv"), "changed")
    api.replaceFailuresRemaining = 1
    const failed = await collector.runNow(source.id)

    // When
    const retried = await collector.runNow(source.id)

    // Then
    expect(failed.lastRun).toMatchObject({ status: "failed", rowsImported: null })
    expect(api.calls.filter((call) => call.startsWith("replace:"))).toHaveLength(2)
    expect(retried.lastRun).toMatchObject({ status: "success", rowsImported: 12 })
  })

  it("coalesces concurrent runs for the same file source", async () => {
    // Given
    const source = await createSource({ triggerScheduleIds: [] })
    await writeFile(join(files, "daily-1.csv"), "changed")
    let release = (): void => undefined
    api.replaceGate = new Promise<void>((resolve) => {
      release = resolve
    })

    // When
    const first = collector.runNow(source.id)
    const second = collector.runNow(source.id)
    release()
    await Promise.all([first, second])

    // Then
    expect(api.calls.filter((call) => call.startsWith("replace:"))).toHaveLength(1)
    await expect(store.list()).resolves.toMatchObject([{ runs: [{ status: "success" }] }])
  })
})
