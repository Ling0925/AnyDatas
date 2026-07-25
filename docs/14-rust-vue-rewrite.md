# Rust + Vue 重构设计与实施状态

更新日期: 2026-07-19

## 1. 重构目标

产品主路径调整为“上传 Excel/CSV -> 选择一个或多个 Sheet/范围 -> 预览字段 -> SQL/公式分析 -> 查看结果”。后台任务和定时任务保留，但降为复杂查询的辅助工作区，不再主导产品信息架构。

本轮采用单机优先架构。一个 Rust 进程同时提供 API、静态前端、SQLite 元数据、后台任务 worker 和计划调度器；每个逻辑表首次查询生成不可变 DuckDB 缓存，查询临时库只读挂载所需缓存并在结束后清理。部署不依赖 Kubernetes、Redis、Temporal 或 Docker socket。

## 2. 技术栈

| 层级 | 选型 | 职责 |
| --- | --- | --- |
| Web API | Rust 1.97 + Axum 0.8 | 路由、上传、参数校验、静态前端托管 |
| 元数据 | SQLite + SQLx 0.8 | 文件配置、任务、日志、结果和计划持久化 |
| 表格读取 | Calamine 0.36 + csv | Excel 多工作表、起始单元格和 CSV 读取 |
| 查询引擎 | DuckDB 1.10504 | 聚合、筛选、窗口函数和计算字段 |
| 前端 | Vue 3 + TypeScript + Vite | 桌面工作台和后台任务管理 |
| UI 组件 | Element Plus + Lucide | 表单、对话框、状态控件和图标 |
| SQL 编辑器 | Monaco Editor | 本地离线 SQL 编辑体验 |
| 图表 | ECharts 6 按需加载 | 查询结果的七类多指标图表 |
| AI 接口 | Reqwest + OpenAI Chat Completions 格式 | 工作区级连接、多轮上下文、追问和 SQL 提案 |

## 3. 前端信息架构

### 数据分析

- 左侧: 文件到 Sheet/逻辑表的树、搜索、上传、删除和加入查询。
- 导入预检: 正式导入前选择 Sheet、查看原始样本并修改字段类型。
- 中间查询 Tab: 上半部 SQL 编辑器，下半部查询结果。
- 查询绑定栏: 多文件/多 Sheet 绑定、别名修改、移除和同表多别名。
- 查询工具栏: 载入、创建、更新和删除包含完整表绑定的保存查询。
- 结果区域: 表格/图表切换、维度、最多四个数值指标、聚合方式和 CSV 导出。
- AI 分析: 右侧多轮聊天使用当前表绑定、服务端字段结构、现有 SQL 和可选结果样本；支持追问、候选 SQL、独立预览、应用和应用并运行。
- 中间数据预览 Tab: 使用右侧读取设置重新解析并预览。
- 右侧数据 Tab: 文件信息、逻辑表名称、工作表、起止单元格、表头、缓存状态和字段列表。
- 右侧 AI Tab: 按用户和工作区保留最近对话，刷新后可继续同一需求或新建会话。
- 计算字段: 在右侧添加 DuckDB 表达式，前端生成可继续编辑的 SQL。

### 后台任务

- 左侧: 全部、运行、排队、完成、失败、停止和计划任务入口。
- 左侧状态数量来自独立的工作区任务汇总接口，不受当前筛选条件影响。
- 中间: 运行记录或计划列表。
- 右侧: 任务元数据、SQL、错误、步骤日志和结果预览。
- 操作: 新建、停止、重试、删除、启停计划、立即运行和编辑计划。

移动端不在本轮范围内，页面设置最小桌面宽度 1180px。

## 4. 后端模块

