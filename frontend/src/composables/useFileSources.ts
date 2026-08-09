import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api, errorMessage } from '../api'
import type { DataSource, ScheduleItem } from '../types'

export interface FileSourceForm {
  name: string
  directory: string
  pattern: string
  targetSourceId: string
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
  const hasDesktop = Boolean(window.desktop)
  const sources = ref<DesktopFileSource[]>([])
  const dataSources = ref<DataSource[]>([])
  const schedules = ref<ScheduleItem[]>([])
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
    directory: '',
    pattern: '',
    targetSourceId: '',
    cron: '0 8 * * *',
    timezone: 'Asia/Shanghai',
    triggerScheduleIds: [],
    enabled: true,
  })

  let unsubscribe: (() => void) | undefined

  onMounted(async () => {
    if (!hasDesktop) return
    unsubscribe = window.desktop.onFileSourceEvent((payload) => {
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
    form.directory = ''
    form.pattern = ''
    form.targetSourceId = ''
    form.cron = '0 8 * * *'
    form.timezone = 'Asia/Shanghai'
    form.triggerScheduleIds = []
    form.enabled = true
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
      const [loadedSources, loadedSchedules] = await Promise.all([
        api.listSources(),
        api.listSchedules(),
      ])
      dataSources.value = loadedSources
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
  }

  async function openEditDialog(source: DesktopFileSource) {
    editingId.value = source.id
    form.name = source.name
    form.directory = source.directory
    form.pattern = source.pattern
    form.targetSourceId = source.targetSourceId
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
    if (!isValidCron(cron)) {
      ElMessage.warning('定时表达式需要 5 个以空格分隔的字段（分 时 日 月 周）')
      return
    }
    const config = {
      name,
      directory,
      pattern,
      targetSourceId: form.targetSourceId,
      cron,
      timezone: form.timezone,
      triggerScheduleIds: [...form.triggerScheduleIds],
    }
    saving.value = true
    try {
      if (editingId.value) {
        replaceInList(await window.desktop.updateFileSource(editingId.value, { ...config, enabled: form.enabled }))
      } else {
        const created = await window.desktop.createFileSource(config)
        // 新建接口默认启用；取消勾选“创建后立即启用”时跟随一次原子关闭并保存返回值。
        const updated = form.enabled ? created : await window.desktop.toggleFileSource(created.id, false)
        replaceInList(updated)
      }
      dialogVisible.value = false
      ElMessage.success(editingId.value ? '文件源已更新' : '文件源已创建')
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
      const updated = await window.desktop.runFileSourceNow(source.id)
      replaceInList(updated)
      const status = updated.lastRun?.status
      if (status === 'failed') {
        ElMessage.error(updated.lastRun?.error || '采集失败')
      } else if (status === 'skipped') {
        ElMessage.success('文件未变化，已跳过')
      } else {
        ElMessage.success('采集完成')
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
    sources, dataSources, schedules,
    loading, actionId, toggleId,
    dialogTargetsLoading, saving, editingId,
    expandedRunsId, dialogVisible, form,
    loadFileSources, loadTargets,
    openCreateDialog, openEditDialog, pickDirectory,
    saveFileSource, toggleSource, runNow, removeSource,
    toggleRuns,
  }
}
