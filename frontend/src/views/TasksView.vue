<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CalendarClock,
  CircleCheck,
  CircleX,
  Clock3,
  Download,
  FileSpreadsheet,
  ListChecks,
  LoaderCircle,
  PauseCircle,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  Trash2,
} from '@lucide/vue'

import { api, errorMessage } from '../api'
import DataGrid from '../components/DataGrid.vue'
import SqlEditor from '../components/SqlEditor.vue'
import TableBindingEditor from '../components/TableBindingEditor.vue'
import { useTasksStore } from '../stores/tasks'
import { useWorkspaceStore } from '../stores/workspace'
import type {
  Job,
  JobResultPage,
  JobStatus,
  JobSummary,
  QueryTableBinding,
  ScheduleItem,
  SchedulePayload,
} from '../types'

const tasks = useTasksStore()
const workspace = useWorkspaceStore()
const activeTab = ref<'runs' | 'schedules'>('runs')
const jobDialogVisible = ref(false)
const scheduleDialogVisible = ref(false)
const saving = ref(false)
const resultLoading = ref(false)
const resultPage = ref<JobResultPage | null>(null)
const resultPageNumber = ref(1)
const resultPageSize = 100
const jobForm = reactive<{ tables: QueryTableBinding[]; name: string; sql: string }>({
  tables: [],
  name: '',
  sql: 'SELECT *\nFROM data\nLIMIT 1000;',
})
const scheduleForm = reactive({
  id: null as string | null,
  tables: [] as QueryTableBinding[],
  name: '',
  sql: 'SELECT *\nFROM data\nLIMIT 1000;',
  preset: 'daily' as 'hourly' | 'daily' | 'weekdays' | 'custom',
  time: '09:00',
  customCron: '0 0 9 * * *',
  timezone: 'Asia/Shanghai',
  enabled: true,
})

const filters: Array<{
  value: JobStatus | ''
  label: string
  icon: typeof ListChecks
  countKey: keyof JobSummary
}> = [
  { value: '', label: '全部任务', icon: ListChecks, countKey: 'total' },
  { value: 'running', label: '运行中', icon: LoaderCircle, countKey: 'running' },
  { value: 'queued', label: '排队中', icon: Clock3, countKey: 'queued' },
  { value: 'succeeded', label: '已完成', icon: CircleCheck, countKey: 'succeeded' },
  { value: 'failed', label: '失败', icon: CircleX, countKey: 'failed' },
  { value: 'canceled', label: '已停止', icon: PauseCircle, countKey: 'canceled' },
]

const statusMeta: Record<JobStatus, { label: string; className: string }> = {
  queued: { label: '排队中', className: 'queued' },
  running: { label: '运行中', className: 'running' },
  succeeded: { label: '已完成', className: 'succeeded' },
  failed: { label: '失败', className: 'failed' },
  canceled: { label: '已停止', className: 'canceled' },
}

const editorOptions = {
  automaticLayout: true,
  minimap: { enabled: false },
  fontSize: 13,
  lineHeight: 21,
  fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
  scrollBeyondLastLine: false,
  padding: { top: 12 },
  renderLineHighlight: 'line' as const,
  overviewRulerBorder: false,
  wordWrap: 'on' as const,
  tabSize: 2,
}

const currentCron = computed(() => {
  if (scheduleForm.preset === 'hourly') return '0 0 * * * *'
  if (scheduleForm.preset === 'custom') return scheduleForm.customCron.trim()
  const hour = Number(scheduleForm.time.split(':')[0] ?? 9)
  const minute = Number(scheduleForm.time.split(':')[1] ?? 0)
  return scheduleForm.preset === 'weekdays'
    ? `0 ${minute} ${hour} * * MON-FRI`
    : `0 ${minute} ${hour} * * *`
})
const displayedResult = computed(() => resultPage.value ?? tasks.selectedJob?.result ?? null)

let pollTimer: ReturnType<typeof setTimeout> | undefined
let stopped = false
let pollFailures = 0
let refreshPaused = false

