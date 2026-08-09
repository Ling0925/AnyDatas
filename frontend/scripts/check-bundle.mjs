import { readFile, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const manifestPath = resolve(root, 'dist/.vite/manifest.json')
const manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
const entry = Object.values(manifest).find((chunk) => chunk.isEntry)

if (!entry) {
  throw new Error('Vite manifest does not contain an application entry.')
}

/**
 * 只遍历静态 imports；dynamicImports 是用户进入对应页面或功能后才下载的分块，
 * 将二者分开计算才能真正约束首屏，而不会误罚按需加载的 Monaco 和 ECharts。
 */
function collectStaticChunks(initialChunk) {
  const chunks = new Map()

  function visit(chunk) {
    if (chunks.has(chunk.file)) return
    chunks.set(chunk.file, chunk)
    for (const importedKey of chunk.imports ?? []) {
      const imported = manifest[importedKey]
      if (!imported) throw new Error(`Vite manifest import is missing: ${importedKey}`)
      visit(imported)
    }
  }

  visit(initialChunk)
  return [...chunks.values()]
}

/** 汇总构建文件原始字节数，CI 使用稳定上限及时发现全量依赖回流入口。 */
async function totalBytes(files) {
  let bytes = 0
  for (const file of files) {
    bytes += (await stat(resolve(root, 'dist', file))).size
  }
  return bytes
}

const staticChunks = collectStaticChunks(entry)
const initialJavaScript = staticChunks.map((chunk) => chunk.file)
const initialCss = [...new Set(staticChunks.flatMap((chunk) => chunk.css ?? []))]
const jsBytes = await totalBytes(initialJavaScript)
const cssBytes = await totalBytes(initialCss)
const forbiddenInitialChunks = initialJavaScript.filter((file) => (
  file.includes('monaco-editor') || file.includes('echarts')
))

if (forbiddenInitialChunks.length) {
  throw new Error(`Heavy runtime leaked into initial load: ${forbiddenInitialChunks.join(', ')}`)
}
if (jsBytes > 350 * 1024) {
  throw new Error(`Initial JavaScript exceeds 350 KiB: ${Math.ceil(jsBytes / 1024)} KiB`)
}
if (cssBytes > 180 * 1024) {
  throw new Error(`Initial CSS exceeds 180 KiB: ${Math.ceil(cssBytes / 1024)} KiB`)
}

/** Electron 以 file:// 加载 dist/index.html，根绝对路径会解析成 file:///... 而找不到资源。 */
const html = await readFile(resolve(root, 'dist/index.html'), 'utf8')
const rootAbsoluteUrls = [...html.matchAll(/(?:src|href)=(["'])(.+?)\1/g)]
  .map((match) => match[2])
  .filter((url) => /^\/[^/]/.test(url))

if (rootAbsoluteUrls.length) {
  throw new Error(
    `Root-absolute local URLs break Electron loadFile: ${rootAbsoluteUrls.join(', ')}`,
  )
}

console.log(
  `Initial bundle: ${Math.ceil(jsBytes / 1024)} KiB JS, `
  + `${Math.ceil(cssBytes / 1024)} KiB CSS; Monaco and ECharts remain lazy.`,
)
