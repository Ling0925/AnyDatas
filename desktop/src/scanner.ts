import { createHash } from "node:crypto"
import { createReadStream } from "node:fs"
import { readdir, stat } from "node:fs/promises"
import { join } from "node:path"
import { pipeline } from "node:stream/promises"

export type ScannedFile = {
  readonly path: string
  readonly name: string
  readonly mtimeMs: number
  readonly size: number
  readonly sha256: string
}

export type ScanResult =
  | { readonly kind: "found"; readonly file: ScannedFile }
  | { readonly kind: "no_match" }
  | { readonly kind: "unreadable"; readonly error: ScannerAccessError }

type FileCandidate = {
  readonly path: string
  readonly name: string
  readonly mtimeMs: number
  readonly size: number
}

export class ScannerAccessError extends Error {
  override readonly name = "ScannerAccessError"

  constructor(readonly path: string, readonly operation: "scan" | "hash", options?: ErrorOptions) {
    super(`Unable to ${operation} "${path}"`, options)
  }
}

export class ScannerPatternError extends Error {
  override readonly name = "ScannerPatternError"

  constructor(readonly pattern: string, message: string) {
    super(`Invalid file-name pattern "${pattern}": ${message}`)
  }
}

/**
 * Compiles an MVP file-name glob. `*` and `?` never cross directories;
 * recursive `**` patterns are deliberately unsupported.
 */
function compilePattern(pattern: string): RegExp {
  if (pattern.length === 0) {
    throw new ScannerPatternError(pattern, "pattern must not be empty")
  }
  if (pattern.includes("/") || pattern.includes("\\")) {
    throw new ScannerPatternError(pattern, "directory separators are not supported")
  }
  if (pattern.includes("**")) {
    throw new ScannerPatternError(pattern, "recursive ** matching is not supported")
  }

  let source = "^"
  for (const character of pattern) {
    if (character === "*") {
      source += ".*"
    } else if (character === "?") {
      source += "."
    } else {
      source += character.replace(/[|\\{}()[\]^$+*.?]/g, "\\$&")
    }
  }
  return new RegExp(`${source}$`, "u")
}

async function hashFile(path: string): Promise<string> {
  const hash = createHash("sha256")
  await pipeline(createReadStream(path), hash)
  return hash.digest("hex")
}

export async function scanNewestFile(directory: string, pattern: string): Promise<ScanResult> {
  const matcher = compilePattern(pattern)
  const candidates: FileCandidate[] = []

  try {
    const entries = await readdir(directory, { withFileTypes: true })
    for (const entry of entries) {
      if (!entry.isFile() || !matcher.test(entry.name)) {
        continue
      }
      const path = join(directory, entry.name)
      const metadata = await stat(path)
      if (metadata.isFile()) {
        candidates.push({ path, name: entry.name, mtimeMs: metadata.mtimeMs, size: metadata.size })
      }
    }
  } catch (error) {
    return {
      kind: "unreadable",
      error: new ScannerAccessError(directory, "scan", { cause: error }),
    }
  }

  candidates.sort((left, right) => {
    const timeOrder = right.mtimeMs - left.mtimeMs
    if (timeOrder !== 0) {
      return timeOrder
    }
    return left.name < right.name ? -1 : left.name > right.name ? 1 : 0
  })
  const [selected] = candidates
  if (selected === undefined) {
    return { kind: "no_match" }
  }

  try {
    const sha256 = await hashFile(selected.path)
    return { kind: "found", file: { ...selected, sha256 } }
  } catch (error) {
    return {
      kind: "unreadable",
      error: new ScannerAccessError(selected.path, "hash", { cause: error }),
    }
  }
}
