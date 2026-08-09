<script setup lang="ts">
import { Plus, RefreshCw } from '@lucide/vue'

import FileSourceDialog from '../components/file-sources/FileSourceDialog.vue'
import FileSourceList from '../components/file-sources/FileSourceList.vue'
import { useFileSources } from '../composables/useFileSources'

const {
  hasDesktop,
  sources,
  dataSources,
  schedules,
  loading,
  actionId,
  toggleId,
  dialogTargetsLoading,
  saving,
  editingId,
  expandedRunsId,
  dialogVisible,
  form,
  loadFileSources,
  openCreateDialog,
  openEditDialog,
  pickDirectory,
  saveFileSource,
  toggleSource,
  runNow,
  removeSource,
  toggleRuns,
} = useFileSources()
</script>

<template>
  <div class="file-sources-view">
    <header class="file-sources-header">
      <div>
        <h1>文件采集</h1>
        <p>在桌面客户端（Electron）中按计划扫描本地目录，自动上传并覆盖更新服务器数据源。</p>
        <p v-if="!hasDesktop" class="desktop-hint">
          当前运行在网页浏览器中，此功能仅桌面客户端可用。
        </p>
      </div>
      <el-tooltip v-if="!hasDesktop" content="仅桌面客户端可用" placement="bottom">
        <span>
          <el-button type="primary" disabled><Plus :size="15" /> 新建文件源</el-button>
        </span>
      </el-tooltip>
      <el-button v-else type="primary" @click="openCreateDialog"><Plus :size="15" /> 新建文件源</el-button>
    </header>

    <div class="file-sources-toolbar">
      <div class="toolbar-copy">
        <strong>本地文件源</strong>
        <span>{{ sources.length }} 个</span>
      </div>
      <el-tooltip content="刷新" placement="bottom">
        <el-button class="icon-button plain" aria-label="刷新" @click="loadFileSources">
          <RefreshCw :size="15" />
        </el-button>
      </el-tooltip>
    </div>

    <FileSourceList
      :sources="sources"
      :loading="loading"
      :action-id="actionId"
      :toggle-id="toggleId"
      :expanded-runs-id="expandedRunsId"
      :data-sources="dataSources"
      :schedules="schedules"
      @toggle="toggleSource"
      @run="runNow"
      @edit="openEditDialog"
      @remove="removeSource"
      @toggle-runs="toggleRuns"
    />

    <FileSourceDialog
      v-model="dialogVisible"
      :editing-id="editingId"
      :form="form"
      :data-sources="dataSources"
      :schedules="schedules"
      :targets-loading="dialogTargetsLoading"
      :saving="saving"
      @save="saveFileSource"
      @pick-directory="pickDirectory"
    />
  </div>
</template>

<style scoped>
.file-sources-view {
  --fs-grid-cols: 76px minmax(190px, 1.3fr) 120px minmax(150px, 1fr) 150px minmax(150px, 1fr) minmax(246px, 1fr);
  height: 100%;
  min-height: 0;
  display: grid;
  grid-template-rows: auto 50px minmax(0, 1fr);
  background: var(--app-bg);
}

/* 窄视口：表格收缩为六个数据列，操作列由 FileSourceRow 移至整行底部。 */
@media (max-width: 1180px) {
  .file-sources-view {
    --fs-grid-cols: 70px minmax(140px, 1.3fr) 96px minmax(110px, 1fr) 124px minmax(110px, 1fr);
  }
}

.file-sources-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

.file-sources-header > div {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.file-sources-header h1 {
  font-size: 17px;
  font-weight: 750;
  line-height: 1.3;
}

.file-sources-header p {
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.file-sources-header .desktop-hint {
  color: var(--amber);
  font-size: 11px;
}

.file-sources-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 20px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

.file-sources-toolbar .toolbar-copy strong {
  font-size: 12px;
  font-weight: 700;
}

.file-sources-toolbar .toolbar-copy span {
  color: var(--text-secondary);
  font-size: 11px;
}
</style>
