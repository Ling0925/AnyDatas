<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Bot,
  Brain,
  Check,
  CircleHelp,
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
  AiAskUserPrompt,
  AiToolRun,
  QueryResponse,
} from '../types'
import AiMarkdown from './AiMarkdown.vue'
import AiAgentTimeline from './AiAgentTimeline.vue'
import AiResultPreview from './AiResultPreview.vue'
import AiChartPreview from './AiChartPreview.vue'

const props = withDefaults(defineProps<{
  embedded?: boolean
}>(), {
  embedded: false,
})

const emit = defineEmits<{
  applySql: [payload: { sql: string; chart?: AgentChartSpec }]
  runSql: [payload: { sql: string; chart?: AgentChartSpec }]
}>()

const auth = useAuthStore()
const store = useWorkspaceStore()
const openAiSettings = inject<() => void>('openAiSettings', () => {})
const aiReady = ref<boolean | null>(null)
const conversations = ref<AiAgentConversationSummary[]>([])
const activeConversation = ref<AiAgentConversationDetail | null>(null)
const activeRun = ref<AiAgentRun | null>(null)
const draft = ref('')
const reasoningEffort = ref<AiAgentReasoningEffort>(loadReasoningEffort())
const includeResultContext = ref(false)
const listLoading = ref(false)
const conversationLoading = ref(false)
const startingRun = ref(false)
const answeringRun = ref(false)
const stoppingRun = ref(false)
const selectedAnswerIds = ref<string[]>([])
const answerText = ref('')
const previewingId = ref<string | null>(null)
const applyingRunId = ref<string | null>(null)
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

interface PromptTemplate {
  icon: string
  label: string
  prompt: string
}

const dataTemplates: PromptTemplate[] = [
  {
    icon: '1',
    label: '这是什么数据',
    prompt: '先看已选表的字段和几行样本，用两三句话说明这是什么数据、哪些字段值得分析。',
  },
  {
    icon: '2',
    label: '找出问题',
    prompt: '检查空值、重复和明显异常，只指出最值得处理的问题。',
  },
  {
    icon: '3',
    label: '看趋势',
    prompt: '如果有日期或批次字段，按时间汇总最重要的指标，并给出一条可运行的 SQL。',
  },
  {
    icon: '4',
    label: '把表连起来',
    prompt: '根据已选表找出可以 JOIN 的字段，给出一条能跑通的关联查询。',
  },
]

const currentTemplates = computed(() => (
  store.agentTableBindings.length ? dataTemplates : []
))


const messages = computed(() => activeConversation.value?.messages ?? [])
const filteredConversations = computed(() => {
  const query = conversationSearch.value.trim().toLocaleLowerCase()
  if (!query) return conversations.value
  return conversations.value.filter((conversation) => (
    conversation.title.toLocaleLowerCase().includes(query)
  ))
})
const sending = computed(() => (
  startingRun.value || answeringRun.value || isBusyRun(activeRun.value)
))
const waitingPrompt = computed(() => parseAskUserPrompt(activeRun.value))
const canAnswer = computed(() => {
  const prompt = waitingPrompt.value
  if (!prompt || answeringRun.value) return false
  if (selectedAnswerIds.value.length) return true
  return prompt.allowFreeText && Boolean(answerText.value.trim())
})
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
  && !sending.value,
))
const canUseAgentSql = computed(() => Boolean(
  store.agentTableBindings.length
  && currentContextReady.value
  && !contextChanged.value,
))
/** 当发送被禁用时给出可读原因，避免按钮静默置灰让人以为界面坏了。 */
const sendDisabledReason = computed(() => {
  if (sending.value) return ''
  if (contextChanged.value) return '表格已变，发送会开启新对话'
  if (!currentContextReady.value) {
    return props.embedded
      ? '所选表格无效或超过 16 张，请调整当前查询用到的表'
      : '所选表格无效或超过 16 张，请调整数据表选择'
  }
  if (!draft.value.trim()) return '请先输入问题'
  return ''
})
const sqlDisabledReason = computed(() => {
  if (contextChanged.value) return '数据表已变化，请先确认或恢复选择'
  if (!store.agentTableBindings.length) {
    return props.embedded
      ? '先把工作表加入查询，或在输入框使用 /all'
      : '当前为纯对话。请先选择数据表，或输入 /all'
  }
  if (!currentContextReady.value) return '所选表格无效或超过 16 张'
  return ''
})

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
  if (waitingPrompt.value) return `${tables} · 等待回答`
  if (sending.value) return `${tables} · Agent 运行中`
  return workbenchContextMatches.value && store.queryResult && includeResultContext.value
    ? `${tables} · 含结果样本`
    : tables
})
const agentModeLabel = computed(() => (
  store.agentTableBindings.length ? '会查询已选表格' : '未选择数据表'
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
    aiReady.value = null
    if (userId) {
      void loadAiReady()
      void initializeConversations()
    }
  },
  { immediate: true },
)

