<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  BarChart3,
  ChartArea,
  ChartNoAxesCombined,
  Layers3,
  LineChart,
  PieChart,
  Radar,
  ScatterChart,
} from '@lucide/vue'
import {
  BarChart,
  LineChart as EChartsLineChart,
  PieChart as EChartsPieChart,
  RadarChart as EChartsRadarChart,
  ScatterChart as EChartsScatterChart,
} from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  RadarComponent,
  TooltipComponent,
} from 'echarts/components'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

import { useTheme } from '../theme'
import type { FieldDefinition } from '../types'

use([
  BarChart,
  EChartsLineChart,
  EChartsPieChart,
  EChartsRadarChart,
  EChartsScatterChart,
  GridComponent,
  LegendComponent,
  RadarComponent,
  TooltipComponent,
  CanvasRenderer,
])

type ChartType = 'bar' | 'stacked-bar' | 'line' | 'area' | 'pie' | 'scatter' | 'radar'
type Aggregation = 'sum' | 'average' | 'max' | 'min'

interface MetricAccumulator {
  sum: number
  count: number
  min: number
  max: number
}

interface AggregatedSeries {
  name: string
  values: Array<number | null>
}

interface GroupDescriptor {
  key: string
  label: string
}

interface ChartVisualTheme {
  palette: string[]
  text: string
  textStrong: string
  axis: string
  split: string
  radarAreas: string[]
  tooltipBackground: string
  tooltipBorder: string
  tooltipText: string
}

const props = defineProps<{
  columns: FieldDefinition[]
  rows: unknown[][]
}>()

const { isDark } = useTheme()
const chartElement = ref<HTMLDivElement | null>(null)
const chartType = ref<ChartType>('bar')
const categoryIndex = ref(0)
const valueIndexes = ref<number[]>([])
const groupIndexes = ref<number[]>([])
const aggregation = ref<Aggregation>('sum')
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const chartTypes = [
  { value: 'bar' as const, label: '分组柱状图', icon: BarChart3 },
  { value: 'stacked-bar' as const, label: '堆叠柱状图', icon: Layers3 },
  { value: 'line' as const, label: '折线图', icon: LineChart },
  { value: 'area' as const, label: '面积图', icon: ChartArea },
  { value: 'pie' as const, label: '饼图', icon: PieChart },
  { value: 'scatter' as const, label: '散点图', icon: ScatterChart },
  { value: 'radar' as const, label: '雷达图', icon: Radar },
]

const aggregationOptions = [
  { value: 'sum' as const, label: '求和' },
  { value: 'average' as const, label: '平均值' },
  { value: 'max' as const, label: '最大值' },
  { value: 'min' as const, label: '最小值' },
]

const chartVisualTheme = computed<ChartVisualTheme>(() => (
  isDark.value
    ? {
        palette: ['#59c9a6', '#78a8ff', '#e3b35a', '#ef7a80', '#a78bfa', '#55c2d6'],
        text: '#a7b7af',
        textStrong: '#d8e5df',
        axis: '#3b4d46',
        split: '#293831',
        radarAreas: ['rgba(89, 201, 166, 0.035)', 'rgba(255, 255, 255, 0.018)'],
        tooltipBackground: 'rgba(17, 27, 23, 0.96)',
        tooltipBorder: '#3b4d46',
        tooltipText: '#e8f0ec',
      }
    : {
        palette: ['#147d64', '#2563eb', '#d28a16', '#c34a52', '#6c5ce7', '#398b9d'],
        text: '#687770',
        textStrong: '#4b5b54',
        axis: '#cbd6d1',
        split: '#e8edeb',
        radarAreas: ['#fbfcfb', '#f4f7f5'],
        tooltipBackground: 'rgba(255, 255, 255, 0.96)',
        tooltipBorder: '#d8e1dd',
        tooltipText: '#1c2924',
      }
))
const MAX_GROUP_COMBINATIONS = 16
const OTHER_GROUP_KEY = '__anydatas_other_groups__'

const numericIndexes = computed(() => props.columns
  .map((column, index) => ({ column, index }))
  .filter(({ column }) => column.dataType === '整数' || column.dataType === '小数')
  .map(({ index }) => index))

