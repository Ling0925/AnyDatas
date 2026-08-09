<script setup lang="ts">
import { computed } from 'vue'
import { FolderOpen } from '@lucide/vue'

import type { DataSource, ScheduleItem } from '../../types'
import type { FileSourceForm } from '../../composables/useFileSources'

const TIMEZONES = [
  'Asia/Shanghai',
  'UTC',
  'Asia/Hong_Kong',
  'Asia/Tokyo',
  'America/Los_Angeles',
  'Europe/London',
]

const props = defineProps<{
  modelValue: boolean
  editingId: string | null
  form: FileSourceForm
  dataSources: DataSource[]
  schedules: ScheduleItem[]
  targetsLoading: boolean
  saving: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  save: []
  pickDirectory: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const targetLabel = computed(() => {
  const source = props.dataSources.find((item) => item.id === props.form.targetSourceId)
  return source ? `${source.name}（${source.originalFilename}）` : ''
})

const triggerLabels = computed(() =>
  props.form.triggerScheduleIds
    .map((id) => props.schedules.find((schedule) => schedule.id === id))
    .filter((schedule): schedule is ScheduleItem => Boolean(schedule))
    .map((schedule) => `${schedule.name}（${schedule.sourceName}）`),
)
</script>

<template>
  <el-dialog v-model="visible" :title="editingId ? '编辑文件源' : '新建文件源'" width="620px">
    <el-form label-position="top" v-loading="targetsLoading">
      <el-form-item label="名称">
        <el-input v-model="form.name" maxlength="80" placeholder="如：日报数据" />
      </el-form-item>
      <el-form-item label="目录">
        <div class="file-source-dir-field">
          <el-input
            v-model="form.directory"
            placeholder="选择或输入要扫描的本地目录"
            :title="form.directory || undefined"
            :aria-label="`目录：${form.directory || '未选择'}`"
            @keyup.enter="emit('pickDirectory')"
          />
          <el-button @click="emit('pickDirectory')"><FolderOpen :size="14" /> 选择目录</el-button>
        </div>
      </el-form-item>
      <div class="dialog-form-grid">
        <el-form-item label="文件模式">
          <el-input v-model="form.pattern" placeholder="*.xlsx / daily_*.xlsx" />
        </el-form-item>
        <el-form-item label="目标数据源">
          <el-select
            v-model="form.targetSourceId"
            placeholder="选择服务器数据源"
            :title="targetLabel || undefined"
            :aria-label="`目标数据源：${targetLabel || '未选择'}`"
          >
            <el-option
              v-for="source in dataSources"
              :key="source.id"
              :label="`${source.name}（${source.originalFilename}）`"
              :value="source.id"
            />
          </el-select>
        </el-form-item>
      </div>
      <div class="dialog-form-grid">
        <el-form-item label="定时表达式">
          <el-input v-model="form.cron" placeholder="0 8 * * *" />
          <p class="form-hint">分 时 日 月 周</p>
        </el-form-item>
        <el-form-item label="时区">
          <el-select v-model="form.timezone">
            <el-option v-for="timezone in TIMEZONES" :key="timezone" :label="timezone" :value="timezone" />
          </el-select>
        </el-form-item>
      </div>
      <el-form-item label="触发下游调度">
        <el-select
          v-model="form.triggerScheduleIds"
          multiple
          placeholder="采集成功后触发这些计划"
          :title="triggerLabels.join('、') || undefined"
          :aria-label="`触发下游调度：${triggerLabels.join('、') || '未选择'}`"
        >
          <el-option
            v-for="schedule in schedules"
            :key="schedule.id"
            :label="`${schedule.name}（${schedule.sourceName}）`"
            :value="schedule.id"
          />
        </el-select>
        <p class="form-hint">采集成功并覆盖数据源后，会依次立即运行选中的查询调度。</p>
      </el-form-item>
      <el-checkbox v-model="form.enabled">{{ editingId ? '启用此文件源' : '创建后立即启用' }}</el-checkbox>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="emit('save')">
        {{ editingId ? '保存修改' : '创建文件源' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.file-source-dir-field {
  width: 100%;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.form-hint {
  color: var(--text-secondary);
  font-size: 12px;
}

/* Deep selector scopes the Element Plus multiple-select tag contrast. */
:deep(.el-select__selection .el-select__selected-item > .el-tag) {
  --el-tag-text-color: var(--text-secondary);
  --el-tag-bg-color: var(--surface-active);
  --el-tag-border-color: var(--line);
}
</style>
