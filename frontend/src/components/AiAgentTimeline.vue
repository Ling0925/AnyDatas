<script setup lang="ts">
import { computed } from 'vue'
import {
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  Code2,
  Database,
  LoaderCircle,
  RefreshCw,
  Square,
} from '@lucide/vue'

import type { AiAgentReasoningEffort, AiAgentRun, AiAgentRunStep } from '../types'

interface TimelineItem {
  step: AiAgentRunStep
  title: string
  reasoning: string | null
  sql: string | null
  alias: string | null
  outcome: string | null
}

const props = withDefaults(defineProps<{
  run: AiAgentRun
  retryable?: boolean
}>(), {
  retryable: false,
})

const emit = defineEmits<{
  retry: []
}>()

const isActive = computed(() => ['queued', 'running'].includes(props.run.status))
const hasStreamingAnswer = computed(() => props.run.steps.some((step) => {
  if (step.kind !== 'model' || step.status !== 'running') return false
  const output = recordValue(step.output)
  return typeof output?.content === 'string' && output.content.trim().length > 0
}))
const timelineItems = computed<TimelineItem[]>(() => props.run.steps
  .filter((step) => step.kind === 'tool')
  .map((step) => ({
    step,
    title: stepTitle(step),
    reasoning: stepText(step, 'reasoningSummary'),
    sql: stepText(step, 'sql'),
    alias: stepText(step, 'alias'),
    outcome: stepOutcome(step),
  })))
const waitingForModel = computed(() => {
  if (!isActive.value || hasStreamingAnswer.value) return false
  const lastStep = props.run.steps[props.run.steps.length - 1]
  return !lastStep || lastStep.kind === 'model' || lastStep.status !== 'running'
})

/** 根据 Run 状态生成稳定的标题，具体动作标题仍全部来自模型工具参数。 */
function runTitle(): string {
  if (props.run.status === 'completed') return 'Agent 已完成分析'
  if (props.run.status === 'failed') return 'Agent 运行失败'
  if (props.run.status === 'canceled') return 'Agent 已停止'
  if (timelineItems.value.length) return 'Agent 正在验证分析'
  return 'Agent 正在思考'
}

/** 将本次 Run 的持久化思考等级显示为短标签，历史执行也能看出当时选择。 */
function reasoningLabel(value: AiAgentReasoningEffort): string {
  if (value === 'low') return '快速'
  if (value === 'high') return '深入'
  return '均衡'
}

/** 读取模型生成的步骤标题；旧 Run 没有新字段时才回退到兼容名称。 */
function stepTitle(step: AiAgentRunStep): string {
  const title = stepText(step, 'stepTitle')
  if (title) return title
  if (step.toolName === 'inspect_table') return '检查逻辑表样本'
  if (step.toolName === 'preview_sql') return '验证 SQL 查询'
  return step.toolName ? `执行 ${step.toolName}` : '执行只读工具'
}