const categoryOptions = computed(() => props.columns.map((_, index) => index))

const supportsGrouping = computed(() => (
  chartType.value === 'bar'
  || chartType.value === 'stacked-bar'
  || chartType.value === 'line'
  || chartType.value === 'area'
  || chartType.value === 'scatter'
))

const activeGroupIndexes = computed(() => supportsGrouping.value
  ? groupIndexes.value.filter((index) => (
      index !== categoryIndex.value && !valueIndexes.value.includes(index)
    ))
  : [])

const groupOptions = computed(() => props.columns
  .map((_, index) => index)
  .filter((index) => index !== categoryIndex.value && !valueIndexes.value.includes(index)))

const selectedChartType = computed(() => (
  chartTypes.find((option) => option.value === chartType.value) ?? chartTypes[0]
))

const hasChartData = computed(() => {
  if (!props.rows.length || !valueIndexes.value.length) return false
  return chartType.value !== 'scatter'
    || (Boolean(props.columns[categoryIndex.value])
      && valueIndexes.value.some((index) => (
        numericIndexes.value.includes(index) && index !== categoryIndex.value
      )))
})

const chartOption = computed<EChartsCoreOption>(() => {
  if (!hasChartData.value) return {}
  if (chartType.value === 'scatter') return scatterOption()
  const aggregated = aggregateRows(chartType.value === 'radar' ? 12 : 80)
  if (!aggregated.categories.length) return {}
  if (chartType.value === 'pie') return pieOption(aggregated.categories, aggregated.series)
  if (chartType.value === 'radar') return radarOption(aggregated.categories, aggregated.series)
  return cartesianOption(aggregated.categories, aggregated.series)
})

watch(
  () => props.columns.map((column) => `${column.name}:${column.dataType}`).join('|'),
  () => resetSelections(),
  { immediate: true },
)

watch(chartType, (nextType, previousType) => {
  resetSelections(previousType === 'scatter' && nextType !== 'scatter')
  renderChart()
})

watch(categoryIndex, () => resetSelections())

watch(valueIndexes, normalizeGroupSelections, { deep: true })

// 主题本身作为独立监听源，确保空数据与同构配置场景也会清理旧画布并立即重绘。
watch([chartOption, isDark], renderChart, { deep: true })

onMounted(() => {
  if (!chartElement.value) return
  chart = init(chartElement.value, undefined, { renderer: 'canvas' })
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartElement.value)
  renderChart()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
})

/** 在列或图表类型变化后保留有效选择；离开散点图时恢复常规维度和完整默认指标。 */
function resetSelections(leavingScatter = false) {
  const numeric = numericIndexes.value
  valueIndexes.value = valueIndexes.value
    .filter((index) => numeric.includes(index))
    .slice(0, 4)
  if (chartType.value === 'scatter') {
    const onlyNumericMetricSelectedAsX = numeric.length === 1 && categoryIndex.value === numeric[0]
    if (!props.columns[categoryIndex.value] || onlyNumericMetricSelectedAsX) {
      categoryIndex.value = props.columns.findIndex((_, index) => !numeric.includes(index))
      if (categoryIndex.value < 0) categoryIndex.value = numeric[0] ?? -1
    }
    valueIndexes.value = valueIndexes.value.filter((index) => index !== categoryIndex.value)
    if (!valueIndexes.value.length) {
      valueIndexes.value = numeric.filter((index) => index !== categoryIndex.value).slice(0, 4)
    }
  } else {
    if (!props.columns[categoryIndex.value]) {
      categoryIndex.value = props.columns.findIndex((_, index) => !numeric.includes(index))
      if (categoryIndex.value < 0) categoryIndex.value = 0
    }
    if (leavingScatter) {
      categoryIndex.value = props.columns.findIndex((_, index) => !numeric.includes(index))
      if (categoryIndex.value < 0) categoryIndex.value = 0
      valueIndexes.value = numeric.slice(0, 3)
    } else if (!valueIndexes.value.length) {
      valueIndexes.value = numeric.slice(0, 3)
    }
  }
  normalizeGroupSelections()
}