| 路径 | 职责 |
| --- | --- |
| `backend/src/api/auth.rs` | 首次初始化、密码登录、会话、退出和工作区身份提取 |
| `backend/src/api/data_sources.rs` | 文件暂存预检、确认导入、列表、删除和历史默认表兼容接口 |
| `backend/src/api/ai.rs` | 工作区 AI 配置、连接测试、可信 Schema 上下文和 SQL 生成 |
| `backend/src/api/source_tables.rs` | 逻辑表列表、范围配置、独立预览、创建和删除 |
| `backend/src/api/queries.rs` | 前台同步 SQL 查询 |
| `backend/src/api/saved_queries.rs` | 工作区内保存查询的增删改查 |
| `backend/src/api/jobs.rs` | 后台任务生命周期和记录查询 |
| `backend/src/api/schedules.rs` | Cron 计划管理和立即运行 |
| `backend/src/services/spreadsheet.rs` | 表格解析、表头规范化和类型推断 |
| `backend/src/services/query_bindings.rs` | 新旧请求绑定规范化及保存/任务/计划关系持久化 |
| `backend/src/services/query_engine.rs` | 单表缓存、多表挂载、只读 SQL、查询中断和结果转换 |
| `backend/src/services/secrets.rs` | 单机主密钥和工作区 API Key 的 AES-256-GCM 加解密 |
| `backend/src/workers.rs` | SQLite 队列消费和到期计划入队 |
| `backend/migrations/0001_init.sql` | 文件、查询、任务和计划元数据模型 |
| `backend/migrations/0002_auth_workspaces.sql` | 用户、工作区、成员关系、会话和登录限流 |
| `backend/migrations/0003_multi_table_queries.sql` | 逻辑表、多表绑定和历史 data 别名回填 |
| `backend/migrations/0004_staged_imports.sql` | 24 小时导入暂存记录和文件归属校验 |
| `backend/migrations/0005_workspace_ai.sql` | 工作区 AI 设置和加密 API Key |

DuckDB 连接在服务端完成缓存挂载后关闭外部访问和扩展自动加载，并只接受单条 `SELECT` 或 `WITH` 查询。源数据不设置行数硬上限；CSV 逐行导入持久化单表缓存，配置版本变化后生成新缓存键。一次查询最多绑定 16 张逻辑表，同表多别名复用一个挂载。查询通过子查询包装限制返回前端的结果行数。后台任务注册 DuckDB 中断句柄，并在缓存导入期间轮询取消状态；停止运行中任务会中断查询并释放 worker，而不是只修改数据库状态。

密码使用 Argon2 散列。浏览器只保存 `HttpOnly`、`SameSite=Lax` 会话 Cookie，数据库只保存会话 token 的 SHA-256 摘要；连续 5 次登录失败会锁定 15 分钟。所有数据源、查询、任务和计划接口均从会话解析工作区并在 SQL 查询层约束范围，写操作要求 Owner、Admin 或 Analyst。

AI API Key 使用 AES-256-GCM 加密，主密钥默认保存在数据卷 `/data/.secret-key` 且不会经 API 返回。AI Schema 上下文由后端根据已授权的逻辑表绑定重建，不接受浏览器伪造字段结构；最近消息与结果样本受数量和字符上限约束。模型返回的候选内容仍需通过单条只读 SQL 校验，预览不会修改编辑器，只有用户确认后才应用或执行。

## 5. API 覆盖

| API | 状态 |
| --- | --- |
| `GET /api/health` | 完成 |
| `GET /api/auth/status` | 完成 |
| `POST /api/auth/setup` | 完成 |
| `POST /api/auth/login` | 完成 |
| `POST /api/auth/logout` | 完成 |
| `GET /api/auth/me` | 完成 |
| `GET/POST /api/data-sources` | 完成，保留兼容直传 |
| `POST /api/data-sources/inspect` | 完成 |
| `POST /api/data-sources/import` | 完成 |
| `DELETE /api/data-sources/imports/{token}` | 完成 |
| `GET/DELETE /api/data-sources/{id}` | 完成 |
| `PATCH /api/data-sources/{id}/config` | 完成 |
| `GET /api/data-sources/{id}/preview` | 完成 |
| `GET /api/source-tables` | 完成 |
| `GET/PATCH/DELETE /api/source-tables/{id}` | 完成 |
| `POST /api/data-sources/{id}/tables` | 完成 |
| `GET /api/source-tables/{id}/preview` | 完成 |
| `POST /api/query` | 完成 |
| `GET/POST /api/saved-queries` | 完成 |
| `GET/PUT/DELETE /api/saved-queries/{id}` | 完成 |
| `GET/POST /api/jobs` | 完成 |
| `GET /api/jobs/summary` | 完成 |
| `GET/DELETE /api/jobs/{id}` | 完成 |
| `POST /api/jobs/{id}/cancel` | 完成 |
| `POST /api/jobs/{id}/retry` | 完成 |
| `GET/POST /api/schedules` | 完成 |
| `PUT/DELETE /api/schedules/{id}` | 完成 |
| `POST /api/schedules/{id}/toggle` | 完成 |
| `POST /api/schedules/{id}/run` | 完成 |
| `GET/PUT /api/ai/settings` | 完成 |
| `POST /api/ai/settings/test` | 完成 |
| `POST /api/ai/sql` | 完成 |