/** 从已持久化的工具输入读取字符串，异常历史数据不会影响整个时间轴。 */
function stepText(step: AiAgentRunStep, key: string): string | null {
  const input = recordValue(step.input)
  const value = input?.[key]
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** 从工具输出提取行数和耗时，用户无需展开 SQL 就能判断本步是否产生有效观察。 */
function stepOutcome(step: AiAgentRunStep): string | null {
  const output = recordValue(step.output)
  const result = recordValue(output?.result)
  if (!result) return null
  const rowCount = typeof result.rowCount === 'number' ? `${result.rowCount} 行` : null
  const elapsed = typeof result.elapsedMs === 'number' ? `${result.elapsedMs} ms` : null
  return [rowCount, elapsed].filter(Boolean).join(' · ') || null
}

/** 仅接受普通对象作为结构化数据，数组和空值保持不可解析状态。 */
function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

/** 将步骤状态翻译为界面文案，失败原因由独立区域完整展示。 */
function stepStatus(step: AiAgentRunStep): string {
  if (step.status === 'running') return '执行中'
  if (step.status === 'completed') return '已完成'
  if (step.status === 'canceled') return '已停止'
  return '失败'
}

/** 显示步骤开始时间，时间信息放在标题右侧而不挤压时间轴主内容。 */
function stepClock(step: AiAgentRunStep): string {
  return new Date(step.startedAt).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** 计算已结束步骤耗时，长操作使用秒，小操作保留毫秒便于性能判断。 */
function stepDuration(step: AiAgentRunStep): string | null {
  if (!step.finishedAt) return null
  const elapsed = new Date(step.finishedAt).getTime() - new Date(step.startedAt).getTime()
  if (!Number.isFinite(elapsed) || elapsed < 0) return null
  return elapsed < 1_000 ? `${elapsed} ms` : `${(elapsed / 1_000).toFixed(1)} s`
}
</script>

<template>
  <details class="agent-run-timeline" :open="isActive || retryable">
    <summary class="agent-run-summary">
      <span class="agent-run-summary-icon">
        <LoaderCircle v-if="isActive" class="ai-spin" :size="16" />
        <CircleAlert v-else-if="run.status === 'failed'" :size="16" />
        <Square v-else-if="run.status === 'canceled'" :size="14" />
        <Check v-else :size="16" />
      </span>
      <span class="agent-run-summary-copy">
        <strong>{{ runTitle() }}</strong>
        <small>{{ timelineItems.length }} 个数据步骤</small>
      </span>
      <span class="agent-run-meta">
        <em>{{ reasoningLabel(run.reasoningEffort) }}思考</em>
        <span class="agent-run-model">{{ run.model }}</span>
      </span>
      <ChevronDown class="agent-run-chevron" :size="15" />
    </summary>

    <div class="agent-run-content">
      <ol v-if="timelineItems.length" class="agent-step-timeline">
        <li
          v-for="item in timelineItems"
          :key="item.step.id"
          class="agent-step"
          :class="item.step.status"
        >
          <span class="agent-step-marker">
            <LoaderCircle v-if="item.step.status === 'running'" class="ai-spin" :size="13" />
            <CircleAlert v-else-if="item.step.status === 'failed'" :size="13" />
            <Square v-else-if="item.step.status === 'canceled'" :size="11" />
            <Check v-else :size="13" />
          </span>

          <div class="agent-step-content">
            <header class="agent-step-header">
              <div>
                <strong>{{ item.title }}</strong>
                <span>{{ item.step.toolName === 'inspect_table' ? '读取数据' : 'SQL 验证' }}</span>
              </div>
              <div class="agent-step-meta">
                <span>{{ stepStatus(item.step) }}</span>
                <time :datetime="item.step.startedAt">{{ stepClock(item.step) }}</time>
              </div>
            </header>

            <div v-if="item.reasoning" class="agent-step-reasoning">
              <Brain :size="14" />
              <p><strong>思考摘要</strong>{{ item.reasoning }}</p>
            </div>

            <div v-if="item.outcome || stepDuration(item.step)" class="agent-step-outcome">
              <span v-if="item.outcome">{{ item.outcome }}</span>
              <span v-if="stepDuration(item.step)"><Clock3 :size="12" />{{ stepDuration(item.step) }}</span>
            </div>

            <div v-if="item.alias && !item.sql" class="agent-step-parameter">
              <Database :size="13" />
              <span>逻辑表：{{ item.alias }}</span>
            </div>

            <details v-if="item.sql" class="agent-step-detail">
              <summary>
                <Code2 :size="13" />
                <span>查看执行 SQL</span>
                <ChevronDown :size="13" />
              </summary>
              <pre><code>{{ item.sql }}</code></pre>
            </details>

            <div v-if="item.step.errorMessage" class="agent-step-error">
              <CircleAlert :size="13" />
              <span>{{ item.step.errorMessage }}</span>
            </div>
          </div>
        </li>
      </ol>

      <div v-if="waitingForModel" class="agent-thinking-state">
        <span><LoaderCircle class="ai-spin" :size="15" /></span>
        <div>
          <strong>正在形成下一步</strong>
          <small>模型正在结合最新工具结果继续分析</small>
        </div>
      </div>

      <div v-if="retryable" class="agent-run-recovery">
        <span>{{ run.errorMessage || '本次运行已停止，可以从原需求继续重试。' }}</span>
        <el-button size="small" @click.stop="emit('retry')">
          <RefreshCw :size="13" />重试
        </el-button>
      </div>
    </div>
  </details>
</template>

<style scoped>
.agent-run-timeline {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  background: var(--panel);
}

.agent-run-summary {
  min-height: 46px;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto 18px;
  align-items: center;
  gap: 9px;
  padding: 7px 12px;
  color: var(--text-secondary);
  background: var(--primary-soft);
  cursor: pointer;
  list-style: none;
}

.agent-run-summary::-webkit-details-marker,
.agent-step-detail > summary::-webkit-details-marker {
  display: none;
}

.agent-run-summary-icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--primary-border);
  border-radius: 6px;
  background: var(--panel-muted);
}

