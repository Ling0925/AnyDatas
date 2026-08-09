import * as z from "zod"
import type { FileSourceEvent } from "./collector.js"
import { FileSourceNotFoundError, FileSourceStore } from "./store.js"
import type { FileSourceRunner } from "./scheduler.js"
import type { DesktopFileSourceConfig } from "./types.js"

export const FILE_SOURCE_CHANNELS = {
  list: "desktop:file-sources:list",
  create: "desktop:file-sources:create",
  update: "desktop:file-sources:update",
  delete: "desktop:file-sources:delete",
  toggle: "desktop:file-sources:toggle",
  runNow: "desktop:file-sources:run-now",
  pickDirectory: "desktop:pick-directory",
  apiTarget: "desktop:api-target",
  event: "desktop:file-source-event",
} as const

export type IpcHandler = (...args: readonly unknown[]) => Promise<unknown>

export type IpcRegistrar = {
  readonly handle: (channel: string, handler: IpcHandler) => void
  readonly removeHandler: (channel: string) => void
}

type DirectoryDialog = {
  readonly showOpenDialog: (options: {
    readonly properties: readonly ["openDirectory"]
  }) => Promise<{ readonly canceled: boolean; readonly filePaths: string[] }>
}

type RegisterIpcOptions = {
  readonly registrar: IpcRegistrar
  readonly store: FileSourceStore
  readonly runner: FileSourceRunner
  readonly dialog: DirectoryDialog
  readonly apiTarget: () => string | null
}

type FileSourceEventSender = {
  readonly send: (channel: string, payload: FileSourceEvent) => void
}

const emptyArgsSchema = z.tuple([])
const idSchema = z.string().min(1)
const configString = z.string().trim().min(1)
const updateSchema = z.strictObject({
  name: configString.optional(),
  directory: configString.optional(),
  pattern: configString.optional(),
  targetSourceId: configString.optional(),
  cron: configString.optional(),
  timezone: configString.optional(),
  triggerScheduleIds: z.array(configString).optional(),
  enabled: z.boolean().optional(),
})

export function emitFileSourceEvent(
  sender: FileSourceEventSender,
  event: FileSourceEvent,
): void {
  sender.send(FILE_SOURCE_CHANNELS.event, event)
}

export function registerFileSourceIpc(options: RegisterIpcOptions): () => void {
  const registered: string[] = []
  const handle = (channel: string, handler: IpcHandler): void => {
    options.registrar.handle(channel, handler)
    registered.push(channel)
  }

  handle(FILE_SOURCE_CHANNELS.list, async (...args) => {
    emptyArgsSchema.parse(args)
    return options.store.list()
  })
  handle(FILE_SOURCE_CHANNELS.create, async (...args) => {
    const [input] = z.tuple([z.unknown()]).parse(args)
    return options.store.create(input)
  })
  handle(FILE_SOURCE_CHANNELS.update, async (...args) => {
    const [idInput, updateInput] = z.tuple([z.unknown(), z.unknown()]).parse(args)
    const id = idSchema.parse(idInput)
    const update = updateSchema.parse(updateInput)
    const sources = await options.store.list()
    const source = sources.find((candidate) => candidate.id === id)
    if (source === undefined) {
      throw new FileSourceNotFoundError(id)
    }
    const merged: DesktopFileSourceConfig = {
      name: update.name ?? source.name,
      directory: update.directory ?? source.directory,
      pattern: update.pattern ?? source.pattern,
      targetSourceId: update.targetSourceId ?? source.targetSourceId,
      cron: update.cron ?? source.cron,
      timezone: update.timezone ?? source.timezone,
      triggerScheduleIds: update.triggerScheduleIds ?? source.triggerScheduleIds,
    }
    const updated = await options.store.update(id, merged)
    if (update.enabled !== undefined && update.enabled !== updated.enabled) {
      return options.store.toggle(id, update.enabled)
    }
    return updated
  })
  handle(FILE_SOURCE_CHANNELS.delete, async (...args) => {
    const [idInput] = z.tuple([z.unknown()]).parse(args)
    await options.store.delete(idSchema.parse(idInput))
  })
  handle(FILE_SOURCE_CHANNELS.toggle, async (...args) => {
    const [idInput, enabledInput] = z.tuple([z.unknown(), z.unknown()]).parse(args)
    return options.store.toggle(idSchema.parse(idInput), z.boolean().parse(enabledInput))
  })
  handle(FILE_SOURCE_CHANNELS.runNow, async (...args) => {
    const [idInput] = z.tuple([z.unknown()]).parse(args)
    return options.runner.runNow(idSchema.parse(idInput))
  })
  handle(FILE_SOURCE_CHANNELS.pickDirectory, async (...args) => {
    emptyArgsSchema.parse(args)
    const result = await options.dialog.showOpenDialog({ properties: ["openDirectory"] })
    if (result.canceled) {
      return null
    }
    return result.filePaths.at(0) ?? null
  })
  handle(FILE_SOURCE_CHANNELS.apiTarget, async (...args) => {
    emptyArgsSchema.parse(args)
    return options.apiTarget()
  })

  return () => {
    for (const channel of registered) {
      options.registrar.removeHandler(channel)
    }
  }
}