/** 移除与横轴或指标重复的分组字段，避免同一列同时承担相互冲突的图表角色。 */
function normalizeGroupSelections() {
  groupIndexes.value = groupIndexes.value
    .filter((index) => props.columns[index]
      && index !== categoryIndex.value
      && !valueIndexes.value.includes(index))
    .slice(0, 2)
}

/** 按维度聚合多个数值列，查询结果包含重复分类时图表仍能给出稳定且可比较的序列。 */
function aggregateRows(limit: number) {
  const categories: string[] = []
  const categorySet = new Set<string>()
  const groups: GroupDescriptor[] = []
  const buckets = new Map<string, Map<string, MetricAccumulator[]>>()
  for (const row of props.rows) {
    const category = displayValue(row[categoryIndex.value])
    if (!categorySet.has(category)) {
      if (categories.length >= limit) continue
      categorySet.add(category)
      categories.push(category)
    }
    const group = resolveGroup(row, groups)
    const groupBuckets = buckets.get(group.key) ?? new Map<string, MetricAccumulator[]>()
    const metrics = groupBuckets.get(category) ?? valueIndexes.value.map(() => emptyAccumulator())
    valueIndexes.value.forEach((columnIndex, metricIndex) => {
      const value = numericValue(row[columnIndex])
      if (value === null) return
      const metric = metrics[metricIndex]
      metric.sum += value
      metric.count += 1
      metric.min = Math.min(metric.min, value)
      metric.max = Math.max(metric.max, value)
    })
    groupBuckets.set(category, metrics)
    buckets.set(group.key, groupBuckets)
  }
  const series = groups.flatMap((group) => valueIndexes.value.map((columnIndex, metricIndex) => ({
    name: chartSeriesName(group.label, props.columns[columnIndex]?.name ?? `指标 ${metricIndex + 1}`),
    values: categories.map((category) => (
      aggregateValue(buckets.get(group.key)?.get(category)?.[metricIndex])
    )),
  })))
  return { categories, series }
}

/** 生成柱状、堆叠、折线和面积图的共享坐标轴配置，多个指标各自形成序列。 */
function cartesianOption(categories: string[], series: AggregatedSeries[]): EChartsCoreOption {
  const isBar = chartType.value === 'bar' || chartType.value === 'stacked-bar'
  const isArea = chartType.value === 'area'
  const visual = chartVisualTheme.value
  return {
    animationDuration: 280,
    backgroundColor: 'transparent',
    color: visual.palette,
    textStyle: { color: visual.text },
    tooltip: { ...tooltipStyle('axis'), valueFormatter: formatNumber },
    legend: { type: 'scroll', top: 6, right: 18, textStyle: { color: visual.text, fontSize: 10 } },
    grid: { top: 42, right: 24, bottom: 54, left: 62 },
    xAxis: {
      type: 'category',
      name: props.columns[categoryIndex.value]?.name,
      nameLocation: 'middle',
      nameGap: 34,
      data: categories,
      nameTextStyle: { color: visual.textStrong },
      axisLabel: { color: visual.text, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: visual.axis } },
      axisTick: { lineStyle: { color: visual.axis } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: visual.text, fontSize: 10 },
      axisLine: { lineStyle: { color: visual.axis } },
      axisTick: { lineStyle: { color: visual.axis } },
      splitLine: { lineStyle: { color: visual.split } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: isBar ? 'bar' : 'line',
      data: item.values,
      stack: chartType.value === 'stacked-bar' ? 'total' : undefined,
      smooth: !isBar,
      symbolSize: 6,
      barMaxWidth: 32,
      areaStyle: isArea ? { opacity: 0.16 } : undefined,
      itemStyle: isBar ? { borderRadius: [3, 3, 0, 0] } : undefined,
    })),
  }
}

