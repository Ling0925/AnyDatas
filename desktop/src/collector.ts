import { scanNewestFile } from "./scanner.js"
import type { ScanResult, ScannedFile } from "./scanner.js"
import { FileSourceNotFoundError, FileSourceStore } from "./store.js"
import type {
  DesktopFileSource,
  DesktopFileSourceLastRun,
  DesktopFileSourceRun,
} from "./types.js"

export type CollectorApi = {
  readonly replaceSource: (
    sourceId: string,
    filePath: string,
  ) => Promise<{ readonly rowCount: number }>
  readonly runSchedule: (scheduleId: string) => Promise<void>
}

export type FileSourceEvent = {
  readonly id: string
  readonly lastRun: DesktopFileSourceLastRun | null
  readonly runs: DesktopFileSourceRun[]
}

type CollectorOptions = {
  readonly now?: () => Date
  readonly emit: (event: FileSourceEvent) => void
  readonly scan?: typeof scanNewestFile
}

export class ScheduleTriggerError extends Error {
  override readonly name = "ScheduleTriggerError"

  constructor(readonly failures: readonly string[]) {
    super(`下游任务触发失败：${failures.join("；")}`)
  }
}

export class CollectorStateError extends Error {
  override readonly name = "CollectorStateError"
}

function assertNever(value: never): never {
  throw new CollectorStateError(`Unknown scan result: ${String(value)}`)
}

export class Collector {
  readonly #active = new Map<string, Promise<DesktopFileSource>>()

  constructor(
    private readonly store: FileSourceStore,
    private readonly api: CollectorApi,
    private readonly options: CollectorOptions,
  ) {}

  async #persist(
    id: string,
    run: DesktopFileSourceRun,
    fileHash: string | null,
  ): Promise<DesktopFileSource> {
    const updated = await this.store.appendRun(id, { run, fileHash })
    this.options.emit({ id, lastRun: updated.lastRun, runs: updated.runs })
    return updated
  }

  async #triggerSchedules(scheduleIds: readonly string[]): Promise<void> {
    const failures: string[] = []
    for (const scheduleId of scheduleIds) {
      try {
        await this.api.runSchedule(scheduleId)
      } catch (error) {
        if (error instanceof Error) {
          failures.push(`${scheduleId}: ${error.message}`)
        } else {
          throw error
        }
      }
    }
    if (failures.length > 0) {
      throw new ScheduleTriggerError(failures)
    }
  }

  async #collectFile(source: DesktopFileSource, file: ScannedFile): Promise<DesktopFileSource> {
    const at = (this.options.now ?? (() => new Date()))().toISOString()
    const previous = source.lastRun
    const previouslyCompleted = previous?.status === "success" || previous?.status === "skipped"
    if (previouslyCompleted && previous.fileHash === file.sha256) {
      return this.#persist(
        source.id,
        {
          at,
          status: "skipped",
          file: file.name,
          error: null,
          rowsImported: null,
        },
        file.sha256,
      )
    }

    const replacementCompleted =
      previous?.status === "failed" &&
      previous.fileHash === file.sha256 &&
      previous.rowsImported !== null
    if (replacementCompleted) {
      try {
        await this.#triggerSchedules(source.triggerScheduleIds)
        return this.#persist(
          source.id,
          {
            at,
            status: "success",
            file: file.name,
            error: null,
            rowsImported: previous.rowsImported,
          },
          file.sha256,
        )
      } catch (error) {
        if (!(error instanceof Error)) {
          throw error
        }
        return this.#persist(
          source.id,
          {
            at,
            status: "failed",
            file: file.name,
            error: `采集或下游任务失败：${error.message}`,
            rowsImported: previous.rowsImported,
          },
          file.sha256,
        )
      }
    }

    let rowsImported: number | null = null
    try {
      const replacement = await this.api.replaceSource(source.targetSourceId, file.path)
      rowsImported = replacement.rowCount
      await this.#triggerSchedules(source.triggerScheduleIds)
      return this.#persist(
        source.id,
        { at, status: "success", file: file.name, error: null, rowsImported },
        file.sha256,
      )
    } catch (error) {
      if (!(error instanceof Error)) {
        throw error
      }
      return this.#persist(
        source.id,
        {
          at,
          status: "failed",
          file: file.name,
          error: `采集或下游任务失败：${error.message}`,
          rowsImported,
        },
        file.sha256,
      )
    }
  }

  async #scanResult(source: DesktopFileSource, result: ScanResult): Promise<DesktopFileSource> {
    const at = (this.options.now ?? (() => new Date()))().toISOString()
    switch (result.kind) {
      case "found":
        return this.#collectFile(source, result.file)
      case "no_match":
        return this.#persist(
          source.id,
          {
            at,
            status: "failed",
            file: null,
            error: "未找到匹配文件",
            rowsImported: null,
          },
          null,
        )
      case "unreadable":
        return this.#persist(
          source.id,
          {
            at,
            status: "failed",
            file: null,
            error: "无法读取目录或文件",
            rowsImported: null,
          },
          null,
        )
      default:
        return assertNever(result)
    }
  }

  async #execute(id: string): Promise<DesktopFileSource> {
    const sources = await this.store.list()
    const source = sources.find((candidate) => candidate.id === id)
    if (source === undefined) {
      throw new FileSourceNotFoundError(id)
    }
    const scan = this.options.scan ?? scanNewestFile
    return this.#scanResult(source, await scan(source.directory, source.pattern))
  }

  runNow(id: string): Promise<DesktopFileSource> {
    const active = this.#active.get(id)
    if (active !== undefined) {
      return active
    }
    const run = this.#execute(id).finally(() => {
      this.#active.delete(id)
    })
    this.#active.set(id, run)
    return run
  }
}
