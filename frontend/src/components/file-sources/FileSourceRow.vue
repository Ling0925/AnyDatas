<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  ChevronDown,
  ChevronRight,
  FolderOpen,
  History,
  Pencil,
  Play,
  Trash2,
} from '@lucide/vue'

import type { DataSource, SavedQuery, ScheduleItem } from '../../types'
import FileSourceRuns from './FileSourceRuns.vue'

const props = defineProps<{
  source: DesktopFileSource
  actionId: string | null
  toggleId: string | null
  expanded: boolean
  dataSources: DataSource[]
  savedQueries: SavedQuery[]
  schedules: ScheduleItem[]
  activity: DesktopFileSourceActivity | null
}>()

const emit = defineEmits<{
  toggle: [source: DesktopFileSource, value: boolean | string | number]
  run: [source: DesktopFileSource]
  edit: [source: DesktopFileSource]
  remove: [source: DesktopFileSource]
  toggleRuns: [id: string]
}>()

const router = useRouter()

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

function lastRunMeta(status: DesktopFileSourceLastRun['status'] | null): {
  label: string
  className: string
} {
  if (status === 'success') return { label: '成功', className: 'succeeded' }
  if (status === 'skipped') return { label: '跳过', className: 'skipped' }
  if (status === 'failed') return { label: '失败', className: 'failed' }
  return { label: '从未运行', className: 'empty' }
}

function activityMeta(activity: DesktopFileSourceActivity | null): { label: string; className: string } | null {
  if (!activity || activity.phase === 'idle') return null
  if (activity.phase === 'waiting') return { label: '等待文件', className: 'skipped' }
  if (activity.phase === 'needs_login') return { label: '需要登录', className: 'failed' }
  if (activity.phase === 'needs_attention') return { label: '需要处理', className: 'failed' }
  if (activity.phase === 'queued') return { label: '已入队', className: 'succeeded' }
  return { label: '运行中', className: 'running' }
}

function scheduleNames(ids: string[]): string[] {
  return ids
    .map((id) => props.schedules.find((schedule) => schedule.id === id)?.name)
    .filter((name): name is string => Boolean(name))
}

const persistedActivity = computed<DesktopFileSourceActivity | null>(() => {
  if (!props.source.activeAttempt) return null
  const phase = props.source.activeAttempt.phase
  if (phase === 'needs_login' || phase === 'needs_attention' || phase === 'waiting') {
    return { phase, message: phase === 'needs_login' ? '登录后继续' : '等待处理', file: props.source.activeAttempt.file }
  }
  return { phase: 'publishing', message: '恢复中的自动化尝试', file: props.source.activeAttempt.file }
})
const effectiveActivity = computed(() => props.activity ?? persistedActivity.value)
const targetResolved = computed(() => props.dataSources.some((source) => source.id === props.source.targetSourceId))
const savedQueryResolved = computed(() => (
  props.source.mode !== 'saved_query'
  || props.savedQueries.some((query) => query.id === props.source.savedQueryId)
))
const missingScheduleCount = computed(() => props.source.triggerScheduleIds.filter((id) => (
  !props.schedules.some((schedule) => schedule.id === id)
)).length)
const referencesBroken = computed(() => (
  !targetResolved.value
  || !savedQueryResolved.value
  || (props.source.mode === 'legacy_schedule' && missingScheduleCount.value > 0)
))
const statusMeta = computed(() => (
  referencesBroken.value
    ? { label: '引用失效', className: 'failed' }
    : activityMeta(effectiveActivity.value)
      ?? (!props.source.enabled
        ? { label: '已暂停', className: 'empty' }
        : lastRunMeta(props.source.lastRun?.status ?? null))
))
const targetName = computed(
  () => props.dataSources.find((source) => source.id === props.source.targetSourceId)?.name ?? '目标不存在',
)
const savedQueryName = computed(
  () => props.savedQueries.find((query) => query.id === props.source.savedQueryId)?.name ?? '查询不存在',
)
const triggerNames = computed(() => scheduleNames(props.source.triggerScheduleIds))
const trustedRecipeTested = computed(() => (
  props.source.mode === 'legacy_schedule'
  || props.source.lastRun?.status === 'success'
  || (props.source.lastRun?.status === 'skipped' && Boolean(props.source.lastRun.revisionId))
))
const isActive = computed(() => effectiveActivity.value !== null && !['idle', 'waiting', 'needs_login', 'needs_attention'].includes(effectiveActivity.value.phase))

function openTarget() {
  void router.push({ path: '/workbench', query: { source: props.source.targetSourceId } })
}

function openJob() {
  if (!props.source.lastRun?.jobId) return
  void router.push({ path: '/tasks', query: { job: props.source.lastRun.jobId } })
}
</script>

