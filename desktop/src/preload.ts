import { contextBridge, ipcRenderer } from "electron"
import type { IpcRendererEvent } from "electron"
import * as z from "zod"
import { BACKEND_CHANNELS } from "./backend-ipc.js"
import { FILE_SOURCE_CHANNELS } from "./ipc.js"
import type { BackendSelection, BackendStatus } from "./backend-types.js"
import type {
  DesktopFileSource,
  DesktopFileSourceConfig,
  DesktopFileSourceRun,
} from "./types.js"

const API_BASE = "http://127.0.0.1:28090"
const runStatus = z.union([z.literal("success"), z.literal("skipped"), z.literal("failed")])
const runSchema = z.strictObject({
  at: z.string(),
  status: runStatus,
  file: z.string().nullable(),
  error: z.string().nullable(),
  rowsImported: z.number().nullable(),
})
const lastRunSchema = z.strictObject({
  status: runStatus.nullable(),
  at: z.string().nullable(),
  file: z.string().nullable(),
  fileHash: z.string().nullable(),
  rowsImported: z.number().nullable(),
  error: z.string().nullable(),
})
const sourceSchema = z.strictObject({
  id: z.string(),
  name: z.string(),
  directory: z.string(),
  pattern: z.string(),
  targetSourceId: z.string(),
  cron: z.string(),
  timezone: z.string(),
  enabled: z.boolean(),
  triggerScheduleIds: z.array(z.string()),
  createdAt: z.string(),
  updatedAt: z.string(),
  lastRun: lastRunSchema.nullable(),
  runs: z.array(runSchema),
})
const eventSchema = z.strictObject({
  id: z.string(),
  lastRun: lastRunSchema.nullable(),
  runs: z.array(runSchema),
})
const backendModeSchema = z.union([z.literal("standalone"), z.literal("remote")])
const backendStatusSchema = z.strictObject({
  mode: backendModeSchema.nullable(),
  phase: z.union([
    z.literal("unconfigured"),
    z.literal("starting"),
    z.literal("downloading"),
    z.literal("ready"),
    z.literal("failed"),
  ]),
  serverUrl: z.string().nullable(),
  serverVersion: z.string().nullable(),
  protocolVersion: z.number().nullable(),
  message: z.string(),
  progress: z.number().nullable(),
})

type DesktopBridge = {
  readonly apiBase: string
  readonly getBackendStatus: () => Promise<BackendStatus>
  readonly configureBackend: (selection: BackendSelection) => Promise<BackendStatus>
  readonly resetBackend: () => Promise<BackendStatus>
  readonly listFileSources: () => Promise<DesktopFileSource[]>
  readonly createFileSource: (config: DesktopFileSourceConfig) => Promise<DesktopFileSource>
  readonly updateFileSource: (
    id: string,
    config: Partial<DesktopFileSource>,
  ) => Promise<DesktopFileSource>
  readonly deleteFileSource: (id: string) => Promise<void>
  readonly toggleFileSource: (id: string, enabled: boolean) => Promise<DesktopFileSource>
  readonly runFileSourceNow: (id: string) => Promise<DesktopFileSource>
  readonly pickDirectory: () => Promise<string | null>
  readonly apiTarget: () => Promise<string | null>
  readonly onBackendStatus: (callback: (status: BackendStatus) => void) => () => void
  readonly onFileSourceEvent: (
    callback: (payload: {
      readonly id: string
      readonly lastRun: DesktopFileSource["lastRun"]
      readonly runs: DesktopFileSourceRun[]
    }) => void,
  ) => () => void
}

const bridge: DesktopBridge = {
  apiBase: API_BASE,
  getBackendStatus: async () =>
    backendStatusSchema.parse(await ipcRenderer.invoke(BACKEND_CHANNELS.status)),
  configureBackend: async (selection) =>
    backendStatusSchema.parse(await ipcRenderer.invoke(BACKEND_CHANNELS.configure, selection)),
  resetBackend: async () =>
    backendStatusSchema.parse(await ipcRenderer.invoke(BACKEND_CHANNELS.reset)),
  listFileSources: async () => sourceSchema.array().parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.list)),
  createFileSource: async (config) =>
    sourceSchema.parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.create, config)),
  updateFileSource: async (id, config) =>
    sourceSchema.parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.update, id, config)),
  deleteFileSource: async (id) => {
    await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.delete, id)
  },
  toggleFileSource: async (id, enabled) =>
    sourceSchema.parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.toggle, id, enabled)),
  runFileSourceNow: async (id) =>
    sourceSchema.parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.runNow, id)),
  pickDirectory: async () =>
    z.string().nullable().parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.pickDirectory)),
  apiTarget: async () =>
    z.string().nullable().parse(await ipcRenderer.invoke(FILE_SOURCE_CHANNELS.apiTarget)),
  onBackendStatus: (callback) => {
    const listener = (_event: IpcRendererEvent, payload: unknown): void => {
      callback(backendStatusSchema.parse(payload))
    }
    ipcRenderer.on(BACKEND_CHANNELS.event, listener)
    return () => {
      ipcRenderer.removeListener(BACKEND_CHANNELS.event, listener)
    }
  },
  onFileSourceEvent: (callback) => {
    const listener = (_event: IpcRendererEvent, payload: unknown): void => {
      callback(eventSchema.parse(payload))
    }
    ipcRenderer.on(FILE_SOURCE_CHANNELS.event, listener)
    return () => {
      ipcRenderer.removeListener(FILE_SOURCE_CHANNELS.event, listener)
    }
  },
}

contextBridge.exposeInMainWorld("desktop", bridge)
contextBridge.exposeInMainWorld("__ANYDATAS_API_BASE__", API_BASE)
