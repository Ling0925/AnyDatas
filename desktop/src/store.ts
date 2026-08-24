import { randomUUID } from "node:crypto"
import { mkdir, open, readFile, rename } from "node:fs/promises"
import { join } from "node:path"
import { removeStableFileSnapshot } from "./file-snapshot.js"
import {
  FileSourceDataError,
  FileSourceValidationError,
  parseFileSourceConfig,
  parseFileSources,
  parseAttempt,
  parseRunAppend,
  serializeFileSources,
} from "./store-schema.js"
import type { FileSourceRunAppend } from "./store-schema.js"
import type { DesktopFileSource, DesktopFileSourceAttempt } from "./types.js"

export { FileSourceDataError, FileSourceValidationError }
export type { FileSourceRunAppend }

type StoreDependencies = {
  readonly createId: () => string
  readonly now: () => Date
}

const DEFAULT_DEPENDENCIES: StoreDependencies = {
  createId: randomUUID,
  now: () => new Date(),
}

export class FileSourceNotFoundError extends Error {
  override readonly name = "FileSourceNotFoundError"

  constructor(readonly id: string) {
    super(`File source "${id}" was not found`)
  }
}

function isMissingFile(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "ENOENT"
}

export class FileSourceStore {
  readonly #filePath: string
  #mutationQueue: Promise<void> = Promise.resolve()

  constructor(
    private readonly userData: string,
    private readonly dependencies: StoreDependencies = DEFAULT_DEPENDENCIES,
  ) {
    this.#filePath = join(userData, "file-sources.json")
  }

  async #read(): Promise<DesktopFileSource[]> {
    let content: string
    try {
      content = await readFile(this.#filePath, "utf8")
    } catch (error) {
      if (isMissingFile(error)) {
        return []
      }
      throw error
    }

    let input: unknown
    try {
      input = JSON.parse(content)
    } catch (error) {
      if (error instanceof SyntaxError) {
        throw new FileSourceDataError(this.#filePath, error.message, { cause: error })
      }
      throw error
    }
    return parseFileSources(input, this.#filePath)
  }

  async #write(sources: readonly DesktopFileSource[]): Promise<void> {
    await mkdir(this.userData, { recursive: true })
    const temporaryPath = `${this.#filePath}.${process.pid}.${randomUUID()}.tmp`
    const handle = await open(temporaryPath, "wx", 0o600)
    try {
      await handle.writeFile(serializeFileSources(sources), "utf8")
      await handle.sync()
    } finally {
      await handle.close()
    }
    await rename(temporaryPath, this.#filePath)
  }

  async #replace(
    sources: readonly DesktopFileSource[],
    replacement: DesktopFileSource,
  ): Promise<DesktopFileSource> {
    await this.#write(sources.map((source) => (source.id === replacement.id ? replacement : source)))
    return replacement
  }

  #mutate<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.#mutationQueue.then(operation)
    this.#mutationQueue = result.then(
      () => undefined,
      () => undefined,
    )
    return result
  }

  async list(): Promise<DesktopFileSource[]> {
    return this.#read()
  }

  create(input: unknown): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const config = parseFileSourceConfig(input)
      const sources = await this.#read()
      const timestamp = this.dependencies.now().toISOString()
      const created: DesktopFileSource = {
        ...config,
        mode: config.mode ?? "legacy_schedule",
        savedQueryId: config.savedQueryId ?? null,
        workspaceId: config.workspaceId ?? null,
        triggerScheduleIds: [...(config.triggerScheduleIds ?? [])],
        id: this.dependencies.createId(),
        enabled: true,
        createdAt: timestamp,
        updatedAt: timestamp,
        lastAppliedHash: null,
        activeAttempt: null,
        lastRun: null,
        runs: [],
      }
      await this.#write([...sources, created])
      return created
    })
  }

  update(id: string, input: unknown): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const config = parseFileSourceConfig(input)
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      const nextMode = config.mode ?? source.mode
      const nextSavedQueryId = config.savedQueryId ?? null
      const nextWorkspaceId = config.workspaceId ?? null
      const invalidatesAttempt = source.directory !== config.directory
        || source.pattern !== config.pattern
        || source.targetSourceId !== config.targetSourceId
        || source.mode !== nextMode
        || source.savedQueryId !== nextSavedQueryId
        || source.workspaceId !== nextWorkspaceId
      if (invalidatesAttempt && source.activeAttempt?.stagedPath) {
        await removeStableFileSnapshot(this.userData, source.activeAttempt.stagedPath)
      }
      return this.#replace(sources, {
        ...source,
        ...config,
        mode: nextMode,
        savedQueryId: nextSavedQueryId,
        workspaceId: nextWorkspaceId,
        triggerScheduleIds: [...(config.triggerScheduleIds ?? [])],
        activeAttempt: invalidatesAttempt ? null : source.activeAttempt,
        createdAt: source.createdAt,
        updatedAt: this.dependencies.now().toISOString(),
      })
    })
  }

  delete(id: string): Promise<void> {
    return this.#mutate(async () => {
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      if (source.activeAttempt?.stagedPath) {
        await removeStableFileSnapshot(this.userData, source.activeAttempt.stagedPath)
      }
      await this.#write(sources.filter((candidate) => candidate.id !== id))
    })
  }

  toggle(id: string, enabled: boolean): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      return this.#replace(sources, {
        ...source,
        enabled,
        updatedAt: this.dependencies.now().toISOString(),
      })
    })
  }

  setActiveAttempt(
    id: string,
    input: DesktopFileSourceAttempt | null,
  ): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const attempt = parseAttempt(input)
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      return this.#replace(sources, {
        ...source,
        activeAttempt: attempt,
        updatedAt: this.dependencies.now().toISOString(),
      })
    })
  }

  appendRun(id: string, input: unknown): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const appended = parseRunAppend(input)
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      const successfulHash = appended.run.status === "success" || appended.run.status === "skipped"
        ? appended.fileHash
        : source.lastAppliedHash
      const updated: DesktopFileSource = {
        ...source,
        updatedAt: this.dependencies.now().toISOString(),
        lastAppliedHash: successfulHash,
        activeAttempt: null,
        runs: [...source.runs, appended.run].slice(-20),
        lastRun: {
          status: appended.run.status,
          at: appended.run.at,
          file: appended.run.file,
          fileHash: appended.fileHash,
          rowsImported: appended.run.rowsImported,
          error: appended.run.error,
          revisionId: appended.run.revisionId,
          jobId: appended.run.jobId,
          failureStage: appended.run.failureStage,
        },
      }
      return this.#replace(sources, updated)
    })
  }
}