## 6. 数据与迁移边界

新实现使用独立的 `data_sources`、`source_tables`、`jobs`、`schedules` 和 `saved_queries` 表。Rust 版现有 SQLite 会由 SQLx 原地升级：每个历史文件回填默认逻辑表，历史保存查询、任务和计划回填 `data` 绑定。旧 Python 数据库仍不会被原地修改。

迁移顺序:

1. 固定 Excel/CSV 工作台的数据模型和交互。
2. 增加密码身份、用户和工作区映射。已完成。
3. 增加保存查询、结果导出和基础图表。已完成。
4. 增加逻辑表、跨文件/跨 Sheet 查询、单表缓存和任务绑定快照。已完成。
5. 增加导入前类型确认、多指标图表和工作区 AI SQL。已完成。
6. 编写只读旧 Python 库导入器，把可兼容的文件源和用户映射到新模型。
7. 再决定外部数据库、S3、Python 项目和完整报表的取舍，不做机械照搬。

## 7. 当前验证

- `cargo fmt --check`、`cargo test --locked` 和 `cargo clippy --locked --all-targets -- -D warnings` 通过。
- 23 个 Rust 单元测试通过，新增覆盖字段类型覆盖、日期推断、前导零文本、AI Chat 地址、对话追问与 SQL 提案拆分、历史裁剪、结果样本压缩、合法 JSON 上下文裁剪和认证加密防篡改。
- Vue TypeScript 与生产构建通过。
- 真实 HTTP 验证通过两份各 50 万行 CSV 上传、跨文件聚合查询、保存查询、多表后台任务、取消和计划绑定。
- 真实 HTTP 验证通过首次初始化、会话恢复、退出、5 次失败登录限流、匿名 401、Viewer 写入 403 和跨工作区资源 404。
- 保存查询创建、筛选、更新和删除已同时通过 API 与浏览器验证。
- 真实 Excel 验证通过多工作表发现、`B3` 起始单元格、结束单元格、表头和字段读取。
- 以 1000 亿行 `range` 聚合验证运行中任务可真正中断，中断后队列可立即继续处理任务。
- Docker Compose release 镜像构建、健康检查、SPA history fallback 和持久卷重建保留均验证通过。
- Chromium 1440x900 验证通过文件-Sheet 树、双表绑定、别名修改、跨文件结果、任务详情和多表任务表单；控制台 0 错误、0 警告。
- 分组柱状、堆叠柱状、饼图、散点图和雷达图通过真实查询切换；ECharts Canvas 均检测到非空像素。
- 导入预检通过 CSV 前导零样本验证，覆盖推断、类型改写、确认导入、查询保真和取消清理。
- AI 设置、密钥不回显、密文落库、连接测试、完整表上下文、多轮追问、SQL 独立预览、写回、执行与刷新恢复通过模拟 OpenAI-compatible HTTP 服务和 Chromium 验证。

ARM64 Docker Desktop 首次 bundled DuckDB release 构建实测约 24 分钟、约 4 GB Docker 虚拟机内存；启用 BuildKit 缓存后，Rust 业务代码重建约 22 秒。

## 8. 后续阶段

1. 增加成员管理、邀请、角色调整和多工作区切换。
2. 增加旧文件源导入器和迁移审计。
3. 为大 Excel 增加异步导入进度和 Parquet 路径，降低首次缓存构建内存。
4. 增加持久化报表、XLSX/PDF 导出、分享和快照刷新。
5. 拆分 Monaco、Element Plus 和路由模块，继续降低首次加载体积。
6. 根据真实使用决定外部数据库和 Python 运行时是否回迁。

跨文件与跨 Sheet 的数据模型、API、缓存生命周期和验收证据详见 [15 跨文件与跨 Sheet 分析实现](15-cross-file-sheet-analysis.md)。

导入预检、多指标图表与 AI SQL 的具体约束详见 [16 导入、图表与 AI SQL 实现](16-import-charts-ai.md)。
