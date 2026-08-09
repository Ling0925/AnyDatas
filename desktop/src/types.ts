export type DesktopFileSourceRun = {
  readonly at: string
  readonly status: "success" | "skipped" | "failed"
  readonly file: string | null
  readonly error: string | null
  readonly rowsImported: number | null
}

export type DesktopFileSourceLastRun = {
  readonly status: "success" | "skipped" | "failed" | null
  readonly at: string | null
  readonly file: string | null
  readonly fileHash: string | null
  readonly rowsImported: number | null
  readonly error: string | null
}

export type DesktopFileSource = {
  readonly id: string
  readonly name: string
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly cron: string
  readonly timezone: string
  readonly enabled: boolean
  readonly triggerScheduleIds: string[]
  readonly createdAt: string
  readonly updatedAt: string
  readonly lastRun: DesktopFileSourceLastRun | null
  readonly runs: DesktopFileSourceRun[]
}

export type DesktopFileSourceConfig = {
  readonly name: string
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly cron: string
  readonly timezone: string
  readonly triggerScheduleIds: string[]
}
