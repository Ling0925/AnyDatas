import { describe, expect, it } from "vitest"
import { FileSourceScheduler } from "./scheduler.js"
import type {
  FileSourceReader,
  FileSourceRunner,
  SchedulerTimer,
  TimerHandle,
} from "./scheduler.js"
import type { DesktopFileSource } from "./types.js"

function source(
  id: string,
  enabled: boolean,
  overrides: Partial<DesktopFileSource> = {},
): DesktopFileSource {
  return {
    id,
    name: id,
    mode: "legacy_schedule",
    directory: "/tmp",
    pattern: "*.csv",
    targetSourceId: "target",
    savedQueryId: null,
    workspaceId: null,
    cron: "* * * * *",
    timezone: "UTC",
    enabled,
    triggerScheduleIds: [],
    createdAt: "2026-08-09T00:00:00.000Z",
    updatedAt: "2026-08-09T00:00:00.000Z",
    lastAppliedHash: null,
    activeAttempt: null,
    lastRun: null,
    runs: [],
    ...overrides,
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
  readonly outcomes: import("./types.js").CollectorRunOutcome[] = []

  async runNow(id: string): Promise<import("./types.js").CollectorRunResult> {
    this.ids.push(id)
    const updated = source(id, true)
    const outcome = this.outcomes.shift() ?? "success"
    return {
      source: updated,
      outcome,
      jobId: null,
      revisionId: null,
      message: outcome,
    }
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

  it("retries a settling daily file across the original cron minute", async () => {
    let now = new Date("2026-08-09T08:00:05.000Z")
    const runner = new FakeRunner()
    runner.outcomes.push("waiting", "success")
    const scheduled = source("daily", true, { cron: "0 8 * * *" })
    const scheduler = new FileSourceScheduler(new FakeReader([scheduled]), runner, {
      now: () => now,
      timer: new FakeTimer(),
      onError: () => undefined,
    })

    await scheduler.tick()
    now = new Date("2026-08-09T08:01:05.000Z")
    await scheduler.tick()
    now = new Date("2026-08-09T08:02:05.000Z")
    await scheduler.tick()

    expect(runner.ids).toEqual(["daily", "daily"])
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
