import { cronMatches } from "./cron.js"
import type { CollectorRunResult, DesktopFileSource, FileSourceRunOrigin } from "./types.js"

export type TimerHandle = object

export type SchedulerTimer = {
  readonly set: (callback: () => void, intervalMs: number) => TimerHandle
  readonly clear: (handle: TimerHandle) => void
}

export type FileSourceReader = {
  readonly list: () => Promise<DesktopFileSource[]>
}

export type FileSourceRunner = {
  readonly runNow: (
    id: string,
    origin?: FileSourceRunOrigin,
  ) => Promise<CollectorRunResult>
}

type SchedulerOptions = {
  readonly now: () => Date
  readonly timer: SchedulerTimer
  readonly onError: (error: unknown) => void
  readonly catchUpWindowMinutes?: number
}

export class NativeSchedulerTimer implements SchedulerTimer {
  readonly #nativeHandles = new Map<TimerHandle, ReturnType<typeof setInterval>>()

  set(callback: () => void, intervalMs: number): TimerHandle {
    const handle = {}
    this.#nativeHandles.set(handle, setInterval(callback, intervalMs))
    return handle
  }

  clear(handle: TimerHandle): void {
    const nativeHandle = this.#nativeHandles.get(handle)
    if (nativeHandle === undefined) return
    clearInterval(nativeHandle)
    this.#nativeHandles.delete(handle)
  }
}

function hasMissedOccurrence(
  source: DesktopFileSource,
  previous: Date | null,
  now: Date,
  catchUpWindowMinutes: number,
): boolean {
  if (previous === null || now.getTime() <= previous.getTime() + 60_000) return false
  const earliest = Math.max(
    previous.getTime(),
    now.getTime() - catchUpWindowMinutes * 60_000,
  )
  let minute = Math.floor(earliest / 60_000) * 60_000 + 60_000
  const finalMinute = Math.floor(now.getTime() / 60_000) * 60_000
  while (minute <= finalMinute) {
    if (cronMatches(source.cron, new Date(minute), source.timezone)) return true
    minute += 60_000
  }
  return false
}

export class FileSourceScheduler {
  readonly #lastUtcMinute = new Map<string, number>()
  readonly #pendingRetries = new Set<string>()
  #lastTickAt: Date | null = null
  #timerHandle: TimerHandle | undefined

  constructor(
    private readonly reader: FileSourceReader,
    private readonly runner: FileSourceRunner,
    private readonly options: SchedulerOptions,
  ) {}

  async tick(): Promise<void> {
    const now = this.options.now()
    const utcMinute = Math.floor(now.getTime() / 60_000)
    const sources = await this.reader.list()
    const currentIds = new Set(sources.map((source) => source.id))
    for (const id of this.#lastUtcMinute.keys()) {
      if (!currentIds.has(id)) this.#lastUtcMinute.delete(id)
    }
    for (const id of this.#pendingRetries) {
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined || !source.enabled) this.#pendingRetries.delete(id)
    }

    const previousTick = this.#lastTickAt
    this.#lastTickAt = now
    const catchUpWindow = this.options.catchUpWindowMinutes ?? 24 * 60
    const due = sources.filter((source) => {
      if (!source.enabled) return false
      if (this.#pendingRetries.has(source.id)) return true
      if (this.#lastUtcMinute.get(source.id) === utcMinute) return false
      return cronMatches(source.cron, now, source.timezone)
        || hasMissedOccurrence(source, previousTick, now, catchUpWindow)
    })

    await Promise.all(due.map(async (source) => {
      if (!this.#pendingRetries.has(source.id)) {
        this.#lastUtcMinute.set(source.id, utcMinute)
      }
      try {
        const result = await this.runner.runNow(source.id, "scheduled")
        if (result.outcome === "waiting") {
          this.#pendingRetries.add(source.id)
        } else {
          this.#pendingRetries.delete(source.id)
        }
      } catch (error) {
        this.#pendingRetries.delete(source.id)
        this.options.onError(error)
      }
    }))
  }

  start(): void {
    if (this.#timerHandle !== undefined) return
    this.#timerHandle = this.options.timer.set(() => {
      void this.tick().catch(this.options.onError)
    }, 30_000)
  }

  stop(): void {
    if (this.#timerHandle === undefined) return
    this.options.timer.clear(this.#timerHandle)
    this.#timerHandle = undefined
  }
}
