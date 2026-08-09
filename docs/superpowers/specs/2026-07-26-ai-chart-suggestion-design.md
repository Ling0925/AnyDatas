# AI 图表推荐（AI Chart Suggestion）设计

- 日期: 2026-07-26
- 状态: 已通过设计评审，待实现
- 目标: 让 AI Agent 在提出候选 SQL 的同时，按数据内容推荐一张最合适的图表；用户可在对话里预览，并「应用并运行」把该图表带入工作台展示完整结果。

## 1. 背景与决策

现状：AI Agent 会在最终回复里以 ```sql 代码块给出候选 SQL（`split_reply_and_sql` 提取），用户可复制/应用/预览/应用并运行。工作台的 `ResultChart` 支持 7 种图表（分组柱/堆叠柱/折线/面积/饼/散点/雷达），配置模型为「图表类型 + 维度列 + 度量列(≤4) + 分组列 + 聚合方式」，聚合在客户端完成，列以**结果列索引**引用。

评审通过的两个关键决策：
1. **接入点**：AI 在提候选 SQL 时**一并推荐图表**（不新增独立入口）。
2. **表示**：AI 从**现有 7 种图表类型 + 列映射 + 聚合**中智能选择（结构化配置，用现有 ResultChart 渲染），**不生成自由 ECharts option**（安全、一致、可带入工作台继续手调）。
3. **机制**：模型在同一次回复里 **co-emit 一个 ```chart JSON 块**，复用现有 SQL 提取模式解析+校验（无额外工具往返）。

非目标（YAGNI）：自由 ECharts option、独立于对话的「结果区一键推荐」按钮、图表持久化到报表、多图/仪表盘。

## 2. 数据模型：图表配置（Chart Spec）

模型在回复中输出（紧随 ```sql 之后）：

````
```chart
{
  "type": "bar",
  "category": "月份",
  "values": ["销售额", "利润"],
  "groups": ["区域"],
  "aggregation": "sum",
  "title": "各区域月度销售",
  "rationale": "按月份看多区域销售趋势，柱状便于对比"
}
```
````

字段（**按 SELECT 输出列名引用**，非索引）：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `type` | string | 必填，∈ {bar, stacked-bar, line, area, pie, scatter, radar} |
| `category` | string | 必填，维度列名 |
| `values` | string[] | 必填，1–4 个度量列名 |
| `groups` | string[] | 可选，分组列名（0–1 项；多余忽略） |
| `aggregation` | string | 可选，∈ {sum, average, max, min}，默认 sum |
| `title` | string | 可选，≤120 字符 |
| `rationale` | string | 可选，≤240 字符，展示"为什么这么画" |

## 3. 架构与数据流

```
模型回复(含 ```sql + ```chart)
  └─ 后端 split_reply_sql_and_chart → (reply, sql, chart_spec?)
       ├─ 校验 chart_spec（白名单/条数/字符串）→ 非法则丢弃(chart=None)，不影响 sql/回复
       └─ 写入 ai_messages.sql_text + ai_messages.chart_spec_json
  └─ 消息 DTO 携带 { sql, chart }
       └─ 前端 AiAssistantPanel:
            ├─ 候选消息带 chart 时，预览区在样本表下多渲染一张【图表缩略预览】
            │    （用 preview 样本 + chart_spec，列名→索引映射；映射失败则不渲染缩略图，仅显示配置说明）
            └─ 应用/应用并运行时，把 chart_spec 一起带入工作台
                 └─ store.appliedChart ← chart_spec
                      └─ WorkbenchView 运行结果后，把 appliedChart 传给 ResultChart
                           └─ ResultChart 接收外部初始配置：列名→索引映射 + 切到图表视图渲染
                                （列名匹配不到→跳过该度量/回退默认；用户仍可手调）
