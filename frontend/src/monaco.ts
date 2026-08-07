import type { Component } from 'vue'

let sqlEditorPromise: Promise<Component> | null = null

/**
 * 首次真正显示 SQL 编辑器时才加载 Monaco、SQL 语言和 Worker。
 *
 * 全局复用同一个 Promise 可以避免工作台和任务弹窗同时挂载时重复初始化，
 * 也让登录页、Agent 聊天页完全不承担编辑器下载成本。
 */
export function loadSqlEditor(): Promise<Component> {
  if (sqlEditorPromise) return sqlEditorPromise
  sqlEditorPromise = (async () => {
    const [{ VueMonacoEditor, loader }, monaco, workerModule] = await Promise.all([
      import('@guolao/vue-monaco-editor'),
      import('monaco-editor/esm/vs/editor/editor.api.js'),
      import('monaco-editor/esm/vs/editor/editor.worker?worker'),
      import('monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js'),
      import('monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js'),
    ])
    const environment = self as typeof self & {
      MonacoEnvironment?: { getWorker: () => Worker }
    }
    environment.MonacoEnvironment ??= {
      getWorker() {
        return new workerModule.default()
      },
    }
    loader.config({ monaco })
    return VueMonacoEditor
  })()
  return sqlEditorPromise
}
