import { randomUUID } from "node:crypto"
import { createReadStream } from "node:fs"
import { stat } from "node:fs/promises"
import { request as httpRequest } from "node:http"
import type { ClientRequest, IncomingMessage, OutgoingHttpHeaders } from "node:http"
import { request as httpsRequest } from "node:https"
import { basename } from "node:path"
import { pipeline } from "node:stream/promises"
import * as z from "zod"

const MAX_RESPONSE_BYTES = 1024 * 1024
const rowCountSchema = z.object({ rowCount: z.number().int().nonnegative() })
const apiErrorSchema = z.object({
  error: z.object({ message: z.string().trim().min(1).max(4_096) }),
})

type LocalApiClientOptions = {
  readonly baseUrl: URL
  readonly timeoutMs?: number
}

type PendingRequest = {
  readonly request: ClientRequest
  readonly response: Promise<IncomingMessage>
}

export type ReplaceSourceResult = {
  readonly rowCount: number
}

export class ApiRequestError extends Error {
  override readonly name = "ApiRequestError"

  constructor(
    readonly path: string,
    readonly statusCode: number,
    readonly serverMessage: string | null,
  ) {
    super(
      serverMessage === null
        ? `Local API request failed with status ${statusCode}`
        : `Local API request failed with status ${statusCode}: ${serverMessage}`,
    )
  }
}

export class ApiTransportError extends Error {
  override readonly name = "ApiTransportError"

  constructor(readonly path: string, options?: ErrorOptions) {
    super("Local API request could not be completed", options)
  }
}

export class ApiResponseError extends Error {
  override readonly name = "ApiResponseError"

  constructor(readonly path: string, message: string, options?: ErrorOptions) {
    super(`Invalid local API response: ${message}`, options)
  }
}

async function readJson(response: IncomingMessage, path: string): Promise<unknown> {
  const body = await new Promise<Buffer>((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    response.on("data", (chunk: Buffer) => {
      size += chunk.length
      if (size > MAX_RESPONSE_BYTES) {
        reject(new ApiResponseError(path, "response exceeds 1 MiB"))
        response.destroy()
        return
      }
      chunks.push(chunk)
    })
    response.once("end", () => resolve(Buffer.concat(chunks)))
    response.once("error", reject)
  })

  try {
    const input: unknown = JSON.parse(body.toString("utf8"))
    return input
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new ApiResponseError(path, "response is not JSON", { cause: error })
    }
    throw error
  }
}

async function requireSuccess(response: IncomingMessage, path: string): Promise<void> {
  const statusCode = response.statusCode ?? 502
  if (statusCode >= 200 && statusCode < 300) {
    return
  }
  let serverMessage: string | null = null
  try {
    const parsed = apiErrorSchema.safeParse(await readJson(response, path))
    if (parsed.success) {
      serverMessage = parsed.data.error.message
    }
  } catch (error) {
    if (!(error instanceof ApiResponseError)) {
      throw error
    }
  }
  throw new ApiRequestError(path, statusCode, serverMessage)
}

export class LocalApiClient {
  constructor(private readonly options: LocalApiClientOptions) {}

  #open(path: string, method: string, headers: OutgoingHttpHeaders): PendingRequest {
    const send = this.options.baseUrl.protocol === "https:" ? httpsRequest : httpRequest
    const request = send({
      protocol: this.options.baseUrl.protocol,
      hostname: this.options.baseUrl.hostname,
      port: this.options.baseUrl.port,
      path,
      method,
      headers,
    })
    request.setTimeout(this.options.timeoutMs ?? 300_000, () => request.destroy())
    const response = new Promise<IncomingMessage>((resolve, reject) => {
      request.once("response", resolve)
      request.once("error", (error) => {
        reject(new ApiTransportError(path, { cause: error }))
      })
    })
    return { request, response }
  }

  async replaceSource(sourceId: string, filePath: string): Promise<ReplaceSourceResult> {
    const metadata = await stat(filePath)
    const boundary = `anydatas-${randomUUID()}`
    const safeFilename = basename(filePath).replace(/["\r\n]/g, "_")
    const prefix = Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${safeFilename}"\r\nContent-Type: application/octet-stream\r\n\r\n`,
    )
    const suffix = Buffer.from(`\r\n--${boundary}--\r\n`)
    const path = `/api/data-sources/${encodeURIComponent(sourceId)}/replace`
    const pending = this.#open(path, "POST", {
      "content-type": `multipart/form-data; boundary=${boundary}`,
      "content-length": prefix.length + metadata.size + suffix.length,
    })
    pending.request.write(prefix)
    const upload = pipeline(createReadStream(filePath), pending.request, { end: false }).then(() => {
      pending.request.end(suffix)
    })
    const [response] = await Promise.all([pending.response, upload])
    await requireSuccess(response, path)
    const result = rowCountSchema.safeParse(await readJson(response, path))
    if (!result.success) {
      throw new ApiResponseError(path, "rowCount is missing or invalid")
    }
    return { rowCount: result.data.rowCount }
  }

  async runSchedule(scheduleId: string): Promise<void> {
    const path = `/api/schedules/${encodeURIComponent(scheduleId)}/run`
    const pending = this.#open(path, "POST", { "content-length": 0 })
    pending.request.end()
    const response = await pending.response
    await requireSuccess(response, path)
    response.resume()
  }
}
