<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import { errorMessage } from '../api'
import AiAssistantPanel from '../components/AiAssistantPanel.vue'
import AgentTableSelector from '../components/AgentTableSelector.vue'
import { useWorkspaceStore } from '../stores/workspace'
import type { AgentChartSpec } from '../types'

interface CandidatePayload {
  sql: string
  chart?: AgentChartSpec
}

const router = useRouter()
const store = useWorkspaceStore()

onMounted(async () => {
  try {
    await store.loadSources()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
})

/**
 * 只在用户确认后写入工作台 SQL，并切回统一编辑器继续细调。
 * 这样聊天页和查询页共享同一个 SQL 状态，避免出现两个互相覆盖的编辑器。
 */
async function applyAgentSql(payload: CandidatePayload) {
  if (store.agentTableBindings.length) {
    await store.setQueryContext(store.agentTableBindings)
  }
  store.setAppliedChart(payload.chart ?? null)
  store.currentSql = payload.sql
  // 就地应用：只写入工作台 SQL，不跳走，用户可继续读对话，需要时再切到「数据分析」。
  ElMessage.success(
    payload.chart
      ? '候选 SQL 与图表已写入工作台，切到「数据分析」即可查看'
      : '候选 SQL 已写入工作台，切到「数据分析」即可查看',
  )
}

/**
 * 使用工作区正式查询链路执行候选 SQL，完成后回到数据分析页展示完整结果。
 * Agent 的小样本预览不会冒充正式结果，用户仍可继续制图、导出或转为后台任务。
 */
async function runAgentSql(payload: CandidatePayload) {
  if (store.agentTableBindings.length) {
    await store.setQueryContext(store.agentTableBindings)
  }
  store.setAppliedChart(payload.chart ?? null)
  store.currentSql = payload.sql
  try {
    await store.runQuery()
    await router.push('/workbench')
    ElMessage.success('候选 SQL 已运行')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}
</script>

<template>
  <div class="agent-layout">
    <AiAssistantPanel @apply-sql="applyAgentSql" @run-sql="runAgentSql" />
    <div class="agent-file-panel">
      <AgentTableSelector />
    </div>
  </div>
</template>
