<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Bot,
  Brain,
  Check,
  Copy,
  Database,
  Eye,
  FilePenLine,
  History,
  LoaderCircle,
  MessageSquarePlus,
  Play,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Square,
  Trash2,
  UserRound,
  Wrench,
} from '@lucide/vue'

import { api, errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'
import type {
  AgentChartSpec,
  AiAgentConversationDetail,
  AiAgentConversationSummary,
  AiAgentMessage,
  AiAgentReasoningEffort,
  AiAgentRun,
  AiToolRun,
  QueryResponse,
} from '../types'
import AiMarkdown from './AiMarkdown.vue'
import AiAgentTimeline from './AiAgentTimeline.vue'
import AiResultPreview from './AiResultPreview.vue'
import AiChartPreview from './AiChartPreview.vue'

const emit = defineEmits<{
  applySql: [payload: { sql: string; chart?: AgentChartSpec }]
  runSql: [payload: { sql: string; chart?: AgentChartSpec }]
}>()

const auth = useAuthStore()
const store = useWorkspaceStore()
const conversations = ref<AiAgentConversationSummary[]>([])
const activeConversation = ref<AiAgentConversationDetail | null>(null)
const activeRun = ref<AiAgentRun | null>(null)
const draft = ref('')
const reasoningEffort = ref<AiAgentReasoningEffort>(loadReasoningEffort())
const includeResultContext = ref(false)
const listLoading = ref(false)
const conversationLoading = ref(false)
const startingRun = ref(false)
const stoppingRun = ref(false)
const previewingId = ref<string | null>(null)
const manualPreviews = ref<Record<string, QueryResponse>>({})
const previewErrors = ref<Record<string, string>>({})
const conversationSearch = ref('')
const messageList = ref<HTMLDivElement | null>(null)
let pollGeneration = 0
let runEventSource: EventSource | null = null

const reasoningOptions = [
  { label: '快速', value: 'low' },
  { label: '均衡', value: 'medium' },
  { label: '深入', value: 'high' },
]

const dataStarterPrompts = [
  '先检查已选表的结构和实际数据，帮我识别适合分析的指标',
  '检查已选表是否存在口径或关联问题',
  '根据当前表格上下文设计一套分析方案',
]
const generalStarterPrompts = [
  '帮我梳理这个分析需求，先列出需要确认的业务口径',
  '给我一套从原始数据到分析结论的实施步骤',
  '解释一下怎样设计一条清晰、可复核的数据分析流程',
]

const messages = computed(() => activeConversation.value?.messages ?? [])
const filteredConversations = computed(() => {
  const query = conversationSearch.value.trim().toLocaleLowerCase()
  if (!query) return conversations.value
  return conversations.value.filter((conversation) => (
    conversation.title.toLocaleLowerCase().includes(query)
  ))
})
const sending = computed(() => startingRun.value || isActiveRun(activeRun.value))
const currentContextReady = computed(() => store.agentContextReady)
const currentContextSignature = computed(() => store.agentTableBindings
  .map((binding) => {
    const version = store.sourceTables.find((table) => table.id === binding.tableId)?.configVersion ?? 0
    return `${binding.tableId}:${binding.alias}:${version}`
  })
  .join('|'))
const contextChanged = computed(() => Boolean(
  activeConversation.value
  && currentContextReady.value
  && activeConversation.value.conversation.contextSignature !== currentContextSignature.value,
))
const canSend = computed(() => Boolean(
  draft.value.trim()
  && currentContextReady.value
  && !sending.value
  && !contextChanged.value,
))
const canUseAgentSql = computed(() => Boolean(
  store.agentTableBindings.length
  && currentContextReady.value
  && !contextChanged.value,
))
/** 当发送被禁用时给出可读原因，避免按钮静默置灰让人以为界面坏了。 */
const sendDisabledReason = computed(() => {
  if (sending.value) return ''
  if (contextChanged.value) return '数据上下文已变更，请先按新选择继续或恢复原选择'
  if (!currentContextReady.value) return '所选表格无效或超过 16 张，请调整右侧数据上下文'
  if (!draft.value.trim()) return '请先输入问题'
  return ''
})
const sqlDisabledReason = computed(() => {
  if (contextChanged.value) return '数据上下文已变更，请先确认或恢复选择'
  if (!store.agentTableBindings.length) return '当前为纯对话模式，请先在右侧选择数据表'
  if (!currentContextReady.value) return '所选表格无效或超过 16 张'
  return ''
})
const starterPrompts = computed(() => (
  store.agentTableBindings.length ? dataStarterPrompts : generalStarterPrompts
))
const workbenchContextMatches = computed(() => {
  if (!store.agentTableBindings.length || !currentContextReady.value) return false
  if (store.agentTableBindings.length !== store.queryBindings.length) return false
  return store.agentTableBindings.every((binding, index) => {
    const queryBinding = store.queryBindings[index]
    return queryBinding?.tableId === binding.tableId && queryBinding.alias === binding.alias
  })
})
const slashCommandVisible = computed(() => {
  const value = draft.value.trim().toLocaleLowerCase()
  if (value === '/') return true
  return value.startsWith('/') && ['/all', '/clear'].some((command) => command.startsWith(value))
})
const contextLabel = computed(() => {
  const tables = store.agentTableBindings.length
    ? `${store.agentBoundTables.length} 张表`
    : '未选择表格'
  if (sending.value) return `${tables} · Agent 运行中`
  return workbenchContextMatches.value && store.queryResult && includeResultContext.value
    ? `${tables} · 含结果样本`
    : tables
})
const agentModeLabel = computed(() => (
  store.agentTableBindings.length ? '自动规划 · 只读工具' : '仅对话模式'
))
const runNeedsAttention = computed(() => Boolean(
  activeRun.value
  && !activeRun.value.assistantMessageId
  && ['failed', 'canceled'].includes(activeRun.value.status),
))
const latestRunHasToolSteps = computed(() => Boolean(
  activeRun.value?.steps.some((step) => step.kind === 'tool'),
))
const streamingContent = computed(() => {
  const run = activeRun.value
  if (!isActiveRun(run)) return ''
  const modelStep = [...run.steps]
    .reverse()
    .find((step) => step.kind === 'model' && step.status === 'running')
  const output = recordValue(modelStep?.output)
  return typeof output?.content === 'string' ? output.content : ''
})

watch(
  () => auth.user?.userId,
  (userId) => {
    resetState()
    if (userId) void initializeConversations()
  },
  { immediate: true },
)
watch(
  () => [messages.value.length, activeRun.value?.stepCount, streamingContent.value.length],
  () => { void scrollToBottom(false) },
)
watch(reasoningEffort, (value) => {
  window.localStorage.setItem('anydatas.agent.reasoningEffort', value)
})
watch(workbenchContextMatches, (matches) => {
  if (!matches) includeResultContext.value = false
})
onBeforeUnmount(() => {
  invalidateRunTracking()
})

/** 恢复用户上次使用的思考等级，异常或旧值统一回退为均衡。 */
function loadReasoningEffort(): AiAgentReasoningEffort {
  const value = window.localStorage.getItem('anydatas.agent.reasoningEffort')
  return value === 'low' || value === 'high' ? value : 'medium'
}

/** 只把普通对象作为模型步骤输出读取，损坏的历史 JSON 不会中断聊天渲染。 */
function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

/** 关闭当前事件流并使旧回调失效，切换会话时不会继续写入上一段 Run。 */
function invalidateRunTracking() {
  pollGeneration += 1
  runEventSource?.close()
  runEventSource = null
}

/** 切换登录身份时清空内存视图，所有持久化历史随后从当前用户的服务端空间重新加载。 */
function resetState() {
  invalidateRunTracking()
  conversations.value = []
  activeConversation.value = null
  activeRun.value = null
  store.clearAgentTableBindings()
  draft.value = ''
  conversationSearch.value = ''
  manualPreviews.value = {}
  previewErrors.value = {}
}

/** 初始化服务端会话列表并打开最近会话，若其 Run 尚未结束则自动恢复实时订阅。 */
async function initializeConversations() {
  listLoading.value = true
  try {
    conversations.value = await api.listAgentConversations()
    const first = conversations.value[0]
    if (first) await openConversation(first.id)
  } catch (error) {
    ElMessage.error(`AI 会话加载失败：${errorMessage(error)}`)
  } finally {
    listLoading.value = false
  }
}

/** 仅刷新列表摘要，不替换当前消息，运行结束后可更新标题和最终状态而不闪烁界面。 */
async function refreshConversationList() {
  conversations.value = await api.listAgentConversations()
}

/** 打开指定服务端会话并接管其最近 Run；切换会话不会停止另一个后台运行。 */
async function openConversation(id: string) {
  invalidateRunTracking()
  conversationLoading.value = true
  try {
    const detail = await api.getAgentConversation(id)
    activeConversation.value = detail
    activeRun.value = detail.latestRun
    store.setAgentTableBindings(detail.conversation.tables)
    manualPreviews.value = {}
    previewErrors.value = {}
    if (isActiveRun(detail.latestRun)) void trackRun(detail.latestRun)
    await scrollToBottom()
  } catch (error) {
    ElMessage.error(`AI 会话读取失败：${errorMessage(error)}`)
  } finally {
    conversationLoading.value = false
  }
}

/**
 * 进入本地新对话草稿态并清空表格选择，首次发送时才创建服务端记录。
 * 这样每个新对话都真正从零上下文开始，也不会制造没有消息的空历史。
 */
async function startNewConversation() {
  invalidateRunTracking()
  activeConversation.value = null
  activeRun.value = null
  store.clearAgentTableBindings()
  includeResultContext.value = false
  draft.value = ''
  manualPreviews.value = {}
  previewErrors.value = {}
  await scrollToBottom()
}

/** 归档当前历史项并切换到下一条会话；后台运行中的会话由后端拒绝归档。 */
async function archiveConversation(conversation: AiAgentConversationSummary) {
  try {
    await ElMessageBox.confirm(`归档“${conversation.title}”？`, '归档 AI 对话', {
      type: 'warning',
      confirmButtonText: '归档',
      cancelButtonText: '取消',
    })
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    throw error
  }
  try {
    await api.archiveAgentConversation(conversation.id)
    invalidateRunTracking()
    conversations.value = conversations.value.filter((item) => item.id !== conversation.id)
    if (activeConversation.value?.conversation.id === conversation.id) {
      activeConversation.value = null
      activeRun.value = null
      const next = conversations.value[0]
      if (next) await openConversation(next.id)
      else store.clearAgentTableBindings()
    }
    ElMessage.success('AI 对话已归档')
  } catch (error) {
    ElMessage.error(`归档失败：${errorMessage(error)}`)
  }
}

/**
 * 表格选择发生变化时从当前历史分叉为本地新对话，保留用户刚刚做出的选择。
 * 不原地改写旧会话可避免已排除表格仍从历史消息或工具结果进入模型。
 */
async function continueWithCurrentSelection() {
  invalidateRunTracking()
  activeConversation.value = null
  activeRun.value = null
  includeResultContext.value = false
  manualPreviews.value = {}
  previewErrors.value = {}
  await scrollToBottom()
}

/**
 * 重新读取当前历史会话，同时恢复它固化的表格快照。
 * 这是取消误操作的安全路径，不会把新选择写回已有消息历史。
 */
async function restoreConversationSelection() {
  const id = activeConversation.value?.conversation.id
  if (id) await openConversation(id)
}

/** 将起始问题放入输入框，让用户可以补充业务口径后再发送。 */
function selectStarter(prompt: string) {
  draft.value = prompt
}

/** 确保首次发送拥有服务端会话，创建过程不会清空已经输入的草稿。 */
async function ensureConversation(): Promise<AiAgentConversationDetail> {
  if (activeConversation.value) return activeConversation.value
  const detail = await api.createAgentConversation(store.agentTableBindings)
  activeConversation.value = detail
  activeRun.value = detail.latestRun
  await refreshConversationList()
  return detail
}

/**
 * 仅发送本轮增量和小型结果样本；历史、摘要、工具观察都由后端 Agent Runtime 负责。
 * API 返回 202 后立即刷新用户消息，再通过 Run 事件流观察真实模型与工具步骤。
 */
async function sendMessage() {
  const content = draft.value.trim()
  if (!content || sending.value) return
  if (/^\/all(?:\s+|$)/i.test(content)) {
    await applyAllTablesCommand(true)
    return
  }
  if (!currentContextReady.value) {
    ElMessage.warning('所选表格已失效，请先从右侧移除或重新选择')
    return
  }
  if (contextChanged.value) {
    ElMessage.warning('请先确认当前数据上下文')
    return
  }
  startingRun.value = true
  try {
    const detail = await ensureConversation()
    const run = await api.startAgentRun(detail.conversation.id, {
      message: content,
      currentSql: workbenchContextMatches.value && store.currentSql.trim()
        ? store.currentSql
        : undefined,
      tables: store.agentTableBindings,
      reasoningEffort: reasoningEffort.value,
      resultContext: workbenchContextMatches.value
        && includeResultContext.value
        && store.queryResult
        ? {
            columns: store.queryResult.columns.slice(0, 20),
            rows: store.queryResult.rows.slice(0, 8).map((row) => row.slice(0, 20)),
            rowCount: store.queryResult.rowCount,
            truncated: store.queryResult.truncated
              || store.queryResult.rows.length > 8
              || store.queryResult.columns.length > 20,
          }
        : undefined,
    })
    draft.value = ''
    activeRun.value = run
    activeConversation.value = await api.getAgentConversation(detail.conversation.id)
    await refreshConversationList()
    void trackRun(run)
  } catch (error) {
    ElMessage.error(`AI 请求失败：${errorMessage(error)}`)
  } finally {
    startingRun.value = false
    await scrollToBottom()
  }
}

/**
 * 优先通过 SSE 接收持久化 Run 快照，使模型公开文本和工具步骤都能实时增长。
 * 浏览器或代理不支持事件流时自动回退到轮询，后台 Run 本身不会依赖连接存活。
 */
function trackRun(initialRun: AiAgentRun) {
  runEventSource?.close()
  const generation = ++pollGeneration
  let run = initialRun
  activeRun.value = run
  if (typeof EventSource === 'undefined') {
    void pollRun(run, generation)
    return
  }

  const source = new EventSource(api.agentRunEventsUrl(run.id), { withCredentials: true })
  runEventSource = source
  source.addEventListener('run', (event) => {
    if (generation !== pollGeneration) {
      source.close()
      return
    }
    try {
      run = JSON.parse((event as MessageEvent<string>).data) as AiAgentRun
      activeRun.value = run
      if (!isActiveRun(run)) {
        source.close()
        if (runEventSource === source) runEventSource = null
        void finishRunTracking(run, generation)
      }
    } catch {
      source.close()
      if (runEventSource === source) runEventSource = null
      void pollRun(run, generation)
    }
  })
  source.addEventListener('run-error', () => {
    source.close()
    if (runEventSource === source) runEventSource = null
    if (generation === pollGeneration) void pollRun(run, generation)
  })
  source.onerror = () => {
    source.close()
    if (runEventSource === source) runEventSource = null
    if (generation === pollGeneration && isActiveRun(run)) void pollRun(run, generation)
  }
}

/** 事件流不可用时恢复原有短轮询；连续三次失败才提示，容忍局域网瞬时抖动。 */
async function pollRun(initialRun: AiAgentRun, generation: number) {
  let run = initialRun
  let failures = 0
  while (isActiveRun(run) && generation === pollGeneration) {
    await delay(700)
    if (generation !== pollGeneration) return
    try {
      run = await api.getAgentRun(run.id)
      activeRun.value = run
      failures = 0
    } catch (error) {
      failures += 1
      if (failures < 3) continue
      ElMessage.error(`Agent 状态同步失败：${errorMessage(error)}`)
      return
    }
  }
  if (generation === pollGeneration) await finishRunTracking(run, generation)
}

/** Run 收敛后刷新服务端消息和会话摘要，流式临时文本会被最终持久化消息无缝替换。 */
async function finishRunTracking(run: AiAgentRun, generation: number) {
  if (generation !== pollGeneration) return
  try {
    activeConversation.value = await api.getAgentConversation(run.conversationId)
    if (generation !== pollGeneration) return
    activeRun.value = activeConversation.value.latestRun
    await refreshConversationList()
    await scrollToBottom()
  } catch (error) {
    ElMessage.error(`Agent 结果刷新失败：${errorMessage(error)}`)
  }
}

/** 停止服务端 Run，同时中断模型等待和正在执行的 DuckDB 工具查询。 */
async function stopGenerating() {
  const run = activeRun.value
  if (!isActiveRun(run) || stoppingRun.value) return
  stoppingRun.value = true
  try {
    invalidateRunTracking()
    activeRun.value = await api.cancelAgentRun(run.id)
    if (activeConversation.value) {
      activeConversation.value = await api.getAgentConversation(activeConversation.value.conversation.id)
      activeRun.value = activeConversation.value.latestRun
    }
    await refreshConversationList()
  } catch (error) {
    ElMessage.error(`停止失败：${errorMessage(error)}`)
  } finally {
    stoppingRun.value = false
  }
}

/** 原位重试最近失败或停止的 Run，不制造重复用户消息。 */
async function retryRun() {
  const run = activeRun.value
  if (!run || !['failed', 'canceled'].includes(run.status) || sending.value) return
  try {
    const retried = await api.retryAgentRun(run.id)
    activeRun.value = retried
    if (activeConversation.value) {
      activeConversation.value = await api.getAgentConversation(activeConversation.value.conversation.id)
    }
    void trackRun(retried)
  } catch (error) {
    ElMessage.error(`重试失败：${errorMessage(error)}`)
  }
}

/**
 * 判断某条助手消息对应的 Run 是否失败/取消：这类 Run 即便已产出部分回复，也应显示可见的
 * 重试入口，而不是只在“完全没有回复”时才可重试。
 */
function messageRunFailed(message: AiAgentMessage): boolean {
  const run = activeRun.value
  if (!run || message.role !== 'assistant') return false
  return run.assistantMessageId === message.id && ['failed', 'canceled'].includes(run.status)
}

/** 从指定助手答复处分叉，后端负责 superseded 标记和历史摘要重建。 */
async function regenerateMessage(message: AiAgentMessage) {
  const conversation = activeConversation.value
  if (!conversation || sending.value || contextChanged.value || message.role !== 'assistant') return
  const index = messages.value.findIndex((item) => item.id === message.id)
  if (index < messages.value.length - 1) {
    try {
      await ElMessageBox.confirm('此回复之后的消息会从当前分支移除，是否重新生成？', '重新生成', {
        type: 'warning',
        confirmButtonText: '重新生成',
        cancelButtonText: '取消',
      })
    } catch (error) {
      if (error === 'cancel' || error === 'close') return
      throw error
    }
  }
  try {
    const run = await api.regenerateAgentRun(
      conversation.conversation.id,
      message.id,
      reasoningEffort.value,
    )
    activeRun.value = run
    activeConversation.value = await api.getAgentConversation(conversation.conversation.id)
    void trackRun(run)
  } catch (error) {
    ElMessage.error(`重新生成失败：${errorMessage(error)}`)
  }
}

/** 判断 Run 是否仍由后台处理，queued 和 running 在交互上都应锁定重复发送。 */
function isActiveRun(run: AiAgentRun | null): run is AiAgentRun {
  return Boolean(run && ['queued', 'running'].includes(run.status))
}

/** 将协议值转换为紧凑中文标签，运行中和历史时间轴使用同一套文案。 */
function reasoningEffortLabel(value: AiAgentReasoningEffort): string {
  if (value === 'low') return '快速'
  if (value === 'high') return '深入'
  return '均衡'
}

/** 返回工具的中文名称，预览和表样本在消息记录中可以清楚区分。 */
function toolTitle(run: AiToolRun): string {
  return run.tool === 'inspectTable' ? '表样本' : 'SQL 预览'
}

/** 将候选 SQL 与 Agent 工具结果或当前手动预览匹配。 */
function messagePreview(message: AiAgentMessage): QueryResponse | undefined {
  if (!message.sql) return undefined
  if (manualPreviews.value[message.id]) return manualPreviews.value[message.id]
  const candidate = normalizeSql(message.sql)
  return [...message.toolRuns]
    .reverse()
    .find((run) => run.ok && run.result && normalizeSql(run.sql) === candidate)
    ?.result ?? undefined
}

/** 忽略排版和末尾分号比较 SQL，模型格式化查询后仍能复用刚完成的工具结果。 */
function normalizeSql(sql: string): string {
  return sql.trim().replace(/;+$/, '').replace(/\s+/g, ' ').toLocaleLowerCase()
}

/** 在不修改编辑器的情况下执行候选查询，并把临时结果附着在当前消息。 */
async function previewSql(message: AiAgentMessage) {
  if (
    !message.sql
    || !store.agentPrimarySourceId
    || !store.agentTableBindings.length
    || !currentContextReady.value
  ) {
    ElMessage.warning('请先选择候选 SQL 所需的表格')
    return
  }
  previewingId.value = message.id
  const errors = { ...previewErrors.value }
  delete errors[message.id]
  previewErrors.value = errors
  try {
    const result = await api.runQuery({
      sourceId: store.agentPrimarySourceId,
      tables: store.agentTableBindings,
      sql: message.sql,
      limit: 20,
    })
    manualPreviews.value = {
      ...manualPreviews.value,
      [message.id]: {
        ...result,
        columns: result.columns.slice(0, 12),
        rows: result.rows.slice(0, 10).map((row) => row.slice(0, 12)),
        truncated: result.truncated || result.columns.length > 12 || result.rows.length > 10,
      },
    }
  } catch (error) {
    previewErrors.value = { ...previewErrors.value, [message.id]: errorMessage(error) }
  } finally {
    previewingId.value = null
    await scrollToBottom()
  }
}

/** 将候选 SQL 交给父工作台应用，编辑器和正式结果区仍保持单一状态来源。 */
function applySql(sql: string, chart?: AgentChartSpec) {
  if (!canUseAgentSql.value) {
    ElMessage.warning('请先恢复这段对话的有效表格上下文')
    return
  }
  emit('applySql', { sql, chart })
}

/** 将候选 SQL 应用并运行，Agent 的小样本工具结果不会替代正式查询结果。 */
function runSql(sql: string, chart?: AgentChartSpec) {
  if (!canUseAgentSql.value) {
    ElMessage.warning('请先恢复这段对话的有效表格上下文')
    return
  }
  emit('runSql', { sql, chart })
}

/**
 * 执行 `/all` 时把命令展开为明确的表格快照，并从真正发送给模型的文本中剥离命令。
 * 前端固化具体 ID 可避免未来新增表格后悄悄改变旧对话上下文。
 */
async function applyAllTablesCommand(sendRemaining = false) {
  const result = store.selectAllAgentTables()
  if (!result.ok) {
    ElMessage.warning(result.message ?? '无法选择全部表格')
    return
  }
  const normalizedDraft = draft.value.trim()
  draft.value = /^\/all(?:\s+|$)/i.test(normalizedDraft)
    ? normalizedDraft.replace(/^\/all(?:\s+|$)/i, '').trimStart()
    : ''
  if (activeConversation.value) await continueWithCurrentSelection()
  ElMessage.success(`已选择全部 ${store.agentTableBindings.length} 张表`)
  if (sendRemaining && draft.value.trim()) await sendMessage()
}

/** `/clear` 命令：清空表格选择回到纯对话模式，命令文本本身不会发送给 AI。 */
function applyClearCommand() {
  store.clearAgentTableBindings()
  const normalized = draft.value.trim()
  draft.value = /^\/clear(?:\s+|$)/i.test(normalized)
    ? normalized.replace(/^\/clear(?:\s+|$)/i, '').trimStart()
    : ''
  ElMessage.success('已清空表格选择，回到纯对话模式')
}

/**
 * Enter 优先识别精确 `/all` 命令，其余内容正常发送；Shift+Enter 与输入法组合仍用于换行。
 * 严格命令边界可避免 `/alligator` 一类普通文本被误当成全选操作。
 */
function handleComposerKeydown(event: Event | KeyboardEvent) {
  if (!(event instanceof KeyboardEvent)) return
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  if (/^\/all(?:\s+|$)/i.test(draft.value.trim())) {
    void applyAllTablesCommand(true)
    return
  }
  if (/^\/clear(?:\s+|$)/i.test(draft.value.trim())) {
    applyClearCommand()
    return
  }
  void sendMessage()
}

/** 复制整条回答及候选 SQL，便于带到报表或其他工作流。 */
async function copyMessage(message: AiAgentMessage) {
  const content = message.sql
    ? `${message.content}\n\n\`\`\`sql\n${message.sql}\n\`\`\``
    : message.content
  await copyText(content, '回答已复制')
}

/** 单独复制候选 SQL，避免从较长说明中手工选择代码。 */
async function copySql(sql: string) {
  await copyText(sql, 'SQL 已复制')
}

/** 优先使用剪贴板 API，并为普通 HTTP 局域网部署保留 DOM 复制回退。 */
async function copyText(value: string, successMessage: string) {
  let copied = false
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      copied = true
    } catch {
      copied = false
    }
  }
  if (!copied) copied = copyWithSelection(value)
  if (copied) ElMessage.success(successMessage)
  else ElMessage.error('复制失败，请手动选择内容')
}

