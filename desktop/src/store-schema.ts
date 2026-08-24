import * as z from "zod"
import { CronExpressionError, CronTimezoneError, cronMatches } from "./cron.js"
import type {
  DesktopFileSource,
  DesktopFileSourceAttempt,
  DesktopFileSourceConfig,
  DesktopFileSourceRun,
  FileSourceRecipeMode,
} from "./types.js"

export const FILE_SOURCE_STORE_SCHEMA_VERSION = 2 as const

export type FileSourceRunAppend = {
  readonly run: DesktopFileSourceRun
  readonly fileHash: string | null
}

export class FileSourceValidationError extends Error {
  override readonly name = "FileSourceValidationError"

  constructor(readonly issues: readonly string[]) {
    super(`Invalid file source: ${issues.join("; ")}`)
  }
}

export class FileSourceDataError extends Error {
  override readonly name = "FileSourceDataError"

  constructor(readonly filePath: string, message: string, options?: ErrorOptions) {
    super(`Invalid file source data in "${filePath}": ${message}`, options)
  }
}

const nonEmptyString = z.string().trim().min(1)
const safeAttemptId = z.string().regex(/^[A-Za-z0-9._-]{1,128}$/u)
const nullableNonEmptyString = nonEmptyString.nullable()
const runStatus = z.union([z.literal("success"), z.literal("skipped"), z.literal("failed")])
const recipeMode = z.union([z.literal("legacy_schedule"), z.literal("saved_query")])
const failureStage = z.union([
  z.literal("scan"),
  z.literal("snapshot"),
  z.literal("refresh"),
  z.literal("enqueue"),
]).nullable()
const attemptPhase = z.union([
  z.literal("copying"),
  z.literal("waiting"),
  z.literal("uploading"),
  z.literal("publishing"),
  z.literal("needs_login"),
  z.literal("needs_attention"),
])

const baseConfigShape = {
  name: nonEmptyString,
  directory: nonEmptyString,
  pattern: nonEmptyString,
  targetSourceId: nonEmptyString,
  cron: nonEmptyString,
  timezone: nonEmptyString,
}

function scheduleIssues(config: { readonly cron: string; readonly timezone: string }): string[] {
  try {
    cronMatches(config.cron, new Date(0), config.timezone)
    return []
  } catch (error) {
    if (error instanceof CronExpressionError || error instanceof CronTimezoneError) {
      return [error.message]
    }
    throw error
  }
}

function addScheduleIssues(
  config: { readonly cron: string; readonly timezone: string },
  context: z.RefinementCtx,
): void {
  for (const message of scheduleIssues(config)) {
    context.addIssue({ code: "custom", message })
  }
}

const configSchema = z.strictObject({
  ...baseConfigShape,
  mode: recipeMode.optional(),
  savedQueryId: nullableNonEmptyString.optional(),
  workspaceId: nullableNonEmptyString.optional(),
  triggerScheduleIds: z.array(nonEmptyString).optional(),
}).superRefine((config, context) => {
  addScheduleIssues(config, context)
  const mode: FileSourceRecipeMode = config.mode
    ?? (config.savedQueryId ? "saved_query" : "legacy_schedule")
  if (mode === "saved_query" && !config.savedQueryId) {
    context.addIssue({ code: "custom", path: ["savedQueryId"], message: "saved query is required" })
  }
  if (mode === "saved_query" && !config.workspaceId) {
    context.addIssue({ code: "custom", path: ["workspaceId"], message: "workspace is required" })
  }
})

const runSchema = z.strictObject({
  at: nonEmptyString,
  status: runStatus,
  file: z.string().nullable(),
  error: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
  contentSha256: z.string().nullable().default(null),
  revisionId: z.string().nullable().default(null),
  jobId: z.string().nullable().default(null),
  failureStage: failureStage.default(null),
})
const lastRunSchema = z.strictObject({
  status: runStatus.nullable(),
  at: z.string().nullable(),
  file: z.string().nullable(),
  fileHash: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
  error: z.string().nullable(),
  revisionId: z.string().nullable().default(null),
  jobId: z.string().nullable().default(null),
  failureStage: failureStage.default(null),
})
const attemptSchema = z.strictObject({
  id: safeAttemptId,
  stagedPath: z.string().nullable(),
  contentSha256: z.string().nullable(),
  file: z.string().nullable(),
  phase: attemptPhase,
  createdAt: nonEmptyString,
})

