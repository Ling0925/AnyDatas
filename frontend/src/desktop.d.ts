// Electron desktop bridge exposed by preload through contextBridge.

interface DesktopFileSourceRun {
  readonly at: string
  readonly status: 'success' | 'skipped' | 'failed'
  readonly file: string | null
  readonly error: string | null
  readonly rowsImported: number | null
  readonly contentSha256: string | null
  readonly revisionId: string | null
  readonly jobId: string | null
  readonly failureStage: 'scan' | 'snapshot' | 'refresh' | 'enqueue' | null
}

interface DesktopFileSourceLastRun {
  readonly status: 'success' | 'skipped' | 'failed' | null
  readonly at: string | null
  readonly file: string | null
  readonly fileHash: string | null
  readonly rowsImported: number | null
  readonly error: string | null
  readonly revisionId: string | null
  readonly jobId: string | null
  readonly failureStage: 'scan' | 'snapshot' | 'refresh' | 'enqueue' | null
}

interface DesktopFileSourceAttempt {
  readonly id: string
  readonly stagedPath: string | null
  readonly contentSha256: string | null
  readonly file: string | null
  readonly phase: 'copying' | 'waiting' | 'uploading' | 'publishing' | 'needs_login' | 'needs_attention'
  readonly createdAt: string
}

interface DesktopFileSource {
  readonly id: string
  readonly name: string
  readonly mode: 'legacy_schedule' | 'saved_query'
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

interface DesktopFileSourceConfig {
  readonly name: string
  readonly mode?: 'legacy_schedule' | 'saved_query'
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly savedQueryId?: string | null
  readonly workspaceId?: string | null
  readonly cron: string
  readonly timezone: string
  readonly triggerScheduleIds?: string[]
}

interface DesktopFileSourceActivity {
  readonly phase: 'scanning' | 'waiting' | 'uploading' | 'publishing' | 'queued' | 'needs_login' | 'needs_attention' | 'idle'
  readonly message: string
  readonly file: string | null
}

interface DesktopCollectorRunResult {
  readonly source: DesktopFileSource
  readonly outcome: 'success' | 'skipped' | 'failed' | 'waiting'
  readonly jobId: string | null
  readonly revisionId: string | null
  readonly message: string
}

type DesktopBackendSelection =
  | { readonly mode: 'standalone' }
  | { readonly mode: 'remote'; readonly serverUrl: string }

interface DesktopBackendStatus {
  readonly mode: 'standalone' | 'remote' | null
  readonly phase: 'unconfigured' | 'starting' | 'downloading' | 'ready' | 'failed'
  readonly serverUrl: string | null
  readonly serverVersion: string | null
  readonly protocolVersion: number | null
  readonly capabilities: string[]
  readonly message: string
  readonly progress: number | null
}

interface Window {
  desktop: {
    readonly apiBase: string
    readonly getBackendStatus: () => Promise<DesktopBackendStatus>
    readonly configureBackend: (selection: DesktopBackendSelection) => Promise<DesktopBackendStatus>
    readonly resetBackend: () => Promise<DesktopBackendStatus>
    readonly listFileSources: () => Promise<DesktopFileSource[]>
    readonly createFileSource: (config: DesktopFileSourceConfig) => Promise<DesktopFileSource>
    readonly updateFileSource: (id: string, config: Partial<DesktopFileSource>) => Promise<DesktopFileSource>
    readonly deleteFileSource: (id: string) => Promise<void>
    readonly toggleFileSource: (id: string, enabled: boolean) => Promise<DesktopFileSource>
    readonly runFileSourceNow: (id: string) => Promise<DesktopCollectorRunResult>
    readonly pickDirectory: () => Promise<string | null>
    readonly apiTarget: () => Promise<string | null>
    readonly onBackendStatus: (callback: (status: DesktopBackendStatus) => void) => () => void
    readonly onFileSourceEvent: (
      callback: (payload: {
        readonly id: string
        readonly activity: DesktopFileSourceActivity
        readonly lastRun: DesktopFileSource['lastRun']
        readonly runs: DesktopFileSourceRun[]
      }) => void,
    ) => () => void
  }
  __ANYDATAS_API_BASE__?: string
}
