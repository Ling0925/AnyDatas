#!/usr/bin/env node

import { createHash } from "node:crypto"
import { readdir, readFile, stat, writeFile } from "node:fs/promises"
import { basename, join, resolve } from "node:path"

const ASSET_PREFIX = "anydatas-server-"

/**
 * 读取 Cargo 包版本，确保 Git 标签、握手版本和发行清单来自同一个事实来源。
 *
 * 这样做的好处是服务端拆到独立仓库后仍不会因为手工复制版本号而发布不兼容资产。
 */
async function cargoVersion(cargoTomlPath) {
  const content = await readFile(cargoTomlPath, "utf8")
  const packageSection = content.match(/\[package\]([\s\S]*?)(?:\n\[|$)/u)?.[1] ?? ""
  const version = packageSection.match(/^version\s*=\s*"([^"]+)"\s*$/mu)?.[1]
  if (version === undefined) {
    throw new Error(`cannot read package version from ${cargoTomlPath}`)
  }
  return version
}

/**
 * 对原生二进制逐字节计算 SHA-256，桌面端会在赋予执行权限前复算并比对。
 *
 * 流水线生成清单可以避免人工粘贴摘要，同时让下载损坏在启动前被确定性拦截。
 */
async function assetMetadata(directory, fileName) {
  const path = join(directory, fileName)
  const content = await readFile(path)
  const details = await stat(path)
  return {
    name: fileName,
    sha256: createHash("sha256").update(content).digest("hex"),
    size: details.size,
  }
}

/**
 * 从固定命名的跨平台二进制生成稳定清单，平台键直接对应 Electron 的 platform/arch。
 *
 * 严格拒绝未知或重复资产的好处是工作流配置漂移时发行会失败，而不是让用户下载错误架构。
 */
async function buildManifest(directory, tag) {
  const cargoTomlPath = resolve("backend/Cargo.toml")
  const serverVersion = await cargoVersion(cargoTomlPath)
  const expectedTag = `server-v${serverVersion}`
  if (tag !== expectedTag) {
    throw new Error(`release tag ${tag} does not match Cargo version ${expectedTag}`)
  }

  const names = (await readdir(directory))
    .filter((name) => name.startsWith(ASSET_PREFIX) && name !== "anydatas-server-manifest.json")
    .sort()
  const expectedNames = [
    "anydatas-server-linux-x64",
    "anydatas-server-macos-arm64",
    "anydatas-server-macos-x64",
    "anydatas-server-windows-x64.exe",
  ]
  if (JSON.stringify(names) !== JSON.stringify(expectedNames)) {
    throw new Error(`release assets mismatch: got ${names.join(", ")}`)
  }

  const assets = {}
  for (const name of names) {
    const platformKey = basename(name)
      .slice(ASSET_PREFIX.length)
      .replace(/\.exe$/u, "")
    assets[platformKey] = await assetMetadata(directory, name)
  }
  return {
    schemaVersion: 1,
    serverVersion,
    protocolVersion: 1,
    tag,
    assets,
  }
}

/**
 * 生成机器可读的 Release 资产清单，并使用换行结尾方便 Git 与命令行审阅。
 *
 * 主函数只接收显式目录和标签，避免在本地或 CI 中意外扫描其他构建产物。
 */
async function main() {
  const directory = resolve(process.argv[2] ?? "dist/server-release")
  const tag = process.argv[3] ?? process.env.GITHUB_REF_NAME
  if (tag === undefined || tag.length === 0) {
    throw new Error("release tag is required")
  }
  const manifest = await buildManifest(directory, tag)
  const output = join(directory, "anydatas-server-manifest.json")
  await writeFile(output, `${JSON.stringify(manifest, null, 2)}\n`, "utf8")
  process.stdout.write(`${output}\n`)
}

await main()
