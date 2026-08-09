import * as z from "zod"
import { CronExpressionError, CronTimezoneError, cronMatches } from "./cron.js"
import type {
  DesktopFileSource,
  DesktopFileSourceConfig,
  DesktopFileSourceRun,
} from "./types.js"

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
const runStatus = z.union([z.literal("success"), z.literal("skipped"), z.literal("failed")])
const configShape = {
  name: nonEmptyString,
  directory: nonEmptyString,
  pattern: nonEmptyString,
  targetSourceId: nonEmptyString,
  cron: nonEmptyString,
  timezone: nonEmptyString,
  triggerScheduleIds: z.array(nonEmptyString),
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

const configSchema = z.strictObject(configShape).superRefine(addScheduleIssues)
const runSchema = z.strictObject({
  at: nonEmptyString,
  status: runStatus,
  file: z.string().nullable(),
  error: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
})
const lastRunSchema = z.strictObject({
  status: runStatus.nullable(),
  at: z.string().nullable(),
  file: z.string().nullable(),
  fileHash: z.string().nullable(),
  rowsImported: z.number().int().nonnegative().nullable(),
  error: z.string().nullable(),
})
const sourceSchema = z
  .strictObject({
    id: nonEmptyString,
    ...configShape,
    enabled: z.boolean(),
    createdAt: nonEmptyString,
    updatedAt: nonEmptyString,
    lastRun: lastRunSchema.nullable(),
    runs: z.array(runSchema),
  })
  .superRefine(addScheduleIssues)
const sourcesSchema = z.array(sourceSchema)
const runAppendSchema = z.strictObject({ run: runSchema, fileHash: z.string().nullable() })

function issueMessages(error: z.ZodError): string[] {
  return error.issues.map((issue) => `${issue.path.join(".") || "value"}: ${issue.message}`)
}

export function parseFileSourceConfig(input: unknown): DesktopFileSourceConfig {
  const result = configSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceValidationError(issueMessages(result.error))
  }
  return result.data
}

export function parseFileSources(input: unknown, filePath: string): DesktopFileSource[] {
  const result = sourcesSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceDataError(filePath, issueMessages(result.error).join("; "))
  }
  return result.data
}

export function parseRunAppend(input: unknown): FileSourceRunAppend {
  const result = runAppendSchema.safeParse(input)
  if (!result.success) {
    throw new FileSourceValidationError(issueMessages(result.error))
  }
  return result.data
}
