type FieldBounds = {
  readonly name: string
  readonly minimum: number
  readonly maximum: number
  readonly weekday: boolean
}

type ParsedField = {
  readonly values: ReadonlySet<number>
  readonly wildcard: boolean
}

type LocalDateParts = {
  readonly minute: number
  readonly hour: number
  readonly day: number
  readonly month: number
  readonly weekday: number
}

type NumericRange = {
  readonly start: number
  readonly end: number
  readonly step: number
}

const WEEKDAYS: Readonly<Record<string, number>> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
}

export class CronExpressionError extends Error {
  override readonly name = "CronExpressionError"

  constructor(readonly expression: string, message: string) {
    super(`Invalid cron expression "${expression}": ${message}`)
  }
}

export class CronTimezoneError extends Error {
  override readonly name = "CronTimezoneError"

  constructor(readonly timezone: string, options?: ErrorOptions) {
    super(`Invalid or unsupported timezone "${timezone}"`, options)
  }
}

function parseInteger(expression: string, text: string, bounds: FieldBounds): number {
  if (!/^\d+$/.test(text)) {
    throw new CronExpressionError(expression, `${bounds.name} contains "${text}"`)
  }
  const value = Number(text)
  if (value < bounds.minimum || value > bounds.maximum) {
    throw new CronExpressionError(
      expression,
      `${bounds.name} must be between ${bounds.minimum} and ${bounds.maximum}`,
    )
  }
  return value
}

function addRange(
  values: Set<number>,
  range: NumericRange,
  weekday: boolean,
): void {
  for (let value = range.start; value <= range.end; value += range.step) {
    values.add(weekday && value === 7 ? 0 : value)
  }
}

function parseSegment(
  expression: string,
  segment: string,
  bounds: FieldBounds,
): ReadonlySet<number> {
  const values = new Set<number>()
  const slashParts = segment.split("/")
  if (slashParts.length > 2) {
    throw new CronExpressionError(expression, `${bounds.name} has too many step separators`)
  }
  const [base = "", stepText] = slashParts
  const step = stepText === undefined ? 1 : parseInteger(expression, stepText, {
    name: `${bounds.name} step`,
    minimum: 1,
    maximum: bounds.maximum - bounds.minimum + 1,
    weekday: false,
  })

  if (base === "*") {
    addRange(values, { start: bounds.minimum, end: bounds.maximum, step }, bounds.weekday)
    return values
  }

  const rangeParts = base.split("-")
  if (rangeParts.length > 2) {
    throw new CronExpressionError(expression, `${bounds.name} has too many range separators`)
  }
  const [startText = "", endText] = rangeParts
  const start = parseInteger(expression, startText, bounds)
  const end = endText === undefined ? (stepText === undefined ? start : bounds.maximum) : parseInteger(expression, endText, bounds)
  if (start > end) {
    throw new CronExpressionError(expression, `${bounds.name} range starts after it ends`)
  }
  addRange(values, { start, end, step }, bounds.weekday)
  return values
}

function parseField(expression: string, text: string, bounds: FieldBounds): ParsedField {
  if (text.length === 0) {
    throw new CronExpressionError(expression, `${bounds.name} is empty`)
  }
  const values = new Set<number>()
  for (const segment of text.split(",")) {
    for (const value of parseSegment(expression, segment, bounds)) {
      values.add(value)
    }
  }
  return { values, wildcard: text.startsWith("*") }
}

function localDateParts(date: Date, timezone: string): LocalDateParts {
  let parts: Intl.DateTimeFormatPart[]
  try {
    parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      minute: "2-digit",
      hour: "2-digit",
      day: "2-digit",
      month: "2-digit",
      weekday: "short",
      hourCycle: "h23",
    }).formatToParts(date)
  } catch (error) {
    if (error instanceof RangeError) {
      throw new CronTimezoneError(timezone, { cause: error })
    }
    throw error
  }

  const part = (type: Intl.DateTimeFormatPartTypes): string => {
    const value = parts.find((candidate) => candidate.type === type)?.value
    if (value === undefined) {
      throw new CronTimezoneError(timezone)
    }
    return value
  }
  const weekdayText = part("weekday")
  const weekday = WEEKDAYS[weekdayText]
  if (weekday === undefined) {
    throw new CronTimezoneError(timezone)
  }
  return {
    minute: Number(part("minute")),
    hour: Number(part("hour")),
    day: Number(part("day")),
    month: Number(part("month")),
    weekday,
  }
}

export function cronMatches(expression: string, date: Date, timezone: string): boolean {
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5) {
    throw new CronExpressionError(expression, "expected five fields")
  }
  const [minuteText = "", hourText = "", dayText = "", monthText = "", weekdayText = ""] = fields
  const minute = parseField(expression, minuteText, { name: "minute", minimum: 0, maximum: 59, weekday: false })
  const hour = parseField(expression, hourText, { name: "hour", minimum: 0, maximum: 23, weekday: false })
  const day = parseField(expression, dayText, { name: "day", minimum: 1, maximum: 31, weekday: false })
  const month = parseField(expression, monthText, { name: "month", minimum: 1, maximum: 12, weekday: false })
  const weekday = parseField(expression, weekdayText, { name: "weekday", minimum: 0, maximum: 7, weekday: true })
  const local = localDateParts(date, timezone)
  const dayMatches = day.values.has(local.day)
  const weekdayMatches = weekday.values.has(local.weekday)
  const calendarDayMatches = day.wildcard
    ? weekdayMatches
    : weekday.wildcard
      ? dayMatches
      : dayMatches || weekdayMatches

  return (
    minute.values.has(local.minute) &&
    hour.values.has(local.hour) &&
    month.values.has(local.month) &&
    calendarDayMatches
  )
}