const v2SourceSchema = z.strictObject({
  id: nonEmptyString,
  ...baseConfigShape,
  mode: recipeMode,
  savedQueryId: nullableNonEmptyString,
  workspaceId: nullableNonEmptyString,
  triggerScheduleIds: z.array(nonEmptyString),
  enabled: z.boolean(),
  createdAt: nonEmptyString,
  updatedAt: nonEmptyString,
  lastAppliedHash: z.string().nullable(),
  activeAttempt: attemptSchema.nullable(),
  lastRun: lastRunSchema.nullable(),
  runs: z.array(runSchema),
}).superRefine((source, context) => {
  addScheduleIssues(source, context)
  if (source.mode === "saved_query" && !source.savedQueryId) {
    context.addIssue({ code: "custom", path: ["savedQueryId"], message: "saved query is required" })
  }
  if (source.mode === "saved_query" && !source.workspaceId) {
    context.addIssue({ code: "custom", path: ["workspaceId"], message: "workspace is required" })
  }
})

const legacyRunSchema = z.strictObject({
  at: nonEmptyString,
  status: runStatus,
  file: z.string().nullable(),
  error: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
})
const legacyLastRunSchema = z.strictObject({
  status: runStatus.nullable(),
  at: z.string().nullable(),
  file: z.string().nullable(),
  fileHash: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
  error: z.string().nullable(),
})
const legacySourceSchema = z.strictObject({
  id: nonEmptyString,
  ...baseConfigShape,
  enabled: z.boolean(),
  triggerScheduleIds: z.array(nonEmptyString),
  createdAt: nonEmptyString,
  updatedAt: nonEmptyString,
  lastRun: legacyLastRunSchema.nullable(),
  runs: z.array(legacyRunSchema),
}).superRefine(addScheduleIssues)

const v2StoreSchema = z.strictObject({
  schemaVersion: z.literal(FILE_SOURCE_STORE_SCHEMA_VERSION),
  sources: z.array(v2SourceSchema),
})
const legacyStoreSchema = z.array(legacySourceSchema)
const runAppendSchema = z.strictObject({ run: runSchema, fileHash: z.string().nullable() })

function issueMessages(error: z.ZodError): string[] {
  return error.issues.map((issue) => `${issue.path.join(".") || "value"}: ${issue.message}`)
}

function normalizeConfig(parsed: z.infer<typeof configSchema>): DesktopFileSourceConfig {
  const mode: FileSourceRecipeMode = parsed.mode
    ?? (parsed.savedQueryId ? "saved_query" : "legacy_schedule")
  return {
    name: parsed.name,
    mode,
    directory: parsed.directory,
    pattern: parsed.pattern,
    targetSourceId: parsed.targetSourceId,
    savedQueryId: parsed.savedQueryId ?? null,
    workspaceId: parsed.workspaceId ?? null,
    cron: parsed.cron,
    timezone: parsed.timezone,
    triggerScheduleIds: parsed.triggerScheduleIds ?? [],
  }
}

function migrateLegacySource(source: z.infer<typeof legacySourceSchema>): DesktopFileSource {
  const runs: DesktopFileSourceRun[] = source.runs.map((run) => ({
    ...run,
    contentSha256: null,
    revisionId: null,
    jobId: null,
    failureStage: null,
  }))
  return {
    ...source,
    mode: "legacy_schedule",
    savedQueryId: null,
    workspaceId: null,
    lastAppliedHash: source.lastRun?.fileHash ?? null,
    activeAttempt: null,
    lastRun: source.lastRun === null ? null : {
      ...source.lastRun,
      revisionId: null,
      jobId: null,
      failureStage: null,
    },
    runs,
  }
}

export function parseFileSourceConfig(input: unknown): DesktopFileSourceConfig {
  const result = configSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceValidationError(issueMessages(result.error))
  }
  return normalizeConfig(result.data)
}

export function parseFileSources(input: unknown, filePath: string): DesktopFileSource[] {
  const current = v2StoreSchema.safeParse(input)
  if (current.success) {
    return current.data.sources
  }
  const legacy = legacyStoreSchema.safeParse(input)
  if (legacy.success) {
    return legacy.data.map(migrateLegacySource)
  }
  throw new FileSourceDataError(
    filePath,
    issueMessages(current.error).join("; "),
  )
}

export function serializeFileSources(sources: readonly DesktopFileSource[]): string {
  const validated = v2StoreSchema.parse({
    schemaVersion: FILE_SOURCE_STORE_SCHEMA_VERSION,
    sources,
  })
  return `${JSON.stringify(validated, null, 2)}\n`
}

export function parseRunAppend(input: unknown): FileSourceRunAppend {
  const result = runAppendSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceValidationError(issueMessages(result.error))
  }
  return result.data
}

export function parseAttempt(input: unknown): DesktopFileSourceAttempt | null {
  if (input === null) return null
  const result = attemptSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceValidationError(issueMessages(result.error))
  }
  return result.data
}