<template>
  <div class="file-source-card">
    <div class="file-source-grid file-source-row">
      <span>
        <span class="status-badge" :class="statusMeta.className">{{ statusMeta.label }}</span>
      </span>
      <span class="file-source-copy">
        <strong :title="source.name">{{ source.name }}</strong>
        <small class="file-source-directory" tabindex="0" :aria-label="source.directory" :title="source.directory">
          <FolderOpen :size="13" />
          {{ source.directory }}
        </small>
      </span>
      <code class="file-source-pattern" :title="source.pattern">{{ source.pattern }}</code>
      <span class="file-source-copy">
        <button class="source-link" type="button" :title="targetName" @click="openTarget">
          {{ targetName }}
        </button>
        <small v-if="source.mode === 'saved_query'" class="file-source-triggers" :title="savedQueryName">
          运行查询：{{ savedQueryName }}
        </small>
        <small v-else-if="missingScheduleCount" class="run-error">
          {{ missingScheduleCount }} 个计划引用已失效
        </small>
        <small v-else-if="triggerNames.length" class="file-source-triggers" tabindex="0" :aria-label="triggerNames.join('、')" :title="triggerNames.join('、')">
          兼容模式 · 触发 {{ triggerNames.join('、') }}
        </small>
        <small v-else>不触发下游调度</small>
      </span>
      <span class="file-source-copy">
        <code tabindex="0" :aria-label="source.cron" :title="source.cron">{{ source.cron }}</code>
        <small tabindex="0" :aria-label="source.timezone" :title="source.timezone">{{ source.timezone }}</small>
      </span>
      <span class="file-source-copy">
        <template v-if="effectiveActivity">
          <small>{{ effectiveActivity.message }}</small>
          <small v-if="effectiveActivity.file" :title="effectiveActivity.file">{{ effectiveActivity.file }}</small>
        </template>
        <template v-else-if="source.lastRun">
          <small>{{ formatDate(source.lastRun.at) }}</small>
          <button v-if="source.lastRun.jobId" class="job-link" type="button" @click="openJob">
            查看任务 {{ source.lastRun.jobId.slice(0, 8) }}
          </button>
          <small v-else-if="source.lastRun.status === 'success' && source.lastRun.rowsImported !== null">
            {{ source.lastRun.rowsImported.toLocaleString() }} 行
          </small>
          <small v-else-if="source.lastRun.error" class="run-error" :title="source.lastRun.error">
            {{ source.lastRun.error }}
          </small>
        </template>
        <small v-else>—</small>
      </span>
      <span class="file-source-actions">
        <el-switch
          :model-value="source.enabled"
          :loading="toggleId === source.id"
          :disabled="referencesBroken || (!source.enabled && !trustedRecipeTested)"
          aria-label="启用"
          @change="(value: boolean | string | number) => emit('toggle', source, value)"
        />
        <el-tooltip content="立即运行" placement="top">
          <el-button
            class="icon-button plain"
            :loading="actionId === source.id || isActive"
            :disabled="isActive || referencesBroken"
            aria-label="立即运行"
            @click="emit('run', source)"
          >
            <Play :size="14" />
          </el-button>
        </el-tooltip>
        <el-button class="icon-button plain" aria-label="编辑" @click="emit('edit', source)">
          <Pencil :size="14" />
        </el-button>
        <el-tooltip content="删除" placement="top">
          <el-button class="icon-button danger" aria-label="删除" @click="emit('remove', source)">
            <Trash2 :size="14" />
          </el-button>
        </el-tooltip>
        <button
          class="runs-toggle"
          type="button"
          :aria-expanded="expanded"
          @click="emit('toggleRuns', source.id)"
        >
          <History :size="14" />
          运行历史
          <ChevronDown v-if="expanded" :size="14" />
          <ChevronRight v-else :size="14" />
        </button>
      </span>
    </div>

    <FileSourceRuns v-if="expanded" :runs="source.runs" />
  </div>
</template>

<style scoped>
.file-source-grid {
  display: grid;
  grid-template-columns: var(--fs-grid-cols, 76px minmax(190px, 1.3fr) 120px minmax(150px, 1fr) 150px minmax(150px, 1fr) minmax(246px, 1fr));
  align-items: center;
  gap: 12px;
}

.file-source-row {
  min-height: 68px;
  padding: 8px 14px;
  background: var(--panel);
}

.file-source-row:hover {
  background: var(--surface-hover);
}

.file-source-row .status-badge {
  font-size: 11px;
}

.file-source-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: hidden;
}

.file-source-copy strong,
.file-source-copy small,
.file-source-copy code,
.file-source-pattern,
.runs-toggle {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.25;
}

.source-link,
.job-link {
  width: fit-content;
  max-width: 100%;
  padding: 0;
  border: 0;
  color: var(--primary-text);
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-link {
  color: var(--info);
  font-size: 11px;
}

.file-source-copy strong {
  color: var(--text);
  font-size: 12px;
  font-weight: 650;
}

.file-source-copy small {
  color: var(--text-secondary);
  font-size: 11px;
}

.file-source-copy code,
.file-source-pattern,
.file-source-directory {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

.file-source-copy code {
  color: var(--primary-text);
  font-size: 11px;
}

.file-source-directory {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--text-secondary);
  font-size: 11px;
}

.file-source-pattern {
  width: fit-content;
  max-width: 100%;
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--primary-text);
  background: var(--primary-soft);
  font-size: 11px;
}

.file-source-triggers {
  color: var(--info) !important;
}

.run-error {
  color: var(--red) !important;
}

.file-source-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}

.runs-toggle {
  flex-shrink: 0;
  height: 32px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
  border: 0;
  border-radius: 4px;
  color: var(--text-secondary);
  background: transparent;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.runs-toggle:hover {
  color: var(--primary);
  background: var(--primary-soft);
}

/* 窄视口：六个数据列，操作控制整行排列在底部，保留全部行内控件。 */
@media (max-width: 1180px) {
  .file-source-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
    padding-top: 8px;
    border-top: 1px solid var(--line-soft);
  }
}
</style>
