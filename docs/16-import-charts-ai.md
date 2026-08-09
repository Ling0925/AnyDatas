# 导入、图表与 AI SQL 实现

更新日期: 2026-07-19

## 1. 目标与边界

本轮补齐 Excel/CSV 分析主路径中的三个缺口:

1. 文件在正式进入工作区前先展示 Sheet、字段推断和原始样本，用户可确认字段类型。
2. 查询结果允许用一个维度和多个数值列生成更丰富的图表。
3. Owner/Admin 可为工作区配置 OpenAI Chat Completions 兼容接口，Analyst 以上角色可在右侧 AI 面板持续澄清需求、预览并迭代 SQL。

这些能力均在现有 Rust 单进程、SQLite 元数据、DuckDB 查询和 Vue 桌面工作台内完成，不增加 Kubernetes、Redis、独立 AI 网关或外部密钥服务。

## 2. 导入前预检

### 2.1 用户流程

1. 用户选择一个 Excel 或 CSV 文件。
2. `POST /api/data-sources/inspect` 将文件写入 `/data/staging`，读取工作表结构并返回最多 20 行样本。
3. 前端打开“确认数据导入”对话框，允许逐 Sheet 选择是否导入，并逐字段选择类型。
4. `POST /api/data-sources/import` 重新校验暂存文件、Sheet 和字段结构，在同一数据卷中原子移动文件，然后事务性创建数据源和逻辑表。
5. 用户取消时调用 `DELETE /api/data-sources/imports/{token}`；超过 24 小时的暂存记录会在后续预检时清理。

正式导入前不会创建工作区数据源。确认阶段发生文件变化、字段数量变化、字段改名、重复 Sheet 或越权访问时，服务端会拒绝提交并要求重新预检。

### 2.2 字段类型

当前支持六种类型:

| 显示类型 | DuckDB 类型 | 说明 |
| --- | --- | --- |
| 文本 | `VARCHAR` | 保留 CSV 前导零和原始字符串 |
| 整数 | `BIGINT` | 只接受有限且无小数部分的数值 |
| 小数 | `DOUBLE` | 接受有限数值 |
| 布尔 | `BOOLEAN` | 支持 true/false、1/0、yes/no、是/否 |
| 日期 | `DATE` | 支持常见年月日格式 |
| 日期时间 | `TIMESTAMP` | 支持 RFC 3339 和常见日期时间格式 |

类型选择是逻辑表 Schema，不会重写原始文件。首次查询构建 DuckDB 缓存时执行转换；无法转换的单元格写为 `NULL`。CSV 解析阶段保留原始字符串，因此把推断为整数的 `00123` 改成文本后仍可查询到 `00123`。

右侧字段列表也允许修改已导入逻辑表的类型。保存后 `config_version` 递增，旧 DuckDB 缓存不会继续复用，下次查询按新 Schema 构建缓存。

### 2.3 大文件行为

- 源数据没有行数硬上限，`ANYDATAS_MAX_SOURCE_ROWS` 已移除。
- `ANYDATAS_MAX_UPLOAD_BYTES` 只控制单文件上传体积，应按磁盘容量调整。
- 预检最多读取 2000 行用于类型推断，只返回前 20 行样本，不会在弹窗中加载整份文件。
- CSV 构建缓存时逐行读取；Excel 仍由 Calamine 在单进程内解析，大型工作簿的实际上限取决于可用内存。
- 单个文件最多确认导入 64 张工作表，避免一次请求产生失控的元数据和缓存任务。

## 3. 多指标结果图表

### 3.1 已实现图表

| 图表 | 数据映射 | 保护限制 |
| --- | --- | --- |
| 分组柱状图 | 一个维度、最多两个分组字段、最多四个指标 | 最多 80 个分类、16 个分组组合 |
| 堆叠柱状图 | 一个维度、最多两个分组字段、最多四个指标 | 最多 80 个分类、16 个分组组合 |
| 折线图 | 一个维度、最多两个分组字段、最多四个指标 | 最多 80 个分类、16 个分组组合 |
| 面积图 | 一个维度、最多两个分组字段、最多四个指标 | 最多 80 个分类、16 个分组组合 |
| 饼图 | 一个维度、一个或多个指标 | 多指标使用同心环，最多 80 个分类 |
| 散点图 | 一个日期、文本或数值 X 轴，最多两个分组字段、最多四个数值 Y 轴 | 最多绘制前 500 行、16 个分组组合 |
| 雷达图 | 一个维度、最多四个指标 | 最多 12 个分类 |

常规图表支持求和、平均值、最大值和最小值。查询结果存在重复维度时，前端先按所选方式聚合，再构建 ECharts 序列；这只影响当前可视化，不修改查询结果。柱状图、堆叠柱状图、折线图、面积图和散点图可再选择最多两个分组字段，超过 16 个组合的数据合并为“其他分组”，避免图例和序列数量失控。

