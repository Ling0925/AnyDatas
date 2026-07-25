export type WorkspaceRole = 'owner' | 'admin' | 'analyst' | 'viewer'

export interface AuthUser {
  userId: string
  email: string
  name: string
  workspaceId: string
  workspaceName: string
  role: WorkspaceRole
}

export interface AuthStatus {
  setupRequired: boolean
  authenticated: boolean
  user: AuthUser | null
}

export interface SetupPayload {
  email: string
  name: string
  workspaceName: string
  password: string
}

export interface LoginPayload {
  email: string
  password: string
}

export interface AiSettings {
  enabled: boolean
  baseUrl: string
  model: string
  apiKeyConfigured: boolean
  updatedAt: string | null
}

export interface AiSettingsPayload {
  enabled: boolean
  baseUrl: string
  model: string
  apiKey?: string
  clearApiKey: boolean
}

export interface AiSqlResponse {
  sql: string
  model: string
}

export type AiChatRole = 'user' | 'assistant'

export interface AiChatHistoryMessage {
  role: AiChatRole
  content: string
}

export interface AiResultContext {
  columns: FieldDefinition[]
  rows: unknown[][]
  rowCount: number
  truncated: boolean
}

export interface AiChatRequest {
  message: string
  currentSql?: string
  tables: QueryTableBinding[]
  history: AiChatHistoryMessage[]
  resultContext?: AiResultContext
}

export interface AiToolRun {
  tool: 'previewSql' | 'inspectTable' | string
  sql: string
  ok: boolean
  result: QueryResponse | null
  error: string | null
}

export interface AiChatResponse {
  message: string
  sql: string | null
  model: string
  toolRuns: AiToolRun[]
}

export type AiAgentRunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'canceled'
export type AiAgentStepStatus = 'running' | 'completed' | 'failed' | 'canceled'
export type AiAgentReasoningEffort = 'low' | 'medium' | 'high'

export interface AiAgentConversationSummary {
  id: string
  title: string
  tables: QueryTableBinding[]
  contextSignature: string
  status: 'active' | 'archived'
  lastRunStatus: AiAgentRunStatus | null
  createdAt: string
  updatedAt: string
}

export interface AiAgentMessage {
  id: string
  role: AiChatRole
  content: string
  sql: string | null
  model: string | null
  toolRuns: AiToolRun[]
  sequence: number
  createdAt: string
}

export interface AiAgentRunStep {
  id: string
  ordinal: number
  kind: 'model' | 'tool'
  status: AiAgentStepStatus
  toolName: string | null
  toolCallId: string | null
  input: unknown | null
  output: unknown | null
  errorMessage: string | null
  startedAt: string
  finishedAt: string | null
}

export interface AiAgentRun {
  id: string
  conversationId: string
  userMessageId: string
  assistantMessageId: string | null
  status: AiAgentRunStatus
  model: string
  reasoningEffort: AiAgentReasoningEffort
  finishReason: string | null
  stepCount: number
  errorMessage: string | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  updatedAt: string
  steps: AiAgentRunStep[]
}

export interface AiAgentConversationDetail {
  conversation: AiAgentConversationSummary
  messages: AiAgentMessage[]
  latestRun: AiAgentRun | null
}

export interface AiAgentRunPayload {
  message: string
  currentSql?: string
  tables: QueryTableBinding[]
  resultContext?: AiResultContext
  reasoningEffort: AiAgentReasoningEffort
}

export type FieldDataType = '文本' | '整数' | '小数' | '布尔' | '日期' | '日期时间'

export interface FieldDefinition {
  name: string
  dataType: FieldDataType
  nullable: boolean
}

export interface ImportSheetInspection {
  name: string
  rowCount: number
  columnCount: number
  fields: FieldDefinition[]
  rows: unknown[][]
}

export interface ImportInspection {
  token: string
  originalFilename: string
  fileKind: 'excel' | 'csv'
  sizeBytes: number
  sheets: ImportSheetInspection[]
  expiresAt: string
}

export interface ImportTableConfig {
  name: string
  sheetName: string
  startCell: string
  endCell: string | null
  firstRowAsHeader: boolean
  fields: FieldDefinition[]
}

export interface DataSource {
  id: string
  name: string
  originalFilename: string
  mediaType: string
  fileKind: 'excel' | 'csv'
  sizeBytes: number
  selectedSheet: string
  startCell: string
  firstRowAsHeader: boolean
  sheetNames: string[]
  rowCount: number
  columnCount: number
  sqlTableName: 'data'
  createdAt: string
  updatedAt: string
}

export interface SourceTable {
  id: string
  sourceId: string
  sourceName: string
  originalFilename: string
  fileKind: 'excel' | 'csv'
  name: string
  sheetName: string
  startCell: string
  endCell: string | null
  firstRowAsHeader: boolean
  rowCount: number
  columnCount: number
  fields: FieldDefinition[]
  configVersion: number
  cacheStatus: 'pending' | 'building' | 'ready' | 'failed'
  cacheError: string | null
  isDefault: boolean
  createdAt: string
  updatedAt: string
}

export interface QueryTableBinding {
  tableId: string
  alias: string
}

export interface SourceTablePayload {
  name: string
  sheetName: string
  startCell: string
  endCell: string | null
  firstRowAsHeader: boolean
  fields?: FieldDefinition[]
}

export interface PreviewResponse {
  columns: FieldDefinition[]
  rows: unknown[][]
  totalRows: number
  truncated: boolean
  sheet: string
  startCell: string
  endCell: string | null
}

export interface QueryResponse {
  columns: FieldDefinition[]
  rows: unknown[][]
  rowCount: number
  elapsedMs: number
  truncated: boolean
}

export interface SavedQuery {
  id: string
  sourceId: string
  sourceName: string
  name: string
  sql: string
  tables: QueryTableBinding[]
  createdAt: string
  updatedAt: string
}

export interface SavedQueryPayload {
  sourceId: string
  tables: QueryTableBinding[]
  name: string
  sql: string
}

export interface JobLog {
  at: string
  level: 'info' | 'success' | 'warning' | 'error'
  message: string
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'

export interface Job {
  id: string
  sourceId: string
  sourceName: string
  scheduleId: string | null
  name: string
  kind: string
  sql: string
  tables: QueryTableBinding[]
  status: JobStatus
  progress: number
  triggerType: string
  result: QueryResponse | null
  resultRowCount: number | null
  errorMessage: string | null
  logs: JobLog[]
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  updatedAt: string
}

export interface JobSummary {
  total: number
  queued: number
  running: number
  succeeded: number
  failed: number
  canceled: number
}

export interface ScheduleItem {
  id: string
  sourceId: string
  sourceName: string
  name: string
  sql: string
  tables: QueryTableBinding[]
  cronExpression: string
  timezone: string
  enabled: boolean
  nextRunAt: string | null
  lastRunAt: string | null
  createdAt: string
  updatedAt: string
}

export interface SchedulePayload {
  sourceId: string
  tables: QueryTableBinding[]
  name: string
  sql: string
  cronExpression: string
  timezone: string
  enabled: boolean
}
