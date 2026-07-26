<script setup lang="ts">
import { defineAsyncComponent, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  BarChart3,
  Clock3,
  Download,
  Link2,
  ListPlus,
  Play,
  RefreshCw,
  Save,
  Sparkles,
  Table2,
  Trash2,
  X,
} from '@lucide/vue'

import { api, errorMessage } from '../api'
import DataGrid from '../components/DataGrid.vue'
import FileSidebar from '../components/FileSidebar.vue'
import InspectorPanel from '../components/InspectorPanel.vue'
import SqlEditor from '../components/SqlEditor.vue'
import { downloadQueryCsv } from '../export'
import { useWorkspaceStore } from '../stores/workspace'

const ResultChart = defineAsyncComponent(() => import('../components/ResultChart.vue'))

const router = useRouter()
const store = useWorkspaceStore()
const activeTab = ref<'query' | 'preview'>('query')
const resultMode = ref<'table' | 'chart'>('table')
const taskDialogVisible = ref(false)
const taskForm = reactive({ name: '' })
const taskCreating = ref(false)
const saveQueryDialogVisible = ref(false)
const saveQueryName = ref('')
const saveQuerySaving = ref(false)
let completionDisposable: { dispose: () => void } | null = null

const editorOptions = {
  automaticLayout: true,
  minimap: { enabled: false },
  fontSize: 14,
  lineHeight: 22,
  fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
  scrollBeyondLastLine: false,
  padding: { top: 14 },
  renderLineHighlight: 'line' as const,
  overviewRulerBorder: false,
  wordWrap: 'on' as const,
  tabSize: 2,
}

