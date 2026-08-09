import { cronMatches } from "./cron.js"
import type { DesktopFileSource } from "./types.js"

export type TimerHandle = object

export type SchedulerTimer = {
  readonly set: (callback: () => void, intervalMs: number) => TimerHandle
  readonly clear: (handle: TimerHandle) => void
}

export type FileSourceReader = {
  readonly list: () => Promise<DesktopFileSource[]>
}

export type FileSourceRunner = {
  readonly runNow: (id: string) => Promise<DesktopFileSource>
}

type SchedulerOptions = {
  readonly now: () => Date
  readonly timer: SchedulerTimer
  readonly onError: (error: unknown) => void
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
    if (nativeHandle === undefined) {
      return
    }
    clearInterval(nativeHandle)
    this.#nativeHandles.delete(handle)
  }
}

export class FileSourceScheduler {
  readonly #lastUtcMinute = new Map<string, number>()
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
      if (!currentIds.has(id)) {
        this.#lastUtcMinute.delete(id)
      }
    }

    for (const source of sources) {
      if (
        !source.enabled ||
        this.#lastUtcMinute.get(source.id) === utcMinute ||
        !cronMatches(source.cron, now, source.timezone)
      ) {
        continue
      }
      this.#lastUtcMinute.set(source.id, utcMinute)
      try {
        await this.runner.runNow(source.id)
      } catch (error) {
        this.options.onError(error)
      }
    }
  }

  start(): void {
    if (this.#timerHandle !== undefined) {
      return
    }
    this.#timerHandle = this.options.timer.set(() => {
      void this.tick().catch(this.options.onError)
    }, 30_000)
  }

  stop(): void {
    if (this.#timerHandle === undefined) {
      return
    }
    this.options.timer.clear(this.#timerHandle)
    this.#timerHandle = undefined
  }
}
