<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Check,
  ChevronDown,
  ChevronRight,
  FileSpreadsheet,
  Search,
  TableProperties,
  TriangleAlert,
  X,
} from '@lucide/vue'

import { MAX_AGENT_TABLES, useWorkspaceStore } from '../stores/workspace'
import type { DataSource, SourceTable } from '../types'

interface SourceTableGroup {
  source: DataSource
  tables: SourceTable[]
}

const store = useWorkspaceStore()
const search = ref('')
const expandedSourceIds = ref<Set<string>>(new Set())

const selectedTableIds = computed(() => new Set(
  store.agentTableBindings.map((binding) => binding.tableId),
))

const filteredGroups = computed<SourceTableGroup[]>(() => {
  const query = search.value.trim().toLowerCase()
  return store.sources.flatMap((source) => {
    const allTables = store.sourceTables.filter((table) => table.sourceId === source.id)
    if (!query) return allTables.length ? [{ source, tables: allTables }] : []

    const sourceMatches = [
      source.name,
      source.originalFilename,
    ].some((value) => value.toLowerCase().includes(query))
    const tables = sourceMatches
      ? allTables
      : allTables.filter((table) => [
        table.name,
        table.sheetName,
      ].some((value) => value.toLowerCase().includes(query)))
    return tables.length ? [{ source, tables }] : []
  })
})

watch(
  () => store.sources.map((source) => source.id),
  (sourceIds, previousIds) => {
    const previous = new Set(previousIds ?? [])
    const next = new Set(expandedSourceIds.value)
    sourceIds.forEach((sourceId) => {
      if (!previous.has(sourceId)) next.add(sourceId)
    })
    expandedSourceIds.value = next
  },
  { immediate: true },
)

/**
 * 展开或收起单个数据源，让表格较多的工作区仍能保持清晰的浏览层级。
 */
function toggleSource(sourceId: string) {
  const next = new Set(expandedSourceIds.value)
  if (next.has(sourceId)) next.delete(sourceId)
  else next.add(sourceId)
  expandedSourceIds.value = next
}

/**
 * 返回表格当前使用的 Agent 别名，界面直接展示别名可帮助用户理解模型生成 SQL 时的引用方式。
 */
function aliasForTable(tableId: string) {
  return store.agentTableBindings.find((binding) => binding.tableId === tableId)?.alias ?? null
}

/**
 * 切换单张表并透传容量或失效提示，避免表面勾选成功但后端实际无法接收。
 */
function toggleTable(tableId: string) {
  const result = store.toggleAgentTableBinding(tableId)
  if (!result.ok && result.message) ElMessage.warning(result.message)
}

/**
 * 全选操作保持“全部或不变”的语义，超过限制时不会静默截断用户的数据上下文。
 */
function selectAllTables() {
  const result = store.selectAllAgentTables()
  if (!result.ok) {
    ElMessage.warning(result.message)
    return
  }
  ElMessage.success(
    result.selectedCount ? `已选择全部 ${result.selectedCount} 张表` : '当前没有可选表格',
  )
}

/**
 * 一键把工作台正在查询的表同步为 AI 上下文，解决“在工作台绑了表、进 AI 却是已选 0”的错配。
 * 这是显式操作，不改变“默认不向 AI 提供表结构”的隐私默认。
 */
function useWorkbenchTables() {
  const bindings = store.queryBindings.filter(
    (binding) => store.sourceTables.some((table) => table.id === binding.tableId),
  )
  if (!bindings.length) {
    ElMessage.warning('工作台当前没有绑定的查询表')
    return
  }
  if (bindings.length > MAX_AGENT_TABLES) {
    ElMessage.warning(`工作台绑定了 ${bindings.length} 张表，超过单次上限 ${MAX_AGENT_TABLES} 张`)
    return
  }
  store.setAgentTableBindings(bindings.map((binding) => ({ ...binding })))
  ElMessage.success(`已同步工作台的 ${bindings.length} 张表`)
}

/**
 * 清空选择会恢复默认的纯对话模式，且不影响数据分析工作台自己的查询绑定。
 */
function clearSelection() {
  store.clearAgentTableBindings()
}

/**
 * 单独移除历史会话中的失效绑定，用户无需清空其余仍可用的上下文。
 */
function removeInvalidTable(tableId: string) {
  store.removeAgentTableBinding(tableId)
}

/**
 * 将读取范围压缩为一行元信息，既便于核对表格又不会让右侧栏显得拥挤。
 */
function tableMeta(table: SourceTable) {
  const range = table.endCell ? `${table.startCell}:${table.endCell}` : table.startCell
  return `${table.sheetName} · ${range} · ${table.rowCount.toLocaleString()} 行`
}
</script>

