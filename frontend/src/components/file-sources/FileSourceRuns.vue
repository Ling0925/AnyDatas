<script setup lang="ts">
import { Clock3 } from '@lucide/vue'

defineProps<{
  runs: DesktopFileSourceRun[]
}>()

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function lastRunMeta(status: DesktopFileSourceRun['status']): {
  label: string
  className: string
} {
  if (status === 'success') return { label: '成功', className: 'succeeded' }
  if (status === 'skipped') return { label: '跳过', className: 'skipped' }
  return { label: '失败', className: 'failed' }
}
</script>

<template>
  <div class="file-source-runs">
    <div class="file-source-runs-header">
      <strong>运行历史</strong>
      <span>最近 {{ runs.length }} 条</span>
    </div>
    <div v-if="runs.length" class="file-source-runs-grid">
      <div class="file-source-runs-head" aria-hidden="true">
        <span>时间</span><span>状态</span><span>文件</span><span>结果 / 错误</span>
      </div>
      <div v-for="(run, index) in runs" :key="`${run.at}-${index}`" class="file-source-runs-head file-source-runs-row">
        <span>{{ formatDate(run.at) }}</span>
        <span>
          <span class="status-badge" :class="lastRunMeta(run.status).className">
            {{ lastRunMeta(run.status).label }}
          </span>
        </span>
        <code :title="run.file ?? undefined">{{ run.file ?? '—' }}</code>
        <span :class="{ 'run-error': Boolean(run.error) }" :title="run.error ?? undefined">
          <template v-if="run.error">{{ run.error }}</template>
          <template v-else-if="run.status === 'success' && run.rowsImported !== null">
            {{ run.rowsImported.toLocaleString() }} 行
          </template>
          <template v-else>—</template>
        </span>
      </div>
    </div>
    <div v-else class="file-source-runs-empty">
      <Clock3 :size="16" />
      <span>还没有运行记录</span>
    </div>
  </div>
</template>

<style scoped>
.file-source-runs {
  padding: 10px 14px 12px;
  border-top: 1px solid var(--line-soft);
  background: var(--surface-subtle);
}

.file-source-runs-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}

.file-source-runs-header strong {
  font-size: 11px;
  font-weight: 700;
}

.file-source-runs-header span {
  color: var(--text-secondary);
  font-size: 11px;
}

.file-source-runs .status-badge {
  font-size: 11px;
}

.file-source-runs-grid {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 6px;
}

.file-source-runs-head {
  display: grid;
  grid-template-columns: 140px 90px minmax(150px, 1fr) minmax(180px, 1.4fr);
  align-items: center;
  gap: 12px;
  padding: 6px 12px;
  min-height: 0;
  color: var(--text-secondary);
  background: var(--table-head);
  font-size: 11px;
  font-weight: 650;
}

.file-source-runs-row {
  min-height: 38px;
  color: var(--text-secondary);
  background: var(--panel);
  border-top: 1px solid var(--line-soft);
  font-weight: 400;
}

.file-source-runs-row > span,
.file-source-runs-row > code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-source-runs-row > code {
  color: var(--text-secondary);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 11px;
}

.file-source-runs-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px;
  color: var(--text-secondary);
  border: 1px dashed var(--line-strong);
  border-radius: 6px;
  font-size: 11px;
}
</style>