/** 多指标饼图使用同心环表达不同指标，单指标时保留更易读的标签。 */
function pieOption(categories: string[], series: AggregatedSeries[]): EChartsCoreOption {
  const count = Math.min(series.length, 4)
  const ringWidth = Math.max(10, Math.floor(52 / count))
  const visual = chartVisualTheme.value
  return {
    animationDuration: 280,
    backgroundColor: 'transparent',
    color: visual.palette,
    textStyle: { color: visual.text },
    tooltip: { ...tooltipStyle('item'), valueFormatter: formatNumber },
    legend: { type: 'scroll', bottom: 3, textStyle: { color: visual.text, fontSize: 10 } },
    series: series.slice(0, count).map((item, metricIndex) => {
      const outer = 30 + ringWidth * (metricIndex + 1)
      return {
        name: item.name,
        type: 'pie',
        radius: [`${outer - ringWidth + 2}%`, `${outer}%`],
        center: ['50%', '46%'],
        label: count === 1
          ? { color: visual.textStrong, fontSize: 10, formatter: '{b}: {d}%' }
          : { show: false },
        labelLine: { lineStyle: { color: visual.axis } },
        data: categories.map((name, categoryIndex) => ({
          name,
          value: item.values[categoryIndex] ?? 0,
        })),
      }
    }),
  }
}

/** 雷达图以分类作为指标轴、数值列作为系列，限制十二个分类以保持标签可辨认。 */
function radarOption(categories: string[], series: AggregatedSeries[]): EChartsCoreOption {
  const range = radarValueRange(series)
  const visual = chartVisualTheme.value
  const indicators = categories.map((name) => ({
    name,
    min: range.min,
    max: range.max,
  }))
  return {
    animationDuration: 280,
    backgroundColor: 'transparent',
    color: visual.palette,
    textStyle: { color: visual.text },
    tooltip: tooltipStyle('item'),
    legend: { type: 'scroll', top: 5, right: 18, textStyle: { color: visual.text, fontSize: 10 } },
    radar: {
      center: ['50%', '54%'],
      radius: '66%',
      indicator: indicators,
      axisName: { color: visual.text, fontSize: 10 },
      axisLine: { lineStyle: { color: visual.axis } },
      splitLine: { lineStyle: { color: visual.axis } },
      splitArea: { areaStyle: { color: visual.radarAreas } },
    },
    series: [{
      type: 'radar',
      data: series.map((item) => ({
        name: item.name,
        value: item.values.map((value) => value ?? 0),
        areaStyle: { opacity: 0.08 },
      })),
    }],
  }
}

/**
 * 计算所有雷达轴共用的数值范围；分类轴使用同一刻度后，图形才能忠实表达同一指标的大小差异。
 * 范围始终包含零点并保留少量边距，既兼容负数数据，也避免最大值贴住图表边缘。
 */
function radarValueRange(series: AggregatedSeries[]): { min: number; max: number } {
  const values = series
    .flatMap((item) => item.values)
    .filter((value): value is number => value !== null && Number.isFinite(value))
  if (!values.length || values.every((value) => value === 0)) return { min: 0, max: 1 }

  const rawMin = Math.min(0, ...values)
  const rawMax = Math.max(0, ...values)
  const padding = (rawMax - rawMin) * 0.1
  return {
    min: rawMin < 0 ? rawMin - padding : 0,
    max: rawMax > 0 ? rawMax + padding : 0,
  }
}