/** 通过临时文本域执行兼容复制，完成后立即移除且不改变页面布局。 */
function copyWithSelection(value: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  try {
    textarea.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    textarea.remove()
  }
}

/** 使用服务端 ISO 时间显示会话节奏，避免依赖浏览器本地生成时间。 */
function formatMessageTime(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** 历史列表使用相对紧凑日期，今天只显示时间，较早记录显示月日。 */
function formatConversationTime(timestamp: string): string {
  const date = new Date(timestamp)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return formatMessageTime(timestamp)
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

/** 返回会话最近 Run 的简短状态，历史列表可快速发现仍在后台执行的任务。 */
function conversationStatus(conversation: AiAgentConversationSummary): string {
  if (conversation.lastRunStatus === 'queued') return '排队中'
  if (conversation.lastRunStatus === 'running') return '运行中'
  if (conversation.lastRunStatus === 'failed') return '失败'
  if (conversation.lastRunStatus === 'canceled') return '已停止'
  return formatConversationTime(conversation.updatedAt)
}

/** Promise 延时仅用于短轮询节流，Run 本身完全由服务端持久化。 */
function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

/**
 * 消息或步骤变化时仅在用户仍靠近底部时跟随；主动打开会话等调用默认强制定位到最新内容。
 * 这样长回答流式增长时，用户向上阅读不会被每个分片重新拉回底部。
 */
async function scrollToBottom(force = true) {
  const currentList = messageList.value
  const shouldFollow = force || !currentList
    || currentList.scrollHeight - currentList.scrollTop - currentList.clientHeight < 96
  await nextTick()
  if (!messageList.value || !shouldFollow) return
  messageList.value.scrollTop = messageList.value.scrollHeight
}
</script>

<template>
  <section class="ai-assistant-panel">
    <aside class="ai-conversation-sidebar">
      <header class="ai-conversation-header">
        <div>
          <span class="ai-assistant-mark"><Sparkles :size="16" /></span>
          <strong>AI Agent</strong>
        </div>
        <el-tooltip content="新建对话" placement="bottom">
          <el-button
            class="icon-button plain"
            aria-label="新建 AI 对话"
            :disabled="sending"
            @click="startNewConversation"
          >
            <MessageSquarePlus :size="16" />
          </el-button>
        </el-tooltip>
      </header>

      <label class="ai-conversation-search">
        <Search :size="15" />
        <input
          v-model="conversationSearch"
          type="search"
          aria-label="搜索 AI 对话"
          placeholder="搜索对话"
        />
      </label>

      <section class="ai-conversation-history" aria-label="AI 对话历史">
        <header>
          <div><History :size="14" /><strong>最近对话</strong></div>
          <span>{{ conversations.length }}</span>
        </header>
        <div v-if="listLoading" class="ai-history-loading">
          <LoaderCircle :size="15" /> 正在加载
        </div>
        <div v-else-if="!filteredConversations.length" class="ai-history-empty">
          {{ conversations.length ? '没有匹配的对话' : '暂无历史对话' }}
        </div>
        <div v-else class="ai-history-list">
          <div
            v-for="conversation in filteredConversations"
            :key="conversation.id"
            class="ai-history-item"
            :class="{ active: activeConversation?.conversation.id === conversation.id }"
          >
            <button type="button" @click="openConversation(conversation.id)">
              <span>{{ conversation.title }}</span>
              <small>{{ conversationStatus(conversation) }}</small>
            </button>
            <el-tooltip content="归档" placement="right">
              <button
                type="button"
                class="ai-history-archive"
                :aria-label="`归档 ${conversation.title}`"
                @click="archiveConversation(conversation)"
              >
                <Trash2 :size="14" />
              </button>
            </el-tooltip>
          </div>
        </div>
      </section>

      <footer class="ai-conversation-footer">
        <Database :size="15" />
        <span>{{ contextLabel }}</span>
      </footer>
    </aside>

    <section class="ai-chat-stage">
      <header class="ai-assistant-header">
        <div class="ai-assistant-title">
          <span class="ai-assistant-mark"><Bot :size="16" /></span>
          <div>
            <strong>{{ activeConversation?.conversation.title ?? '新对话' }}</strong>
            <span>{{ contextLabel }} · {{ agentModeLabel }}</span>
          </div>
        </div>
        <div class="ai-assistant-actions">
          <el-popover placement="bottom-end" :width="280" trigger="click">
            <template #reference>
              <el-button class="icon-button plain" aria-label="AI 上下文">
                <Database :size="16" />
              </el-button>
            </template>
            <div class="ai-context-popover">
              <strong>当前运行上下文</strong>
              <dl>
                <div><dt>逻辑表</dt><dd>{{ store.agentBoundTables.length }}</dd></div>
                <div>
                  <dt>当前 SQL</dt>
                  <dd>{{ workbenchContextMatches && store.currentSql.trim() ? '已包含' : '不包含' }}</dd>
                </div>
                <div>
                  <dt>查询结果</dt>
                  <dd>
                    {{ workbenchContextMatches && store.queryResult
                      ? `${store.queryResult.rowCount} 行可选`
                      : '不包含' }}
                  </dd>
                </div>
                <div>
                  <dt>执行模式</dt>
                  <dd>{{ store.agentTableBindings.length ? '自动规划 · 只读工具' : '仅对话' }}</dd>
                </div>
              </dl>
              <el-checkbox
                v-model="includeResultContext"
                :disabled="!workbenchContextMatches || !store.queryResult"
              >
                包含小型结果样本
              </el-checkbox>
              <p v-if="!workbenchContextMatches" class="ai-context-note">
                只有 AI 选表与工作台绑定完全一致时，才可附加当前 SQL 和结果样本。
              </p>
            </div>
          </el-popover>
        </div>
      </header>

      <div ref="messageList" v-loading="conversationLoading" class="ai-message-list" aria-live="polite">
        <div v-if="contextChanged" class="ai-context-warning">
          <div>
            <Database :size="16" />
            <span><strong>表格选择已变化</strong>为避免旧消息泄露已排除表格，请使用新对话继续</span>
          </div>
          <div>
            <el-button size="small" @click="restoreConversationSelection">
              恢复原选择
            </el-button>
            <el-button size="small" type="primary" @click="continueWithCurrentSelection">
              按此选择新建
            </el-button>
          </div>
        </div>

        <div v-if="!messages.length && !conversationLoading" class="ai-chat-empty">
          <span><Sparkles :size="26" /></span>
          <strong>{{ store.agentTableBindings.length ? '从已选数据开始分析' : '从一个问题开始' }}</strong>
          <p>
            {{ store.agentTableBindings.length
              ? `本对话只会使用右侧选择的 ${store.agentTableBindings.length} 张表`
              : '默认不携带任何表格信息，也不会调用数据工具' }}
          </p>
          <button v-for="prompt in starterPrompts" :key="prompt" type="button" @click="selectStarter(prompt)">
            {{ prompt }}
          </button>
        </div>

        <article v-for="message in messages" :key="message.id" class="ai-message" :class="message.role">
          <div class="ai-message-avatar">
            <UserRound v-if="message.role === 'user'" :size="15" />
            <Bot v-else :size="16" />
          </div>
          <div class="ai-message-body">
            <AiAgentTimeline
              v-if="message.role === 'assistant'
                && activeRun?.assistantMessageId === message.id
                && latestRunHasToolSteps"
              class="ai-message-timeline"
              :run="activeRun"
            />
            <AiMarkdown class="ai-message-copy" :content="message.content" />

            <section
              v-if="message.toolRuns.length && activeRun?.assistantMessageId !== message.id"
              class="ai-tool-activity"
            >
              <header>
                <div><Wrench :size="14" /><strong>Agent 工具</strong></div>
                <span>{{ message.toolRuns.length }} 步</span>
              </header>
              <details
                v-for="(run, runIndex) in message.toolRuns"
                :key="`${message.id}-tool-${runIndex}`"
                class="ai-tool-run"
                :class="{ failed: !run.ok }"
                :open="!run.ok"
              >
                <summary>
                  <span class="ai-tool-status" />
                  <strong>{{ toolTitle(run) }}</strong>
                  <span v-if="run.result">{{ run.result.rowCount }} 行 · {{ run.result.elapsedMs }} ms</span>
                  <span v-else>执行失败</span>
                </summary>
                <pre v-if="run.sql"><code>{{ run.sql }}</code></pre>
                <AiResultPreview v-if="run.result" :result="run.result" title="工具结果" />
                <div v-else class="ai-preview-error">{{ run.error }}</div>
              </details>
            </section>

            <section v-if="message.sql" class="ai-sql-proposal">
              <header>
                <div><FilePenLine :size="14" /><strong>候选 SQL</strong></div>
                <div class="ai-sql-header-actions">
                  <span>{{ message.model }}</span>
                  <el-tooltip content="复制 SQL" placement="top">
                    <button type="button" aria-label="复制候选 SQL" @click="copySql(message.sql)">
                      <Copy :size="13" />
                    </button>
                  </el-tooltip>
                </div>
              </header>
              <pre><code>{{ message.sql }}</code></pre>
              <el-tooltip :disabled="!sqlDisabledReason" :content="sqlDisabledReason" placement="top">
                <div class="ai-proposal-actions">
                <el-button
                  size="small"
                  aria-label="应用候选 SQL"
                  :disabled="!canUseAgentSql"
                  @click="applySql(message.sql, message.chart)"
                >
                  <Check :size="14" />应用
                </el-button>
                <el-button
                  size="small"
                  aria-label="预览候选 SQL 结果"
                  :loading="previewingId === message.id"
                  :disabled="!canUseAgentSql"
                  @click="previewSql(message)"
                >
                  <Eye :size="14" />预览
                </el-button>
                <el-button
                  size="small"
                  type="primary"
                  aria-label="应用候选 SQL 并运行"
                  :disabled="!canUseAgentSql"
                  @click="runSql(message.sql, message.chart)"
                >
                  <Play :size="14" />应用并运行
                </el-button>
                </div>
              </el-tooltip>

              <AiResultPreview v-if="messagePreview(message)" :result="messagePreview(message)!" />
              <AiChartPreview
                v-if="message.chart && messagePreview(message)"
                :spec="message.chart"
                :result="messagePreview(message)!"
              />
              <div v-else-if="previewErrors[message.id]" class="ai-preview-error">
                {{ previewErrors[message.id] }}
              </div>
            </section>

            <div v-if="messageRunFailed(message)" class="ai-message-failed">
              <span>
                该回复未完成（{{ activeRun?.status === 'canceled' ? '已停止' : '运行失败' }}）
              </span>
              <el-button size="small" :disabled="sending" @click="retryRun">重试</el-button>
            </div>

            <div class="ai-message-footer">
              <span>{{ formatMessageTime(message.createdAt) }}</span>
              <div>
                <el-tooltip content="复制" placement="top">
                  <button type="button" aria-label="复制消息" @click="copyMessage(message)">
                    <Copy :size="13" />
                  </button>
                </el-tooltip>
                <el-tooltip v-if="message.role === 'assistant'" content="重新生成" placement="top">
                  <button
                    type="button"
                    aria-label="重新生成 AI 消息"
                    :disabled="sending || contextChanged"
                    @click="regenerateMessage(message)"
                  >
                    <RefreshCw :size="13" />
                  </button>
                </el-tooltip>
              </div>
            </div>
          </div>
        </article>

        <article v-if="activeRun && (sending || runNeedsAttention)" class="ai-message assistant ai-agent-run">
          <div class="ai-message-avatar"><Bot :size="16" /></div>
          <div class="ai-message-body">
            <AiAgentTimeline
              :run="activeRun"
              :retryable="runNeedsAttention"
              @retry="retryRun"
            />
            <section v-if="streamingContent" class="ai-streaming-response">
              <header>
                <span><i />正在回复</span>
                <small>{{ reasoningEffortLabel(activeRun.reasoningEffort) }}思考</small>
              </header>
              <div class="ai-streaming-copy">
                <AiMarkdown class="ai-message-copy" :content="streamingContent" />
              </div>
            </section>
          </div>
        </article>
      </div>

      <div class="ai-composer-shell">
        <footer class="ai-composer">
          <div v-if="slashCommandVisible" class="ai-slash-menu" role="listbox" aria-label="Slash 命令">
            <button
              v-if="!draft.trim().toLowerCase().startsWith('/clear')"
              type="button"
              role="option"
              :aria-selected="!draft.trim().toLowerCase().startsWith('/clear')"
              @click="applyAllTablesCommand()"
            >
              <code>/all</code>
              <span>
                <strong>使用全部表格</strong>
                <small>选择当前工作区全部可用逻辑表，命令本身不会发送给 AI</small>
              </span>
              <kbd>Enter</kbd>
            </button>
            <button
              v-if="draft.trim() === '/' || '/clear'.startsWith(draft.trim().toLowerCase())"
              type="button"
              role="option"
              :aria-selected="draft.trim().toLowerCase().startsWith('/clear')"
              @click="applyClearCommand()"
            >
              <code>/clear</code>
              <span>
                <strong>清空表格选择</strong>
                <small>回到纯对话模式，不向 AI 提供任何表结构</small>
              </span>
              <kbd>Enter</kbd>
            </button>
          </div>
          <div class="ai-composer-main">
            <el-input
              v-model="draft"
              type="textarea"
              resize="none"
              :autosize="{ minRows: 2, maxRows: 6 }"
              maxlength="4000"
              placeholder="输入问题，或输入 / 查看命令；默认不携带表格"
              @keydown="handleComposerKeydown"
            />
            <el-tooltip v-if="sending" content="停止" placement="top">
              <el-button
                class="ai-send-button ai-stop-button"
                aria-label="停止 Agent 运行"
                :loading="stoppingRun"
                @click="stopGenerating"
              >
                <Square v-if="!stoppingRun" :size="14" />
              </el-button>
            </el-tooltip>
            <el-tooltip v-else :content="sendDisabledReason || '发送'" placement="top">
              <el-button
                class="ai-send-button"
                type="primary"
                aria-label="发送 AI 消息"
                :disabled="!canSend"
                @click="sendMessage"
              >
                <Send :size="16" />
              </el-button>
            </el-tooltip>
          </div>
          <div class="ai-composer-toolbar">
            <span><Brain :size="14" />思考等级</span>
            <el-segmented
              v-model="reasoningEffort"
              :options="reasoningOptions"
              size="small"
              :disabled="sending"
              aria-label="选择 Agent 思考等级"
            />
          </div>
        </footer>
      </div>
    </section>
  </section>
</template>
