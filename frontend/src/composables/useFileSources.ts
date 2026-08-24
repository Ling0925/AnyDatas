import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { api, errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'
import type { DataSource, SavedQuery, ScheduleItem } from '../types'

export interface FileSourceForm {
  name: string
  mode: 'legacy_schedule' | 'saved_query'
  directory: string
  pattern: string
  targetSourceId: string
  savedQueryId: string
  workspaceId: string
  cron: string
  timezone: string
  triggerScheduleIds: string[]
  enabled: boolean
}

function ipcErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string' && error) return error
  return '请求失败'
}

/**
 * 文件采集页唯一的桌面桥接入口；路由已拦截无 window.desktop 的浏览器访问，
 * 每个 IPC 动作仍做 hasDesktop 运行时守卫。
 */
export function useFileSources() {
  const route = useRoute()
  const router = useRouter()
  const auth = useAuthStore()
  const hasDesktop = Boolean(window.desktop)
  const sources = ref<DesktopFileSource[]>([])
  const dataSources = ref<DataSource[]>([])
  const savedQueries = ref<SavedQuery[]>([])
  const schedules = ref<ScheduleItem[]>([])
  const activities = ref<Record<string, DesktopFileSourceActivity>>({})
  const loading = ref(false)
  const actionId = ref<string | null>(null)
  const toggleId = ref<string | null>(null)
  const dialogTargetsLoading = ref(false)
  const saving = ref(false)
  const editingId = ref<string | null>(null)
  const expandedRunsId = ref<string | null>(null)
  const dialogVisible = ref(false)
  const form = reactive<FileSourceForm>({
    name: '',
    mode: 'saved_query',
    directory: '',
    pattern: '',
    targetSourceId: '',
    savedQueryId: '',
    workspaceId: auth.user?.workspaceId ?? '',
    cron: '0 8 * * *',
    timezone: 'Asia/Shanghai',
    triggerScheduleIds: [],
    enabled: false,
  })

  let unsubscribe: (() => void) | undefined

  onMounted(async () => {
    if (!hasDesktop) return
    unsubscribe = window.desktop.onFileSourceEvent((payload) => {
      activities.value = { ...activities.value, [payload.id]: payload.activity }
      const index = sources.value.findIndex((item) => item.id === payload.id)
      const current = index >= 0 ? sources.value[index] : undefined
      if (current) {
        replaceInList({ ...current, lastRun: payload.lastRun, runs: payload.runs })
      } else {
        void loadFileSources()
      }
    })
    await loadFileSources()
    await loadTargets()
    if (route.query.automate === '1') {
      editingId.value = null
      resetForm()
      const sourceId = typeof route.query.sourceId === 'string' ? route.query.sourceId : ''
      const savedQueryId = typeof route.query.savedQueryId === 'string' ? route.query.savedQueryId : ''
      form.targetSourceId = sourceId
      form.savedQueryId = savedQueryId
      form.name = savedQueries.value.find((query) => query.id === savedQueryId)?.name
        ? `${savedQueries.value.find((query) => query.id === savedQueryId)?.name}自动化`
        : '本地文件自动化'
      dialogVisible.value = true
      await router.replace({ path: '/file-sources' })
    }
  })

  onUnmounted(() => {
    unsubscribe?.()
  })

  function replaceInList(updated: DesktopFileSource) {
    const index = sources.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) sources.value[index] = updated
    else sources.value = [updated, ...sources.value]
  }

  function toggleRuns(id: string) {
    expandedRunsId.value = expandedRunsId.value === id ? null : id
  }
  function resetForm() {
    form.name = ''
    form.mode = 'saved_query'
    form.directory = ''
    form.pattern = ''
    form.targetSourceId = ''
    form.savedQueryId = ''
    form.workspaceId = auth.user?.workspaceId ?? ''
    form.cron = '0 8 * * *'
    form.timezone = 'Asia/Shanghai'
    form.triggerScheduleIds = []
    form.enabled = false
  }

  function isValidCron(value: string): boolean {
    const parts = value.trim().split(/\s+/)
    return parts.length === 5 && parts.every((part) => part.length > 0)
  }
  async function loadFileSources() {
    if (!hasDesktop) return
    loading.value = true
    try {
      sources.value = await window.desktop.listFileSources()
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
    } finally {
      loading.value = false
    }
  }

  /** 目标数据源与下游调度一次加载后供列表与弹窗共用，打开弹窗时再刷新一次。 */
  async function loadTargets() {
    if (!hasDesktop) return
    dialogTargetsLoading.value = true
    try {
      const [loadedSources, loadedQueries, loadedSchedules] = await Promise.all([
        api.listSources(),
        api.listSavedQueries(),
        api.listSchedules(),
      ])
      dataSources.value = loadedSources
      savedQueries.value = loadedQueries
      schedules.value = loadedSchedules
    } catch (error) {
      ElMessage.error(errorMessage(error))
    } finally {
      dialogTargetsLoading.value = false
    }
  }

  async function openCreateDialog() {
    editingId.value = null
    resetForm()
    dialogVisible.value = true
    await loadTargets()
    const backend = await window.desktop.getBackendStatus()
    if (!backend.capabilities.includes('refresh-receipts')) {
      form.mode = 'legacy_schedule'
      ElMessage.warning('当前服务端版本不支持可信刷新，新文件源将使用兼容计划模式')
    }
  }

  async function openEditDialog(source: DesktopFileSource) {
    editingId.value = source.id
    form.name = source.name
    form.mode = source.mode
    form.directory = source.directory
    form.pattern = source.pattern
    form.targetSourceId = source.targetSourceId
    form.savedQueryId = source.savedQueryId ?? ''
    form.workspaceId = source.workspaceId ?? (auth.user?.workspaceId ?? '')
    form.cron = source.cron
    form.timezone = source.timezone
    form.triggerScheduleIds = [...source.triggerScheduleIds]
    form.enabled = source.enabled
    dialogVisible.value = true
    await loadTargets()
  }

  async function pickDirectory() {
    if (!hasDesktop) return
    try {
      const directory = await window.desktop.pickDirectory()
      if (directory) form.directory = directory
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
    }
  }

  async function saveFileSource() {
    if (!hasDesktop) return
    const name = form.name.trim()
    const directory = form.directory.trim()
    const pattern = form.pattern.trim()
    const cron = form.cron.trim()
    if (!name || !directory || !pattern || !form.targetSourceId || !cron) {
      ElMessage.warning('请完整填写文件源信息')
      return
    }
    if (form.mode === 'saved_query' && (!form.savedQueryId || !form.workspaceId)) {
      ElMessage.warning('请选择保存查询，并确认当前工作区')
      return
    }
    if (!isValidCron(cron)) {
      ElMessage.warning('定时表达式需要 5 个以空格分隔的字段（分 时 日 月 周）')
      return
    }
    const config: DesktopFileSourceConfig = {
      name,
      mode: form.mode,
      directory,
      pattern,
      targetSourceId: form.targetSourceId,
      savedQueryId: form.mode === 'saved_query' ? form.savedQueryId : null,
      workspaceId: form.mode === 'saved_query' ? form.workspaceId : null,
      cron,
      timezone: form.timezone,
      triggerScheduleIds: form.mode === 'legacy_schedule' ? [...form.triggerScheduleIds] : [],
    }
    saving.value = true
    try {
      if (editingId.value) {
        replaceInList(await window.desktop.updateFileSource(editingId.value, { ...config, enabled: form.enabled }))
        ElMessage.success('文件源已更新')
      } else if (form.mode === 'saved_query') {
        const created = await window.desktop.createFileSource(config)
        const paused = await window.desktop.toggleFileSource(created.id, false)
        replaceInList(paused)
        const result = await window.desktop.runFileSourceNow(created.id)
        replaceInList(result.source)
        if (form.enabled && (result.outcome === 'success' || result.outcome === 'skipped')) {
          replaceInList(await window.desktop.toggleFileSource(created.id, true))
        }
        if (result.outcome === 'failed') {
          ElMessage.error(`试运行失败，自动化保持暂停：${result.message}`)
        } else if (result.outcome === 'waiting') {
          ElMessage.info('文件仍在写入，自动化保持暂停，请稍后再次试运行')
        } else {
          ElMessage.success(form.enabled ? '试运行成功，自动化已启用' : '试运行成功，自动化保持暂停')
        }
      } else {
        const created = await window.desktop.createFileSource(config)
        const updated = form.enabled ? created : await window.desktop.toggleFileSource(created.id, false)
        replaceInList(updated)
        ElMessage.success('兼容文件源已创建')
      }
      dialogVisible.value = false
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
    } finally {
      saving.value = false
    }
  }

  async function toggleSource(source: DesktopFileSource, value: boolean | string | number) {
    if (!hasDesktop) return
    toggleId.value = source.id
    try {
      replaceInList(await window.desktop.toggleFileSource(source.id, Boolean(value)))
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
      await loadFileSources()
    } finally {
      toggleId.value = null
    }
  }

  async function runNow(source: DesktopFileSource) {
    if (!hasDesktop) return
    actionId.value = source.id
    try {
      const result = await window.desktop.runFileSourceNow(source.id)
      replaceInList(result.source)
      if (result.outcome === 'failed') {
        ElMessage.error(result.message || result.source.lastRun?.error || '采集失败')
      } else if (result.outcome === 'waiting') {
        ElMessage.info(result.message || '文件仍在写入，请稍后重试')
      } else if (result.outcome === 'skipped') {
        ElMessage.success(result.message || '文件未变化，已跳过')
      } else {
        ElMessage.success(result.message || '新数据版本已发布，分析任务已入队')
      }
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
    } finally {
      actionId.value = null
    }
  }

  async function removeSource(source: DesktopFileSource) {
    if (!hasDesktop) return
    try {
      await ElMessageBox.confirm(`删除文件源“${source.name}”？`, '删除文件源', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch (error) {
      if (error === 'cancel' || error === 'close') return
      throw error
    }
    actionId.value = source.id
    try {
      await window.desktop.deleteFileSource(source.id)
      sources.value = sources.value.filter((item) => item.id !== source.id)
      ElMessage.success('文件源已删除')
    } catch (error) {
      ElMessage.error(ipcErrorMessage(error))
    } finally {
      actionId.value = null
    }
  }

  return {
    hasDesktop,
    sources, dataSources, savedQueries, schedules, activities,
    loading, actionId, toggleId,
    dialogTargetsLoading, saving, editingId,
    expandedRunsId, dialogVisible, form,
    loadFileSources, loadTargets,
    openCreateDialog, openEditDialog, pickDirectory,
    saveFileSource, toggleSource, runNow, removeSource,
    toggleRuns,
  }
}
