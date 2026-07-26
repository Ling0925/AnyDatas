import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '../api'
import type {
  Job,
  JobStatus,
  JobSummary,
  QueryTableBinding,
  ScheduleItem,
  SchedulePayload,
} from '../types'

const emptySummary = (): JobSummary => ({
  total: 0,
  queued: 0,
  running: 0,
  succeeded: 0,
  failed: 0,
  canceled: 0,
})

export const useTasksStore = defineStore('tasks', () => {
  const jobs = ref<Job[]>([])
  const schedules = ref<ScheduleItem[]>([])
  const selectedJobId = ref<string | null>(null)
  const statusFilter = ref<JobStatus | ''>('')
  const search = ref('')
  const loading = ref(false)
  const summary = ref<JobSummary>(emptySummary())
  // 轮询与手动刷新可能并发触发 loadJobs；用单调递增的代际号丢弃迟到的陈旧响应，
  // 避免旧请求的结果覆盖较新一次的列表、汇总与选中项。
  let jobsRequestId = 0

  const selectedJob = computed(
    () => jobs.value.find((job) => job.id === selectedJobId.value) ?? null,
  )
  const activeCount = computed(() => summary.value.queued + summary.value.running)

  async function loadJobs() {
    const generation = ++jobsRequestId
    loading.value = true
    try {
      const [jobItems, jobSummary] = await Promise.all([
        api.listJobs({ status: statusFilter.value, query: search.value.trim() }),
        api.getJobSummary(),
      ])
      // 更新的一次 loadJobs 已经发出，丢弃本次陈旧结果，防止乱序覆盖。
      if (generation !== jobsRequestId) return
      const previous = selectedJob.value
      jobs.value = jobItems.map((job) => (
        previous?.id === job.id
          ? { ...job, result: previous.result, logs: previous.logs }
          : job
      ))
      summary.value = jobSummary
      if (!selectedJobId.value || !jobs.value.some((job) => job.id === selectedJobId.value)) {
        selectedJobId.value = jobs.value[0]?.id ?? null
      }
      await refreshSelectedJob()
    } finally {
      // 仅由最新一次请求负责结束 loading，避免陈旧请求提前清除加载态。
      if (generation === jobsRequestId) loading.value = false
    }
  }

  async function loadSummary() {
    summary.value = await api.getJobSummary()
  }

  async function refreshSelectedJob() {
    if (!selectedJobId.value) return
    const updated = await api.getJob(selectedJobId.value)
    const index = jobs.value.findIndex((job) => job.id === updated.id)
    if (index >= 0) jobs.value[index] = updated
  }

  /** 选中任务后单独读取详情，列表接口因此无需重复传输每条任务的结果样本。 */
  async function selectJob(id: string) {
    selectedJobId.value = id
    await refreshSelectedJob()
  }

  async function loadSchedules() {
    schedules.value = await api.listSchedules()
  }

  async function createJob(payload: {
    sourceId: string
    tables: QueryTableBinding[]
    name: string
    sql: string
  }) {
    const job = await api.createJob(payload)
    jobs.value = [job, ...jobs.value]
    selectedJobId.value = job.id
    await loadSummary()
    return job
  }

  async function cancelJob(id: string) {
    replaceJob(await api.cancelJob(id))
    await loadSummary()
  }

  async function retryJob(id: string) {
    const job = await api.retryJob(id)
    jobs.value = [job, ...jobs.value]
    selectedJobId.value = job.id
    await loadSummary()
  }

  async function deleteJob(id: string) {
    await api.deleteJob(id)
    jobs.value = jobs.value.filter((job) => job.id !== id)
    if (selectedJobId.value === id) selectedJobId.value = jobs.value[0]?.id ?? null
    await loadSummary()
  }

  async function saveSchedule(id: string | null, payload: SchedulePayload) {
    const schedule = id
      ? await api.updateSchedule(id, payload)
      : await api.createSchedule(payload)
    const index = schedules.value.findIndex((item) => item.id === schedule.id)
    if (index >= 0) schedules.value[index] = schedule
    else schedules.value = [schedule, ...schedules.value]
    return schedule
  }

  async function toggleSchedule(id: string, enabled: boolean) {
    const schedule = await api.toggleSchedule(id, enabled)
    const index = schedules.value.findIndex((item) => item.id === id)
    if (index >= 0) schedules.value[index] = schedule
  }

  async function runSchedule(id: string) {
    const job = await api.runSchedule(id)
    jobs.value = [job, ...jobs.value]
    selectedJobId.value = job.id
    await loadSummary()
    return job
  }

  async function deleteSchedule(id: string) {
    await api.deleteSchedule(id)
    schedules.value = schedules.value.filter((item) => item.id !== id)
  }

  function replaceJob(job: Job) {
    const index = jobs.value.findIndex((item) => item.id === job.id)
    if (index >= 0) jobs.value[index] = job
  }

  return {
    jobs,
    schedules,
    selectedJobId,
    selectedJob,
    statusFilter,
    search,
    loading,
    summary,
    activeCount,
    loadJobs,
    loadSummary,
    refreshSelectedJob,
    selectJob,
    loadSchedules,
    createJob,
    cancelJob,
    retryJob,
    deleteJob,
    saveSchedule,
    toggleSchedule,
    runSchedule,
    deleteSchedule,
  }
})