/** 散点图支持数值、日期或分类 X 轴，并可按至多两个额外字段拆分多个 Y 序列。 */
function scatterOption(): EChartsCoreOption {
  const visual = chartVisualTheme.value
  const yIndexes = valueIndexes.value.filter((index) => index !== categoryIndex.value)
  const xColumn = props.columns[categoryIndex.value]
  const numericX = numericIndexes.value.includes(categoryIndex.value)
  const temporalX = xColumn?.dataType === '日期' || xColumn?.dataType === '日期时间'
  const xAxisType = numericX ? 'value' : temporalX ? 'time' : 'category'
  const groups: GroupDescriptor[] = []
  const points = new Map<string, Map<number, Array<Array<string | number>>>>()
  const categories: string[] = []
  const categorySet = new Set<string>()

  for (const row of props.rows.slice(0, 500)) {
    const rawX = row[categoryIndex.value]
    const x = numericX ? numericValue(rawX) : displayValue(rawX)
    if (x === null || (temporalX && (rawX === null || rawX === undefined || rawX === ''))) continue
    const yValues = yIndexes.map((index) => numericValue(row[index]))
    if (yValues.every((value) => value === null)) continue
    const group = resolveGroup(row, groups)
    const groupPoints = points.get(group.key) ?? new Map<number, Array<Array<string | number>>>()
    yIndexes.forEach((columnIndex, metricIndex) => {
      const y = yValues[metricIndex]
      if (y === null) return
      const values = groupPoints.get(columnIndex) ?? []
      values.push([x, y])
      groupPoints.set(columnIndex, values)
    })
    points.set(group.key, groupPoints)
    if (xAxisType === 'category' && !categorySet.has(String(x))) {
      categorySet.add(String(x))
      categories.push(String(x))
    }
  }

  return {
    animationDuration: 220,
    backgroundColor: 'transparent',
    color: visual.palette,
    textStyle: { color: visual.text },
    tooltip: tooltipStyle('item'),
    legend: { type: 'scroll', top: 6, right: 18, textStyle: { color: visual.text, fontSize: 10 } },
    grid: { top: 42, right: 24, bottom: 50, left: 62 },
    xAxis: {
      type: xAxisType,
      name: xColumn?.name,
      nameLocation: 'middle',
      nameGap: 30,
      data: xAxisType === 'category' ? categories : undefined,
      nameTextStyle: { color: visual.textStrong },
      axisLabel: { color: visual.text, fontSize: 10 },
      axisLine: { lineStyle: { color: visual.axis } },
      axisTick: { lineStyle: { color: visual.axis } },
      splitLine: { lineStyle: { color: visual.split } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: visual.text, fontSize: 10 },
      axisLine: { lineStyle: { color: visual.axis } },
      axisTick: { lineStyle: { color: visual.axis } },
      splitLine: { lineStyle: { color: visual.split } },
    },
    series: groups.flatMap((group) => yIndexes.map((columnIndex) => ({
      name: chartSeriesName(group.label, props.columns[columnIndex]?.name ?? '指标'),
      type: 'scatter',
      symbolSize: 8,
      data: points.get(group.key)?.get(columnIndex) ?? [],
    }))),
  }
}

/**
 * 生成与当前明暗模式一致的提示框外观。
 * 为什么这么做：ECharts 提示框由自身渲染，无法只依赖页面 CSS 继承主题；
 * 好处：鼠标悬停时的背景、边框和文字也能与画布保持一致，并兼顾可读性。
 *
 * @param trigger 提示框的触发粒度。
 * @returns 可复用于各类图表的提示框配置。
 */
function tooltipStyle(trigger: 'axis' | 'item') {
  const visual = chartVisualTheme.value
  return {
    trigger,
    backgroundColor: visual.tooltipBackground,
    borderColor: visual.tooltipBorder,
    textStyle: { color: visual.tooltipText },
  }
}

/** 把当前行映射到稳定的系列分组，并将过多组合合并为“其他分组”控制图例规模。 */
function resolveGroup(row: unknown[], groups: GroupDescriptor[]): GroupDescriptor {
  if (!activeGroupIndexes.value.length) {
    const group = groups[0] ?? { key: '__anydatas_all__', label: '' }
    if (!groups.length) groups.push(group)
    return group
  }
  const values = activeGroupIndexes.value.map((index) => displayValue(row[index]))
  const key = `group:${JSON.stringify(values)}`
  const existing = groups.find((group) => group.key === key)
  if (existing) return existing
  const namedCount = groups.filter((group) => group.key !== OTHER_GROUP_KEY).length
  if (namedCount >= MAX_GROUP_COMBINATIONS) {
    const other = groups.find((group) => group.key === OTHER_GROUP_KEY)
      ?? { key: OTHER_GROUP_KEY, label: '其他分组' }
    if (!groups.includes(other)) groups.push(other)
    return other
  }
  const label = activeGroupIndexes.value
    .map((index, valueIndex) => `${props.columns[index]?.name}: ${values[valueIndex]}`)
    .join(' / ')
  const group = { key, label }
  groups.push(group)
  return group
}

/** 组合分组值和指标名；单指标分组图优先显示更易扫描的分组名称。 */
function chartSeriesName(groupLabel: string, metricName: string): string {
  if (!groupLabel) return metricName
  return valueIndexes.value.length === 1 ? groupLabel : `${groupLabel} · ${metricName}`
}

