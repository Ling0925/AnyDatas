import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import {
  FILE_SOURCE_CHANNELS,
  emitFileSourceEvent,
  registerFileSourceIpc,
} from "./ipc.js"
import type { IpcHandler, IpcRegistrar } from "./ipc.js"
import { FileSourceStore } from "./store.js"
import type { DesktopFileSource, DesktopFileSourceConfig } from "./types.js"

class TestIpcError extends Error {
  override readonly name = "TestIpcError"
}

class FakeRegistrar implements IpcRegistrar {
  readonly handlers = new Map<string, IpcHandler>()

  handle(channel: string, handler: IpcHandler): void {
    this.handlers.set(channel, handler)
  }

  removeHandler(channel: string): void {
    this.handlers.delete(channel)
  }

  async invoke(channel: string, ...args: readonly unknown[]): Promise<unknown> {
    const handler = this.handlers.get(channel)
    if (handler === undefined) {
      throw new TestIpcError(`missing handler ${channel}`)
    }
    return handler(...args)
  }
}

function config(): DesktopFileSourceConfig {
  return {
    name: "Daily",
    directory: "/tmp/export",
    pattern: "*.csv",
    targetSourceId: "target",
    cron: "* * * * *",
    timezone: "UTC",
    triggerScheduleIds: [],
  }
}

describe("registerFileSourceIpc", () => {
  let root = ""
  let now = "2026-08-09T08:00:00.000Z"
  let store: FileSourceStore
  let registrar: FakeRegistrar
  let source: DesktopFileSource
  let dialogProperties: readonly string[] = []

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "anydatas-ipc-"))
    now = "2026-08-09T08:00:00.000Z"
    store = new FileSourceStore(root, {
      createId: () => "file-source-1",
      now: () => new Date(now),
    })
    source = await store.create(config())
    await store.appendRun(source.id, {
      fileHash: "hash-1",
      run: {
        at: now,
        status: "success",
        file: "daily.csv",
        error: null,
        rowsImported: 3,
      },
    })
    registrar = new FakeRegistrar()
    registerFileSourceIpc({
      registrar,
      store,
      runner: { runNow: async () => source },
      dialog: {
        showOpenDialog: async (options) => {
          dialogProperties = options.properties
          return { canceled: false, filePaths: ["/tmp/chosen"] }
        },
      },
      apiTarget: () => "http://127.0.0.1:8080/",
    })
  })

  afterEach(async () => {
    await rm(root, { recursive: true, force: true })
  })

  it("registers every fixed renderer method channel", () => {
    // Given
    const expected = Object.values(FILE_SOURCE_CHANNELS).filter(
      (channel) => channel !== FILE_SOURCE_CHANNELS.event,
    )

    // When
    const registered = [...registrar.handlers.keys()]

    // Then
    expect(registered).toEqual(expected)
  })

  it("merges allowed partial updates while preserving history and immutable fields", async () => {
    // Given
    now = "2026-08-09T09:00:00.000Z"

    // When
    const updated = await registrar.invoke(FILE_SOURCE_CHANNELS.update, source.id, {
      name: "Renamed",
      enabled: false,
    })

    // Then
    expect(updated).toMatchObject({
      id: source.id,
      name: "Renamed",
      enabled: false,
      createdAt: source.createdAt,
      runs: [{ status: "success" }],
    })
  })

  it.each(["id", "lastRun", "runs"])("rejects updates to immutable field %s", async (field) => {
    // Given
    const update = { [field]: field === "id" ? "other" : [] }

    // When
    const invocation = registrar.invoke(FILE_SOURCE_CHANNELS.update, source.id, update)

    // Then
    await expect(invocation).rejects.toBeDefined()
  })

  it("opens the native directory picker with only the directory property", async () => {
    // Given
    const channel = FILE_SOURCE_CHANNELS.pickDirectory

    // When
    const selected = await registrar.invoke(channel)

    // Then
    expect(selected).toBe("/tmp/chosen")
    expect(dialogProperties).toEqual(["openDirectory"])
  })

  it("returns the configured API target", async () => {
    // Given
    const channel = FILE_SOURCE_CHANNELS.apiTarget

    // When
    const target = await registrar.invoke(channel)

    // Then
    expect(target).toBe("http://127.0.0.1:8080/")
  })
})

describe("emitFileSourceEvent", () => {
  it("sends payloads only on the fixed event channel", () => {
    // Given
    const calls: unknown[][] = []
    const event = { id: "source", lastRun: null, runs: [] }

    // When
    emitFileSourceEvent({ send: (...args) => calls.push(args) }, event)

    // Then
    expect(calls).toEqual([[FILE_SOURCE_CHANNELS.event, event]])
  })
})
