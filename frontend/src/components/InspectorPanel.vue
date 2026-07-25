<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Braces,
  CalendarDays,
  CopyPlus,
  FileSpreadsheet,
  Hash,
  Plus,
  Save,
  TextCursorInput,
  ToggleLeft,
  Trash2,
} from '@lucide/vue'

import { errorMessage } from '../api'
import { useWorkspaceStore } from '../stores/workspace'
import type { FieldDataType, FieldDefinition, SourceTablePayload } from '../types'

const emit = defineEmits<{
  formula: [name: string, expression: string]
}>()

const store = useWorkspaceStore()
const form = reactive<SourceTablePayload>({
  name: '',
  sheetName: '',
  startCell: 'A1',
  endCell: null,
  firstRowAsHeader: true,
})
const saving = ref(false)
const formulaVisible = ref(false)
const formula = reactive({ name: '', expression: '' })
const createVisible = ref(false)
const createSaving = ref(false)
const fieldTypeDraft = ref<Record<string, FieldDataType>>({})
const fieldTypes: FieldDataType[] = ['文本', '整数', '小数', '布尔', '日期', '日期时间']
const createForm = reactive<SourceTablePayload>({
  name: '',
  sheetName: '',
  startCell: 'A1',
  endCell: null,
  firstRowAsHeader: true,
})

const fields = computed(() => store.selectedTable?.fields ?? store.preview?.columns ?? [])
const selectedAliases = computed(() => store.queryBindings
  .filter((binding) => binding.tableId === store.selectedTableId)
  .map((binding) => binding.alias))

watch(
  () => store.selectedTable,
  (table) => {
    if (!table) return
    form.name = table.name
    form.sheetName = table.sheetName
    form.startCell = table.startCell
    form.endCell = table.endCell
    form.firstRowAsHeader = table.firstRowAsHeader
    fieldTypeDraft.value = Object.fromEntries(
      table.fields.map((field) => [field.name, field.dataType]),
    )
  },
  { immediate: true },
)

/** 根据推断类型选择字段图标，数值、布尔和文本能在长字段列表中快速区分。 */
function fieldIcon(field: FieldDefinition) {
  if (field.dataType === '整数' || field.dataType === '小数') return Hash
  if (field.dataType === '布尔') return ToggleLeft
  if (field.dataType === '日期' || field.dataType === '日期时间') return CalendarDays
  return TextCursorInput
}

