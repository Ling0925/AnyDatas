<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import { errorMessage } from '../api'
import AiAssistantPanel from '../components/AiAssistantPanel.vue'
import AgentTableSelector from '../components/AgentTableSelector.vue'
import { useWorkspaceStore } from '../stores/workspace'

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
async function applyAgentSql(sql: string) {
  try {
    if (store.agentTableBindings.length) {
      // 跳过预览刷新：大表 inspect 会阻塞，应用 SQL 时只需同步绑定与编辑器。
      await store.setQueryContext(store.agentTableBindings, { refreshPreview: false })
    }
    store.currentSql = sql
    await router.push('/workbench')
    ElMessage.success('候选 SQL 已应用到工作台')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

/**
 * 使用工作区正式查询链路执行候选 SQL，完成后回到数据分析页展示完整结果。
 * Agent 的小样本预览不会冒充正式结果，用户仍可继续制图、导出或转为后台任务。
 */
async function runAgentSql(sql: string) {
  if (!store.agentTableBindings.length) {
    ElMessage.warning('请先在右侧选择 Agent 要用的表格')
    return
  }
  const loading = ElMessage({
    message: '正在应用并运行查询… 大表首次运行可能需要较长时间建立缓存',
    type: 'info',
    duration: 0,
    showClose: true,
  })
  try {
    await store.setQueryContext(store.agentTableBindings, { refreshPreview: false })
    store.currentSql = sql
    await store.runQuery()
    await router.push('/workbench')
    ElMessage.success('候选 SQL 已运行')
  } catch (error) {
    ElMessage.error(`运行失败：${errorMessage(error)}`)
  } finally {
    loading.close()
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