onMounted(async () => {
  try {
    await store.loadSources()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
})

onUnmounted(() => completionDisposable?.dispose())

async function runQuery() {
  try {
    await store.runQuery()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

/** 进入独立 Agent 工作区前确认存在可信表上下文，右侧文件树仍可继续增减绑定。 */
async function openAiWorkspace() {
  if (!store.queryBindings.length) {
    ElMessage.warning('请先从左侧加入查询表')
    return
  }
  await router.push('/agent')
}

function exportResult() {
  if (!store.queryResult) return
  const name = store.selectedSavedQuery?.name ?? store.selectedSource?.name ?? 'anydatas-result'
  downloadQueryCsv(store.queryResult, name)
  if (store.queryResult.truncated) {
    ElMessage.warning(`已导出当前返回的 ${store.queryResult.rowCount.toLocaleString()} 行`)
  } else {
    ElMessage.success('查询结果已导出')
  }
}

/** 恢复保存查询时同时等待表绑定和预览切换完成，避免编辑器短暂显示错误字段。 */
async function selectSavedQuery(value: string | null | undefined) {
  await store.selectSavedQuery(value || null)
}

function openSaveQueryDialog() {
  saveQueryName.value = store.selectedSavedQuery?.name ?? `${store.selectedSource?.name ?? '数据'}查询`
  saveQueryDialogVisible.value = true
}

async function saveQuery() {
  if (!saveQueryName.value.trim()) {
    ElMessage.warning('请输入查询名称')
    return
  }
  saveQuerySaving.value = true
  try {
    const updating = Boolean(store.selectedSavedQueryId)
    await store.saveCurrentQuery(saveQueryName.value)
    saveQueryDialogVisible.value = false
    ElMessage.success(updating ? '查询已更新' : '查询已保存')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saveQuerySaving.value = false
  }
}

async function deleteSavedQuery() {
  if (!store.selectedSavedQuery) return
  try {
    await ElMessageBox.confirm(
      `删除保存的查询“${store.selectedSavedQuery.name}”？`,
      '删除查询',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await store.deleteCurrentSavedQuery()
    ElMessage.success('查询已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function refreshPreview() {
  try {
    await store.refreshPreview()
    ElMessage.success('预览已刷新')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

function openTaskDialog() {
  const firstTable = store.boundTables[0]?.table
  taskForm.name = `${firstTable?.name ?? '数据'}分析`
  taskDialogVisible.value = true
}

async function createTask() {
  if (!store.primarySourceId || !store.queryBindings.length || !taskForm.name.trim()) return
  taskCreating.value = true
  try {
    await api.createJob({
      sourceId: store.primarySourceId,
      tables: store.queryBindings,
      name: taskForm.name.trim(),
      sql: store.currentSql,
    })
    taskDialogVisible.value = false
    ElMessage.success('任务已加入后台队列')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    taskCreating.value = false
  }
}

function insertFormula(name: string, expression: string) {
  store.insertFormula(name, expression)
  activeTab.value = 'query'
}

/** 提交别名编辑并在失败时恢复原值，保证界面状态始终能直接发送给后端。 */
function renameBinding(index: number, event: Event) {
  const input = event.target as HTMLInputElement
  try {
    store.renameTableBinding(index, input.value)
  } catch (error) {
    input.value = store.queryBindings[index]?.alias ?? ''
    ElMessage.warning(errorMessage(error))
  }
}

/** 注册基于当前逻辑表绑定的 SQL 补全，别名和字段配置变化后无需重建编辑器。 */
function configureSqlCompletion(monaco: any) {
  completionDisposable?.dispose()
  completionDisposable = monaco.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['.'],
    provideCompletionItems() {
      const suggestions = store.boundTables.flatMap(({ binding, table }) => {
        const tableSuggestion = {
          label: binding.alias,
          kind: monaco.languages.CompletionItemKind.Struct,
          insertText: binding.alias,
          detail: `${table.sourceName} / ${table.name}`,
        }
        const tableFields = store.selectedTableId === table.id && store.preview
          ? store.preview.columns
          : table.fields
        const fields = tableFields.map((field) => ({
          label: `${binding.alias}.${field.name}`,
          kind: monaco.languages.CompletionItemKind.Field,
          insertText: `${binding.alias}."${field.name.replaceAll('"', '""')}"`,
          detail: `${field.dataType} · ${table.name}`,
        }))
        return [tableSuggestion, ...fields]
      })
      return { suggestions }
    },
  })
}
</script>

<template>
  <div class="workbench-layout">
    <FileSidebar />

    <section class="analysis-main">
      <template v-if="store.selectedSource && store.selectedTable">
        <div class="analysis-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            :aria-selected="activeTab === 'query'"
            @click="activeTab = 'query'"
          >
            查询
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="activeTab === 'preview'"
            @click="activeTab = 'preview'"
          >
            数据预览
          </button>
        </div>

        <div v-show="activeTab === 'query'" class="query-workspace">
          <section class="editor-pane multi-table-editor">
            <div class="pane-toolbar">
              <div class="pane-title">
                <strong>SQL 查询</strong>
                <span>{{ store.queryBindings.length }} 张已绑定表</span>
              </div>
              <div class="toolbar-actions">
                <el-select
                  class="saved-query-select"
                  :model-value="store.selectedSavedQueryId ?? ''"
                  :loading="store.savedQueriesLoading"
                  clearable
                  placeholder="临时查询"
                  @change="selectSavedQuery"
                >
                  <el-option
                    v-for="query in store.savedQueries"
                    :key="query.id"
                    :label="query.name"
                    :value="query.id"
                  />
                </el-select>
                <el-tooltip :content="store.selectedSavedQueryId ? '保存修改' : '保存查询'" placement="bottom">
                  <el-button class="icon-button plain" aria-label="保存查询" @click="openSaveQueryDialog">
                    <Save :size="15" />
                  </el-button>
                </el-tooltip>
                <el-tooltip v-if="store.selectedSavedQueryId" content="删除保存的查询" placement="bottom">
                  <el-button class="icon-button danger" aria-label="删除保存的查询" @click="deleteSavedQuery">
                    <Trash2 :size="15" />
                  </el-button>
                </el-tooltip>
                <el-tooltip content="转为后台任务" placement="bottom">
                  <el-button class="icon-button plain" aria-label="转为后台任务" @click="openTaskDialog">
                    <ListPlus :size="16" />
                  </el-button>
                </el-tooltip>
                <el-tooltip content="打开 AI 分析" placement="bottom">
                  <el-button
                    class="icon-button ai-action"
                    aria-label="打开 AI 分析"
                    @click="openAiWorkspace"
                  >
                    <Sparkles :size="16" />
                  </el-button>
                </el-tooltip>
                <el-button
                  type="primary"
                  aria-label="运行查询"
                  :loading="store.queryLoading"
                  :disabled="!store.queryBindings.length"
                  @click="runQuery"
                >
                  <Play :size="15" />
                  运行
                </el-button>
              </div>
            </div>
            <div class="query-binding-bar">
              <span class="binding-bar-label"><Link2 :size="14" /> 查询表</span>
              <div v-if="store.boundTables.length" class="binding-list">
                <div
                  v-for="({ binding, table }, index) in store.boundTables"
                  :key="`${binding.tableId}-${index}`"
                  class="binding-chip"
                  :class="{ active: store.selectedTableId === table.id }"
                  @click="store.selectTable(table.id)"
                >
                  <span class="binding-source">{{ table.sourceName }} / {{ table.name }}</span>
                  <input
                    :value="binding.alias"
                    :aria-label="`${table.name} 的 SQL 别名`"
                    spellcheck="false"
                    @click.stop
                    @change="renameBinding(index, $event)"
                  />
                  <el-tooltip content="移出查询" placement="bottom">
                    <button
                      type="button"
                      aria-label="移出查询"
                      @click.stop="store.removeTableBinding(index)"
                    >
                      <X :size="13" />
                    </button>
                  </el-tooltip>
                </div>
              </div>
              <span v-else class="binding-empty">从左侧工作表点击 + 加入查询</span>
            </div>
            <div class="editor-host">
              <SqlEditor
                v-model="store.currentSql"
                language="sql"
                :options="editorOptions"
                @before-mount="configureSqlCompletion"
              />
            </div>
          </section>

          <section class="result-pane">
            <div class="pane-toolbar result-toolbar">
              <div class="pane-title">
                <strong>查询结果</strong>
                <span v-if="store.queryResult">
                  {{ store.queryResult.rowCount.toLocaleString() }} 行 · {{ store.queryResult.elapsedMs }} ms
                </span>
              </div>
              <div class="result-toolbar-actions">
                <span v-if="store.queryResult?.truncated" class="result-warning">结果已截断</span>
                <div class="result-view-switch" role="group" aria-label="结果视图">
                  <button type="button" :aria-pressed="resultMode === 'table'" @click="resultMode = 'table'">
                    <Table2 :size="13" /> 表格
                  </button>
                  <button
                    type="button"
                    :aria-pressed="resultMode === 'chart'"
                    :disabled="!store.queryResult"
                    @click="resultMode = 'chart'"
                  >
                    <BarChart3 :size="13" /> 图表
                  </button>
                </div>
                <el-tooltip content="导出 CSV" placement="bottom">
                  <el-button
                    class="icon-button plain"
                    aria-label="导出 CSV"
                    :disabled="!store.queryResult"
                    @click="exportResult"
                  >
                    <Download :size="15" />
                  </el-button>
                </el-tooltip>
              </div>
            </div>
            <DataGrid
              v-if="resultMode === 'table'"
              :columns="store.queryResult?.columns ?? []"
              :rows="store.queryResult?.rows ?? []"
              :loading="store.queryLoading"
              empty-text="运行查询后在此查看结果"
            />
            <ResultChart
              v-else-if="store.queryResult"
              :columns="store.queryResult.columns"
              :rows="store.queryResult.rows"
            />
          </section>
        </div>

        <div v-show="activeTab === 'preview'" class="preview-workspace">
          <div class="pane-toolbar">
            <div class="pane-title">
              <strong>{{ store.selectedTable.name }}</strong>
              <span v-if="store.preview">
                {{ store.preview.sheet }} · {{ store.preview.startCell }}{{ store.preview.endCell ? `:${store.preview.endCell}` : '' }}
                · {{ store.preview.totalRows.toLocaleString() }} 行
              </span>
            </div>
            <el-tooltip content="刷新预览" placement="bottom">
              <el-button class="icon-button plain" aria-label="刷新预览" @click="refreshPreview">
                <RefreshCw :size="15" />
              </el-button>
            </el-tooltip>
          </div>
          <DataGrid
            :columns="store.preview?.columns ?? []"
            :rows="store.preview?.rows ?? []"
            :loading="store.previewLoading"
            empty-text="当前读取范围没有数据"
          />
        </div>
      </template>

      <div v-else class="workspace-empty">
        <span class="empty-icon"><Table2 :size="30" /></span>
        <h2>选择或上传一个数据文件</h2>
        <p>展开文件后选择工作表，并加入查询上下文</p>
      </div>
    </section>

    <aside class="workbench-inspector panel-column">
      <InspectorPanel @formula="insertFormula" />
    </aside>

    <el-dialog
      v-model="saveQueryDialogVisible"
      :title="store.selectedSavedQueryId ? '保存查询修改' : '保存当前查询'"
      width="440px"
    >
      <el-form label-position="top">
        <el-form-item label="查询名称">
          <el-input v-model="saveQueryName" maxlength="80" @keyup.enter="saveQuery" />
        </el-form-item>
        <div class="dialog-summary">
          <Save :size="16" />
          <span>保存 SQL 与当前 {{ store.queryBindings.length }} 张逻辑表的绑定。</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="saveQueryDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saveQuerySaving" @click="saveQuery">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="taskDialogVisible" title="创建后台任务" width="460px">
      <el-form label-position="top">
        <el-form-item label="任务名称">
          <el-input v-model="taskForm.name" maxlength="80" />
        </el-form-item>
        <div class="dialog-summary">
          <Clock3 :size="16" />
          <span>任务将使用当前 SQL 和 {{ store.queryBindings.length }} 张逻辑表执行。</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="taskDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="taskCreating" @click="createTask">加入队列</el-button>
      </template>
    </el-dialog>
  </div>
</template>
