# 09 MVP Backlog

## Epic 1: 账户、组织和工作区

用户故事:

- 作为用户，我可以登录。当前单机版已支持运维设置密码、PBKDF2 哈希和不落库原始 token 的过期会话；自助注册可由部署开关启用，并支持管理员签发一次性密码重置链接。
- 作为 Owner，我可以创建组织和工作区。
- 作为 Admin，我可以邀请成员并设置角色。
- 作为成员，我只能看到自己有权限的工作区。
- 作为自动化调用方，我可以创建默认只读或显式全权限的个人 API token，且 scope 不会绕过成员角色。
- 作为管理员，我可以创建独立 Viewer/Analyst Service Account、轮换凭证并立即停用全部访问。

验收:

- API 查询均带租户过滤。
- 至少有 Owner、Admin、Analyst、Viewer 四类角色。
- 个人 API token 原值只显示一次；`read` scope 不能执行写请求，`full` scope 仍受实时 RBAC 约束。当前 MVP 已实现。
- 自助注册默认关闭；启用后每个新账号创建独立工作区并成为 Owner，重复邮箱、弱密码和确认不一致会被拒绝。当前 MVP 已实现。
- Service Account 没有登录密码，token 原值只显示一次；停用撤销全部 token 并移除 membership，资源和审计归属保留。当前 MVP 已实现。
- 密码重置链接只存摘要、短时过期、新链接撤销旧链接且不可重放；成功后撤销目标全部会话和个人 API token。当前 MVP 已实现。
- 当前 password 模式已支持 Owner/Admin 创建默认 7 天过期、只存 token 摘要、可撤销且不可重放的邀请链接；demo 模式保留直接添加成员。

## Epic 2: 文件数据源

用户故事:

- 作为分析师，我可以上传 CSV、XLSX、Parquet。
- 作为分析师，我可以预览前 100 行。
- 作为分析师，我可以修改字段类型和字段描述。当前 MVP 已实现字段类型、描述、样本和质量摘要的 Schema 页面。
- 作为分析师，我可以在脚本中引用上传数据。
- 作为数据源管理者，我可以把数据源设为工作区可见或私有，并向成员授予查看、查询或管理权限；也可以标记 Public、Internal、Confidential 或 Restricted。当前 MVP 已实现。

验收:

- 500MB 以下文件可上传，当前本地存储基础版已实现大小限制。
- 当前基础版在接入时同步推断；大文件的异步推断后续实现。
- 上传失败和推断失败有明确错误。
- 私有数据源不会通过项目、运行、报表、通知或审计旁路泄露；`restricted` 数据源的运行 CSV/JSON 和报表 CSV/JSON/XLSX/PNG/PDF 导出还要求 `manage` 权限。当前 MVP 已实现。

## Epic 3: 数据库连接

用户故事:

- 作为 Owner/Admin，我可以登记 PostgreSQL 连接 URL 的外部 Secret 引用。
- 作为 Analyst，我可以选择已有引用、测试 PostgreSQL schema/table，并在授权后查询数据表。当前 MVP 已实现。
- 作为 Analyst，我可以接入 MySQL。当前 MVP 已实现。

验收:

- PostgreSQL/MySQL/ClickHouse URL 不回显、不落库；当前 MVP 已实现。
- PostgreSQL/MySQL 运行使用 read-only transaction，ClickHouse 使用 `readonly=1`、查询超时与 500 行结果上限；Docker Runner 仅允许运维显式配置的数据库网络，MySQL 还会设置 `MAX_EXECUTION_TIME` 并拒绝可执行注释。当前 MVP 已实现。
- S3/MinIO Secret JSON 不回显、不落库，CSV/XLSX/Parquet 对象以 VersionId/ETag 约束和双重大小限制导入本地快照；刷新失败保留旧快照，Runner 不接收凭据并保持无网络。当前 MVP 已实现。
- 连接测试和查询日志进入审计；当前 MVP 已记录数据源创建、引用和运行密钥解析，细粒度查询审计后续增强。

## Epic 4: 分析项目和脚本编辑

用户故事:

- 作为分析师，我可以创建 SQL 或 Python 项目。
- 作为分析师，我可以保存草稿和版本。
- 作为分析师，我可以设置参数。
- 作为分析师，我可以查看输出表和图表数据。

验收:

- 编辑器支持 SQL/Python 语法高亮、语言切换、Tab 缩进、安全空白格式化和 `Cmd+S`/`Ctrl+S` 保存；当前已使用仓库内静态资源实现，不依赖外网 CDN。
- 保存版本后生成不可变 version id。
- 运行必须绑定某个 version id。
- 项目版本和运行记录都保存参数 JSON 快照；SQL 使用 `$name`，Python 使用 `params["name"]`。

## Epic 5: 执行运行

用户故事:

- 作为分析师，我可以手动运行项目。
- 作为分析师，我可以取消运行。当前 MVP 已支持 queued run 立即取消，并可终止 LocalSubprocessRunner 或 DockerRunner 的 active run。
- 作为分析师，我可以查看实时或近实时日志。
- 作为分析师，我可以查看运行状态、耗时和产物。

