#!/usr/bin/env node
import { spawn } from "node:child_process"
import process from "node:process"

const pnpmCli = process.env["npm_execpath"]
if (pnpmCli === undefined) {
  throw new Error("必须通过 pnpm run 调用桌面打包脚本")
}
const mode = process.argv[2]
const builderArguments = mode === "win"
  ? ["exec", "electron-builder", "--win", "nsis", "--x64", "--publish", "never"]
  : mode === "win-dir"
    ? ["exec", "electron-builder", "--win", "dir", "--x64", "--publish", "never"]
    : null

if (builderArguments === null) {
  throw new Error("用法：node scripts/package.mjs <win|win-dir>")
}

/**
 * 以参数数组启动构建命令并继承终端输出。
 *
 * 不经过 shell 可以让 Windows 与 Unix 使用同一条安全路径，也避免文件路径被二次解释。
 */
function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: "inherit", shell: false })
    child.once("error", reject)
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolve()
        return
      }
      reject(new Error(`${command} ${args.join(" ")} 失败：code=${code} signal=${signal}`))
    })
  })
}

const desktopDirectory = new URL("../", import.meta.url)
const frontendDirectory = new URL("../../frontend/", import.meta.url)

// 先产出 Vue 静态资源，再构建 Electron 主进程，保证安装包不会混入上一次构建的陈旧文件。
await run(process.execPath, [pnpmCli, "run", "build"], frontendDirectory)
await run(process.execPath, [pnpmCli, "run", "build"], desktopDirectory)
await run(process.execPath, [pnpmCli, ...builderArguments], desktopDirectory)