<template>
  <aside class="agent-table-selector" aria-label="AI 数据上下文">
    <header class="agent-table-heading">
      <div>
        <h2>AI 数据上下文</h2>
        <span>已选 {{ store.agentTableBindings.length }} / {{ store.sourceTables.length }}</span>
      </div>
      <div class="agent-table-heading-actions">
        <el-tooltip content="使用工作台正在查询的表" placement="bottom">
          <button
            type="button"
            :disabled="!store.queryBindings.length"
            aria-label="使用工作台正在查询的表"
            @click="useWorkbenchTables"
          >
            工作台
          </button>
        </el-tooltip>
        <button
          type="button"
          :disabled="!store.sourceTables.length"
          aria-label="选择全部逻辑表"
          @click="selectAllTables"
        >
          全部
        </button>
        <button
          type="button"
          :disabled="!store.agentTableBindings.length"
          aria-label="清空 Agent 表格选择"
          @click="clearSelection"
        >
          清空
        </button>
      </div>
    </header>

    <section
      class="agent-context-status"
      :class="{ active: store.agentTableBindings.length }"
      aria-live="polite"
    >
      <TableProperties :size="16" aria-hidden="true" />
      <div>
        <strong>
          {{
            store.agentTableBindings.length
              ? '仅向 AI 提供已勾选表格'
              : '默认不向 AI 提供表结构'
          }}
        </strong>
        <span>
          {{
            store.agentTableBindings.length
              ? `当前上下文包含 ${store.agentTableBindings.length} 张表`
              : '勾选后才会把对应字段与样本加入对话'
          }}
        </span>
      </div>
    </section>

    <label class="agent-table-search">
      <Search :size="15" aria-hidden="true" />
      <span class="visually-hidden">搜索数据文件或逻辑表</span>
      <input
        v-model="search"
        type="search"
        placeholder="搜索文件、工作表或逻辑表"
        aria-label="搜索数据文件或逻辑表"
      />
    </label>

    <div class="agent-table-list" v-loading="store.sourceLoading">
      <section
        v-if="store.invalidAgentTableBindings.length"
        class="agent-invalid-tables"
        aria-label="失效表格"
      >
        <header>
          <TriangleAlert :size="15" aria-hidden="true" />
          <div>
            <strong>失效表格</strong>
            <span>历史会话引用的表已不存在，请移除后继续</span>
          </div>
        </header>
        <div
          v-for="binding in store.invalidAgentTableBindings"
          :key="`${binding.tableId}:${binding.alias}`"
          class="agent-invalid-table"
        >
          <code>{{ binding.alias }}</code>
          <span :title="binding.tableId">{{ binding.tableId }}</span>
          <button
            type="button"
            :aria-label="`移除失效表格 ${binding.alias}`"
            @click="removeInvalidTable(binding.tableId)"
          >
            <X :size="13" aria-hidden="true" />
          </button>
        </div>
      </section>

      <section
        v-for="group in filteredGroups"
        :key="group.source.id"
        class="agent-source-group"
      >
        <button
          class="agent-source-row"
          type="button"
          :aria-expanded="expandedSourceIds.has(group.source.id)"
          :aria-controls="`agent-source-${group.source.id}`"
          @click="toggleSource(group.source.id)"
        >
          <ChevronDown
            v-if="expandedSourceIds.has(group.source.id)"
            :size="14"
            aria-hidden="true"
          />
          <ChevronRight v-else :size="14" aria-hidden="true" />
          <FileSpreadsheet :size="16" aria-hidden="true" />
          <span>
            <strong :title="group.source.name">{{ group.source.name }}</strong>
            <small>{{ group.tables.length }} 张逻辑表</small>
          </span>
        </button>

        <div
          v-show="expandedSourceIds.has(group.source.id)"
          :id="`agent-source-${group.source.id}`"
          class="agent-source-tables"
        >
          <button
            v-for="table in group.tables"
            :key="table.id"
            class="agent-table-row"
            :class="{ selected: selectedTableIds.has(table.id) }"
            type="button"
            role="checkbox"
            :aria-checked="selectedTableIds.has(table.id)"
            :aria-label="`${selectedTableIds.has(table.id) ? '取消选择' : '选择'}表格 ${table.name}`"
            @click="toggleTable(table.id)"
          >
            <span class="agent-table-checkbox" aria-hidden="true">
              <Check v-if="selectedTableIds.has(table.id)" :size="12" />
            </span>
            <span class="agent-table-copy">
              <strong :title="table.name">{{ table.name }}</strong>
              <small :title="tableMeta(table)">{{ tableMeta(table) }}</small>
            </span>
            <code v-if="aliasForTable(table.id)">{{ aliasForTable(table.id) }}</code>
          </button>
        </div>
      </section>

      <div v-if="!filteredGroups.length && !store.sourceLoading" class="agent-table-empty">
        <FileSpreadsheet :size="26" aria-hidden="true" />
        <strong>{{ store.sourceTables.length ? '没有匹配的表格' : '暂无可用表格' }}</strong>
        <span>
          {{
            store.sourceTables.length
              ? '换个关键词继续查找'
              : '导入数据后，可在这里选择发送给 AI 的上下文'
          }}
        </span>
      </div>
    </div>

    <footer class="agent-table-footer">
      <span>单次最多 {{ MAX_AGENT_TABLES }} 张</span>
      <span>命令 <code>/all</code> 全选 · <code>/clear</code> 清空</span>
    </footer>
  </aside>
</template>