```

## 4. 组件设计

### 4.1 后端（`backend/`）

- **`services/agent.rs`**
  - 将 `split_reply_and_sql` 扩展/新增为 `split_reply_sql_and_chart(content) -> (reply, Option<sql>, Option<ChartSpec>)`：在提取 ```sql 后，再扫描 ```chart 块，`serde_json` 解析为 `ChartSpec`，经 `validate_chart_spec` 校验；任一步失败则 `chart = None`（静默丢弃，绝不影响 SQL 与回复文本）。回复文本需同时剥离 sql 与 chart 两个代码块。
  - `ChartSpec` 结构体 + `validate_chart_spec`：type/aggregation 用 `#[serde(rename_all)]` 枚举或字符串白名单校验；`values` 长度 1–4；`groups` 截断到 ≤1；`title`/`rationale` 长度上限；全部为字符串。
  - 写入助手消息处（`INSERT INTO ai_messages`，约 1022/2170 行附近）增加 `chart_spec_json`（`serde_json::to_string(&chart_spec)`，None 则 NULL）。
  - 消息查询 SELECT 增加 `chart_spec_json` 列；行结构体 `AiMessageRow` 加字段。
  - 更新系统提示（构建上下文处）：当结果适合可视化时，在候选 SQL 之后追加一个 ```chart 配置块，**只用 SELECT 的输出列名**，类型从 7 种里选，度量 1–4 个；不确定就不给图表。明确「chart 块只是建议、不会被执行」。
- **migration `backend/migrations/0009_ai_message_chart.sql`**：`ALTER TABLE ai_messages ADD COLUMN chart_spec_json TEXT;`（可空，向后兼容）。
- **DTO**（`models.rs` / agent DTO）：`AiAgentMessage` 增加 `chart: Option<ChartSpec>`（camelCase 序列化为 `chart`）。

### 4.2 前端（`frontend/`）

- **`types.ts`**：新增 `AgentChartSpec { type; category; values: string[]; groups?: string[]; aggregation?; title?; rationale? }`；`AiAgentMessage` 增加 `chart?: AgentChartSpec`。
- **`ResultChart.vue`**：新增可选 prop `appliedConfig?: AgentChartSpec`。
  - 抽出「列名→当前 columns 索引」的映射逻辑；当 `appliedConfig` 提供或变化时：设置 `chartType`、`categoryIndex`、`valueIndexes`（映射成功的度量，最多 4）、`groupIndexes`、`aggregation`，并把结果视图切到图表。
  - 匹配不到的列名跳过；若 category 或全部 values 都匹配不到，回退现有默认推断逻辑。保持用户可手调（appliedConfig 只作为初始/应用值，不锁定控件）。
- **`components/AiChartPreview.vue`（新）**：轻量缩略图，输入 `spec: AgentChartSpec` + `result: QueryResponse`（预览样本），内部复用 ResultChart 或直接构建一份最小 ECharts option（与 ResultChart 同源的映射/聚合）。列名映射失败则渲染一行「图表建议：<type> · 维度 <category> · 度量 <values>」的说明而非空图。
  - 决策：优先**复用 ResultChart**（传 columns+rows+appliedConfig，隐藏其控件条），避免重复实现聚合。若控件隐藏改造成本高，则做最小只读渲染。
- **`components/AiAssistantPanel.vue`**：候选消息（有 `sql`）若同时有 `chart`，在 `AiResultPreview`（样本表）下方渲染 `AiChartPreview`（用已有的 `messagePreview(message)` 样本）；`applySql`/`runSql` 时把 `message.chart` 一并带出（emit 里附带 chart）。
- **`views/AgentView.vue`**：`applyAgentSql`/`runAgentSql` 接收 chart，写入 `store.appliedChart`。
- **`stores/workspace.ts`**：新增 `appliedChart: AgentChartSpec | null` + `setAppliedChart()`；`runQuery` 成功后不清除（用于渲染）；新查询/切换保存查询时清空。
- **`views/WorkbenchView.vue`**：把 `store.appliedChart` 传给 `ResultChart`；当存在 appliedChart 时，结果默认切到图表视图。

## 5. 安全与健壮性

- 图表配置是**纯数据白名单**（枚举类型/聚合、字符串列名、条数上限），不含任何可执行内容，不引入自由 ECharts option（与决策一致，规避评审提到的 option 注入）。
- 解析失败、字段非法、列名匹配不到：**优雅降级**（丢弃图表 / 跳过度量 / 显示文字建议），绝不影响 SQL、回复、或查询执行。
- 复用现有只读 SQL 安全边界，chart 块不改变任何执行路径。

## 6. 测试

- 后端单测（`agent.rs` tests）：
  - `split_reply_sql_and_chart` 同时含 sql+chart → 正确提取二者且回复剥离两个块。
  - 非法 chart（未知 type / values 为空 / 超 4 个 / 非字符串）→ chart=None，sql 不受影响。
  - 缺 chart 块 → chart=None，行为与现状一致（回归保护）。
  - `validate_chart_spec` 各分支。
- 前端：
  - ResultChart「列名→索引映射 + 回退」：列名齐全 → 正确配置；部分/全部缺失 → 跳过/回退。
  - AiChartPreview 在映射失败时渲染文字建议而非空图。
- 手动/浏览器验证：真实对话里模型给出 chart，预览缩略图正确，「应用并运行」后工作台以该图渲染，用户可手调。

## 7. 实现顺序（供 writing-plans 细化）

1. 后端：ChartSpec + validate + split 扩展 + migration 0009 + DTO + 系统提示 + 单测。（可独立验证：cargo test）
2. 前端类型 + store appliedChart。
3. ResultChart 接收 appliedConfig（列名映射 + 回退）+ 单测思路。
4. AiChartPreview + AiAssistantPanel 预览与携带。
5. AgentView/WorkbenchView 接线。
6. 构建验证 + 浏览器走查 + 部署（沿用现有安全部署流程）。

## 8. 验收标准

- 模型在合适场景随候选 SQL 给出 chart 配置；对话预览区显示图表缩略图（或文字建议）。
- 「应用并运行」后，工作台结果区以 AI 建议的图表类型/维度/度量/聚合渲染，用户可继续手调。
- 非法/缺失 chart 配置不影响任何现有功能；`cargo test` 与前端 `build` 全绿。
