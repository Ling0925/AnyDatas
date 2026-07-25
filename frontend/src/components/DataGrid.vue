<script setup lang="ts">
import type { FieldDefinition } from '../types'

defineProps<{
  columns: FieldDefinition[]
  rows: unknown[][]
  loading?: boolean
  emptyText?: string
}>()

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <div class="data-grid-wrap" v-loading="loading">
    <table v-if="columns.length" class="data-grid">
      <thead>
        <tr>
          <th class="row-number">#</th>
          <th v-for="column in columns" :key="column.name">
            <span>{{ column.name }}</span>
            <small>{{ column.dataType }}</small>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in rows" :key="rowIndex">
          <td class="row-number">{{ rowIndex + 1 }}</td>
          <td v-for="(column, columnIndex) in columns" :key="column.name">
            <span :class="{ 'null-value': row[columnIndex] === null || row[columnIndex] === undefined }">
              {{ displayValue(row[columnIndex]) }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="grid-empty">
      <span>{{ emptyText ?? '暂无数据' }}</span>
    </div>
  </div>
</template>
