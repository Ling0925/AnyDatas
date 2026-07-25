<script setup lang="ts">
import type { QueryResponse } from '../types'

withDefaults(defineProps<{
  result: QueryResponse
  title?: string
}>(), {
  title: '结果预览',
})

/** 把长单元格压缩为可扫描文本，完整数据仍保留在正式查询结果或后续 AI 上下文中。 */
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '空值'
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
  return truncateText(text, 80)
}

/** 按 Unicode 字符截断显示文本，中文和扩展字符不会在代理对中间被切断。 */
function truncateText(value: string, limit: number): string {
  const characters = Array.from(value)
  return characters.length <= limit ? value : `${characters.slice(0, limit).join('')}…`
}
</script>

<template>
  <div class="ai-result-preview">
    <div class="ai-preview-heading">
      <strong>{{ title }}</strong>
      <span>{{ result.rowCount.toLocaleString() }} 行 · {{ result.elapsedMs }} ms</span>
    </div>
    <div class="ai-preview-table-wrap">
      <table>
        <thead>
          <tr>
            <th v-for="column in result.columns.slice(0, 6)" :key="column.name">
              {{ column.name }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in result.rows.slice(0, 5)" :key="rowIndex">
            <td v-for="(_, columnIndex) in result.columns.slice(0, 6)" :key="columnIndex">
              {{ displayValue(row[columnIndex]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <span v-if="result.columns.length > 6" class="ai-preview-more">
      另有 {{ result.columns.length - 6 }} 个字段
    </span>
  </div>
</template>
