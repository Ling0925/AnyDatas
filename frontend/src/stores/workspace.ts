import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '../api'
import type {
  AgentChartSpec,
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
const MAX_ALIAS_LENGTH = 63
export const MAX_AGENT_TABLES = 16
export const DEFAULT_POST_JS = `function process(rows, meta) {
  // rows: 对象数组；返回对象数组
  // 可用 http.request({ method, url, headers, body, timeoutMs })
  return rows
}
`

export interface AgentTableSelectionResult {
  ok: boolean
  selectedCount: number
  message?: string
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const sources = ref<DataSource[]>([])
  const sourceTables = ref<SourceTable[]>([])
  const selectedId = ref<string | null>(null)
  const selectedTableId = ref<string | null>(null)
  const queryBindings = ref<QueryTableBinding[]>([])
  const agentTableBindings = ref<QueryTableBinding[]>([])
  const currentSql = ref(DEFAULT_SQL)
  const currentPostJs = ref('')
  const preview = ref<PreviewResponse | null>(null)
  const queryResult = ref<QueryResponse | null>(null)
  // AI 建议的图表配置：由 Agent「应用」候选 SQL 时写入，供结果区按列名渲染；
  // runQuery 不清除它（应用图表触发的那次运行需要保留），手动切换上下文时清空。
  const appliedChart = ref<AgentChartSpec | null>(null)
  function setAppliedChart(spec: AgentChartSpec | null) {
    appliedChart.value = spec
  }
  const savedQueries = ref<SavedQuery[]>([])
  const selectedSavedQueryId = ref<string | null>(null)
  const sourceLoading = ref(false)
  // 空态里的「上传」入口可能在中心区，而文件 input 归属侧栏；用一个自增信号让任意空态
  // 都能触发侧栏打开文件选择框，无需跨组件传 ref。
  const uploadRequestId = ref(0)
  function requestUpload() {
    uploadRequestId.value += 1
  }
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
  const agentBoundTables = computed(() => agentTableBindings.value.flatMap((binding) => {
    const table = sourceTables.value.find((item) => item.id === binding.tableId)
    return table ? [{ binding, table }] : []
  }))
  const invalidAgentTableBindings = computed(() => agentTableBindings.value.filter((binding) => (
    !sourceTables.value.some((table) => table.id === binding.tableId)
  )))
  const agentPrimarySourceId = computed(
    () => agentBoundTables.value[0]?.table.sourceId ?? null,
  )
  const agentContextReady = computed(() => {
    if (agentTableBindings.value.length > MAX_AGENT_TABLES) return false
    const aliases = agentTableBindings.value.map((binding) => binding.alias.toLowerCase())
    return invalidAgentTableBindings.value.length === 0
      && agentTableBindings.value.every((binding) => ALIAS_PATTERN.test(binding.alias))
      && new Set(aliases).size === aliases.length
  })

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
   * 恢复 Agent 会话保存的表格快照时不丢弃已失效的表。
   * 这样历史上下文发生变化时界面能够明确提示，而不会静默换成另一组数据。
   */
  function setAgentTableBindings(tables: QueryTableBinding[]) {
    agentTableBindings.value = tables.map((binding) => ({ ...binding }))
  }

  /**
   * 切换 Agent 的单张逻辑表选择，并为新增项生成稳定且不重复的 SQL 别名。
   * Agent 绑定独立于工作台查询绑定，因此选择数据上下文不会意外改写正在编辑的查询。
   */
  function toggleAgentTableBinding(tableId: string): AgentTableSelectionResult {
    const existing = agentTableBindings.value.some((binding) => binding.tableId === tableId)
    if (existing) {
      agentTableBindings.value = agentTableBindings.value.filter(
        (binding) => binding.tableId !== tableId,
      )
      return { ok: true, selectedCount: agentTableBindings.value.length }
    }

    const table = sourceTables.value.find((item) => item.id === tableId)
    if (!table) {
      return {
        ok: false,
        selectedCount: agentTableBindings.value.length,
        message: '这张表已不存在，请刷新数据文件后重试',
      }
    }
    if (agentTableBindings.value.length >= MAX_AGENT_TABLES) {
      return {
        ok: false,
        selectedCount: agentTableBindings.value.length,
        message: `Agent 单次最多可选择 ${MAX_AGENT_TABLES} 张表`,
      }
    }

    const base = agentTableBindings.value.length
      ? agentAliasBase(table.name, agentTableBindings.value.length + 1)
      : 'data'
    const alias = uniqueAgentAlias(base, agentTableBindings.value)
    agentTableBindings.value = [...agentTableBindings.value, { tableId, alias }]
    return { ok: true, selectedCount: agentTableBindings.value.length }
  }

  /**
   * 移除指定 Agent 表绑定，失效表同样可以通过此入口清理。
   * 按表 ID 移除全部历史重复项，能让复选框状态恢复为单一、可理解的选择语义。
   */
  function removeAgentTableBinding(tableId: string): AgentTableSelectionResult {
    agentTableBindings.value = agentTableBindings.value.filter(
      (binding) => binding.tableId !== tableId,
    )
    return { ok: true, selectedCount: agentTableBindings.value.length }
  }

  /**
   * 清空 Agent 的全部表格上下文，使后续消息恢复为默认的纯对话模式。
   */
  function clearAgentTableBindings(): AgentTableSelectionResult {
    agentTableBindings.value = []
    return { ok: true, selectedCount: 0 }
  }

  /**
   * 按当前逻辑表顺序一次选择全部表，并在超过后端上限时返回明确失败。
   * 只有完整选择成功才替换现有状态，避免“全部”命令悄悄漏掉部分数据表。
   */
  function selectAllAgentTables(): AgentTableSelectionResult {
    if (!sourceTables.value.length) {
      return {
        ok: false,
        selectedCount: agentTableBindings.value.length,
        message: '当前没有可选择的逻辑表',
      }
    }
    if (sourceTables.value.length > MAX_AGENT_TABLES) {
      return {
        ok: false,
        selectedCount: agentTableBindings.value.length,
        message: `当前共有 ${sourceTables.value.length} 张表，Agent 单次最多可选择 ${MAX_AGENT_TABLES} 张，请手动选择`,
      }
    }

    const nextBindings: QueryTableBinding[] = []
    for (const table of sourceTables.value) {
      const base = nextBindings.length
        ? agentAliasBase(table.name, nextBindings.length + 1)
        : 'data'
      nextBindings.push({
        tableId: table.id,
        alias: uniqueAgentAlias(base, nextBindings),
      })
    }
    agentTableBindings.value = nextBindings
    return { ok: true, selectedCount: nextBindings.length }
  }

  /**
   * 将当前有序绑定整体替换为保存对象的快照，保证别名与 SQL 同步恢复。
   * Agent「应用/运行」可跳过预览刷新，避免大 Excel 在点按钮时先卡死整页。
   */
  async function setQueryContext(
    tables: QueryTableBinding[],
    options?: { refreshPreview?: boolean },
  ) {
    queryBindings.value = tables.filter((binding) => (
      sourceTables.value.some((table) => table.id === binding.tableId)
    ))
    const first = boundTables.value[0]?.table
    if (first) {
      selectedId.value = first.sourceId
      selectedTableId.value = first.id
    }
    queryResult.value = null
    if (options?.refreshPreview === false) return
    await refreshPreview()
  }

  /**
   * 使用完整表绑定执行 SQL，物理 sourceId 仅作为旧接口兼容主文件保留。
   */
  async function runQuery() {
    if (!primarySourceId.value || !queryBindings.value.length) {
      throw new Error('请先绑定至少一张逻辑表再运行查询')
    }
    queryLoading.value = true
    try {
      const postJs = currentPostJs.value.trim() || undefined
      queryResult.value = await api.runQuery({
        sourceId: primarySourceId.value,
        tables: queryBindings.value,
        sql: currentSql.value,
        postJs,
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
    currentPostJs.value = query.postJs ?? ''
    appliedChart.value = null
    await setQueryContext(query.tables)
  }

  /**
   * 原子保存 SQL、可选后处理脚本与表绑定，首张表对应文件用于兼容历史列表字段。
   */
  async function saveCurrentQuery(name: string) {
    if (!primarySourceId.value || !queryBindings.value.length) return null
    const postJs = currentPostJs.value.trim() || null
    const payload = {
      sourceId: primarySourceId.value,
      tables: queryBindings.value,
      name: name.trim(),
      sql: currentSql.value,
      postJs,
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
    appliedChart.value = null
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

  /**
   * 将 Agent 表名转换为最长 63 字符的 SQL 别名，无法直接转换时使用选择顺序生成稳定回退值。
   * 前端提前遵守后端长度约束，可避免长 ASCII 表名看似勾选成功却让整个上下文不可发送。
   */
  function agentAliasBase(value: string, fallbackIndex: number) {
    const normalized = value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, '_')
      .replace(/^\d+/, '')
      .replace(/^_+|_+$/g, '')
    return (normalized || `table_${fallbackIndex}`).slice(0, MAX_ALIAS_LENGTH)
  }

  /**
   * 基于给定 Agent 绑定快照生成大小写不敏感的唯一别名。
   * 去重后缀会预留在 63 字符上限内，让单选与全选都得到稳定且可被后端接受的名称。
   */
  function uniqueAgentAlias(base: string, bindings: QueryTableBinding[]) {
    const used = new Set(bindings.map((binding) => binding.alias.toLowerCase()))
    const validBase = (ALIAS_PATTERN.test(base)
      ? base
      : agentAliasBase(base, bindings.length + 1))
      .slice(0, MAX_ALIAS_LENGTH)
    let candidate = validBase
    let suffix = 2
    while (used.has(candidate.toLowerCase())) {
      const suffixText = `_${suffix}`
      candidate = `${validBase.slice(0, MAX_ALIAS_LENGTH - suffixText.length)}${suffixText}`
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
    appliedChart,
    setAppliedChart,
    queryBindings,
    boundTables,
    primarySourceId,
    agentTableBindings,
    agentBoundTables,
    invalidAgentTableBindings,
    agentPrimarySourceId,
    agentContextReady,
    savedQueries,
    selectedSavedQueryId,
    selectedSavedQuery,
    sourceLoading,
    uploadRequestId,
    requestUpload,
    previewLoading,
    queryLoading,
    uploadLoading,
    savedQueriesLoading,
    currentSql,
    currentPostJs,
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
    setAgentTableBindings,
    toggleAgentTableBinding,
    removeAgentTableBinding,
    clearAgentTableBindings,
    selectAllAgentTables,
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
