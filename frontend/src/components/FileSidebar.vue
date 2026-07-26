<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChevronDown,
  ChevronRight,
  FileSpreadsheet,
  Plus,
  RefreshCw,
  Search,
  TableProperties,
  Trash2,
  Upload,
} from '@lucide/vue'

import { errorMessage } from '../api'
import { useWorkspaceStore } from '../stores/workspace'
import type { FieldDataType, FieldDefinition, ImportInspection } from '../types'
import DataGrid from './DataGrid.vue'

interface ImportRangeDraft {
  startCell: string
  endCell: string
  firstRowAsHeader: boolean
  dirty: boolean
}

const store = useWorkspaceStore()
const search = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const expandedSourceIds = ref<Set<string>>(new Set())
const importInspection = ref<ImportInspection | null>(null)
const importDialogVisible = ref(false)
const importSaving = ref(false)
const activeImportSheet = ref('')
const selectedImportSheets = ref<string[]>([])
const importFields = ref<Record<string, FieldDefinition[]>>({})
const importRanges = ref<Record<string, ImportRangeDraft>>({})
const refreshingImportSheet = ref<string | null>(null)
const fieldTypes: FieldDataType[] = ['文本', '整数', '小数', '布尔', '日期', '日期时间']

const filteredSources = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return store.sources
  return store.sources.filter((source) => {
    const tables = store.sourceTables.filter((table) => table.sourceId === source.id)
    return source.name.toLowerCase().includes(query)
      || tables.some((table) => table.name.toLowerCase().includes(query))
  })
})

const activeInspection = computed(() => importInspection.value?.sheets.find(
  (sheet) => sheet.name === activeImportSheet.value,
) ?? null)

const activeImportRange = computed(() => importRanges.value[activeImportSheet.value] ?? null)

const selectedRangeIsDirty = computed(() => selectedImportSheets.value.some(
  (name) => importRanges.value[name]?.dirty,
))

const importBusy = computed(() => importSaving.value || refreshingImportSheet.value !== null)

watch(
  () => store.selectedId,
  (sourceId) => {
    if (!sourceId) return
    const next = new Set(expandedSourceIds.value)
    next.add(sourceId)
    expandedSourceIds.value = next
  },
  { immediate: true },
)

/** 格式化文件大小，紧凑展示不会挤压文件和 Sheet 名称。 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 返回文件下的逻辑表，后端顺序会把默认 Sheet 放在首位。 */
function tablesForSource(sourceId: string) {
  return store.sourceTables.filter((table) => table.sourceId === sourceId)
}

/** 返回逻辑表当前使用的全部别名，同一表自连接时可以显示多个绑定。 */
function aliasesForTable(tableId: string) {
  return store.queryBindings
    .filter((binding) => binding.tableId === tableId)
    .map((binding) => binding.alias)
}

/** 展开或收起文件节点，文件较多时仍可快速扫描当前需要的 Sheet。 */
function toggleSource(sourceId: string) {
  const next = new Set(expandedSourceIds.value)
  if (next.has(sourceId)) next.delete(sourceId)
  else next.add(sourceId)
  expandedSourceIds.value = next
}

/** 文件先进入服务端暂存区，预检完成后再打开字段类型确认对话框。 */
async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    const inspection = await store.inspectUpload(file)
    importInspection.value = inspection
    activeImportSheet.value = inspection.sheets[0]?.name ?? ''
    selectedImportSheets.value = inspection.sheets.map((sheet) => sheet.name)
    importFields.value = Object.fromEntries(inspection.sheets.map((sheet) => [
      sheet.name,
      sheet.fields.map((field) => ({ ...field })),
    ]))
    importRanges.value = Object.fromEntries(inspection.sheets.map((sheet) => [
      sheet.name,
      {
        startCell: sheet.startCell,
        endCell: sheet.endCell ?? '',
        firstRowAsHeader: sheet.firstRowAsHeader,
        dirty: false,
      },
    ]))
    importDialogVisible.value = true
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

/** 确认导入时提交用户选择的 Sheet 和字段类型，服务端会再次校验文件结构。 */
async function commitImport() {
  const inspection = importInspection.value
  if (!inspection) return
  const selected = inspection.sheets.filter((sheet) => selectedImportSheets.value.includes(sheet.name))
  if (!selected.length) {
    ElMessage.warning('请至少选择一张工作表')
    return
  }
  if (selectedRangeIsDirty.value) {
    ElMessage.warning('请先应用已修改的读取范围')
    return
  }
  importSaving.value = true
  try {
    const source = await store.commitImport(inspection.token, selected.map((sheet) => ({
      name: sheet.name,
      sheetName: sheet.name,
      startCell: importRanges.value[sheet.name]?.startCell ?? sheet.startCell,
      endCell: importRanges.value[sheet.name]?.endCell || null,
      firstRowAsHeader: importRanges.value[sheet.name]?.firstRowAsHeader
        ?? sheet.firstRowAsHeader,
      fields: importFields.value[sheet.name]?.map((field) => ({ ...field })) ?? sheet.fields,
    })))
    importInspection.value = null
    importDialogVisible.value = false
    const next = new Set(expandedSourceIds.value)
    next.add(source.id)
    expandedSourceIds.value = next
    ElMessage.success('文件导入完成')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    importSaving.value = false
  }
}

