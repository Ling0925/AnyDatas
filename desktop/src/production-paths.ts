import { fileURLToPath } from "node:url"
import { join } from "node:path"

export type ProductionPathOptions = {
  readonly isPackaged: boolean
  readonly resourcesPath: string
  readonly moduleUrl: string
}

/**
 * 返回生产 Vue 文件所在目录。
 *
 * 安装包显式使用 Electron resources 根目录，开发构建继续相对 main.js 定位，避免 app.asar 层级变化导致白屏。
 */
export function productionFrontendDirectory(options: ProductionPathOptions): string {
  if (options.isPackaged) {
    return join(options.resourcesPath, "frontend", "dist")
  }
  return fileURLToPath(new URL("../../frontend/dist/", options.moduleUrl))
}
