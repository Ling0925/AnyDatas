#!/usr/bin/env node
import { createHash } from "node:crypto"
import { spawn } from "node:child_process"
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { basename, join } from "node:path"
import process from "node:process"
import { fileURLToPath } from "node:url"

const releaseDirectory = fileURLToPath(new URL("../release/", import.meta.url))
const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
)
const installerName = `AnyDatas-Setup-${packageJson.version}-x64.exe`
const installerPath = join(releaseDirectory, installerName)
const unpackedExecutable = join(releaseDirectory, "win-unpacked", "AnyDatas.exe")
const appArchive = join(releaseDirectory, "win-unpacked", "resources", "app.asar")
const frontendDirectory = join(
  releaseDirectory,
  "win-unpacked",
  "resources",
  "frontend",
  "dist",
)
const frontendIndex = join(frontendDirectory, "index.html")

/**
 * 校验 Windows PE 的 MZ 文件头和最小体积。
 *
 * 同时检查格式与大小可以发现错误上传的 HTML、空文件或被截断的构建产物。
 */
async function verifyPe(path, minimumBytes) {
  const file = await readFile(path)
  if (file.byteLength < minimumBytes || file[0] !== 0x4d || file[1] !== 0x5a) {
    throw new Error(`不是有效的 Windows PE 产物：${path}`)
  }
  return file
}

/**
 * 确认打包后的 Vue 入口及静态资产都位于 Electron resources 下。
 *
 * 这能在发布前拦截“安装成功但启动后白屏”的资源路径回归。
 */
async function verifyFrontend() {
  const index = await readFile(frontendIndex, "utf8")
  if (!index.includes('id="app"')) {
    throw new Error(`前端入口缺少 Vue 挂载点：${frontendIndex}`)
  }
  const assetNames = await readdir(join(frontendDirectory, "assets"))
  if (!assetNames.some((name) => name.endsWith(".js"))) {
    throw new Error("打包后的前端缺少 JavaScript 资产")
  }
}

/**
 * 用独立用户目录启动未压缩的真实桌面程序，并要求其在超时前正常退出。
 *
 * 冒烟模式会在窗口 ready-to-show 后退出，因此能够覆盖 app.asar、preload 和 file:// 页面加载。
 */
async function runSmoke(executable) {
  if (process.platform !== "win32" || !process.argv.includes("--smoke")) {
    return
  }
  const userData = await mkdtemp(join(tmpdir(), "anydatas-desktop-smoke-"))
  try {
    await new Promise((resolve, reject) => {
      const child = spawn(executable, [`--user-data-dir=${userData}`], {
        env: { ...process.env, ANYDATAS_ELECTRON_SMOKE: "1" },
        stdio: "inherit",
        shell: false,
      })
      const timer = setTimeout(() => {
        child.kill()
        reject(new Error("打包后的桌面程序未在 30 秒内完成冒烟启动"))
      }, 30_000)
      child.once("error", (error) => {
        clearTimeout(timer)
        reject(error)
      })
      child.once("exit", (code, signal) => {
        clearTimeout(timer)
        if (code === 0) {
          resolve()
          return
        }
        reject(new Error(`打包后的桌面程序退出异常：code=${code} signal=${signal}`))
      })
    })
  } finally {
    await rm(userData, { recursive: true, force: true })
  }
}

const installer = await verifyPe(installerPath, 10 * 1024 * 1024)
await verifyPe(unpackedExecutable, 10 * 1024 * 1024)
if ((await stat(appArchive)).size < 1_024) {
  throw new Error(`Electron app.asar 体积异常：${appArchive}`)
}
await verifyFrontend()
await runSmoke(unpackedExecutable)

const sha256 = createHash("sha256").update(installer).digest("hex")
const checksumPath = `${installerPath}.sha256`
await writeFile(checksumPath, `${sha256}  ${basename(installerPath)}\n`, "utf8")
console.log(JSON.stringify({ installer: installerName, bytes: installer.byteLength, sha256 }))