/** 更新单个 Sheet 的导入选择，并用 Set 去重避免复选框重复写入名称。 */
function setImportSheetSelected(name: string, selected: boolean) {
  const next = new Set(selectedImportSheets.value)
  if (selected) next.add(name)
  else next.delete(name)
  selectedImportSheets.value = Array.from(next)
}

/** 对话框在未提交时关闭会删除暂存文件，重复清理由服务端按幂等请求处理。 */
async function cleanupCanceledImport() {
  const inspection = importInspection.value
  importInspection.value = null
  importFields.value = {}
  selectedImportSheets.value = []
  activeImportSheet.value = ''
  importRanges.value = {}
  refreshingImportSheet.value = null
  if (!inspection) return
  try {
    await store.discardImport(inspection.token)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

/** 修改范围时先保留草稿，只有用户应用后才更新字段，避免旧字段配置与新读取区域混用。 */
function updateImportRangeCell(name: string, key: 'startCell' | 'endCell', value: string) {
  const range = importRanges.value[name]
  if (!range) return
  range[key] = value
  range.dirty = true
}

/** 表头选项会影响字段名称和第一条数据，因此与单元格范围使用同一套待应用状态。 */
function updateImportHeader(name: string, value: boolean) {
  const range = importRanges.value[name]
  if (!range) return
  range.firstRowAsHeader = value
  range.dirty = true
}

/** 将 Excel 单元格引用转换为可比较坐标，前端先给出明确反馈，服务端仍会执行最终校验。 */
function cellPosition(value: string): { row: number; column: number } | null {
  const match = /^([A-Za-z]+)([1-9]\d*)$/.exec(value.trim())
  if (!match) return null
  let column = 0
  for (const character of match[1]!.toUpperCase()) {
    column = column * 26 + character.charCodeAt(0) - 64
  }
  const row = Number(match[2])
  if (!Number.isSafeInteger(column) || !Number.isSafeInteger(row)) return null
  return { row, column }
}

/** 应用单个 Sheet 的范围并重新推断字段；范围变化后重置类型可避免沿用旧样本得出的错误配置。 */
async function refreshImportRange(name: string) {
  const inspection = importInspection.value
  const range = importRanges.value[name]
  if (!inspection || !range || refreshingImportSheet.value) return
  const startCell = range.startCell.trim().toUpperCase()
  const endCell = range.endCell.trim().toUpperCase()
  const start = cellPosition(startCell)
  const end = endCell ? cellPosition(endCell) : null
  if (!start || (endCell && !end)) {
    ElMessage.warning('单元格格式应类似 A1 或 D200')
    return
  }
  if (end && (end.row < start.row || end.column < start.column)) {
    ElMessage.warning('结束单元格必须位于起始单元格的右下方')
    return
  }

  refreshingImportSheet.value = name
  try {
    const refreshed = await store.inspectImportRange(inspection.token, {
      sheetName: name,
      startCell,
      endCell: endCell || null,
      firstRowAsHeader: range.firstRowAsHeader,
    })
    if (importInspection.value?.token !== inspection.token) return
    importFields.value[name] = refreshed.fields.map((field) => ({ ...field }))
    const sheets = importInspection.value.sheets.slice()
    const index = sheets.findIndex((sheet) => sheet.name === name)
    if (index >= 0) sheets[index] = refreshed
    importInspection.value = { ...importInspection.value, sheets }
    importRanges.value[name] = {
      startCell: refreshed.startCell,
      endCell: refreshed.endCell ?? '',
      firstRowAsHeader: refreshed.firstRowAsHeader,
      dirty: false,
    }
    ElMessage.success('读取范围已更新')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    if (refreshingImportSheet.value === name) refreshingImportSheet.value = null
  }
}

/** 左侧 Sheet 列表同时显示范围和待应用状态，切换工作表时不会遗漏尚未刷新的配置。 */
function importRangeLabel(name: string): string {
  const range = importRanges.value[name]
  if (!range) return 'A1'
  const cells = range.endCell ? `${range.startCell}:${range.endCell}` : range.startCell
  return range.dirty ? `${cells} · 待应用` : cells
}

/** 添加查询绑定但保留当前选择，连续点击可为自连接生成不同别名。 */
function addBinding(tableId: string) {
  const binding = store.addTableBinding(tableId)
  if (binding) ElMessage.success(`已绑定为 ${binding.alias}`)
}

/** 删除物理文件前明确提示级联范围，避免误删逻辑表和历史任务。 */
async function removeSource(id: string, name: string) {
  try {
    await ElMessageBox.confirm(`删除“${name}”及其逻辑表和任务记录？`, '删除数据文件', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await store.deleteSource(id)
    ElMessage.success('文件已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}
</script>

<template>
  <aside class="file-sidebar panel-column">
    <div class="panel-heading">
      <div>
        <h2>数据文件</h2>
        <span>{{ store.sources.length }} 个文件 · {{ store.sourceTables.length }} 张表</span>
      </div>
      <el-tooltip content="上传文件" placement="bottom">
        <el-button
          class="icon-button"
          type="primary"
          :loading="store.uploadLoading"
          aria-label="上传文件"
          @click="fileInput?.click()"
        >
          <Upload :size="16" />
        </el-button>
      </el-tooltip>
      <input
        ref="fileInput"
        class="visually-hidden"
        type="file"
        accept=".xlsx,.xls,.xlsb,.ods,.csv"
        @change="onFileChange"
      />
    </div>

    <div class="sidebar-search">
      <Search :size="15" />
      <input v-model="search" type="search" placeholder="搜索文件或工作表" />
    </div>

    <div class="file-list file-tree" v-loading="store.sourceLoading">
      <div v-for="source in filteredSources" :key="source.id" class="file-tree-node">
        <div class="file-row" :class="{ selected: store.selectedId === source.id }">
          <button
            class="tree-toggle"
            type="button"
            :aria-label="expandedSourceIds.has(source.id) ? '收起文件' : '展开文件'"
            @click="toggleSource(source.id)"
          >
            <ChevronDown v-if="expandedSourceIds.has(source.id)" :size="14" />
            <ChevronRight v-else :size="14" />
          </button>
          <button class="file-main" type="button" @click="store.selectSource(source.id)">
            <span class="file-icon"><FileSpreadsheet :size="17" /></span>
            <span class="file-copy">
              <strong>{{ source.name }}</strong>
              <small>
                {{ source.fileKind === 'csv' ? 'CSV' : 'Excel' }} · {{ formatSize(source.sizeBytes) }}
              </small>
            </span>
          </button>
          <el-tooltip content="删除文件" placement="right">
            <button
              class="row-action"
              type="button"
              aria-label="删除文件"
              @click="removeSource(source.id, source.name)"
            >
              <Trash2 :size="14" />
            </button>
          </el-tooltip>
        </div>

        <div v-show="expandedSourceIds.has(source.id)" class="sheet-tree">
          <div
            v-for="table in tablesForSource(source.id)"
            :key="table.id"
            class="sheet-row"
            :class="{
              selected: store.selectedTableId === table.id,
              bound: aliasesForTable(table.id).length,
            }"
          >
            <button class="sheet-main" type="button" @click="store.selectTable(table.id)">
              <TableProperties :size="14" />
              <span>
                <strong>{{ table.name }}</strong>
                <small>{{ table.startCell }}{{ table.endCell ? `:${table.endCell}` : '' }}</small>
              </span>
            </button>
            <span v-if="aliasesForTable(table.id).length" class="sheet-alias">
              {{ aliasesForTable(table.id).join(', ') }}
            </span>
            <el-tooltip content="加入查询；再次点击可用于自连接" placement="right">
              <button class="sheet-add" type="button" aria-label="加入查询" @click="addBinding(table.id)">
                <Plus :size="14" />
              </button>
            </el-tooltip>
          </div>
        </div>
      </div>
      <div v-if="!filteredSources.length && !store.sourceLoading" class="sidebar-empty">
        <FileSpreadsheet :size="26" />
        <span>{{ store.sources.length ? '没有匹配的文件或工作表' : '上传文件开始分析' }}</span>
      </div>
    </div>
  </aside>

  <el-dialog
    v-model="importDialogVisible"
    title="确认数据导入"
    width="1020px"
    :close-on-click-modal="!importBusy"
    :close-on-press-escape="!importBusy"
    :show-close="!importBusy"
    @closed="cleanupCanceledImport"
  >
    <div v-if="importInspection" class="import-dialog">
      <header class="import-file-summary">
        <span class="file-icon large"><FileSpreadsheet :size="20" /></span>
        <div>
          <strong>{{ importInspection.originalFilename }}</strong>
          <span>{{ importInspection.fileKind === 'csv' ? 'CSV' : 'Excel' }} · {{ formatSize(importInspection.sizeBytes) }}</span>
        </div>
        <span>{{ selectedImportSheets.length }} / {{ importInspection.sheets.length }} 张表</span>
      </header>

      <div class="import-dialog-body">
        <aside class="import-sheet-list">
          <div
            v-for="sheet in importInspection.sheets"
            :key="sheet.name"
            class="import-sheet-row"
            :class="{ active: activeImportSheet === sheet.name }"
          >
            <el-checkbox
              :model-value="selectedImportSheets.includes(sheet.name)"
              :aria-label="`导入 ${sheet.name}`"
              @change="setImportSheetSelected(sheet.name, Boolean($event))"
            />
            <button type="button" @click="activeImportSheet = sheet.name">
              <strong>{{ sheet.name }}</strong>
              <small>
                {{ sheet.rowCount.toLocaleString() }} 行 · {{ sheet.columnCount }} 列 ·
                {{ importRangeLabel(sheet.name) }}
              </small>
            </button>
          </div>
        </aside>

        <section v-if="activeInspection" class="import-sheet-config">
          <div class="import-config-heading">
            <div>
              <strong>{{ activeInspection.name }}</strong>
              <span>{{ activeInspection.rowCount.toLocaleString() }} 行</span>
            </div>
            <el-checkbox
              :model-value="selectedImportSheets.includes(activeInspection.name)"
              @change="setImportSheetSelected(activeInspection.name, Boolean($event))"
            >导入此表</el-checkbox>
          </div>

          <div v-if="activeImportRange" class="import-range-config">
            <label>
              <span>起始单元格</span>
              <el-input
                :model-value="activeImportRange.startCell"
                :disabled="refreshingImportSheet !== null"
                placeholder="A1"
                size="small"
                @update:model-value="updateImportRangeCell(activeInspection.name, 'startCell', String($event))"
                @keyup.enter="refreshImportRange(activeInspection.name)"
              />
            </label>
            <label>
              <span>结束单元格</span>
              <el-input
                :model-value="activeImportRange.endCell"
                :disabled="refreshingImportSheet !== null"
                placeholder="可选，如 H500"
                size="small"
                @update:model-value="updateImportRangeCell(activeInspection.name, 'endCell', String($event))"
                @keyup.enter="refreshImportRange(activeInspection.name)"
              />
            </label>
            <el-checkbox
              :model-value="activeImportRange.firstRowAsHeader"
              :disabled="refreshingImportSheet !== null"
              @change="updateImportHeader(activeInspection.name, Boolean($event))"
            >
              首行为字段名
            </el-checkbox>
            <span v-if="activeImportRange.dirty" class="import-range-dirty">范围已修改</span>
            <el-button
              aria-label="应用读取范围"
              :disabled="!activeImportRange.dirty || refreshingImportSheet !== null"
              :loading="refreshingImportSheet === activeInspection.name"
              size="small"
              @click="refreshImportRange(activeInspection.name)"
            >
              <RefreshCw :size="14" />
              应用范围
            </el-button>
          </div>

          <div class="import-field-table">
            <div class="import-field-header"><span>字段</span><span>数据类型</span></div>
            <div
              v-for="field in importFields[activeInspection.name]"
              :key="field.name"
              class="import-field-row"
            >
              <span :title="field.name">{{ field.name }}</span>
              <el-select
                v-model="field.dataType"
                :disabled="activeImportRange?.dirty || refreshingImportSheet !== null"
                size="small"
                :aria-label="`${field.name} 数据类型`"
              >
                <el-option v-for="type in fieldTypes" :key="type" :label="type" :value="type" />
              </el-select>
            </div>
          </div>

          <div class="import-preview-area">
            <div class="import-preview-heading">数据样本 · 前 {{ activeInspection.rows.length }} 行</div>
            <DataGrid
              :columns="importFields[activeInspection.name] ?? activeInspection.fields"
              :rows="activeInspection.rows"
              :loading="refreshingImportSheet === activeInspection.name"
              empty-text="没有可预览的数据"
            />
          </div>
        </section>
      </div>
    </div>
    <template #footer>
      <span v-if="selectedRangeIsDirty" class="import-footer-hint">请先应用已修改的读取范围</span>
      <el-button :disabled="importBusy" @click="importDialogVisible = false">取消</el-button>
      <el-button
        type="primary"
        :disabled="selectedRangeIsDirty || refreshingImportSheet !== null"
        :loading="importSaving"
        @click="commitImport"
      >
        确认导入
      </el-button>
    </template>
  </el-dialog>
</template>