async function loadAiReady() {
  try {
    const settings = await api.getAiSettings()
    aiReady.value = settings.enabled && Boolean(settings.model.trim())
  } catch {
    aiReady.value = false
  }
}
watch(
  () => [messages.value.length, activeRun.value?.stepCount, streamingContent.value.length],
  () => { void scrollToBottom(false) },
)
watch(reasoningEffort, (value) => {
  window.localStorage.setItem('anydatas.agent.reasoningEffort', value)
})
watch(
  () => waitingPrompt.value?.question ?? '',
  () => {
    selectedAnswerIds.value = []
    answerText.value = ''
  },
)
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
    else if (!store.agentTableBindings.length && store.queryBindings.length) {
      store.setAgentTableBindings(store.queryBindings)
    }
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
 * 进入本地新对话草稿。保留当前选表，避免每次新建都要从零勾表。
 */
async function startNewConversation() {
  invalidateRunTracking()
  activeConversation.value = null
  activeRun.value = null
  if (!store.agentTableBindings.length && store.queryBindings.length) {
    store.setAgentTableBindings(store.queryBindings)
  }
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
    await continueWithCurrentSelection()
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

/** 停止服务端 Run，同时中断模型等待、用户问答和正在执行的 DuckDB 工具查询。 */
async function stopGenerating() {
  const run = activeRun.value
  if (!isBusyRun(run) || stoppingRun.value) return
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

/** 判断 Run 是否仍由后台处理，queued 和 running 才继续订阅 SSE。 */
function isActiveRun(run: AiAgentRun | null): run is AiAgentRun {
  return Boolean(run && ['queued', 'running'].includes(run.status))
}

/** waiting_user 会锁发送、允许取消，但不保持 SSE。 */
function isBusyRun(run: AiAgentRun | null): run is AiAgentRun {
  return Boolean(run && ['queued', 'running', 'waiting_user'].includes(run.status))
}

function parseAskUserPrompt(run: AiAgentRun | null): AiAskUserPrompt | null {
  if (!run || run.status !== 'waiting_user') return null
  const step = [...run.steps].reverse().find((item) => item.kind === 'tool' && item.status === 'waiting')
  const output = recordValue(step?.output)
  const question = typeof output?.question === 'string' ? output.question.trim() : ''
  if (!question) return null
  const options = Array.isArray(output?.options)
    ? output.options.flatMap((item) => {
        const option = recordValue(item)
        const id = typeof option?.id === 'string' ? option.id.trim() : ''
        const label = typeof option?.label === 'string' ? option.label.trim() : ''
        return id && label ? [{ id, label }] : []
      })
    : []
  return {
    question,
    options,
    allowMultiple: output?.allowMultiple === true,
    allowFreeText: output?.allowFreeText !== false || options.length === 0,
  }
}

function toggleAnswerId(id: string) {
  if (waitingPrompt.value?.allowMultiple) {
    selectedAnswerIds.value = selectedAnswerIds.value.includes(id)
      ? selectedAnswerIds.value.filter((item) => item !== id)
      : [...selectedAnswerIds.value, id]
    return
  }
  selectedAnswerIds.value = [id]
}

/** 提交 ask_user 回答后后端会把同一 Run 切回 running，再恢复事件订阅。 */
async function submitAnswer() {
  const run = activeRun.value
  if (!run || run.status !== 'waiting_user' || !canAnswer.value) return
  answeringRun.value = true
  try {
    const resumed = await api.answerAgentRun(run.id, {
      selectedIds: selectedAnswerIds.value,
      text: answerText.value.trim() || undefined,
    })
    activeRun.value = resumed
    void trackRun(resumed)
  } catch (error) {
    ElMessage.error(`提交回答失败：${errorMessage(error)}`)
  } finally {
    answeringRun.value = false
  }
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
    ElMessage.warning(
      contextChanged.value
        ? '表格选择已变化，请先确认上下文或开新对话后再应用 SQL'
        : '请先在右侧选择 Agent 表格，并确认当前数据上下文',
    )
    return
  }
  emit('applySql', { sql, chart })
}

/** 将候选 SQL 应用并运行，Agent 的小样本工具结果不会替代正式查询结果。 */
function runSql(sql: string, chart: AgentChartSpec | undefined, messageId: string) {
  if (!canUseAgentSql.value) {
    ElMessage.warning(
      contextChanged.value
        ? '表格选择已变化，请先确认上下文或开新对话后再运行 SQL'
        : '请先在右侧选择 Agent 表格，并确认当前数据上下文',
    )
    return
  }
  applyingRunId.value = messageId
  emit('runSql', { sql, chart })
  // 父组件异步跑完后会跳转；若仍留在本页，短延迟后清 loading，避免按钮卡死。
  window.setTimeout(() => {
    if (applyingRunId.value === messageId) applyingRunId.value = null
  }, 120_000)
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
  if (conversation.lastRunStatus === 'waiting_user') return '等待回答'
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

defineExpose({
  setPrompt: (text: string) => {
    draft.value = text
  },
})
</script>

<template>
  <section class="ai-assistant-panel" :class="{ embedded }">
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
                <div><dt>数据表</dt><dd>{{ store.agentBoundTables.length }}</dd></div>
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
                  <dt>模式</dt>
                  <dd>{{ store.agentTableBindings.length ? '会查询已选表格' : '未选择数据表' }}</dd>
                </div>
              </dl>
              <el-checkbox
                v-model="includeResultContext"
                :disabled="!workbenchContextMatches || !store.queryResult"
              >
                包含小型结果样本
              </el-checkbox>
              <p v-if="!workbenchContextMatches" class="ai-context-note">
                选表需要和工作台当前查询一致，才能附带正在编辑的 SQL 和结果。
              </p>
            </div>
          </el-popover>
        </div>
      </header>

      <div v-if="embedded" class="ai-embedded-tables">
        <span>当前表</span>
        <template v-if="store.boundTables.length">
          <code v-for="item in store.boundTables" :key="`${item.binding.tableId}-${item.binding.alias}`">
            {{ item.binding.alias }}
          </code>
          <button type="button" class="ai-sync-tables" @click="store.setAgentTableBindings(store.queryBindings)">
            与查询同步
          </button>
        </template>
        <em v-else>查询还没有表。在左侧点 +，或输入 /all</em>
      </div>

      <div ref="messageList" v-loading="conversationLoading" class="ai-message-list" aria-live="polite">
        <div v-if="contextChanged" class="ai-context-warning">
          <div>
            <Database :size="16" />
            <span><strong>选表已经变了。</strong>继续发送会开启新对话，不会改写旧聊天。</span>
          </div>
          <div>
            <el-button size="small" @click="restoreConversationSelection">
              恢复原来的表
            </el-button>
            <el-button size="small" type="primary" @click="continueWithCurrentSelection">
              用新表开始
            </el-button>
          </div>
        </div>

        <div v-if="!messages.length && !conversationLoading && aiReady !== null" class="ai-chat-empty">
          <template v-if="aiReady === false">
            <strong>还没有接上 AI</strong>
            <p>在工作区设置里填写兼容 OpenAI 的接口和模型后，就可以开始问数据。</p>
            <button type="button" class="wb-btn wb-btn-primary" @click="openAiSettings()">
              打开 AI 设置
            </button>
          </template>
          <template v-else-if="!store.agentTableBindings.length">
            <strong>先选要分析的表</strong>
            <p>{{ embedded ? '用当前查询里的表，或到 AI Agent 页勾选工作表。' : '在右侧勾选工作表后，AI 才能查数和写 SQL。' }}</p>
            <div class="ai-empty-actions">
              <button
                v-if="store.queryBindings.length"
                type="button"
                class="wb-btn wb-btn-primary"
                @click="store.setAgentTableBindings(store.queryBindings)"
              >
                用当前查询的表
              </button>
              <button
                v-else-if="store.sourceTables.length && !embedded"
                type="button"
                class="wb-btn wb-btn-primary"
                @click="store.selectAllAgentTables()"
              >
                分析全部表格
              </button>
            </div>
          </template>
          <template v-else>
            <strong>问一句关于这些表的问题</strong>
            <p>已选 {{ store.agentTableBindings.length }} 张表。直接提问，或用下面的起点。</p>
            <div class="ai-empty-templates-grid">
              <button
                v-for="tpl in currentTemplates"
                :key="tpl.label"
                type="button"
                class="ai-template-card"
                @click="selectStarter(tpl.prompt)"
              >
                <strong>{{ tpl.label }}</strong>
                <span class="ai-template-prompt">{{ tpl.prompt }}</span>
              </button>
            </div>
          </template>
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
                <div><Wrench :size="14" /><strong>查询过程</strong></div>
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
                <div><FilePenLine :size="14" /><strong>可运行的 SQL</strong></div>
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
                  <Check :size="14" />写入工作台
                </el-button>
                <el-button
                  size="small"
                  aria-label="预览 SQL 结果"
                  :loading="previewingId === message.id"
                  :disabled="!canUseAgentSql"
                  @click="previewSql(message)"
                >
                  <Eye :size="14" />预览
                </el-button>
                <el-button
                  size="small"
                  type="primary"
                  aria-label="在工作台运行 SQL"
                  :loading="applyingRunId === message.id"
                  :disabled="!canUseAgentSql || applyingRunId !== null"
                  @click="runSql(message.sql, message.chart, message.id)"
                >
                  <Play :size="14" />在工作台运行
                </el-button>
                <p v-if="!canUseAgentSql" class="ai-context-note">
                  {{
                    contextChanged
                      ? '选表已变，确认后再运行这条 SQL'
                      : '先选表才能预览或运行 SQL'
                  }}
                </p>
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
            <section v-if="waitingPrompt" class="ai-ask-card">
              <header>
                <CircleHelp :size="15" />
                <strong>{{ waitingPrompt.question }}</strong>
              </header>
              <div v-if="waitingPrompt.options.length" class="ai-ask-options">
                <button
                  v-for="option in waitingPrompt.options"
                  :key="option.id"
                  type="button"
                  :class="{ active: selectedAnswerIds.includes(option.id) }"
                  @click="toggleAnswerId(option.id)"
                >
                  {{ option.label }}
                </button>
              </div>
              <el-input
                v-if="waitingPrompt.allowFreeText"
                v-model="answerText"
                type="textarea"
                resize="none"
                :autosize="{ minRows: 2, maxRows: 4 }"
                maxlength="2000"
                show-word-limit
                placeholder="补充说明"
              />
              <div class="ai-ask-actions">
                <el-button
                  type="primary"
                  size="small"
                  :disabled="!canAnswer"
                  :loading="answeringRun"
                  @click="submitAnswer"
                >
                  提交回答
                </el-button>
              </div>
            </section>
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

          <!-- 快捷 Prompt 标签条 -->
          <div v-if="messages.length && currentTemplates.length" class="ai-prompt-pills-bar">
            <button
              v-for="tpl in currentTemplates"
              :key="tpl.label"
              type="button"
              class="ai-prompt-pill"
              @click="selectStarter(tpl.prompt)"
            >
              <span>{{ tpl.icon }}</span>
              <span>{{ tpl.label }}</span>
            </button>
          </div>

          <div class="ai-composer-main">


            <el-input
              v-model="draft"
              type="textarea"
              resize="none"
              :autosize="{ minRows: 2, maxRows: 6 }"
              maxlength="4000"
              :placeholder="store.agentTableBindings.length
                ? '输入关于当前数据的问题，或输入 / 查看命令'
                : '输入问题；默认不携带表格。输入 /all 可选全部表'"
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
            <span><Brain :size="14" />回答</span>
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
