import { randomUUID } from "node:crypto"
import { stat } from "node:fs/promises"
import type { FileHandle } from "node:fs/promises"
import { ApiRequestError } from "./api-client.js"
import {
  assertStableSnapshotFile,
  createStableFileSnapshot,
  isStableSnapshotPath,
  openStableSnapshotFile,
  removeStableFileSnapshot,
} from "./file-snapshot.js"
import type { SnapshotResult } from "./file-snapshot.js"
import { scanNewestFile } from "./scanner.js"
import type { ScanResult, ScannedFile } from "./scanner.js"
import { FileSourceNotFoundError, FileSourceStore } from "./store.js"
import type {
  CollectorRunResult,
  DesktopFileSource,
  DesktopFileSourceAttempt,
  DesktopFileSourceLastRun,
  DesktopFileSourceRun,
  FileSourceActivity,
  FileSourceRunOrigin,
} from "./types.js"

export type CollectorApi = {
  readonly replaceSource: (
    sourceId: string,
    file: string | FileHandle,
    originalFilename?: string,
  ) => Promise<{ readonly rowCount: number }>
  readonly refreshSource: (
    sourceId: string,
    savedQueryId: string,
    file: string | FileHandle,
    originalFilename: string,
    idempotencyKey: string,
  ) => Promise<{
    readonly refreshId: string
    readonly revisionId: string
    readonly contentSha256: string
    readonly unchanged: boolean
    readonly jobId: string | null
  }>
  readonly getIdentity: () => Promise<{ readonly workspaceId: string }>
  readonly runSchedule: (scheduleId: string) => Promise<{ readonly id: string }>
}

export type FileSourceEvent = {
  readonly id: string
  readonly activity: FileSourceActivity
  readonly lastRun: DesktopFileSourceLastRun | null
  readonly runs: DesktopFileSourceRun[]
}

