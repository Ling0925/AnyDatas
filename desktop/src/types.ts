export type FileSourceRecipeMode = "legacy_schedule" | "saved_query"
export type FileSourceAttemptPhase =
  | "copying"
  | "waiting"
  | "uploading"
  | "publishing"
  | "needs_login"
  | "needs_attention"

export type DesktopFileSourceRun = {
  readonly at: string
  readonly status: "success" | "skipped" | "failed"
  readonly file: string | null
  readonly error: string | null
  readonly rowsImported: number | null
  readonly contentSha256: string | null
  readonly revisionId: string | null
  readonly jobId: string | null
  readonly failureStage: "scan" | "snapshot" | "refresh" | "enqueue" | null
}

export type DesktopFileSourceLastRun = {
  readonly status: "success" | "skipped" | "failed" | null
  readonly at: string | null
  readonly file: string | null
  readonly fileHash: string | null
  readonly rowsImported: number | null
  readonly error: string | null
  readonly revisionId: string | null
  readonly jobId: string | null
  readonly failureStage: "scan" | "snapshot" | "refresh" | "enqueue" | null
}

export type DesktopFileSourceAttempt = {
  readonly id: string
  readonly stagedPath: string | null
  readonly contentSha256: string | null
  readonly file: string | null
  readonly phase: FileSourceAttemptPhase
  readonly createdAt: string
}

export type DesktopFileSource = {
  readonly id: string
  readonly name: string
  readonly mode: FileSourceRecipeMode
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly savedQueryId: string | null
  readonly workspaceId: string | null
  readonly cron: string
  readonly timezone: string
  readonly enabled: boolean
  readonly triggerScheduleIds: string[]
  readonly createdAt: string
  readonly updatedAt: string
  readonly lastAppliedHash: string | null
  readonly activeAttempt: DesktopFileSourceAttempt | null
  readonly lastRun: DesktopFileSourceLastRun | null
  readonly runs: DesktopFileSourceRun[]
}

export type DesktopFileSourceConfig = {
  readonly name: string
  readonly mode?: FileSourceRecipeMode
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly savedQueryId?: string | null
  readonly workspaceId?: string | null
  readonly cron: string
  readonly timezone: string
  readonly triggerScheduleIds?: string[]
}

export type FileSourceActivity = {
  readonly phase:
    | "scanning"
    | "waiting"
    | "uploading"
    | "publishing"
    | "queued"
    | "needs_login"
    | "needs_attention"
    | "idle"
  readonly message: string
  readonly file: string | null
}

export type FileSourceRunOrigin = "manual" | "scheduled" | "recovery"
export type CollectorRunOutcome = "success" | "skipped" | "failed" | "waiting"

export type CollectorRunResult = {
  readonly source: DesktopFileSource
  readonly outcome: CollectorRunOutcome
  readonly jobId: string | null
  readonly revisionId: string | null
  readonly message: string
}
