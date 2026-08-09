import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { productionFrontendDirectory } from "./production-paths.js"

describe("productionFrontendDirectory", () => {
  it("安装包从 resources 读取前端，避免依赖源码目录", () => {
    expect(productionFrontendDirectory({
      isPackaged: true,
      resourcesPath: join("", "opt", "AnyDatas", "resources"),
      moduleUrl: "file:///ignored/app.asar/dist/main.js",
    })).toBe(join("", "opt", "AnyDatas", "resources", "frontend", "dist"))
  })

  it("开发构建仍从 desktop/dist 相对定位 frontend/dist", () => {
    expect(productionFrontendDirectory({
      isPackaged: false,
      resourcesPath: "/ignored",
      moduleUrl: "file:///workspace/desktop/dist/main.js",
    })).toBe("/workspace/frontend/dist/")
  })
})