type CollectorOptions = {
  readonly userData: string
  readonly now?: () => Date
  readonly createId?: () => string
  readonly emit: (event: FileSourceEvent) => void
  readonly scan?: typeof scanNewestFile
  readonly snapshot?: typeof createStableFileSnapshot
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

function terminalResult(
  source: DesktopFileSource,
  outcome: CollectorRunResult["outcome"],
  message: string,
): CollectorRunResult {
  return {
    source,
    outcome,
    jobId: source.lastRun?.jobId ?? null,
    revisionId: source.lastRun?.revisionId ?? null,
    message,
  }
}

export class Collector {
  readonly #active = new Map<string, Promise<CollectorRunResult>>()
  readonly #targetQueues = new Map<string, Promise<void>>()

  constructor(
    private readonly store: FileSourceStore,
    private readonly api: CollectorApi,
    private readonly options: CollectorOptions,
  ) {}

  #emit(source: DesktopFileSource, activity: FileSourceActivity): void {
    this.options.emit({
      id: source.id,
      activity,
      lastRun: source.lastRun,
      runs: source.runs,
    })
  }

  async #persist(
    id: string,
    run: DesktopFileSourceRun,
    fileHash: string | null,
    message: string,
  ): Promise<DesktopFileSource> {
    const updated = await this.store.appendRun(id, { run, fileHash })
    this.#emit(updated, { phase: "idle", message, file: run.file })
    return updated
  }

  async #setAttempt(
    source: DesktopFileSource,
    attempt: DesktopFileSourceAttempt | null,
    activity: FileSourceActivity,
  ): Promise<DesktopFileSource> {
    const updated = await this.store.setActiveAttempt(source.id, attempt)
    this.#emit(updated, activity)
    return updated
  }

  async #triggerSchedules(scheduleIds: readonly string[]): Promise<string[]> {
    const failures: string[] = []
    const jobIds: string[] = []
    for (const scheduleId of scheduleIds) {
      try {
        jobIds.push((await this.api.runSchedule(scheduleId)).id)
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
    return jobIds
  }

  async #snapshot(
    source: DesktopFileSource,
    file: ScannedFile,
    attempt: DesktopFileSourceAttempt,
  ): Promise<{ source: DesktopFileSource; result: SnapshotResult }> {
    const copying = await this.#setAttempt(
      source,
      { ...attempt, phase: "copying", file: file.name },
      { phase: "scanning", message: "正在创建稳定文件快照", file: file.name },
    )
    const snapshot = this.options.snapshot ?? createStableFileSnapshot
    const result = await snapshot(file, {
      userData: this.options.userData,
      attemptId: attempt.id,
      nowMs: () => (this.options.now ?? (() => new Date()))().getTime(),
    })
    if (result.kind === "waiting") {
      const waiting = await this.#setAttempt(
        copying,
        { ...attempt, phase: "waiting", file: file.name },
        { phase: "waiting", message: "文件仍在写入，等待稳定后重试", file: file.name },
      )
      return { source: waiting, result }
    }
    const ready = await this.#setAttempt(
      copying,
      {
        ...attempt,
        phase: "uploading",
        file: file.name,
        stagedPath: result.snapshot.path,
        contentSha256: result.snapshot.sha256,
      },
      { phase: "uploading", message: "稳定快照已就绪，正在上传", file: file.name },
    )
    return { source: ready, result }
  }

  async #runSavedQueryRecipe(
    source: DesktopFileSource,
    file: ScannedFile,
  ): Promise<CollectorRunResult> {
    if (source.savedQueryId === null || source.workspaceId === null) {
      const updated = await this.#persist(
        source.id,
        {
          at: (this.options.now ?? (() => new Date()))().toISOString(),
          status: "failed",
          file: file.name,
          error: "自动化配置缺少保存查询或工作区，请重新编辑",
          rowsImported: null,
          contentSha256: null,
          revisionId: null,
          jobId: null,
          failureStage: "refresh",
        },
        source.lastAppliedHash,
        "自动化配置需要修复",
      )
      return terminalResult(updated, "failed", "自动化配置需要修复")
    }

    const savedQueryId = source.savedQueryId
    const workspaceId = source.workspaceId
    const attempt: DesktopFileSourceAttempt = source.activeAttempt ?? {
      id: (this.options.createId ?? randomUUID)(),
      stagedPath: null,
      contentSha256: null,
      file: file.name,
      phase: "copying",
      createdAt: (this.options.now ?? (() => new Date()))().toISOString(),
    }
    let current = source
    let stagedPath = attempt.stagedPath
    let contentSha256 = attempt.contentSha256
    if (stagedPath === null || contentSha256 === null) {
      const snapshotted = await this.#snapshot(current, file, attempt)
      current = snapshotted.source
      if (snapshotted.result.kind === "waiting") {
        return {
          source: current,
          outcome: "waiting",
          jobId: null,
          revisionId: null,
          message: "文件仍在写入，等待稳定后重试",
        }
      }
      stagedPath = snapshotted.result.snapshot.path
      contentSha256 = snapshotted.result.snapshot.sha256
    }

    if (current.lastAppliedHash === contentSha256) {
      await removeStableFileSnapshot(this.options.userData, stagedPath)
      const updated = await this.#persist(
        current.id,
        {
          at: (this.options.now ?? (() => new Date()))().toISOString(),
          status: "skipped",
          file: file.name,
          error: null,
          rowsImported: null,
          contentSha256,
          revisionId: current.lastRun?.revisionId ?? null,
          jobId: null,
          failureStage: null,
        },
        contentSha256,
        "文件内容未变化，已跳过",
      )
      return terminalResult(updated, "skipped", "文件内容未变化，已跳过")
    }

    try {
      const identity = await this.api.getIdentity()
      if (identity.workspaceId !== workspaceId) {
        const paused = await this.#setAttempt(
          current,
          { ...attempt, stagedPath, contentSha256, phase: "needs_attention" },
          { phase: "needs_attention", message: "当前工作区与自动化绑定不一致", file: file.name },
        )
        return {
          source: paused,
          outcome: "failed",
          jobId: null,
          revisionId: null,
          message: "当前工作区与自动化绑定不一致",
        }
      }
      current = await this.#setAttempt(
        current,
        { ...attempt, stagedPath, contentSha256, phase: "publishing" },
        { phase: "publishing", message: "正在发布可信数据版本并创建任务", file: file.name },
      )
      const uploadHandle = await openStableSnapshotFile(this.options.userData, stagedPath)
      const receipt = await this.api.refreshSource(
        current.targetSourceId,
        savedQueryId,
        uploadHandle,
        file.name,
        attempt.id,
      )
      if (receipt.contentSha256 !== contentSha256) {
        const attention = await this.#setAttempt(
          current,
          { ...attempt, stagedPath, contentSha256, phase: "needs_attention" },
          { phase: "needs_attention", message: "服务器回执摘要与本地稳定快照不一致", file: file.name },
        )
        return {
          source: attention,
          outcome: "failed",
          jobId: receipt.jobId,
          revisionId: receipt.revisionId,
          message: "服务器回执摘要与本地稳定快照不一致",
        }
      }
      await removeStableFileSnapshot(this.options.userData, stagedPath)
      const updated = await this.#persist(
        current.id,
        {
          at: (this.options.now ?? (() => new Date()))().toISOString(),
          status: receipt.unchanged ? "skipped" : "success",
          file: file.name,
          error: null,
          rowsImported: null,
          contentSha256: receipt.contentSha256,
          revisionId: receipt.revisionId,
          jobId: receipt.jobId,
          failureStage: null,
        },
        receipt.contentSha256,
        receipt.unchanged ? "服务器数据版本未变化" : "新数据版本已发布，分析任务已入队",
      )
      return terminalResult(
        updated,
        receipt.unchanged ? "skipped" : "success",
        receipt.unchanged ? "服务器数据版本未变化" : "新数据版本已发布，分析任务已入队",
      )
    } catch (error) {
      if (error instanceof ApiRequestError && error.statusCode === 401) {
        const paused = await this.#setAttempt(
          current,
          { ...attempt, stagedPath, contentSha256, phase: "needs_login" },
          { phase: "needs_login", message: "登录已失效，登录后将继续采集", file: file.name },
        )
        return {
          source: paused,
          outcome: "waiting",
          jobId: null,
          revisionId: null,
          message: "登录已失效，登录后将继续采集",
        }
      }
      if (!(error instanceof Error)) throw error
      const permanent = error instanceof ApiRequestError
        && [400, 403, 404, 409].includes(error.statusCode)
      if (permanent) {
        const attention = await this.#setAttempt(
          current,
          { ...attempt, stagedPath, contentSha256, phase: "needs_attention" },
          { phase: "needs_attention", message: error.message, file: file.name },
        )
        return {
          source: attention,
          outcome: "failed",
          jobId: null,
          revisionId: null,
          message: error.message,
        }
      }
      const retrying = await this.#setAttempt(
        current,
        { ...attempt, stagedPath, contentSha256, phase: "uploading" },
        { phase: "waiting", message: `刷新暂时失败，将使用同一回执重试：${error.message}`, file: file.name },
      )
      return {
        source: retrying,
        outcome: "waiting",
        jobId: null,
        revisionId: null,
        message: "刷新暂时失败，将使用同一幂等回执重试",
      }
    }
  }

  async #runLegacyRecipe(
    source: DesktopFileSource,
    file: ScannedFile,
  ): Promise<CollectorRunResult> {
    const at = (this.options.now ?? (() => new Date()))().toISOString()
    const attempt: DesktopFileSourceAttempt = source.activeAttempt ?? {
      id: (this.options.createId ?? randomUUID)(),
      stagedPath: null,
      contentSha256: null,
      file: file.name,
      phase: "copying",
      createdAt: at,
    }
    let current = source
    let stagedPath = attempt.stagedPath
    let fileHash = attempt.contentSha256
    if (stagedPath === null || fileHash === null) {
      const snapshotted = await this.#snapshot(current, file, attempt)
      current = snapshotted.source
      if (snapshotted.result.kind === "waiting") {
        return {
          source: current,
          outcome: "waiting",
          jobId: null,
          revisionId: null,
          message: "文件仍在写入，等待稳定后重试",
        }
      }
      stagedPath = snapshotted.result.snapshot.path
      fileHash = snapshotted.result.snapshot.sha256
    }

    const previous = current.lastRun
    const previouslyCompleted = previous?.status === "success" || previous?.status === "skipped"
    if (previouslyCompleted && current.lastAppliedHash === fileHash) {
      await removeStableFileSnapshot(this.options.userData, stagedPath)
      const updated = await this.#persist(current.id, {
        at,
        status: "skipped",
        file: file.name,
        error: null,
        rowsImported: null,
        contentSha256: fileHash,
        revisionId: previous.revisionId,
        jobId: null,
        failureStage: null,
      }, fileHash, "文件内容未变化，已跳过")
      return terminalResult(updated, "skipped", "文件内容未变化，已跳过")
    }

    const replacementCompleted = previous?.status === "failed"
      && previous.fileHash === fileHash
      && previous.rowsImported !== null
    let rowsImported = replacementCompleted ? previous.rowsImported : null
    try {
      if (!replacementCompleted) {
        const uploadHandle = await openStableSnapshotFile(this.options.userData, stagedPath)
        rowsImported = (await this.api.replaceSource(current.targetSourceId, uploadHandle, file.name)).rowCount
      }
      current = await this.#setAttempt(
        current,
        { ...attempt, stagedPath, contentSha256: fileHash, phase: "publishing" },
        { phase: "publishing", message: "正在触发下游计划", file: file.name },
      )
      const jobIds = await this.#triggerSchedules(current.triggerScheduleIds)
      await removeStableFileSnapshot(this.options.userData, stagedPath)
      const updated = await this.#persist(current.id, {
        at,
        status: "success",
        file: file.name,
        error: null,
        rowsImported,
        contentSha256: fileHash,
        revisionId: null,
        jobId: jobIds[0] ?? null,
        failureStage: null,
      }, fileHash, "采集完成，下游任务已入队")
      return terminalResult(updated, "success", "采集完成，下游任务已入队")
    } catch (error) {
      if (!(error instanceof Error)) throw error
      const updated = await this.#persist(current.id, {
        at,
        status: "failed",
        file: file.name,
        error: `采集或下游任务失败：${error.message}`,
        rowsImported,
        contentSha256: fileHash,
        revisionId: null,
        jobId: null,
        failureStage: rowsImported === null ? "refresh" : "enqueue",
      }, fileHash, "采集或下游任务失败")
      return terminalResult(updated, "failed", error.message)
    }
  }

  async #scanResult(
    source: DesktopFileSource,
    result: ScanResult,
  ): Promise<CollectorRunResult> {
    const at = (this.options.now ?? (() => new Date()))().toISOString()
    switch (result.kind) {
      case "found":
        return source.mode === "saved_query"
          ? this.#runSavedQueryRecipe(source, result.file)
          : this.#runLegacyRecipe(source, result.file)
      case "no_match": {
        const updated = await this.#persist(source.id, {
          at,
          status: "failed",
          file: null,
          error: "未找到匹配文件",
          rowsImported: null,
          contentSha256: null,
          revisionId: null,
          jobId: null,
          failureStage: "scan",
        }, source.lastAppliedHash, "未找到匹配文件")
        return terminalResult(updated, "failed", "未找到匹配文件")
      }
      case "unreadable": {
        const updated = await this.#persist(source.id, {
          at,
          status: "failed",
          file: null,
          error: "无法读取目录或文件",
          rowsImported: null,
          contentSha256: null,
          revisionId: null,
          jobId: null,
          failureStage: "scan",
        }, source.lastAppliedHash, "无法读取目录或文件")
        return terminalResult(updated, "failed", "无法读取目录或文件")
      }
      default:
        return assertNever(result)
    }
  }

  async #execute(id: string, _origin: FileSourceRunOrigin): Promise<CollectorRunResult> {
    const sources = await this.store.list()
    let source = sources.find((candidate) => candidate.id === id)
    if (source === undefined) {
      throw new FileSourceNotFoundError(id)
    }
    const persistedAttempt = source.activeAttempt
    if (
      persistedAttempt?.stagedPath
      && persistedAttempt.contentSha256
      && persistedAttempt.file
    ) {
      if (!isStableSnapshotPath(this.options.userData, persistedAttempt.stagedPath)) {
        throw new CollectorStateError("Persisted automation snapshot path is outside staging")
      }
      try {
        await assertStableSnapshotFile(this.options.userData, persistedAttempt.stagedPath)
        const staged = await stat(persistedAttempt.stagedPath)
        const recoveredFile: ScannedFile = {
          path: persistedAttempt.stagedPath,
          name: persistedAttempt.file,
          size: staged.size,
          mtimeMs: staged.mtimeMs,
          sha256: persistedAttempt.contentSha256,
        }
        return source.mode === "saved_query"
          ? this.#runSavedQueryRecipe(source, recoveredFile)
          : this.#runLegacyRecipe(source, recoveredFile)
      } catch (error) {
        if (!(error instanceof Error) || !("code" in error) || error.code !== "ENOENT") {
          throw error
        }
        source = await this.store.setActiveAttempt(source.id, {
          ...persistedAttempt,
          stagedPath: null,
          contentSha256: null,
          phase: "copying",
        })
      }
    }
    this.#emit(source, { phase: "scanning", message: "正在扫描本地目录", file: null })
    const scan = this.options.scan ?? scanNewestFile
    return this.#scanResult(source, await scan(source.directory, source.pattern))
  }

  #runForTarget(
    sourceId: string,
    targetSourceId: string,
    origin: FileSourceRunOrigin,
  ): Promise<CollectorRunResult> {
    const previous = this.#targetQueues.get(targetSourceId) ?? Promise.resolve()
    const run = previous.then(() => this.#execute(sourceId, origin))
    const tail = run.then(() => undefined, () => undefined)
    this.#targetQueues.set(targetSourceId, tail)
    void tail.finally(() => {
      if (this.#targetQueues.get(targetSourceId) === tail) {
        this.#targetQueues.delete(targetSourceId)
      }
    })
    return run
  }

  runNow(id: string, origin: FileSourceRunOrigin = "manual"): Promise<CollectorRunResult> {
    const active = this.#active.get(id)
    if (active !== undefined) return active
    const run = this.store.list().then((sources) => {
      const source = sources.find((candidate) => candidate.id === id)
      if (source === undefined) throw new FileSourceNotFoundError(id)
      return this.#runForTarget(id, source.targetSourceId, origin)
    }).finally(() => {
      this.#active.delete(id)
    })
    this.#active.set(id, run)
    return run
  }

  async recover(): Promise<void> {
    const sources = await this.store.list()
    await Promise.all(sources
      .filter((source) => source.enabled && source.activeAttempt !== null)
      .map((source) => this.runNow(source.id, "recovery").catch(() => undefined)))
  }

  async stop(): Promise<void> {
    await Promise.allSettled(this.#active.values())
  }
}