图表配置目前是查询结果视图状态，刷新页面后不会持久化。持久化图表、仪表盘、共享链接和快照仍属于后续报表阶段。

## 4. 工作区 AI

### 4.1 配置与权限

Owner/Admin 可在顶栏“工作区 AI 设置”中配置:

- 是否启用。
- Base URL，例如 `https://api.openai.com/v1`，也可直接填写完整 `/chat/completions` 地址。
- 模型名称。
- 可选 API Key；本地 OpenAI-compatible 服务可以留空。

连接测试和配置修改仅允许 Owner/Admin。AI 对话允许 Owner、Admin 和 Analyst；Viewer 无权发起。工作台 Agent 使用标准 Chat Completions 原生工具字段:

```json
{
  "model": "configured-model",
  "messages": [
    { "role": "system", "content": "..." },
    { "role": "user", "content": "..." }
  ],
  "stream": true,
  "tools": [
    { "type": "function", "function": { "name": "preview_sql", "parameters": {} } }
  ],
  "tool_choice": "auto",
  "parallel_tool_calls": false
}
```

后端按 OpenAI Chat Completions 的 SSE `choices[].delta.content` 和 `choices[].delta.tool_calls` 增量拼接文字及原生函数调用；不支持流式的兼容服务仍可回退读取完整 `message`。累计 SSE 流量和最终有效回复不设置字符上限，已解析的协议字节会及时丢弃；仅当单个 SSE 事件异常超过 8 MB 时终止，以防损坏响应无限占用缓冲区。当前暂不支持 Responses API 或厂商私有参数。完整实现见 [17-ai-agent-runtime.md](17-ai-agent-runtime.md)。

### 4.2 上下文构建

浏览器始终提交 Agent 当前显式选择的 `tableId/alias` 白名单；新对话默认提交 `tables: []`，不会从工作台查询绑定自动补表。用户可在右侧逐张勾选，或用本地 slash 命令 `/all` 把当前全部逻辑表展开为具体绑定快照，命令文本本身不会发送给模型。仅当 Agent 选表与工作台绑定完全一致时，浏览器才会提交当前 SQL；查询结果小样本默认关闭，需用户显式开启。历史消息、滚动摘要和工具观察由服务端持久化并构建；后端重新验证工作区归属并从服务器元数据构建每张已选表的上下文:

- SQL 别名。
- 数据源名称和原始文件名。
- 逻辑表名、Sheet 名、起始/结束单元格。
- 字段名、字段类型和可空状态。
- 当前编辑器 SQL、最近对话和用户需求。
- 可选的当前查询结果字段、前五行样本、总行数和截断状态。

每张表最多发送 200 个字段，Schema 总上下文最多 30000 字符；单条新消息最多 4000 字符，当前 SQL 最多 20000 字符。默认总上下文预算为 80000 字符；固定规则、工作区上下文、滚动摘要和近期消息使用互不重叠的分区预算，最终请求不会突破全局上限。历史过长时会确定性压缩较早消息并优先保留最新需求，不会由浏览器重复上传整段对话。当前结果样本最多 20 个字段、8 行，序列化后最多 6000 字符。文件名、Sheet 名、表名、字段名、字段值和历史均被明确标记为不受信任的数据，模型被要求忽略其中的指令性内容。

模型返回后，后端提取可选 SQL 代码块，并复用查询引擎的安全校验，只接受一条 `SELECT` 或 `WITH` 查询，拒绝外部文件、网络、扩展加载及写操作。右侧 AI 面板将候选 SQL 作为独立提案展示：

- “预览”使用当前可信表绑定执行最多 20 行，不修改 Monaco 编辑器或正式结果。
- “应用”只替换编辑器内容，不自动执行。
- “应用并运行”替换编辑器并把查询结果写入中央结果区。
- 预览摘要会进入下一轮历史，用户可以继续要求修正字段、筛选、口径或排序。

模型可以在步骤预算内主动请求受控的只读 SQL 预览或逻辑表样本。后端只把最多 10 列、5 行的工具结果回传模型；最后一轮关闭工具声明，要求模型在预算内形成最终答复。`tables: []` 时后端不声明数据工具，并强制丢弃客户端传入的当前 SQL 与结果样本，因此纯对话不会旁路获得表格信息。

会话、消息、Run 和 Step 按用户及工作区保存在 SQLite。运行变化通过服务端事件总线唤醒 SSE 订阅者，空闲连接不轮询 SQLite；刷新或断线后仍从数据库恢复完整快照。重新生成会保留旧分支审计记录并将其标记为 `superseded`。有消息的会话不能原位切换表绑定，改变选表后会创建新会话，避免被排除表格继续从旧消息或工具结果进入模型。浏览器不再保存或回传完整 AI 历史。

