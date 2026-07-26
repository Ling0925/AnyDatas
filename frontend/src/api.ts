import axios from 'axios'

import type {
  AuthStatus,
  AuthUser,
  AiSettings,
  AiSettingsPayload,
  AiChatRequest,
  AiChatResponse,
  AiAgentConversationDetail,
  AiAgentConversationSummary,
  AiAgentRun,
  AiAgentRunPayload,
  AiAgentReasoningEffort,
  AiSqlResponse,
  DataSource,
  ImportInspection,
  InspectImportTablePayload,
  ImportTableConfig,
  Job,
  JobResultPage,
  JobSummary,
  LoginPayload,
  PreviewResponse,
  QueryResponse,
  SavedQuery,
  SavedQueryPayload,
  ScheduleItem,
  SchedulePayload,
  SetupPayload,
  SourceTable,
  SourceTablePayload,
  QueryTableBinding,
} from './types'

const client = axios.create({
  baseURL: '/api',
  timeout: 120_000,
  withCredentials: true,
})

let unauthorizedHandler: (() => void) | null = null

client.interceptors.response.use(undefined, (error: unknown) => {
  if (
    axios.isAxiosError(error)
    && error.response?.status === 401
    && !error.config?.url?.startsWith('/auth/')
  ) {
    unauthorizedHandler?.()
  }
  return Promise.reject(error)
})

export function setUnauthorizedHandler(handler: () => void) {
  unauthorizedHandler = handler
}

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    return error.response?.data?.error?.message ?? error.message ?? '请求失败'
  }
  return error instanceof Error ? error.message : '请求失败'
}

/** 统一识别 Axios 和浏览器 AbortSignal 的取消错误，让主动停止不会被显示成失败消息。 */
export function isRequestCanceled(error: unknown): boolean {
  return axios.isCancel(error)
    || (axios.isAxiosError(error) && error.code === 'ERR_CANCELED')
}

