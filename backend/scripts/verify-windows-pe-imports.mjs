#!/usr/bin/env node

import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { pathToFileURL } from "node:url"

const FORBIDDEN_RUNTIME_DLL = /^(?:msvcp|vcruntime)\d+(?:_\d+)?\.dll$/iu

/**
 * 在验证失败时抛出带文件上下文的错误。
 *
 * 集中生成错误可以让本地和 CI 输出保持一致，定位损坏或非 x64 PE 文件时更直接。
 */
function invalidPe(message) {
  throw new Error(`invalid Windows x64 PE: ${message}`)
}

/**
 * 从 PE 字符串表读取以 NUL 结尾的 DLL 名称。
 *
 * 设置长度上限可以避免损坏文件导致无界扫描，同时足以覆盖 Windows DLL 路径。
 */
function readCString(bytes, offset) {
  if (offset < 0 || offset >= bytes.length) invalidPe("string offset is outside the file")
  const endLimit = Math.min(bytes.length, offset + 260)
  let end = offset
  while (end < endLimit && bytes[end] !== 0) end += 1
  if (end === endLimit) invalidPe("unterminated import name")
  return bytes.toString("ascii", offset, end)
}

/**
 * 将 PE 的内存 RVA 映射到文件偏移。
 *
 * 按节表转换而不是搜索可见字符串，能确保检查的确是 Loader 使用的导入项，避免误报静态库文本。
 */
function rvaToOffset(rva, sections) {
  for (const section of sections) {
    const size = Math.max(section.virtualSize, section.rawSize)
    if (rva >= section.virtualAddress && rva < section.virtualAddress + size) {
      return section.rawPointer + (rva - section.virtualAddress)
    }
  }
  invalidPe(`RVA 0x${rva.toString(16)} is not mapped by a section`)
}

/**
 * 解析 64 位 PE 导入目录并返回 Windows Loader 会加载的 DLL。
 *
 * 使用零依赖解析器让同一检查可在 macOS 本地和 GitHub Windows Runner 上运行，不依赖 dumpbin 环境。
 */
export function importedDlls(bytes) {
  if (bytes.length < 0x40 || bytes.toString("ascii", 0, 2) !== "MZ") {
    invalidPe("missing MZ header")
  }
  const peOffset = bytes.readUInt32LE(0x3c)
  if (peOffset + 24 > bytes.length || bytes.toString("ascii", peOffset, peOffset + 4) !== "PE\0\0") {
    invalidPe("missing PE signature")
  }

  const coffOffset = peOffset + 4
  const sectionCount = bytes.readUInt16LE(coffOffset + 2)
  const optionalSize = bytes.readUInt16LE(coffOffset + 16)
  const optionalOffset = coffOffset + 20
  if (optionalOffset + optionalSize > bytes.length || bytes.readUInt16LE(optionalOffset) !== 0x20b) {
    invalidPe("expected PE32+ optional header")
  }

  const importDirectoryOffset = optionalOffset + 120
  if (importDirectoryOffset + 8 > optionalOffset + optionalSize) {
    invalidPe("missing import data directory")
  }
  const importRva = bytes.readUInt32LE(importDirectoryOffset)
  const importSize = bytes.readUInt32LE(importDirectoryOffset + 4)
  if (importRva === 0 || importSize < 20) invalidPe("empty import directory")

  const sectionTableOffset = optionalOffset + optionalSize
  const sections = []
  for (let index = 0; index < sectionCount; index += 1) {
    const offset = sectionTableOffset + index * 40
    if (offset + 40 > bytes.length) invalidPe("truncated section table")
    sections.push({
      virtualSize: bytes.readUInt32LE(offset + 8),
      virtualAddress: bytes.readUInt32LE(offset + 12),
      rawSize: bytes.readUInt32LE(offset + 16),
      rawPointer: bytes.readUInt32LE(offset + 20),
    })
  }

  const imports = []
  const importOffset = rvaToOffset(importRva, sections)
  const descriptorLimit = Math.min(Math.floor(importSize / 20) + 1, 4_096)
  for (let index = 0; index < descriptorLimit; index += 1) {
    const offset = importOffset + index * 20
    if (offset + 20 > bytes.length) invalidPe("truncated import descriptor")
    const fields = Array.from({ length: 5 }, (_, field) => bytes.readUInt32LE(offset + field * 4))
    if (fields.every((value) => value === 0)) return imports
    const nameRva = fields[3]
    if (nameRva === undefined || nameRva === 0) invalidPe("import descriptor has no name")
    imports.push(readCString(bytes, rvaToOffset(nameRva, sections)))
  }
  invalidPe("import descriptor terminator was not found")
}

/**
 * 验证发行 EXE 不依赖需要用户另行安装的 MSVC C/C++ Runtime。
 *
 * Rust 与预编译 DuckDB 都使用 static CRT 后，程序可在干净 Windows 系统直接启动，避免 0xC0000135。
 */
async function main() {
  const binaryPath = process.argv[2]
  if (binaryPath === undefined) throw new Error("Windows server binary path is required")
  const imports = importedDlls(await readFile(resolve(binaryPath)))
  const forbidden = imports.filter((name) => FORBIDDEN_RUNTIME_DLL.test(name))
  if (forbidden.length > 0) {
    throw new Error(`Windows server dynamically imports VC++ Runtime: ${forbidden.join(", ")}`)
  }
  process.stdout.write(`Windows PE runtime imports verified: ${imports.join(", ")}\n`)
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main()
}
