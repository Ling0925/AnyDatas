import { createServer } from "node:http"
import type { Server } from "node:http"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { ApiRequestError, LocalApiClient } from "./api-client.js"

class TestServerStateError extends Error {
  override readonly name = "TestServerStateError"
}

async function listen(server: Server): Promise<number> {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (address === null || typeof address === "string") {
    throw new TestServerStateError("test server did not expose a TCP address")
  }
  return address.port
}

async function close(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error === undefined ? resolve() : reject(error)))
  })
}

describe("LocalApiClient", () => {
  let directory = ""
  let server: Server
  let client: LocalApiClient
  let receivedBody = Buffer.alloc(0)
  let receivedLength = ""
  const paths: string[] = []

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "anydatas-api-client-"))
    receivedBody = Buffer.alloc(0)
    receivedLength = ""
    paths.length = 0
    server = createServer((request, response) => {
      paths.push(request.url ?? "")
      if (request.url?.includes("/replace") === true) {
        const chunks: Buffer[] = []
        request.on("data", (chunk: Buffer) => chunks.push(chunk))
        request.on("end", () => {
          receivedBody = Buffer.concat(chunks)
          receivedLength = request.headers["content-length"] ?? ""
          response.setHeader("content-type", "application/json")
          response.end(JSON.stringify({ id: "source-1", rowCount: 42, ignored: true }))
        })
        return
      }
      if (request.url === "/api/schedules/failing/run") {
        response.writeHead(500)
        response.end("failed")
        return
      }
      if (request.url === "/api/schedules/schema/run") {
        response.writeHead(400, { "content-type": "application/json" })
        response.end(JSON.stringify({ error: { message: "字段结构与现有数据源不一致" } }))
        return
      }
      if (request.url === "/api/schedules/malformed/run") {
        response.writeHead(400, { "content-type": "application/json" })
        response.end("{broken")
        return
      }
      if (request.url === "/api/schedules/oversized/run") {
        response.writeHead(400, { "content-type": "application/json" })
        response.end(Buffer.alloc(1024 * 1024 + 1, 120))
        return
      }
      response.writeHead(204)
      response.end()
    })
    const port = await listen(server)
    client = new LocalApiClient({ baseUrl: new URL(`http://127.0.0.1:${port}`) })
  })

  afterEach(async () => {
    await close(server)
    await rm(directory, { recursive: true, force: true })
  })

  it("streams a length-delimited multipart replacement and parses rowCount", async () => {
    // Given
    const fileContent = Buffer.alloc(3 * 1024 * 1024, 19)
    const filePath = join(directory, "daily export.csv")
    await writeFile(filePath, fileContent)

    // When
    const result = await client.replaceSource("source/1", filePath)

    // Then
    expect(result).toEqual({ rowCount: 42 })
    expect(Number(receivedLength)).toBe(receivedBody.length)
    expect(receivedBody.indexOf(fileContent)).toBeGreaterThan(0)
    expect(paths).toEqual(["/api/data-sources/source%2F1/replace"])
  })

  it("posts a schedule run-now request", async () => {
    // Given
    const scheduleId = "schedule/1"

    // When
    await client.runSchedule(scheduleId)

    // Then
    expect(paths).toEqual(["/api/schedules/schedule%2F1/run"])
  })

  it("returns a typed error for a failed schedule request", async () => {
    // Given
    const scheduleId = "failing"

    // When
    const run = client.runSchedule(scheduleId)

    // Then
    await expect(run).rejects.toBeInstanceOf(ApiRequestError)
  })

  it("includes the bounded AnyDatas error message for a schema-mismatch response", async () => {
    // Given
    const scheduleId = "schema"

    // When
    const run = client.runSchedule(scheduleId)

    // Then
    await expect(run).rejects.toMatchObject({
      statusCode: 400,
      serverMessage: "字段结构与现有数据源不一致",
      message: "Local API request failed with status 400: 字段结构与现有数据源不一致",
    })
  })

  it.each(["malformed", "oversized"])(
    "uses a safe generic error for a %s error body",
    async (scheduleId) => {
      // Given
      const expectedMessage = "Local API request failed with status 400"

      // When
      const run = client.runSchedule(scheduleId)

      // Then
      await expect(run).rejects.toMatchObject({
        statusCode: 400,
        serverMessage: null,
        message: expectedMessage,
      })
    },
  )
})
