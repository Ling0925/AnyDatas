#!/usr/bin/env node
import { readFile } from "node:fs/promises"

const tag = process.argv[2]
const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
)
const expected = `desktop-v${packageJson.version}`

if (tag !== expected) {
  throw new Error(`桌面发行 Tag 与 package.json 版本不一致：expected=${expected} actual=${tag ?? "<missing>"}`)
}

console.log(`desktop release tag verified: ${tag}`)
