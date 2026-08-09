import { describe, expect, it } from "vitest"
import { FileSourceScheduler } from "./scheduler.js"
import type {
  FileSourceReader,
  FileSourceRunner,
  SchedulerTimer,
  TimerHandle,
} from "./scheduler.js"
import type { DesktopFileSource } from "./types.js"

function source(id: string, enabled: boolean): DesktopFileSource {
  return {
    id,
    name: id,
    directory: "/tmp",
    pattern: "*.csv",
    targetSourceId: "target",
    cron: "* * * * *",
    timezone: "UTC",
    enabled,
    triggerScheduleIds: [],
    createdAt: "2026-08-09T00:00:00.000Z",
    updatedAt: "2026-08-09T00:00:00.000Z",
    lastRun: null,
    runs: [],
  }
}

class FakeReader implements FileSourceReader {
  constructor(readonly sources: DesktopFileSource[]) {}

  async list(): Promise<DesktopFileSource[]> {
    return this.sources
  }
}

class FakeRunner implements FileSourceRunner {
  readonly ids: string[] = []

  async runNow(id: string): Promise<DesktopFileSource> {
    this.ids.push(id)
    return source(id, true)
  }
}

class FakeTimer implements SchedulerTimer {
  intervalMs: number | undefined
  callback: (() => void) | undefined
  cleared: TimerHandle | undefined
  readonly handle = {}

  set(callback: () => void, intervalMs: number): TimerHandle {
    this.callback = callback
    this.intervalMs = intervalMs
    return this.handle
  }

  clear(handle: TimerHandle): void {
    this.cleared = handle
  }
}

describe("FileSourceScheduler", () => {
  it("never runs disabled sources", async () => {
    // Given
    const runner = new FakeRunner()
    const scheduler = new FileSourceScheduler(
      new FakeReader([source("disabled", false)]),
      runner,
      {
        now: () => new Date("2026-08-09T08:00:10.000Z"),
        timer: new FakeTimer(),
        onError: () => undefined,
      },
    )

    // When
    await scheduler.tick()

    // Then
    expect(runner.ids).toEqual([])
  })

  it("runs a matching source at most once per UTC minute", async () => {
    // Given
    let now = new Date("2026-08-09T08:00:05.000Z")
    const runner = new FakeRunner()
    const scheduler = new FileSourceScheduler(new FakeReader([source("enabled", true)]), runner, {
      now: () => now,
      timer: new FakeTimer(),
      onError: () => undefined,
    })

    // When
    await scheduler.tick()
    now = new Date("2026-08-09T08:00:45.000Z")
    await scheduler.tick()
    now = new Date("2026-08-09T08:01:00.000Z")
    await scheduler.tick()

    // Then
    expect(runner.ids).toEqual(["enabled", "enabled"])
  })

  it("starts a thirty-second interval and clears it on stop", () => {
    // Given
    const timer = new FakeTimer()
    const scheduler = new FileSourceScheduler(new FakeReader([]), new FakeRunner(), {
      now: () => new Date(0),
      timer,
      onError: () => undefined,
    })

    // When
    scheduler.start()
    scheduler.stop()

    // Then
    expect(timer.intervalMs).toBe(30_000)
    expect(timer.cleared).toBe(timer.handle)
  })
})
