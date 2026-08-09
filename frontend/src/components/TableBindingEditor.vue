<script setup lang="ts">
import { ref } from 'vue'
import { Plus, TableProperties, X } from '@lucide/vue'

import type { QueryTableBinding, SourceTable } from '../types'

const props = defineProps<{
  tables: SourceTable[]
  modelValue: QueryTableBinding[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: QueryTableBinding[]]
}>()

const pendingTableId = ref('')

/** 根据逻辑表生成下拉标签，同时显示物理文件归属以区分同名 Sheet。 */
function tableLabel(table: SourceTable) {
  return `${table.sourceName} / ${table.name}`
}

/** 查找绑定对应的逻辑表，列表只展示仍然存在的服务端对象。 */
function tableForBinding(binding: QueryTableBinding) {
  return props.tables.find((table) => table.id === binding.tableId)
}

/** 添加逻辑表并生成唯一别名，重复添加同一表时可直接配置自连接。 */
function addBinding() {
  const table = props.tables.find((item) => item.id === pendingTableId.value)
  if (!table) return
  const root = props.modelValue.length ? aliasBase(table.name) : 'data'
  const alias = uniqueAlias(root)
  emit('update:modelValue', [...props.modelValue, { tableId: table.id, alias }])
  pendingTableId.value = ''
}

/** 更新单个别名并保留其余绑定顺序，顺序决定兼容主文件和任务展示。 */
function renameBinding(index: number, alias: string) {
  emit('update:modelValue', props.modelValue.map((binding, bindingIndex) => (
    bindingIndex === index ? { ...binding, alias } : binding
  )))
}

/** 仅移除任务上下文中的绑定，不会删除工作区逻辑表或缓存。 */
function removeBinding(index: number) {
  emit('update:modelValue', props.modelValue.filter((_, bindingIndex) => bindingIndex !== index))
}

/** 将任意表名压缩为 SQL 标识符，无法转换的名称使用稳定序号。 */
function aliasBase(value: string) {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^\d+/, '')
    .replace(/^_+|_+$/g, '')
  return normalized || `table_${props.modelValue.length + 1}`
}

/** 在当前任务绑定中生成大小写不敏感的唯一别名。 */
function uniqueAlias(base: string) {
  const used = new Set(props.modelValue.map((binding) => binding.alias.toLowerCase()))
  let candidate = base
  let suffix = 2
  while (used.has(candidate.toLowerCase())) {
    candidate = `${base}_${suffix}`
    suffix += 1
  }
  return candidate
}
</script>

<template>
  <div class="table-binding-editor">
    <div class="binding-editor-add">
      <el-select v-model="pendingTableId" filterable placeholder="选择文件 / 逻辑表">
        <el-option
          v-for="table in tables"
          :key="table.id"
          :label="tableLabel(table)"
          :value="table.id"
        />
      </el-select>
      <el-button :disabled="!pendingTableId" aria-label="添加查询表" @click="addBinding">
        <Plus :size="15" />
        添加
      </el-button>
    </div>

    <div v-if="modelValue.length" class="binding-editor-list">
      <div v-for="(binding, index) in modelValue" :key="`${binding.tableId}-${index}`" class="binding-editor-row">
        <TableProperties :size="15" />
        <span class="binding-editor-table">
          <strong>{{ tableForBinding(binding)?.name ?? '逻辑表不存在' }}</strong>
          <small>{{ tableForBinding(binding)?.sourceName ?? binding.tableId }}</small>
        </span>
        <el-input
          :model-value="binding.alias"
          aria-label="SQL 别名"
          maxlength="63"
          @update:model-value="(value: string) => renameBinding(index, value)"
        />
        <el-tooltip content="移除" placement="top">
          <el-button class="icon-button plain" aria-label="移除查询表" @click="removeBinding(index)">
            <X :size="14" />
          </el-button>
        </el-tooltip>
      </div>
    </div>
    <div v-else class="binding-editor-empty">未选择查询表</div>
  </div>
</template>