/** 为每个维度和指标创建独立累加器，避免不同序列之间共享聚合状态。 */
function emptyAccumulator(): MetricAccumulator {
  return { sum: 0, count: 0, min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY }
}

/** 根据当前聚合方式返回数值；无有效样本时保留空值而不是误显示为零。 */
function aggregateValue(metric: MetricAccumulator | undefined): number | null {
  if (!metric?.count) return null
  if (aggregation.value === 'average') return metric.sum / metric.count
  if (aggregation.value === 'max') return metric.max
  if (aggregation.value === 'min') return metric.min
  return metric.sum
}

/** 使用完整替换更新 ECharts 配置，切换图表类型时不会残留旧系列和坐标轴。 */
function renderChart() {
  if (!chart) return
  chart.setOption(chartOption.value, true)
}

/** 把维度值转换为稳定标签，显式区分空值并兼容对象型查询结果。 */
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '空值'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

/** 只接受有限数值，避免 NaN 或 Infinity 破坏聚合比例和坐标轴。 */
function numericValue(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? number : null
}

/** 用中文数字格式压缩图表提示文本，同时保留最多四位小数用于分析。 */
function formatNumber(value: unknown): string {
  const number = numericValue(value)
  return number === null ? '—' : number.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}
</script>

<template>
  <div class="result-chart-panel">
    <div
      class="chart-controls"
      :class="{
        'with-grouping': supportsGrouping,
        'without-aggregation': chartType === 'scatter',
      }"
    >
      <label>
        <span>图表</span>
        <el-select v-model="chartType" size="small">
          <template #prefix><component :is="selectedChartType.icon" :size="14" /></template>
          <el-option
            v-for="option in chartTypes"
            :key="option.value"
            :label="option.label"
            :value="option.value"
            :aria-label="option.label"
          >
            <span class="chart-option"><component :is="option.icon" :size="14" />{{ option.label }}</span>
          </el-option>
        </el-select>
      </label>
      <label>
        <span>{{ chartType === 'scatter' ? 'X 轴' : '维度' }}</span>
        <el-select v-model="categoryIndex" size="small">
          <el-option
            v-for="index in categoryOptions"
            :key="`${columns[index]?.name}-${index}`"
            :label="columns[index]?.name"
            :value="index"
            :disabled="chartType === 'scatter' && numericIndexes.length === 1 && numericIndexes.includes(index)"
            :aria-label="columns[index]?.name"
          />
        </el-select>
      </label>
      <label v-if="supportsGrouping" class="chart-group-control">
        <span>分组</span>
        <el-select
          v-model="groupIndexes"
          multiple
          collapse-tags
          :max-collapse-tags="1"
          :multiple-limit="2"
          clearable
          placeholder="可选"
          size="small"
        >
          <el-option
            v-for="index in groupOptions"
            :key="`${columns[index]?.name}-${index}`"
            :label="columns[index]?.name"
            :value="index"
            :aria-label="columns[index]?.name"
          />
        </el-select>
      </label>
      <label class="chart-metrics-control">
        <span>{{ chartType === 'scatter' ? 'Y 轴' : '指标' }}</span>
        <el-select
          v-model="valueIndexes"
          multiple
          collapse-tags
          :max-collapse-tags="1"
          :multiple-limit="4"
          size="small"
        >
          <el-option
            v-for="index in numericIndexes.filter((item) => chartType !== 'scatter' || item !== categoryIndex)"
            :key="`${columns[index]?.name}-${index}`"
            :label="columns[index]?.name"
            :value="index"
            :aria-label="columns[index]?.name"
          />
        </el-select>
      </label>
      <label v-if="chartType !== 'scatter'">
        <span>聚合</span>
        <el-select v-model="aggregation" size="small">
          <el-option
            v-for="option in aggregationOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </label>
    </div>
    <div v-show="hasChartData" ref="chartElement" class="result-chart" />
    <div v-if="!hasChartData" class="chart-empty">
      <ChartNoAxesCombined :size="28" />
      <span>当前字段不足以生成所选图表</span>
    </div>
  </div>
</template>
