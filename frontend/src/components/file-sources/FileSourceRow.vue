<script setup lang="ts">
import { computed } from 'vue'
import {
  ChevronDown,
  ChevronRight,
  FolderOpen,
  History,
  Pencil,
  Play,
  Trash2,
} from '@lucide/vue'

import type { DataSource, ScheduleItem } from '../../types'
import FileSourceRuns from './FileSourceRuns.vue'

const props = defineProps<{
  source: DesktopFileSource
  actionId: string | null
  toggleId: string | null
  expanded: boolean
  dataSources: DataSource[]
  schedules: ScheduleItem[]
}>()

const emit = defineEmits<{
  toggle: [source: DesktopFileSource, value: boolean | string | number]
  run: [source: DesktopFileSource]
  edit: [source: DesktopFileSource]
  remove: [source: DesktopFileSource]
  toggleRuns: [id: string]
}>()

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

function scheduleNames(ids: string[]): string[] {
  return ids
    .map((id) => props.schedules.find((schedule) => schedule.id === id)?.name)
    .filter((name): name is string => Boolean(name))
}

const statusMeta = computed(() => lastRunMeta(props.source.lastRun?.status ?? null))
const targetName = computed(
  () => props.dataSources.find((source) => source.id === props.source.targetSourceId)?.name ?? '未知数据源',
)
const triggerNames = computed(() => scheduleNames(props.source.triggerScheduleIds))
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
        <strong tabindex="0" :aria-label="targetName" :title="targetName">{{ targetName }}</strong>
        <small v-if="triggerNames.length" class="file-source-triggers" tabindex="0" :aria-label="triggerNames.join('、')" :title="triggerNames.join('、')">
          触发 {{ triggerNames.join('、') }}
        </small>
        <small v-else>不触发下游调度</small>
      </span>
      <span class="file-source-copy">
        <code tabindex="0" :aria-label="source.cron" :title="source.cron">{{ source.cron }}</code>
        <small tabindex="0" :aria-label="source.timezone" :title="source.timezone">{{ source.timezone }}</small>
      </span>
      <span class="file-source-copy">
        <template v-if="source.lastRun">
          <small>{{ formatDate(source.lastRun.at) }}</small>
          <small v-if="source.lastRun.status === 'success' && source.lastRun.rowsImported !== null">
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
          aria-label="启用"
          @change="(value: boolean | string | number) => emit('toggle', source, value)"
        />
        <el-tooltip content="立即运行" placement="top">
          <el-button
            class="icon-button plain"
            :loading="actionId === source.id"
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