验收:

- 每次运行创建独立 Docker 运行容器。
- 当前 MVP 已支持 queued、running、succeeded、failed、canceling、canceled 状态；超时、失败、取消和成功状态可追溯。
- 运行详情当前按 100 行分页显示结果、按 200 行分页显示日志，完整授权结果仍可通过 CSV/JSON 下载。工作区运行检索支持按日志/错误关键词、状态、触发类型、项目和日期范围筛选，并提供权限一致的分页 API；列表只返回匹配摘要，不返回完整日志、结果和 Secret 绑定快照。
- 用户代码异常不会导致 API 服务异常。

## Epic 6: 定时任务

用户故事:

- 作为分析师，我可以设置 cron 或 interval。
- 作为分析师，我可以暂停和恢复计划。
- 作为分析师，我可以设置重试和超时。
- 作为 Admin，我可以查看所有计划和运行情况。

验收:

- 支持时区。
- 当前已支持 skip、queue_one、queue_all 和 cancel_previous 四种自动触发并发策略；queue_all 保留每个到期触发并按同一 schedule 串行执行。backfill 可按 interval/cron 的指定时间范围队列化执行，单次最多 100 个 run。
- 调度触发记录可追溯。
- 当前 MVP 已支持可配置重试次数、基础延迟、指数退避和最终失败通知。

## Epic 7: 报表

用户故事:

- 作为分析师，我可以创建报表页面。
- 作为分析师，我可以添加指标卡、表格、折线图、柱状图、散点图、饼图和 Markdown。当前 MVP 已支持这些固定组件的新增与删除。
- 作为分析师，我可以把 widget 绑定到运行结果。
- 作为分析师，我可以发布报表给工作区成员。

验收:

- 报表打开默认读取最近成功快照。
- interval、cron 和手动 schedule 成功后自动更新关联报表快照；最终失败保留最近成功快照并展示失败状态。
- 报表显示最近刷新时间、run id、关联项目、运行版本和数据源；项目工作台显示当前用户可访问的关联报表及其最近快照状态。
- Viewer 无法修改报表。
- 当前组件和持久化筛选器读取最近成功快照，支持 select、文本包含、数值范围过滤、饼图的分类聚合、最多 100 点的散点图、表格数值条件格式，以及可审计的组件新增、删除、桌面拖拽/按钮排序和 1/4、1/2、全宽配置式栅格布局。

## Epic 8: 通知和订阅

用户故事:

- 作为分析师，我可以在任务失败时收到站内通知。当前已实现基础版。
- 作为分析师，我可以订阅或取消订阅可访问报表的刷新成功/失败站内通知，并选择工作区允许的邮件、通用 Webhook、Slack 或 Teams 外部渠道。当前 MVP 已实现。
- 作为 Admin，我可以配置邮件、通用 Webhook、Slack 或 Microsoft Teams 投递渠道。当前 MVP 已实现，所有 Webhook URL 通过外部 Secret Reference 提供。

验收:

- 失败通知包含错误摘要和日志链接。
- 通知记录可查询。当前已实现站内通知、按用户定向且带外部渠道偏好的报表订阅、邮件/通用 HTTPS Webhook/Slack/Teams 队列、指数退避重试、去重、投递审计和失败投递的管理员重入队。

## Epic 9: 安全和审计

用户故事:

- 作为 Admin，我可以查看审计日志。
- 作为 Admin，我可以配置工作区运行配额。
- 作为 Owner/Admin，我可以登记不含明文的外部 Secret 引用；作为 Analyst，我可以将已有引用绑定到项目运行变量。
- 作为平台，我会阻止未授权数据源访问。

验收:

- 关键操作写 audit_event。
- 已绑定密钥的版本和运行只保存引用快照，未绑定密钥不会进入用户脚本环境，日志、结果和错误不会保存已解析的值。
- 运行容器使用最小权限凭证。
- 默认禁止公网 egress。

## Epic 10: 运维基础

用户故事:

- 作为工程师，我可以部署开发、测试和生产环境。
- 作为工程师，我可以查看 API、Scheduler、Worker、运行容器的日志和指标。
- 作为工程师，我可以备份和恢复 PostgreSQL。

验收:

- CI 至少运行 lint、unit test、migration check。
- 当前 MVP 已提供 `/healthz`、`/readyz`、Prometheus 格式 `/metrics` 和 SQLite 数据卷备份/恢复脚本；指标支持部署侧 Bearer token。
- 仓库提供 API、scheduler、失败通知投递和队列积压的基础告警规则模板，以及可选单机 Prometheus/Grafana 叠加、预置数据源和 dashboard。当前 MVP 已实现。

## MVP Definition of Done

- 文件上传到定时报表的端到端流程可用。
- 至少支持 SQL 和 Python 两种项目类型。
- 每次运行可追溯到代码版本、数据源、触发方式和产物。
- 用户代码在隔离 Docker 运行容器中执行，有资源限制和超时。
- 报表支持工作区内分享。
- 基础 RBAC、审计、日志和通知可用。
- 至少完成 20 个端到端自动化测试场景。