### 4.3 密钥和隐私

- API Key 使用随机 Nonce 的 AES-256-GCM 加密后写入 SQLite。
- GET 设置接口只返回 `apiKeyConfigured`，不返回明文或密文。
- 默认主密钥首次启动时生成在 `/data/.secret-key`，Unix 权限为 `0600`。
- 也可通过至少 32 字符的 `ANYDATAS_SECRET_KEY` 提供主密钥；启用后不得随意修改，否则历史 API Key 无法解密。
- 备份和恢复必须包含完整 `anydatas-data` 卷，尤其是 SQLite 数据库与 `.secret-key`。

AI 对话只会把已选表的 Schema 与文件元数据、当前会话消息，以及满足绑定一致性条件后显式启用的 SQL/结果样本发送给配置的 AI 服务。包含敏感列名、字段值或业务信息的工作区应使用获准的服务地址；不选择表格时仅发送纯对话内容。公网服务必须使用 HTTPS，本机和局域网 OpenAI-compatible 服务可直接使用 HTTP。AI Provider 的网络策略与 QuickJS 后处理脚本完全独立。

## 5. API

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/data-sources/inspect` | Analyst+ | 上传到暂存区并返回 Sheet/字段/样本 |
| POST | `/api/data-sources/import` | Analyst+ | 按确认配置正式导入 |
| DELETE | `/api/data-sources/imports/{token}` | Analyst+ | 取消并清理本人工作区暂存文件 |
| GET | `/api/ai/settings` | 已登录成员 | 读取不含密钥的配置摘要 |
| PUT | `/api/ai/settings` | Owner/Admin | 更新工作区 AI 配置 |
| POST | `/api/ai/settings/test` | Owner/Admin | 使用已保存配置执行最小 Chat 请求 |
| GET/POST | `/api/ai/agent/conversations` | Analyst+ | 列出或创建服务端 Agent 会话 |
| GET/DELETE | `/api/ai/agent/conversations/{id}` | Analyst+ | 读取或归档会话 |
| POST | `/api/ai/agent/conversations/{id}/runs` | Analyst+ | 创建异步 Agent Run |
| GET | `/api/ai/agent/runs/{id}` | Analyst+ | 读取 Run 和结构化 Steps |
| GET | `/api/ai/agent/runs/{id}/events` | Analyst+ | 订阅事件驱动的 Run 快照 |
| POST | `/api/ai/agent/runs/{id}/cancel` | Analyst+ | 停止模型与当前 DuckDB 工具 |
| POST | `/api/ai/agent/runs/{id}/retry` | Analyst+ | 原位重试失败或取消 Run |

兼容接口 `POST /api/data-sources` 仍保留，供旧客户端直接上传；新桌面工作台统一使用预检和确认两阶段接口。

## 6. 运维检查

部署或恢复后至少检查:

```bash
curl --fail http://127.0.0.1:28080/api/health
docker compose exec anydatas test -s /data/.secret-key
docker compose logs --tail=100 anydatas
```

在界面完成一次“保存并测试”后，应确认设置接口不包含 API Key。不要只备份 `anydatas.db`；应使用 SQLite Online Backup API 取得一致数据库副本，并同时备份数据卷中的上传文件、DuckDB 缓存和 `.secret-key`。

## 7. 验收结果

- CSV 样本的 `客户编码` 从整数改为文本后，查询结果保留 `00123`、`00456`。
- 日期、整数和小数字段按用户确认类型写入 DuckDB 缓存。
- 取消导入后工作区文件计数不变，暂存文件被清理。
- 日期、文本和数值结果列均可作为散点图 X 轴，最多四个数值列可作为 Y 轴；选择两个额外分组字段后散点图仍检测到非空 Canvas 像素。
- 三个数值结果列可同时生成图表；分组柱状图、堆叠柱状图、折线图、饼图、散点图和雷达图均检测到非空 Canvas 像素。
- 模拟 OpenAI-compatible 服务完成“提出澄清问题 → 用户回答 → 返回候选 SQL”两轮交互。
- 候选 SQL 预览返回结果但不修改编辑器；应用后写入 Monaco，应用并运行后进入正式结果区。
- AI 会话刷新后恢复，1280x800 和 1440x900 桌面视口无重叠，浏览器控制台无错误或警告。
- API Key 不经读取接口回显，数据库中不存在测试明文，篡改密文会导致解密失败。
- `cargo test --offline` 的 44 个测试、Clippy 严格检查、Vue TypeScript 检查和生产构建通过。
