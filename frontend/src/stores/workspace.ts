import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '../api'
import type {
  DataSource,
  ImportInspection,
  InspectImportTablePayload,
  ImportTableConfig,
  PreviewResponse,
  QueryResponse,
  QueryTableBinding,
  SavedQuery,
  SourceTable,
  SourceTablePayload,
} from '../types'

const DEFAULT_SQL = 'SELECT *\nFROM data\nLIMIT 200;'
const ALIAS_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/

export const useWorkspaceStore = defineStore('workspace', () => {
  const sources = ref<DataSource[]>([])
  const sourceTables = ref<SourceTable[]>([])
  const selectedId = ref<string | null>(null)
  const selectedTableId = ref<string | null>(null)
  const queryBindings = ref<QueryTableBinding[]>([])
  const currentSql = ref(DEFAULT_SQL)
  const preview = ref<PreviewResponse | null>(null)
  const queryResult = ref<QueryResponse | null>(null)
  const savedQueries = ref<SavedQuery[]>([])
  const selectedSavedQueryId = ref<string | null>(null)
  const sourceLoading = ref(false)
  const previewLoading = ref(false)
  const queryLoading = ref(false)
  const uploadLoading = ref(false)
  const savedQueriesLoading = ref(false)

  const selectedSource = computed(
    () => sources.value.find((source) => source.id === selectedId.value) ?? null,
  )
  const selectedTable = computed(
    () => sourceTables.value.find((table) => table.id === selectedTableId.value) ?? null,
  )
  const selectedSavedQuery = computed(
    () => savedQueries.value.find((query) => query.id === selectedSavedQueryId.value) ?? null,
  )
  const boundTables = computed(() => queryBindings.value.flatMap((binding) => {
    const table = sourceTables.value.find((item) => item.id === binding.tableId)
    return table ? [{ binding, table }] : []
  }))
  const primarySourceId = computed(
    () => boundTables.value[0]?.table.sourceId ?? selectedId.value,
  )

  /**
   * 同时加载物理文件和逻辑表，保证文件树、预览与查询绑定来自同一份服务端快照。
   */
  async function loadSources() {
    sourceLoading.value = true
    try {
      const [loadedSources, loadedTables] = await Promise.all([
        api.listSources(),
        api.listSourceTables(),
      ])
      sources.value = loadedSources
      sourceTables.value = loadedTables
      queryBindings.value = queryBindings.value.filter((binding) => (
        loadedTables.some((table) => table.id === binding.tableId)
      ))
      if (!selectedId.value || !loadedSources.some((source) => source.id === selectedId.value)) {
        selectedId.value = loadedSources[0]?.id ?? null
      }
      if (!selectedTableId.value || !loadedTables.some((table) => table.id === selectedTableId.value)) {
        selectedTableId.value = defaultTableForSource(selectedId.value)?.id ?? loadedTables[0]?.id ?? null
      }
      if (!queryBindings.value.length && selectedTableId.value) {
        addTableBinding(selectedTableId.value, 'data')
      }
      await Promise.all([refreshPreview(), loadSavedQueries()])
    } finally {
      sourceLoading.value = false
    }
  }

  /**
   * 选择文件只切换浏览焦点，不清空查询上下文，因此用户可以连续查看并绑定多个文件。
   */
  async function selectSource(id: string) {
    selectedId.value = id
    const table = defaultTableForSource(id) ?? sourceTables.value.find((item) => item.sourceId === id)
    selectedTableId.value = table?.id ?? null
    await refreshPreview()
  }

  /**
   * 选择逻辑表会同步右侧检查器和数据预览，但不会擅自改写 SQL 或已绑定表。
   */
  async function selectTable(id: string) {
    const table = sourceTables.value.find((item) => item.id === id)
    if (!table) return
    selectedTableId.value = id
    selectedId.value = table.sourceId
    await refreshPreview()
  }

  /** 上传到服务端暂存区并读取 Sheet 样本，确认前不会创建正式数据源。 */
  async function inspectUpload(file: File): Promise<ImportInspection> {
    uploadLoading.value = true
    try {
      return await api.inspectSource(file)
    } finally {
      uploadLoading.value = false
    }
  }

  /** 按用户在导入弹窗中设置的范围重新读取暂存文件，字段类型因此始终基于真实表头和数据样本。 */
  async function inspectImportRange(token: string, payload: InspectImportTablePayload) {
    return api.previewSourceImport(token, payload)
  }

  /**
   * 提交用户确认的字段类型并刷新工作区，新文件的默认表会直接加入当前查询上下文。
   */
  async function commitImport(token: string, tables: ImportTableConfig[]) {
    uploadLoading.value = true
    try {
      const source = await api.commitSourceImport(token, tables)
      sources.value = [source, ...sources.value.filter((item) => item.id !== source.id)]
      sourceTables.value = await api.listSourceTables()
      const table = defaultTableForSource(source.id)
      selectedId.value = source.id
      selectedTableId.value = table?.id ?? null
      if (table) addTableBinding(table.id)
      selectedSavedQueryId.value = null
      queryResult.value = null
      await Promise.all([refreshPreview(), loadSavedQueries()])
      return source
    } finally {
      uploadLoading.value = false
    }
  }

  /** 用户取消导入时主动清理暂存文件，避免大文件继续占用服务器空间。 */
  async function discardImport(token: string) {
    await api.discardSourceImport(token)
  }

  /**
   * 读取当前逻辑表的独立预览，预览范围与 DuckDB 缓存构建使用同一服务端配置。
   */
  async function refreshPreview() {
    if (!selectedTableId.value) {
      preview.value = null
      return
    }
    previewLoading.value = true
    try {
      preview.value = await api.previewSourceTable(selectedTableId.value)
    } finally {
      previewLoading.value = false
    }
  }

  /**
   * 保存逻辑表读取配置并替换本地对象，配置版本变化会由后端自动使旧缓存失效。
   */
  async function applyTableConfig(payload: SourceTablePayload) {
    if (!selectedTableId.value) return null
    const updated = await api.updateSourceTable(selectedTableId.value, payload)
    replaceTable(updated)
    await refreshPreview()
    return updated
  }

  /**
   * 在当前物理文件上新增可复用范围，同一 Sheet 可以因此拆分成多张逻辑表。
   */
  async function createTable(payload: SourceTablePayload) {
    if (!selectedId.value) return null
    const created = await api.createSourceTable(selectedId.value, payload)
    sourceTables.value = [...sourceTables.value, created]
    await selectTable(created.id)
    return created
  }

  /**
   * 删除非默认逻辑表并同步移除其查询绑定，避免工作台保留不可执行的上下文。
   */
  async function deleteTable(id: string) {
    await api.deleteSourceTable(id)
    const removed = sourceTables.value.find((table) => table.id === id)
    sourceTables.value = sourceTables.value.filter((table) => table.id !== id)
    queryBindings.value = queryBindings.value.filter((binding) => binding.tableId !== id)
    if (selectedTableId.value === id) {
      selectedTableId.value = defaultTableForSource(removed?.sourceId ?? selectedId.value)?.id ?? null
      await refreshPreview()
    }
  }

  /**
   * 为查询新增表别名；首张表固定建议 data，后续别名按表名生成并自动去重。
   */
  function addTableBinding(tableId: string, preferredAlias?: string) {
    const table = sourceTables.value.find((item) => item.id === tableId)
    if (!table) return null
    const base = preferredAlias
      ?? (queryBindings.value.length ? aliasBase(table.name) : 'data')
    const alias = uniqueAlias(base)
    const binding = { tableId, alias }
    queryBindings.value = [...queryBindings.value, binding]
    queryResult.value = null
    return binding
  }

  /**
   * 重命名绑定前执行与后端一致的校验，错误可以在编辑区立即反馈而无需发起查询。
   */
  function renameTableBinding(index: number, alias: string) {
    const normalized = alias.trim()
    if (!ALIAS_PATTERN.test(normalized)) {
      throw new Error('别名只能包含字母、数字和下划线，且不能以数字开头')
    }
    const duplicate = queryBindings.value.some((binding, bindingIndex) => (
      bindingIndex !== index && binding.alias.toLowerCase() === normalized.toLowerCase()
    ))
    if (duplicate) throw new Error('表别名不能重复')
    queryBindings.value = queryBindings.value.map((binding, bindingIndex) => (
      bindingIndex === index ? { ...binding, alias: normalized } : binding
    ))
    queryResult.value = null
  }

  /**
   * 移除单个绑定而不删除逻辑表，用户可以随时调整 JOIN 上下文并保留数据配置。
   */
  function removeTableBinding(index: number) {
    queryBindings.value = queryBindings.value.filter((_, bindingIndex) => bindingIndex !== index)
    queryResult.value = null
  }

  /**
   * 将当前有序绑定整体替换为保存对象的快照，保证别名与 SQL 同步恢复。
   */
  async function setQueryContext(tables: QueryTableBinding[]) {
    queryBindings.value = tables.filter((binding) => (
      sourceTables.value.some((table) => table.id === binding.tableId)
    ))
    const first = boundTables.value[0]?.table
    if (first) {
      selectedId.value = first.sourceId
      selectedTableId.value = first.id
    }
    queryResult.value = null
    await refreshPreview()
  }

  /**
   * 使用完整表绑定执行 SQL，物理 sourceId 仅作为旧接口兼容主文件保留。
   */
  async function runQuery() {
    if (!primarySourceId.value || !queryBindings.value.length) return null
    queryLoading.value = true
    try {
      queryResult.value = await api.runQuery({
        sourceId: primarySourceId.value,
        tables: queryBindings.value,
        sql: currentSql.value,
        limit: 1_000,
      })
      return queryResult.value
    } finally {
      queryLoading.value = false
    }
  }

  /**
   * 加载工作区全部保存查询，跨文件查询不会因为当前浏览文件不同而被隐藏。
   */
  async function loadSavedQueries() {
    savedQueriesLoading.value = true
    try {
      savedQueries.value = await api.listSavedQueries()
      if (
        selectedSavedQueryId.value
        && !savedQueries.value.some((query) => query.id === selectedSavedQueryId.value)
      ) {
        selectedSavedQueryId.value = null
      }
    } finally {
      savedQueriesLoading.value = false
    }
  }

  /**
   * 恢复保存查询的 SQL 和表绑定，用户得到的是完整分析上下文而非单独文本。
   */
  async function selectSavedQuery(id: string | null) {
    selectedSavedQueryId.value = id
    if (!id) return
    const query = savedQueries.value.find((item) => item.id === id)
    if (!query) return
    currentSql.value = query.sql
    await setQueryContext(query.tables)
  }

  /**
   * 原子保存 SQL 与表绑定，首张表对应文件用于兼容历史列表字段。
   */
  async function saveCurrentQuery(name: string) {
    if (!primarySourceId.value || !queryBindings.value.length) return null
    const payload = {
      sourceId: primarySourceId.value,
      tables: queryBindings.value,
      name: name.trim(),
      sql: currentSql.value,
    }
    const saved = selectedSavedQueryId.value
      ? await api.updateSavedQuery(selectedSavedQueryId.value, payload)
      : await api.createSavedQuery(payload)
    const index = savedQueries.value.findIndex((query) => query.id === saved.id)
    if (index >= 0) savedQueries.value[index] = saved
    else savedQueries.value = [saved, ...savedQueries.value]
    selectedSavedQueryId.value = saved.id
    return saved
  }

  /** 删除当前保存查询并保留工作台中的临时 SQL，方便用户继续调整。 */
  async function deleteCurrentSavedQuery() {
    if (!selectedSavedQueryId.value) return
    const id = selectedSavedQueryId.value
    await api.deleteSavedQuery(id)
    savedQueries.value = savedQueries.value.filter((query) => query.id !== id)
    selectedSavedQueryId.value = null
  }

  /**
   * 删除物理文件后同步清理其逻辑表和绑定，随后选择仍存在的首个文件。
   */
  async function deleteSource(id: string) {
    await api.deleteSource(id)
    const tableIds = new Set(
      sourceTables.value.filter((table) => table.sourceId === id).map((table) => table.id),
    )
    sources.value = sources.value.filter((source) => source.id !== id)
    sourceTables.value = sourceTables.value.filter((table) => table.sourceId !== id)
    queryBindings.value = queryBindings.value.filter((binding) => !tableIds.has(binding.tableId))
    if (selectedId.value === id) {
      selectedId.value = sources.value[0]?.id ?? null
      selectedTableId.value = defaultTableForSource(selectedId.value)?.id ?? null
      selectedSavedQueryId.value = null
      queryResult.value = null
      await Promise.all([refreshPreview(), loadSavedQueries()])
    }
  }

  /**
   * 把公式写入首张绑定表的查询模板，避免仍然硬编码已经被用户移除的 data 表。
   */
  function insertFormula(name: string, expression: string) {
    const escapedName = name.replaceAll('"', '""')
    const tableAlias = queryBindings.value[0]?.alias ?? 'data'
    currentSql.value = `SELECT *,\n  ${expression} AS "${escapedName}"\nFROM ${tableAlias}\nLIMIT 200;`
  }

  /** 返回文件的默认逻辑表，旧 SQL 和首次打开都以它作为 data 绑定。 */
  function defaultTableForSource(sourceId: string | null | undefined) {
    if (!sourceId) return null
    return sourceTables.value.find((table) => table.sourceId === sourceId && table.isDefault) ?? null
  }

  /** 将任意表名转成可用 SQL 别名，中文等无法直接转换时使用稳定序号。 */
  function aliasBase(value: string) {
    const normalized = value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, '_')
      .replace(/^\d+/, '')
      .replace(/^_+|_+$/g, '')
    return normalized || `table_${queryBindings.value.length + 1}`
  }

  /** 在大小写不敏感的别名集合中生成唯一名称，连续添加同表也能直接用于自连接。 */
  function uniqueAlias(base: string) {
    const used = new Set(queryBindings.value.map((binding) => binding.alias.toLowerCase()))
    const validBase = ALIAS_PATTERN.test(base) ? base : aliasBase(base)
    let candidate = validBase
    let suffix = 2
    while (used.has(candidate.toLowerCase())) {
      candidate = `${validBase}_${suffix}`
      suffix += 1
    }
    return candidate
  }

  /** 原位替换服务端返回的逻辑表，保持数组排序和当前选择稳定。 */
  function replaceTable(updated: SourceTable) {
    const index = sourceTables.value.findIndex((table) => table.id === updated.id)
    if (index >= 0) sourceTables.value[index] = updated
    else sourceTables.value = [...sourceTables.value, updated]
  }

  return {
    sources,
    sourceTables,
    selectedId,
    selectedTableId,
    selectedSource,
    selectedTable,
    preview,
    queryResult,
    queryBindings,
    boundTables,
    primarySourceId,
    savedQueries,
    selectedSavedQueryId,
    selectedSavedQuery,
    sourceLoading,
    previewLoading,
    queryLoading,
    uploadLoading,
    savedQueriesLoading,
    currentSql,
    loadSources,
    selectSource,
    selectTable,
    inspectUpload,
    inspectImportRange,
    commitImport,
    discardImport,
    refreshPreview,
    applyTableConfig,
    createTable,
    deleteTable,
    addTableBinding,
    renameTableBinding,
    removeTableBinding,
    setQueryContext,
    runQuery,
    loadSavedQueries,
    selectSavedQuery,
    saveCurrentQuery,
    deleteCurrentSavedQuery,
    deleteSource,
    insertFormula,
  }
})
