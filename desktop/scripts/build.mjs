import { mkdir, readdir, rm } from "node:fs/promises"
import { basename, join } from "node:path"
import { fileURLToPath } from "node:url"
import { build } from "esbuild"

// fileURLToPath 会按当前平台去掉 Windows file URL 的前导斜杠，避免 esbuild 收到 `\\D:\\...` 非法路径。
const sourceDirectory = fileURLToPath(new URL("../src/", import.meta.url))
const outputDirectory = fileURLToPath(new URL("../dist/", import.meta.url))
const runtimeNames = new Set(["main.ts", "preload.ts"])

let sourceNames = []
try {
  sourceNames = await readdir(sourceDirectory)
} catch (error) {
  if (error instanceof Error && "code" in error && error.code === "ENOENT") {
    process.exit(0)
  }
  throw error
}

const runtimeEntries = sourceNames.filter((name) => runtimeNames.has(name))
const coreEntries = sourceNames.filter(
  (name) => name.endsWith(".ts") && !name.endsWith(".test.ts") && !runtimeNames.has(name),
)
await rm(outputDirectory, { recursive: true, force: true })
await mkdir(outputDirectory, { recursive: true })

if (runtimeEntries.includes("main.ts")) {
  await build({
    entryPoints: { main: join(sourceDirectory, "main.ts") },
    outdir: outputDirectory,
    bundle: true,
    format: "esm",
    platform: "node",
    target: "node24",
    sourcemap: true,
    external: ["electron"],
  })
}

if (runtimeEntries.includes("preload.ts")) {
  await build({
    entryPoints: [{ in: join(sourceDirectory, "preload.ts"), out: "preload" }],
    outdir: outputDirectory,
    outExtension: { ".js": ".cjs" },
    bundle: true,
    format: "cjs",
    platform: "node",
    target: "node24",
    sourcemap: true,
    external: ["electron"],
  })
}

if (runtimeEntries.length === 0 && coreEntries.length > 0) {
  await build({
    entryPoints: Object.fromEntries(
      coreEntries.map((name) => [basename(name, ".ts"), join(sourceDirectory, name)]),
    ),
    outdir: outputDirectory,
    bundle: true,
    format: "esm",
    platform: "node",
    target: "node24",
    sourcemap: true,
  })
}
