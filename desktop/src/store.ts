import { randomUUID } from "node:crypto"
import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { join } from "node:path"
import {
  FileSourceDataError,
  FileSourceValidationError,
  parseFileSourceConfig,
  parseFileSources,
  parseRunAppend,
} from "./store-schema.js"
import type { FileSourceRunAppend } from "./store-schema.js"
import type { DesktopFileSource } from "./types.js"

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
    await writeFile(temporaryPath, `${JSON.stringify(sources, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    })
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
        triggerScheduleIds: [...config.triggerScheduleIds],
        id: this.dependencies.createId(),
        enabled: true,
        createdAt: timestamp,
        updatedAt: timestamp,
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
      return this.#replace(sources, {
        ...source,
        ...config,
        triggerScheduleIds: [...config.triggerScheduleIds],
        createdAt: source.createdAt,
        updatedAt: this.dependencies.now().toISOString(),
      })
    })
  }

  delete(id: string): Promise<void> {
    return this.#mutate(async () => {
      const sources = await this.#read()
      if (!sources.some((source) => source.id === id)) {
        throw new FileSourceNotFoundError(id)
      }
      await this.#write(sources.filter((source) => source.id !== id))
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

  appendRun(id: string, input: unknown): Promise<DesktopFileSource> {
    return this.#mutate(async () => {
      const appended = parseRunAppend(input)
      const sources = await this.#read()
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) {
        throw new FileSourceNotFoundError(id)
      }
      const updated: DesktopFileSource = {
        ...source,
        updatedAt: this.dependencies.now().toISOString(),
        runs: [...source.runs, appended.run].slice(-20),
        lastRun: {
          status: appended.run.status,
          at: appended.run.at,
          file: appended.run.file,
          fileHash: appended.fileHash,
          rowsImported: appended.run.rowsImported,
          error: appended.run.error,
        },
      }
      return this.#replace(sources, updated)
    })
  }
}