/** 格式化文件大小，右侧元数据保持紧凑并适合桌面工作台宽度。 */
function formatSize(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 将缓存状态转换为用户可理解的短标签，不暴露实现细节。 */
function cacheLabel(status: string) {
  return {
    pending: '待准备',
    building: '准备中',
    ready: '可复用',
    failed: '失败',
  }[status] ?? status
}

/** 保存当前逻辑表配置，后端会同步重建字段结构并使旧缓存失效。 */
async function saveConfig() {
  saving.value = true
  try {
    const table = store.selectedTable
    const structureUnchanged = Boolean(table)
      && form.sheetName === table?.sheetName
      && form.startCell.trim().toUpperCase() === table?.startCell
      && (form.endCell?.trim().toUpperCase() || null) === table?.endCell
      && form.firstRowAsHeader === table?.firstRowAsHeader
    await store.applyTableConfig({
      ...form,
      endCell: form.endCell?.trim() || null,
      fields: structureUnchanged
        ? fields.value.map((field) => ({
            ...field,
            dataType: fieldTypeDraft.value[field.name] ?? field.dataType,
          }))
        : undefined,
    })
    ElMessage.success('逻辑表设置已应用')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

/** 以当前 Sheet 为模板打开新范围表单，减少重复配置并允许一页多块数据。 */
function openCreateTable() {
  const table = store.selectedTable
  createForm.name = table ? `${table.name} 范围` : '新逻辑表'
  createForm.sheetName = table?.sheetName ?? store.selectedSource?.sheetNames[0] ?? ''
  createForm.startCell = table?.startCell ?? 'A1'
  createForm.endCell = table?.endCell ?? null
  createForm.firstRowAsHeader = table?.firstRowAsHeader ?? true
  createVisible.value = true
}

/** 创建额外逻辑表后保持其为当前检查对象，用户可以马上预览并加入查询。 */
async function createTable() {
  if (!createForm.name.trim()) {
    ElMessage.warning('请输入逻辑表名称')
    return
  }
  createSaving.value = true
  try {
    await store.createTable({
      ...createForm,
      name: createForm.name.trim(),
      endCell: createForm.endCell?.trim() || null,
    })
    createVisible.value = false
    ElMessage.success('逻辑表已创建')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    createSaving.value = false
  }
}

/** 删除前显示逻辑表名称，默认表由服务端保护且不会出现此操作。 */
async function deleteTable() {
  const table = store.selectedTable
  if (!table || table.isDefault) return
  try {
    await ElMessageBox.confirm(`删除逻辑表“${table.name}”？`, '删除逻辑表', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await store.deleteTable(table.id)
    ElMessage.success('逻辑表已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

/** 将公式写入查询编辑器而不直接改动源数据，便于继续组合 JOIN 与聚合。 */
function addFormula() {
  if (!formula.name.trim() || !formula.expression.trim()) {
    ElMessage.warning('请填写字段名称和表达式')
    return
  }
  emit('formula', formula.name.trim(), formula.expression.trim())
  formulaVisible.value = false
  formula.name = ''
  formula.expression = ''
  ElMessage.success('计算字段已写入查询')
}
</script>

<template>
  <div class="inspector panel-column">
    <template v-if="store.selectedSource && store.selectedTable">
      <section class="inspector-section file-summary">
        <div class="section-title">
          <h2>文件与逻辑表</h2>
          <div class="section-actions">
            <el-tooltip content="新建范围" placement="left">
              <el-button class="icon-button plain" aria-label="新建范围" @click="openCreateTable">
                <CopyPlus :size="15" />
              </el-button>
            </el-tooltip>
            <el-tooltip v-if="!store.selectedTable.isDefault" content="删除逻辑表" placement="left">
              <el-button class="icon-button danger" aria-label="删除逻辑表" @click="deleteTable">
                <Trash2 :size="14" />
              </el-button>
            </el-tooltip>
          </div>
        </div>
        <div class="file-summary-main">
          <span class="file-icon large"><FileSpreadsheet :size="20" /></span>
          <div>
            <strong>{{ store.selectedTable.name }}</strong>
            <span>{{ store.selectedSource.name }} · {{ store.selectedTable.sheetName }}</span>
          </div>
        </div>
        <dl class="metadata-list">
          <div><dt>文件类型</dt><dd>{{ store.selectedSource.fileKind === 'csv' ? 'CSV' : 'Excel' }}</dd></div>
          <div><dt>文件大小</dt><dd>{{ formatSize(store.selectedSource.sizeBytes) }}</dd></div>
          <div><dt>数据行</dt><dd>{{ store.selectedTable.rowCount.toLocaleString() }}</dd></div>
          <div><dt>字段数</dt><dd>{{ store.selectedTable.columnCount }}</dd></div>
          <div><dt>查询别名</dt><dd><code>{{ selectedAliases.join(', ') || '未绑定' }}</code></dd></div>
          <div><dt>查询缓存</dt><dd>{{ cacheLabel(store.selectedTable.cacheStatus) }}</dd></div>
        </dl>
      </section>

      <section class="inspector-section read-settings">
        <div class="section-title"><h2>读取设置</h2></div>
        <label class="form-label">
          <span>逻辑表名称</span>
          <el-input v-model="form.name" size="small" maxlength="80" />
        </label>
        <label class="form-label">
          <span>工作表</span>
          <el-select v-model="form.sheetName" size="small">
            <el-option
              v-for="sheet in store.selectedSource.sheetNames"
              :key="sheet"
              :label="sheet"
              :value="sheet"
            />
          </el-select>
        </label>
        <div class="cell-range-grid">
          <label class="form-label">
            <span>起始单元格</span>
            <el-input v-model="form.startCell" size="small" maxlength="10" />
          </label>
          <label class="form-label">
            <span>结束单元格</span>
            <el-input v-model="form.endCell" size="small" maxlength="10" placeholder="自动" />
          </label>
        </div>
        <el-checkbox v-model="form.firstRowAsHeader">首行作为字段名</el-checkbox>
        <el-button
          class="full-button"
          aria-label="应用读取设置"
          :loading="saving"
          @click="saveConfig"
        >
          <Save :size="15" />
          应用设置
        </el-button>
      </section>

      <section class="inspector-section fields-section">
        <div class="section-title">
          <div>
            <h2>字段</h2>
            <span>{{ fields.length }} 个</span>
          </div>
          <el-tooltip content="添加计算字段" placement="left">
            <el-button class="icon-button plain" aria-label="添加计算字段" @click="formulaVisible = true">
              <Plus :size="15" />
            </el-button>
          </el-tooltip>
        </div>
        <div class="field-list">
          <div v-for="field in fields" :key="field.name" class="field-row">
            <component :is="fieldIcon(field)" :size="15" />
            <span>{{ field.name }}</span>
            <el-select
              v-model="fieldTypeDraft[field.name]"
              size="small"
              :aria-label="`${field.name} 数据类型`"
            >
              <el-option v-for="type in fieldTypes" :key="type" :label="type" :value="type" />
            </el-select>
          </div>
          <div v-if="!fields.length" class="field-empty">当前范围没有字段</div>
        </div>
      </section>
    </template>

    <div v-else class="inspector-empty">
      <FileSpreadsheet :size="26" />
      <span>从左侧选择一个工作表</span>
    </div>

    <el-dialog v-model="createVisible" title="新建逻辑表范围" width="500px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="逻辑表名称">
          <el-input v-model="createForm.name" maxlength="80" />
        </el-form-item>
        <el-form-item label="工作表">
          <el-select v-model="createForm.sheetName">
            <el-option
              v-for="sheet in store.selectedSource?.sheetNames ?? []"
              :key="sheet"
              :label="sheet"
              :value="sheet"
            />
          </el-select>
        </el-form-item>
        <div class="dialog-form-grid">
          <el-form-item label="起始单元格">
            <el-input v-model="createForm.startCell" maxlength="10" />
          </el-form-item>
          <el-form-item label="结束单元格">
            <el-input v-model="createForm.endCell" maxlength="10" placeholder="自动读取到末尾" />
          </el-form-item>
        </div>
        <el-checkbox v-model="createForm.firstRowAsHeader">首行作为字段名</el-checkbox>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" @click="createTable">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="formulaVisible" title="添加计算字段" width="460px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="字段名称">
          <el-input v-model="formula.name" placeholder="例如：含税金额" />
        </el-form-item>
        <el-form-item label="DuckDB 表达式">
          <el-input
            v-model="formula.expression"
            type="textarea"
            :rows="4"
            placeholder='例如：data."金额" * 1.13'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formulaVisible = false">取消</el-button>
        <el-button type="primary" @click="addFormula"><Braces :size="15" /> 写入查询</el-button>
      </template>
    </el-dialog>
  </div>
</template>