export const api = {
  async authStatus() {
    return (await client.get<AuthStatus>('/auth/status')).data
  },
  async setup(payload: SetupPayload) {
    return (await client.post<AuthUser>('/auth/setup', payload)).data
  },
  async login(payload: LoginPayload) {
    return (await client.post<AuthUser>('/auth/login', payload)).data
  },
  async logout() {
    await client.post('/auth/logout')
  },
  async me() {
    return (await client.get<AuthUser>('/auth/me')).data
  },
  async getAiSettings() {
    return (await client.get<AiSettings>('/ai/settings')).data
  },
  async updateAiSettings(payload: AiSettingsPayload) {
    return (await client.put<AiSettings>('/ai/settings', payload)).data
  },
  async testAiSettings() {
    return (await client.post<{ ok: boolean; model: string }>('/ai/settings/test')).data
  },
  async generateSql(payload: { instruction: string; currentSql?: string; tables: QueryTableBinding[] }) {
    return (await client.post<AiSqlResponse>('/ai/sql', payload)).data
  },
  async chatWithAi(payload: AiChatRequest, signal?: AbortSignal) {
    return (await client.post<AiChatResponse>('/ai/chat', payload, { signal, timeout: 0 })).data
  },
  /** 从服务端读取会话列表，浏览器不再承担 AI 历史的持久化职责。 */
  async listAgentConversations() {
    return (await client.get<AiAgentConversationSummary[]>('/ai/agent/conversations')).data
  },
  /** 创建绑定当前逻辑表快照的新会话。 */
  async createAgentConversation(tables: QueryTableBinding[]) {
    return (await client.post<AiAgentConversationDetail>('/ai/agent/conversations', { tables })).data
  },
  /** 读取会话消息和最近 Run，刷新页面后可以恢复完整执行状态。 */
  async getAgentConversation(id: string) {
    return (await client.get<AiAgentConversationDetail>(`/ai/agent/conversations/${id}`)).data
  },
  /** 归档不再使用的会话，后端仍保留审计记录。 */
  async archiveAgentConversation(id: string) {
    await client.delete(`/ai/agent/conversations/${id}`)
  },
  /** 明确将会话切换到工作台当前表绑定和配置版本。 */
  async updateAgentConversationContext(id: string, tables: QueryTableBinding[]) {
    return (await client.put<AiAgentConversationDetail>(
      `/ai/agent/conversations/${id}/context`,
      { tables },
    )).data
  },
  /** 创建异步 Agent Run，后续通过轻量轮询读取模型和工具步骤。 */
  async startAgentRun(conversationId: string, payload: AiAgentRunPayload) {
    return (await client.post<AiAgentRun>(
      `/ai/agent/conversations/${conversationId}/runs`,
      payload,
    )).data
  },
  /** 从指定助手回复处分叉重新生成，历史上下文由后端负责。 */
  async regenerateAgentRun(
    conversationId: string,
    assistantMessageId: string,
    reasoningEffort: AiAgentReasoningEffort,
  ) {
    return (await client.post<AiAgentRun>(
      `/ai/agent/conversations/${conversationId}/regenerate`,
      { assistantMessageId, reasoningEffort },
    )).data
  },
  /** 读取 Run 和结构化步骤，供界面展示真实执行轨迹。 */
  async getAgentRun(id: string) {
    return (await client.get<AiAgentRun>(`/ai/agent/runs/${id}`)).data
  },
  /** EventSource 使用同源 Cookie 订阅持久化 Run 快照。 */
  agentRunEventsUrl(id: string) {
    return `/api/ai/agent/runs/${encodeURIComponent(id)}/events`
  },
  /** 停止模型请求和正在执行的只读 DuckDB 工具。 */
  async cancelAgentRun(id: string) {
    return (await client.post<AiAgentRun>(`/ai/agent/runs/${id}/cancel`)).data
  },
  /** 原位重试最近失败或停止的 Run，不新增重复用户消息。 */
  async retryAgentRun(id: string) {
    return (await client.post<AiAgentRun>(`/ai/agent/runs/${id}/retry`)).data
  },
  async listSources() {
    return (await client.get<DataSource[]>('/data-sources')).data
  },
  async uploadSource(file: File) {
    const form = new FormData()
    form.append('file', file)
    return (await client.post<DataSource>('/data-sources', form)).data
  },
  async inspectSource(file: File) {
    const form = new FormData()
    form.append('file', file)
    return (await client.post<ImportInspection>('/data-sources/inspect', form, { timeout: 0 })).data
  },
  async previewSourceImport(token: string, payload: InspectImportTablePayload) {
    return (await client.post<ImportInspection['sheets'][number]>(
      `/data-sources/imports/${token}/preview`,
      payload,
      { timeout: 0 },
    )).data
  },
  async commitSourceImport(token: string, tables: ImportTableConfig[]) {
    return (await client.post<DataSource>('/data-sources/import', { token, tables })).data
  },
  async discardSourceImport(token: string) {
    await client.delete(`/data-sources/imports/${token}`)
  },
  async deleteSource(id: string) {
    await client.delete(`/data-sources/${id}`)
  },
  async listSourceTables(sourceId?: string) {
    return (await client.get<SourceTable[]>('/source-tables', { params: { sourceId } })).data
  },
  async createSourceTable(sourceId: string, payload: SourceTablePayload) {
    return (await client.post<SourceTable>(`/data-sources/${sourceId}/tables`, payload)).data
  },
  async updateSourceTable(id: string, payload: SourceTablePayload) {
    return (await client.patch<SourceTable>(`/source-tables/${id}`, payload)).data
  },
  async previewSourceTable(id: string) {
    return (await client.get<PreviewResponse>(`/source-tables/${id}/preview`, { params: { limit: 200 } })).data
  },
  async deleteSourceTable(id: string) {
    await client.delete(`/source-tables/${id}`)
  },
  async updateSource(
    id: string,
    payload: Pick<DataSource, 'selectedSheet' | 'startCell' | 'firstRowAsHeader'>,
  ) {
    return (await client.patch<DataSource>(`/data-sources/${id}/config`, payload)).data
  },
  async previewSource(id: string) {
    return (await client.get<PreviewResponse>(`/data-sources/${id}/preview`, { params: { limit: 200 } })).data
  },
  async runQuery(payload: { sourceId: string; tables: QueryTableBinding[]; sql: string; limit?: number }) {
    return (await client.post<QueryResponse>('/query', payload)).data
  },
  async listSavedQueries(sourceId?: string) {
    return (await client.get<SavedQuery[]>('/saved-queries', { params: { sourceId } })).data
  },
  async createSavedQuery(payload: SavedQueryPayload) {
    return (await client.post<SavedQuery>('/saved-queries', payload)).data
  },
  async updateSavedQuery(id: string, payload: SavedQueryPayload) {
    return (await client.put<SavedQuery>(`/saved-queries/${id}`, payload)).data
  },
  async deleteSavedQuery(id: string) {
    await client.delete(`/saved-queries/${id}`)
  },
  async listJobs(params?: { status?: string; query?: string }) {
    return (await client.get<Job[]>('/jobs', { params })).data
  },
  async getJobSummary() {
    return (await client.get<JobSummary>('/jobs/summary')).data
  },
  async getJob(id: string) {
    return (await client.get<Job>(`/jobs/${id}`)).data
  },
  async getJobResult(id: string, offset = 0, limit = 100) {
    return (await client.get<JobResultPage>(`/jobs/${id}/result`, {
      params: { offset, limit },
    })).data
  },
  jobResultDownloadUrl(id: string) {
    return `/api/jobs/${encodeURIComponent(id)}/result.csv`
  },
  async createJob(payload: { sourceId: string; tables: QueryTableBinding[]; name: string; sql: string }) {
    return (await client.post<Job>('/jobs', payload)).data
  },
  async cancelJob(id: string) {
    return (await client.post<Job>(`/jobs/${id}/cancel`)).data
  },
  async retryJob(id: string) {
    return (await client.post<Job>(`/jobs/${id}/retry`)).data
  },
  async deleteJob(id: string) {
    await client.delete(`/jobs/${id}`)
  },
  async listSchedules() {
    return (await client.get<ScheduleItem[]>('/schedules')).data
  },
  async createSchedule(payload: SchedulePayload) {
    return (await client.post<ScheduleItem>('/schedules', payload)).data
  },
  async updateSchedule(id: string, payload: SchedulePayload) {
    return (await client.put<ScheduleItem>(`/schedules/${id}`, payload)).data
  },
  async toggleSchedule(id: string, enabled: boolean) {
    return (await client.post<ScheduleItem>(`/schedules/${id}/toggle`, { enabled })).data
  },
  async runSchedule(id: string) {
    return (await client.post<Job>(`/schedules/${id}/run`)).data
  },
  async deleteSchedule(id: string) {
    await client.delete(`/schedules/${id}`)
  },
}