onMounted(async () => {
  try {
    await Promise.all([workspace.loadSources(), tasks.loadJobs(), tasks.loadSchedules()])
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
  schedulePoll()
})

onUnmounted(() => {
  stopped = true
  if (pollTimer) clearTimeout(pollTimer)
})

watch(
  () => `${tasks.selectedJob?.id ?? ''}:${tasks.selectedJob?.resultAvailable ? 'ready' : 'empty'}`,
  () => {
    resultPage.value = null
    resultPageNumber.value = 1
    void loadSelectedResult()
  },
)

function schedulePoll() {
  if (stopped) return
  pollTimer = setTimeout(async () => {
    if (activeTab.value === 'runs') {
      try {
        if (tasks.activeCount > 0) await tasks.loadJobs()
        else await tasks.loadSummary()
        // 恢复成功：若此前提示过“自动刷新中断”，明确告知已恢复，避免用户误判数据仍是陈旧的。
        if (refreshPaused) {
          refreshPaused = false
          ElMessage.success('后台任务自动刷新已恢复')
        }
        pollFailures = 0
      } catch {
        // 轮询失败不能中断循环（否则将永久停更），但也不该完全静默。连续失败到阈值时
        // 提示一次“自动刷新已暂停”，让用户知道当前列表可能不是最新的，随后继续静默重试。
        pollFailures += 1
        if (pollFailures >= 3 && !refreshPaused) {
          refreshPaused = true
          ElMessage.warning('后台任务自动刷新暂时中断，正在后台重试…')
        }
      }
    }
    schedulePoll()
  }, tasks.activeCount > 0 ? 2000 : 10000)
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function duration(job: Job): string {
  if (!job.startedAt) return '—'
  const end = job.finishedAt ? new Date(job.finishedAt).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - new Date(job.startedAt).getTime()) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function triggerLabel(value: string): string {
  return {
    manual: '手动创建',
    schedule: '计划触发',
    retry: '手动重试',
  }[value] ?? value
}

async function reloadJobs() {
  try {
    await tasks.loadJobs()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function setFilter(value: JobStatus | '') {
  tasks.statusFilter = value
  await reloadJobs()
}

async function selectJob(id: string) {
  try {
    await tasks.selectJob(id)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

/** 结果分页始终由服务端读取 DuckDB 产物，页面切换不会重复传输完整数据集。 */
async function loadSelectedResult() {
  const job = tasks.selectedJob
  if (!job?.resultAvailable) return
  resultLoading.value = true
  try {
    resultPage.value = await api.getJobResult(
      job.id,
      (resultPageNumber.value - 1) * resultPageSize,
      resultPageSize,
    )
  } catch (error) {
    resultPage.value = null
    ElMessage.error(errorMessage(error))
  } finally {
    resultLoading.value = false
  }
}

async function changeResultPage(page: number) {
  resultPageNumber.value = page
  await loadSelectedResult()
}

function downloadJobResult(id: string) {
  const link = document.createElement('a')
  link.href = api.jobResultDownloadUrl(id)
  link.download = ''
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function openJobDialog() {
  jobForm.tables = defaultBindings()
  jobForm.name = ''
  jobForm.sql = 'SELECT *\nFROM data\nLIMIT 1000;'
  jobDialogVisible.value = true
}

async function createJob() {
  const sourceId = sourceIdForBindings(jobForm.tables)
  if (!sourceId || !jobForm.tables.length || !jobForm.name.trim() || !jobForm.sql.trim()) {
    ElMessage.warning('请完整填写任务信息')
    return
  }
  saving.value = true
  try {
    await tasks.createJob({
      sourceId,
      tables: jobForm.tables,
      name: jobForm.name.trim(),
      sql: jobForm.sql.trim(),
    })
    jobDialogVisible.value = false
    ElMessage.success('任务已加入队列')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function cancelJob(id: string) {
  try {
    await tasks.cancelJob(id)
    ElMessage.success('任务已停止')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function retryJob(id: string) {
  try {
    await tasks.retryJob(id)
    ElMessage.success('已创建重试任务')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function deleteJob(id: string) {
  try {
    await ElMessageBox.confirm('删除这条任务记录？', '删除任务', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await tasks.deleteJob(id)
    ElMessage.success('任务记录已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

function openScheduleDialog(schedule?: ScheduleItem) {
  scheduleForm.id = schedule?.id ?? null
  scheduleForm.tables = schedule?.tables.map((binding) => ({ ...binding })) ?? defaultBindings()
  scheduleForm.name = schedule?.name ?? ''
  scheduleForm.sql = schedule?.sql ?? 'SELECT *\nFROM data\nLIMIT 1000;'
  scheduleForm.time = '09:00'
  scheduleForm.timezone = schedule?.timezone ?? 'Asia/Shanghai'
  scheduleForm.enabled = schedule?.enabled ?? true
  scheduleForm.customCron = schedule?.cronExpression ?? '0 0 9 * * *'
  scheduleForm.preset = schedule ? cronPreset(schedule.cronExpression) : 'daily'
  const match = schedule?.cronExpression.match(/^0\s+(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(?:\*|MON-FRI)$/)
  if (match) scheduleForm.time = `${match[2]?.padStart(2, '0')}:${match[1]?.padStart(2, '0')}`
  scheduleDialogVisible.value = true
}

function cronPreset(expression: string): 'hourly' | 'daily' | 'weekdays' | 'custom' {
  if (expression === '0 0 * * * *') return 'hourly'
  if (/^0\s+\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+MON-FRI$/.test(expression)) return 'weekdays'
  if (/^0\s+\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+\*$/.test(expression)) return 'daily'
  return 'custom'
}

async function saveSchedule() {
  const sourceId = sourceIdForBindings(scheduleForm.tables)
  if (!sourceId || !scheduleForm.tables.length || !scheduleForm.name.trim() || !scheduleForm.sql.trim() || !currentCron.value) {
    ElMessage.warning('请完整填写计划信息')
    return
  }
  const payload: SchedulePayload = {
    sourceId,
    tables: scheduleForm.tables,
    name: scheduleForm.name.trim(),
    sql: scheduleForm.sql.trim(),
    cronExpression: currentCron.value,
    timezone: scheduleForm.timezone,
    enabled: scheduleForm.enabled,
  }
  saving.value = true
  try {
    await tasks.saveSchedule(scheduleForm.id, payload)
    scheduleDialogVisible.value = false
    ElMessage.success(scheduleForm.id ? '计划已更新' : '计划已创建')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

/** 使用工作台当前绑定作为任务起点；没有上下文时回退到当前文件的默认逻辑表。 */
function defaultBindings(): QueryTableBinding[] {
  if (workspace.queryBindings.length) {
    return workspace.queryBindings.map((binding) => ({ ...binding }))
  }
  const table = workspace.sourceTables.find((item) => (
    item.sourceId === workspace.selectedId && item.isDefault
  )) ?? workspace.sourceTables[0]
  return table ? [{ tableId: table.id, alias: 'data' }] : []
}

/** 从首张逻辑表推导兼容 sourceId，后端仍可用它完成工作区列表和级联关系。 */
function sourceIdForBindings(bindings: QueryTableBinding[]): string | null {
  const table = workspace.sourceTables.find((item) => item.id === bindings[0]?.tableId)
  return table?.sourceId ?? null
}

async function toggleSchedule(id: string, value: boolean | string | number) {
  try {
    await tasks.toggleSchedule(id, Boolean(value))
  } catch (error) {
    ElMessage.error(errorMessage(error))
    await tasks.loadSchedules()
  }
}

async function runSchedule(id: string) {
  try {
    await tasks.runSchedule(id)
    tasks.statusFilter = ''
    activeTab.value = 'runs'
    await tasks.loadJobs()
    ElMessage.success('计划已立即加入队列')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function deleteSchedule(id: string) {
  try {
    await ElMessageBox.confirm('删除这个计划任务？', '删除计划', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await tasks.deleteSchedule(id)
    ElMessage.success('计划已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}
</script>

<template>
  <div class="tasks-layout" :class="{ 'schedule-mode': activeTab === 'schedules' }">
    <aside class="task-nav panel-column">
      <div class="panel-heading task-nav-heading">
        <div><h2>后台任务</h2><span>{{ tasks.activeCount }} 个活动</span></div>
      </div>
      <div class="task-filter-list">
        <button
          v-for="filter in filters"
          :key="filter.value"
          type="button"
          :class="{ selected: activeTab === 'runs' && tasks.statusFilter === filter.value }"
          @click="activeTab = 'runs'; setFilter(filter.value)"
        >
          <component :is="filter.icon" :size="16" />
          <span>{{ filter.label }}</span>
          <small>{{ tasks.summary[filter.countKey] }}</small>
        </button>
      </div>
      <div class="task-nav-divider" />
      <button
        type="button"
        class="schedule-nav-button"
        :class="{ selected: activeTab === 'schedules' }"
        @click="activeTab = 'schedules'"
      >
        <CalendarClock :size="16" />
        <span>计划任务</span>
        <small>{{ tasks.schedules.length }}</small>
      </button>
    </aside>

    <section class="task-main">
      <div class="task-tabs">
        <button :aria-selected="activeTab === 'runs'" @click="activeTab = 'runs'">运行记录</button>
        <button :aria-selected="activeTab === 'schedules'" @click="activeTab = 'schedules'">计划任务</button>
      </div>

      <template v-if="activeTab === 'runs'">
        <div class="task-toolbar">
          <div class="task-search">
            <Search :size="15" />
            <input v-model="tasks.search" type="search" placeholder="搜索任务或文件" @keyup.enter="reloadJobs" />
          </div>
          <span class="task-result-count">显示 {{ tasks.jobs.length }} / {{ tasks.summary.total }} 条</span>
          <el-tooltip content="刷新" placement="bottom">
            <el-button class="icon-button plain" aria-label="刷新" @click="reloadJobs">
              <RefreshCw :size="15" />
            </el-button>
          </el-tooltip>
          <el-button type="primary" @click="openJobDialog"><Plus :size="15" /> 新建任务</el-button>
        </div>

        <div class="job-list" v-loading="tasks.loading">
          <div class="job-list-header" aria-hidden="true">
            <span>状态</span>
            <span>任务与数据文件</span>
            <span>执行进度</span>
            <span>结果</span>
            <span>创建时间</span>
          </div>
          <button
            v-for="job in tasks.jobs"
            :key="job.id"
            type="button"
            class="job-row"
            :class="{ selected: tasks.selectedJobId === job.id }"
            @click="selectJob(job.id)"
          >
            <span class="status-badge" :class="statusMeta[job.status].className">
              {{ statusMeta[job.status].label }}
            </span>
            <span class="job-copy">
              <strong>{{ job.name }}</strong>
              <small>
                <FileSpreadsheet :size="13" />
                {{ job.sourceName }} · {{ job.tables.length }} 张表
              </small>
            </span>
            <span v-if="job.status === 'running' || job.status === 'queued'" class="job-progress">
              <span><i :style="{ width: `${job.progress}%` }" /></span>
              <small>{{ job.progress }}%</small>
            </span>
            <span v-else class="job-result-count">
              {{ job.resultRowCount === null ? '—' : `${job.resultRowCount.toLocaleString()} 行` }}
            </span>
            <span class="job-time">{{ formatDate(job.createdAt) }}</span>
          </button>
          <div v-if="!tasks.jobs.length && !tasks.loading" class="task-empty">
            <ListChecks :size="28" />
            <span>当前没有任务记录</span>
          </div>
        </div>
      </template>

      <template v-else>
        <div class="task-toolbar schedule-toolbar">
          <div class="toolbar-copy">
            <strong>计划任务</strong>
            <span>{{ tasks.schedules.length }} 个计划</span>
          </div>
          <el-button type="primary" @click="openScheduleDialog()"><Plus :size="15" /> 新建计划</el-button>
        </div>
        <div class="schedule-list">
          <div v-for="schedule in tasks.schedules" :key="schedule.id" class="schedule-row">
            <el-switch
              :model-value="schedule.enabled"
              @change="(value: boolean | string | number) => toggleSchedule(schedule.id, value)"
            />
            <span class="schedule-icon"><CalendarClock :size="18" /></span>
            <span class="schedule-copy">
              <strong>{{ schedule.name }}</strong>
              <small>{{ schedule.sourceName }} · {{ schedule.tables.length }} 张表 · {{ schedule.cronExpression }}</small>
            </span>
            <span class="schedule-run">
              <small>下次运行</small>
              <strong>{{ schedule.enabled ? formatDate(schedule.nextRunAt) : '已暂停' }}</strong>
            </span>
            <div class="schedule-actions">
              <el-tooltip content="立即运行" placement="top">
                <el-button class="icon-button plain" aria-label="立即运行" @click="runSchedule(schedule.id)">
                  <Play :size="14" />
                </el-button>
              </el-tooltip>
              <el-button @click="openScheduleDialog(schedule)">编辑</el-button>
              <el-tooltip content="删除" placement="top">
                <el-button class="icon-button danger" aria-label="删除" @click="deleteSchedule(schedule.id)">
                  <Trash2 :size="14" />
                </el-button>
              </el-tooltip>
            </div>
          </div>
          <div v-if="!tasks.schedules.length" class="task-empty">
            <CalendarClock :size="28" />
            <span>当前没有计划任务</span>
          </div>
        </div>
      </template>
    </section>

    <aside v-if="activeTab === 'runs'" class="task-detail panel-column">
      <template v-if="tasks.selectedJob">
        <div class="detail-heading">
          <div>
            <span class="status-badge" :class="statusMeta[tasks.selectedJob.status].className">
              {{ statusMeta[tasks.selectedJob.status].label }}
            </span>
            <h2>{{ tasks.selectedJob.name }}</h2>
          </div>
          <div class="detail-actions">
            <el-tooltip
              v-if="tasks.selectedJob.resultAvailable"
              content="下载完整 CSV"
              placement="bottom"
            >
              <el-button
                class="icon-button plain"
                aria-label="下载完整 CSV"
                @click="downloadJobResult(tasks.selectedJob.id)"
              >
                <Download :size="14" />
              </el-button>
            </el-tooltip>
            <el-tooltip
              v-if="tasks.selectedJob.status === 'queued' || tasks.selectedJob.status === 'running'"
              content="停止任务"
              placement="bottom"
            >
              <el-button class="icon-button danger" aria-label="停止任务" @click="cancelJob(tasks.selectedJob.id)">
                <Square :size="14" />
              </el-button>
            </el-tooltip>
            <el-tooltip v-else content="重试" placement="bottom">
              <el-button class="icon-button plain" aria-label="重试" @click="retryJob(tasks.selectedJob.id)">
                <RotateCcw :size="14" />
              </el-button>
            </el-tooltip>
            <el-tooltip
              v-if="tasks.selectedJob.status !== 'queued' && tasks.selectedJob.status !== 'running'"
              content="删除记录"
              placement="bottom"
            >
              <el-button class="icon-button danger" aria-label="删除记录" @click="deleteJob(tasks.selectedJob.id)">
                <Trash2 :size="14" />
              </el-button>
            </el-tooltip>
          </div>
        </div>

        <dl class="detail-metadata">
          <div><dt>数据文件</dt><dd>{{ tasks.selectedJob.sourceName }}</dd></div>
          <div><dt>查询表</dt><dd>{{ tasks.selectedJob.tables.length }} 张</dd></div>
          <div><dt>触发方式</dt><dd>{{ triggerLabel(tasks.selectedJob.triggerType) }}</dd></div>
          <div><dt>开始时间</dt><dd>{{ formatDate(tasks.selectedJob.startedAt) }}</dd></div>
          <div><dt>运行耗时</dt><dd>{{ duration(tasks.selectedJob) }}</dd></div>
          <div><dt>完整结果</dt><dd>{{ tasks.selectedJob.resultRowCount?.toLocaleString() ?? '—' }} 行</dd></div>
          <div><dt>产物大小</dt><dd>{{ formatBytes(tasks.selectedJob.resultSizeBytes) }}</dd></div>
        </dl>

        <section class="detail-section">
          <h3>表绑定</h3>
          <div class="task-binding-list">
            <span v-for="binding in tasks.selectedJob.tables" :key="`${binding.tableId}-${binding.alias}`">
              {{ binding.alias }}
            </span>
          </div>
        </section>

        <section class="detail-section">
          <h3>SQL</h3>
          <pre>{{ tasks.selectedJob.sql }}</pre>
        </section>

        <section v-if="tasks.selectedJob.errorMessage" class="detail-section task-error">
          <h3>错误</h3>
          <p>{{ tasks.selectedJob.errorMessage }}</p>
        </section>

        <section class="detail-section logs-section">
          <h3>运行日志</h3>
          <div class="log-list">
            <div v-for="(log, index) in tasks.selectedJob.logs" :key="`${log.at}-${index}`" class="log-row">
              <span class="log-dot" :class="log.level" />
              <div><strong>{{ log.message }}</strong><small>{{ formatDate(log.at) }}</small></div>
            </div>
          </div>
        </section>

        <section v-if="displayedResult" class="detail-section detail-result" v-loading="resultLoading">
          <div class="detail-result-heading">
            <h3>结果数据</h3>
            <span v-if="tasks.selectedJob.resultRowCount !== null">
              {{ tasks.selectedJob.resultRowCount.toLocaleString() }} 行
            </span>
          </div>
          <DataGrid
            :columns="displayedResult.columns"
            :rows="displayedResult.rows"
            :row-offset="(resultPageNumber - 1) * resultPageSize"
          />
          <el-pagination
            v-if="tasks.selectedJob.resultAvailable && (tasks.selectedJob.resultRowCount ?? 0) > resultPageSize"
            background
            layout="prev, pager, next"
            :current-page="resultPageNumber"
            :page-size="resultPageSize"
            :total="tasks.selectedJob.resultRowCount ?? 0"
            :pager-count="5"
            @current-change="changeResultPage"
          />
        </section>

        <section
          v-else-if="tasks.selectedJob.status === 'succeeded' && tasks.selectedJob.resultRowCount !== null"
          class="detail-section result-expired"
        >
          <h3>结果数据</h3>
          <p>完整结果已按保留策略清理，任务 SQL 和运行日志仍然保留。</p>
        </section>
      </template>
      <div v-else class="inspector-empty"><ListChecks :size="26" /><span>选择一条任务查看详情</span></div>
    </aside>

    <el-dialog v-model="jobDialogVisible" title="新建后台任务" width="620px">
      <el-form label-position="top">
        <el-form-item label="任务名称">
          <el-input v-model="jobForm.name" maxlength="80" />
        </el-form-item>
        <el-form-item label="查询表">
          <TableBindingEditor v-model="jobForm.tables" :tables="workspace.sourceTables" />
        </el-form-item>
        <el-form-item label="SQL">
          <div class="dialog-sql-editor">
            <SqlEditor
              v-model="jobForm.sql"
              language="sql"
              :options="editorOptions"
            />
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="jobDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="createJob">加入队列</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="scheduleDialogVisible" :title="scheduleForm.id ? '编辑计划' : '新建计划'" width="660px">
      <el-form label-position="top">
        <el-form-item label="计划名称">
          <el-input v-model="scheduleForm.name" maxlength="80" />
        </el-form-item>
        <el-form-item label="查询表">
          <TableBindingEditor v-model="scheduleForm.tables" :tables="workspace.sourceTables" />
        </el-form-item>
        <div class="dialog-form-grid schedule-config-grid">
          <el-form-item label="执行频率">
            <el-select v-model="scheduleForm.preset">
              <el-option label="每小时" value="hourly" />
              <el-option label="每天" value="daily" />
              <el-option label="工作日" value="weekdays" />
              <el-option label="自定义 Cron" value="custom" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="scheduleForm.preset === 'daily' || scheduleForm.preset === 'weekdays'" label="执行时间">
            <el-time-select v-model="scheduleForm.time" start="00:00" step="00:30" end="23:30" />
          </el-form-item>
          <el-form-item v-else-if="scheduleForm.preset === 'custom'" label="Cron 表达式">
            <el-input v-model="scheduleForm.customCron" />
          </el-form-item>
          <el-form-item v-else label="时区">
            <el-select v-model="scheduleForm.timezone">
              <el-option label="中国标准时间" value="Asia/Shanghai" />
              <el-option label="协调世界时" value="UTC" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="SQL">
          <div class="dialog-sql-editor schedule-sql-editor">
            <SqlEditor
              v-model="scheduleForm.sql"
              language="sql"
              :options="editorOptions"
            />
          </div>
        </el-form-item>
        <el-checkbox v-model="scheduleForm.enabled">创建后立即启用</el-checkbox>
      </el-form>
      <template #footer>
        <el-button @click="scheduleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveSchedule">保存计划</el-button>
      </template>
    </el-dialog>
  </div>
</template>
