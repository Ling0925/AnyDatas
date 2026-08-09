// Electron 桌面壳的类型声明（preload 通过 contextBridge 注入 window.desktop）。
// 网页浏览器中 window.desktop 与 window.__ANYDATAS_API_BASE__ 均为 undefined，
// 所有访问都必须先做运行时守卫（见 AppShell.vue / router.ts / FileSourcesView.vue）。
// 本文件不导入任何模块，保持全局脚本声明以便 Electron 主进程侧也能复用同一契约。
// 服务端/主进程返回的数据不可变：字段一律 readonly，渲染层只能整体替换，不能原地修改。

interface DesktopFileSourceRun {
  readonly at: string
  readonly status: 'success' | 'skipped' | 'failed'
  readonly file: string | null
  readonly error: string | null
  readonly rowsImported: number | null
}

interface DesktopFileSourceLastRun {
  readonly status: 'success' | 'skipped' | 'failed' | null
  readonly at: string | null
  readonly file: string | null
  readonly fileHash: string | null
  readonly rowsImported: number | null
  readonly error: string | null
}

interface DesktopFileSource {
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

interface DesktopFileSourceConfig {
  readonly name: string
  readonly directory: string
  readonly pattern: string
  readonly targetSourceId: string
  readonly cron: string
  readonly timezone: string
  readonly triggerScheduleIds: string[]
}

interface Window {
  desktop: {
    readonly apiBase: string
    readonly listFileSources: () => Promise<DesktopFileSource[]>
    readonly createFileSource: (config: DesktopFileSourceConfig) => Promise<DesktopFileSource>
    readonly updateFileSource: (id: string, config: Partial<DesktopFileSource>) => Promise<DesktopFileSource>
    readonly deleteFileSource: (id: string) => Promise<void>
    readonly toggleFileSource: (id: string, enabled: boolean) => Promise<DesktopFileSource>
    readonly runFileSourceNow: (id: string) => Promise<DesktopFileSource>
    readonly pickDirectory: () => Promise<string | null>
    readonly apiTarget: () => Promise<string>
    readonly onFileSourceEvent: (
      callback: (payload: {
        readonly id: string
        readonly lastRun: DesktopFileSource['lastRun']
        readonly runs: DesktopFileSourceRun[]
      }) => void,
    ) => () => void
  }
  __ANYDATAS_API_BASE__?: string
}
