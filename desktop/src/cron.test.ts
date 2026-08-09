import { describe, expect, it } from "vitest"
import { CronExpressionError, cronMatches } from "./cron.js"

describe("cronMatches", () => {
  it("matches every minute when all fields are wildcards", () => {
    // Given
    const date = new Date("2026-08-09T05:47:00Z")

    // When
    const matches = cronMatches("* * * * *", date, "UTC")

    // Then
    expect(matches).toBe(true)
  })

  it("matches an exact local time", () => {
    // Given
    const date = new Date("2026-08-09T08:30:00Z")

    // When
    const matches = cronMatches("30 8 9 8 0", date, "UTC")

    // Then
    expect(matches).toBe(true)
  })

  it("supports lists, ranges, and steps", () => {
    // Given
    const date = new Date("2026-01-05T08:30:00Z")

    // When
    const matches = cronMatches("*/15 8-10 * 1,6 1-5", date, "UTC")

    // Then
    expect(matches).toBe(true)
  })

  it("treats weekday seven as Sunday", () => {
    // Given
    const date = new Date("2026-08-09T00:00:00Z")

    // When
    const matches = cronMatches("0 0 * * 7", date, "UTC")

    // Then
    expect(matches).toBe(true)
  })

  it("evaluates date fields in the requested timezone", () => {
    // Given
    const date = new Date("2026-01-01T00:00:00Z")

    // When
    const utcMatches = cronMatches("0 8 * * *", date, "UTC")
    const shanghaiMatches = cronMatches("0 8 * * *", date, "Asia/Shanghai")

    // Then
    expect({ utcMatches, shanghaiMatches }).toEqual({
      utcMatches: false,
      shanghaiMatches: true,
    })
  })

  it("rejects malformed and out-of-range fields with a typed error", () => {
    // Given
    const date = new Date("2026-01-01T00:00:00Z")

    // When
    const matchInvalidCron = (): boolean => cronMatches("60 8 * * *", date, "UTC")

    // Then
    expect(matchInvalidCron).toThrowError(CronExpressionError)
  })
})
