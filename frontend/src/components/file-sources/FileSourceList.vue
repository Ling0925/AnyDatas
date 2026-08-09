<script setup lang="ts">
import { FolderSync } from '@lucide/vue'

import type { DataSource, ScheduleItem } from '../../types'
import FileSourceRow from './FileSourceRow.vue'

defineProps<{
  sources: DesktopFileSource[]
  loading: boolean
  actionId: string | null
  toggleId: string | null
  expandedRunsId: string | null
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
</script>

<template>
  <div v-loading="loading" class="file-sources-list">
    <div v-if="sources.length" class="file-source-grid file-source-header" aria-hidden="true">
      <span>状态</span>
      <span>名称与目录</span>
      <span>文件模式</span>
      <span>目标数据源</span>
      <span>定时</span>
      <span>上次运行</span>
      <span class="file-source-actions-head">操作</span>
    </div>

    <FileSourceRow
      v-for="source in sources"
      :key="source.id"
      :source="source"
      :action-id="actionId"
      :toggle-id="toggleId"
      :expanded="expandedRunsId === source.id"
      :data-sources="dataSources"
      :schedules="schedules"
      @toggle="(row, value) => emit('toggle', row, value)"
      @run="(row) => emit('run', row)"
      @edit="(row) => emit('edit', row)"
      @remove="(row) => emit('remove', row)"
      @toggle-runs="emit('toggleRuns', source.id)"
    />

    <div v-if="!sources.length && !loading" class="task-empty">
      <FolderSync :size="28" />
      <span>当前没有文件源</span>
    </div>
  </div>
</template>

<style scoped>
.file-sources-list {
  min-height: 0;
  overflow-y: auto;
  padding: 12px 20px 24px;
}

.file-source-grid {
  display: grid;
  grid-template-columns: var(--fs-grid-cols, 76px minmax(190px, 1.3fr) 120px minmax(150px, 1fr) 150px minmax(150px, 1fr) minmax(246px, 1fr));
  align-items: center;
  gap: 12px;
}

.file-source-header {
  position: sticky;
  top: 0;
  z-index: 2;
  height: 30px;
  padding: 0 14px;
  color: var(--text-secondary);
  background: var(--surface-hover);
  border-bottom: 1px solid var(--line);
  border-radius: 6px 6px 0 0;
  font-size: 11px;
  font-weight: 650;
}

/* 窄视口下操作列移至行底，表头只隐藏“操作”单元格本身。 */
@media (max-width: 1180px) {
  .file-source-header .file-source-actions-head {
    display: none;
  }
}

.file-source-card {
  overflow: hidden;
  border: 1px solid var(--line-soft);
  border-bottom: 0;
  background: var(--panel);
}

.file-source-card:last-child {
  border-bottom: 1px solid var(--line-soft);
  border-radius: 0 0 6px 6px;
}

.file-source-card + .file-source-card {
  border-top: 0;
}
</style>
