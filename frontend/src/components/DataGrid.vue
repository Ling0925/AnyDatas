<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { FieldDefinition } from '../types'

const props = withDefaults(defineProps<{
  columns: FieldDefinition[]
  rows: unknown[][]
  loading?: boolean
  emptyText?: string
  rowOffset?: number
  virtualThreshold?: number
}>(), {
  loading: false,
  emptyText: '暂无数据',
  rowOffset: 0,
  virtualThreshold: 200,
})

const wrapper = ref<HTMLElement | null>(null)
const scrollTop = ref(0)
const viewportHeight = ref(0)
const rowHeight = 34
const headerHeight = 38
const overscan = 8
let resizeObserver: ResizeObserver | null = null

const virtualized = computed(() => props.rows.length > props.virtualThreshold)
const firstVisibleIndex = computed(() => {
  if (!virtualized.value) return 0
  const first = Math.floor(Math.max(0, scrollTop.value - headerHeight) / rowHeight)
  return Math.max(0, first - overscan)
})
const visibleCount = computed(() => {
  if (!virtualized.value) return props.rows.length
  return Math.ceil(viewportHeight.value / rowHeight) + overscan * 2
})
const lastVisibleIndex = computed(() => (
  Math.min(props.rows.length, firstVisibleIndex.value + visibleCount.value)
))
const visibleRows = computed(() => (
  props.rows
    .slice(firstVisibleIndex.value, lastVisibleIndex.value)
    .map((row, index) => ({ row, index: firstVisibleIndex.value + index }))
))
const topSpacerHeight = computed(() => firstVisibleIndex.value * rowHeight)
const bottomSpacerHeight = computed(() => (
  Math.max(0, props.rows.length - lastVisibleIndex.value) * rowHeight
))

/** 只记录纵向位置；横向滚动继续由原生表格处理，不触发 Vue 状态更新。 */
function handleScroll(event: Event) {
  scrollTop.value = (event.currentTarget as HTMLElement).scrollTop
}

/** 结果区域会随编辑器分栏变化，ResizeObserver 可即时修正可见行数量而不猜测高度。 */
onMounted(() => {
  if (!wrapper.value) return
  viewportHeight.value = wrapper.value.clientHeight
  resizeObserver = new ResizeObserver((entries) => {
    viewportHeight.value = entries[0]?.contentRect.height ?? wrapper.value?.clientHeight ?? 0
  })
  resizeObserver.observe(wrapper.value)
})

onBeforeUnmount(() => resizeObserver?.disconnect())

watch(
  () => props.rows,
  async () => {
    scrollTop.value = 0
    await nextTick()
    if (wrapper.value) wrapper.value.scrollTop = 0
  },
)

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <div
    ref="wrapper"
    class="data-grid-wrap"
    v-loading="loading"
    @scroll.passive="handleScroll"
  >
    <table
      v-if="columns.length"
      class="data-grid"
      :aria-rowcount="rows.length + 1"
    >
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
        <tr v-if="topSpacerHeight" class="data-grid-spacer" aria-hidden="true">
          <td :colspan="columns.length + 1" :style="{ height: `${topSpacerHeight}px` }" />
        </tr>
        <tr
          v-for="item in visibleRows"
          :key="item.index"
          :aria-rowindex="item.index + 2"
        >
          <td class="row-number">{{ rowOffset + item.index + 1 }}</td>
          <td v-for="(column, columnIndex) in columns" :key="column.name">
            <span
              :class="{
                'null-value': item.row[columnIndex] === null || item.row[columnIndex] === undefined,
              }"
            >
              {{ displayValue(item.row[columnIndex]) }}
            </span>
          </td>
        </tr>
        <tr v-if="bottomSpacerHeight" class="data-grid-spacer" aria-hidden="true">
          <td :colspan="columns.length + 1" :style="{ height: `${bottomSpacerHeight}px` }" />
        </tr>
      </tbody>
    </table>
    <div v-else class="grid-empty">
      <span>{{ emptyText }}</span>
    </div>
  </div>
</template>