.agent-run-summary-copy {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.agent-run-summary-copy strong {
  overflow: hidden;
  font-size: 14px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-run-summary-copy small,
.agent-run-model {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.35;
}

.agent-run-meta {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.agent-run-meta em {
  flex: 0 0 auto;
  padding: 2px 6px;
  border: 1px solid var(--primary-border);
  border-radius: 4px;
  color: var(--primary-text);
  background: var(--panel-muted);
  font-size: 10px;
  font-style: normal;
  line-height: 1.35;
}

.agent-run-model {
  max-width: 150px;
  overflow: hidden;
  font-family: "SFMono-Regular", Consolas, monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-run-chevron {
  transition: transform 160ms ease;
}

.agent-run-timeline[open] > .agent-run-summary .agent-run-chevron,
.agent-step-detail[open] > summary svg:last-child {
  transform: rotate(180deg);
}

.agent-run-content {
  padding: 17px 18px 16px;
}

.agent-step-timeline {
  margin: 0;
  padding: 0;
  list-style: none;
}

.agent-step {
  position: relative;
  min-width: 0;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 13px;
  padding: 0 0 20px;
}

.agent-step:last-child {
  padding-bottom: 0;
}

.agent-step:not(:last-child)::after {
  position: absolute;
  top: 29px;
  bottom: 0;
  left: 13px;
  width: 1px;
  background: var(--line-strong);
  content: "";
}

.agent-step-marker {
  position: relative;
  z-index: 1;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--primary-border);
  border-radius: 50%;
  color: var(--primary-text);
  background: var(--panel-muted);
}

.agent-step.failed .agent-step-marker,
.agent-step.canceled .agent-step-marker {
  border-color: var(--red-border);
  color: var(--red);
  background: var(--red-soft);
}

.agent-step-content {
  min-width: 0;
  padding-top: 2px;
}

.agent-step-header {
  min-height: 27px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.agent-step-header > div:first-child {
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.agent-step-header strong {
  color: var(--text);
  font-size: 14px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.agent-step-header > div:first-child > span {
  color: var(--muted);
  font-size: 11px;
}

.agent-step-meta {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--subtle);
  font-size: 11px;
  line-height: 1.55;
  white-space: nowrap;
}

.agent-step-meta time {
  font-family: "SFMono-Regular", Consolas, monospace;
}

.agent-step-reasoning {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  gap: 7px;
  margin-top: 7px;
  padding: 9px 10px;
  color: var(--text-secondary);
  background: var(--panel-muted);
}

.agent-step-reasoning > svg {
  margin-top: 2px;
  color: var(--primary-text);
}

.agent-step-reasoning p {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.agent-step-reasoning p strong {
  display: block;
  margin-bottom: 2px;
  color: var(--text);
  font-size: 11px;
}

.agent-step-outcome {
  min-height: 24px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 5px;
  color: var(--muted);
  font-size: 11px;
}

.agent-step-outcome span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.agent-step-detail {
  margin-top: 5px;
}

.agent-step-parameter {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 5px;
  color: var(--muted);
  font-size: 11px;
}

.agent-step-detail > summary {
  width: fit-content;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  list-style: none;
}

.agent-step-detail > summary svg:last-child {
  transition: transform 160ms ease;
}

.agent-step-detail pre {
  max-height: 170px;
  margin: 8px 0 0;
  overflow: auto;
  padding: 10px 11px;
  border: 1px solid var(--line);
  color: var(--text);
  background: var(--code-bg);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  line-height: 1.55;
  white-space: pre;
}

.agent-step-error {
  display: grid;
  grid-template-columns: 15px minmax(0, 1fr);
  gap: 6px;
  margin-top: 7px;
  color: var(--red);
  font-size: 12px;
  line-height: 1.55;
}

.agent-step-error svg {
  margin-top: 2px;
}

.agent-thinking-state {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  align-items: center;
  gap: 13px;
  margin-top: 14px;
  color: var(--text-secondary);
}

.agent-thinking-state > span {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--primary-border);
  border-radius: 50%;
}

.agent-thinking-state > div {
  display: grid;
  gap: 2px;
}

.agent-thinking-state strong {
  font-size: 13px;
}

.agent-thinking-state small {
  color: var(--subtle);
  font-size: 11px;
}

.agent-run-recovery {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  margin: 14px -18px -16px;
  padding: 10px 12px;
  border-top: 1px solid var(--red-border);
  color: var(--red);
  background: var(--red-soft);
  font-size: 11px;
  line-height: 1.5;
}
</style>
