<script setup lang="ts">
import { computed } from 'vue'
import { BarChart3 } from '@lucide/vue'

import ResultChart from './ResultChart.vue'
import type { AgentChartSpec, QueryResponse } from '../types'

const props = defineProps<{ spec: AgentChartSpec; result: QueryResponse }>()

const chartTypeLabels: Record<string, string> = {
  bar: '分组柱状图',
  'stacked-bar': '堆叠柱状图',
  line: '折线图',
  area: '面积图',
  pie: '饼图',
  scatter: '散点图',
  radar: '雷达图',
}

/** 维度或任一度量列能在结果里找到才渲染缩略图，否则退化为文字建议，避免空图。 */
const canRender = computed(() => {
  const names = new Set(props.result.columns.map((column) => column.name))
  return names.has(props.spec.category) || props.spec.values.some((value) => names.has(value))
})

const summaryText = computed(() => {
  const type = chartTypeLabels[props.spec.type] ?? props.spec.type
  return `图表建议：${type} · 维度 ${props.spec.category} · 度量 ${props.spec.values.join('、')}`
})
</script>

<template>
  <div class="ai-chart-preview">
    <div class="ai-chart-preview-heading">
      <BarChart3 :size="14" aria-hidden="true" />
      <strong>{{ spec.title || '图表建议' }}</strong>
      <span v-if="spec.rationale">{{ spec.rationale }}</span>
    </div>
    <div v-if="canRender" class="ai-chart-preview-body">
      <ResultChart :columns="result.columns" :rows="result.rows" :applied-config="spec" compact />
    </div>
    <div v-else class="ai-chart-preview-fallback">{{ summaryText }}</div>
  </div>
</template>

<style scoped>
.ai-chart-preview {
  margin-top: 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.ai-chart-preview-heading {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: var(--surface-subtle);
  color: var(--text-secondary);
  font-size: var(--fs-caption);
}

.ai-chart-preview-heading span {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-chart-preview-body {
  height: 240px;
}

.ai-chart-preview-body :deep(.result-chart-panel),
.ai-chart-preview-body :deep(.result-chart) {
  height: 100%;
}

.ai-chart-preview-fallback {
  padding: 10px;
  color: var(--muted);
  font-size: var(--fs-caption);
}
</style>
