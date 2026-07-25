# 12 完整开发计划

更新日期: 2026-07-11

## 1. 项目目标

AnyDatas 的目标是建设一个通用在线数据分析平台，让数据工作者在浏览器中完成:

1. 上传或连接数据源。
2. 编写 SQL/Python 分析脚本。
3. 手动或定时执行计算任务。
4. 查看运行历史、日志、产物和错误。
5. 将结果发布为报表、Dashboard 或订阅通知。
6. 在团队内进行权限控制、审计和治理。

产品定位不是传统 BI，也不是单纯 Notebook，而是 **面向团队的数据分析自动化平台**。核心差异是把“脚本化分析”生产化，让一次性分析变成可运行、可调度、可分享、可追溯的数据产品。

## 2. 当前 MVP 基线

当前仓库已经实现一个最小可运行版本:

| 模块 | 当前状态 | 文件 |
| --- | --- | --- |
| Web 工作台 | 已实现响应式侧边导航、概览指标与最近活动、模块化单页工作区、数据源/项目即时筛选、折叠式项目管理和按需挂载的本地 SQL/Python 脚本编辑器；详情、报表和身份页面统一使用同一设计系统，并提供本地中英文切换与跨页面语言持久化 | `templates/`、`static/styles.css`、`static/i18n.js`、`static/workspace.js`、`static/code-editor.js` |
| 用户/工作区 | 已实现显式 demo 模式、单机密码登录、PBKDF2 哈希、opaque 过期会话、默认关闭的自助注册、自助密码轮换、管理员签发的一次性密码重置、一次性可过期/撤销邀请、带 read/full scope 的个人 API token、独立 Viewer/Analyst Service Account、默认/个人工作区、成员管理和基础角色 | `app/auth.py`、`app/main.py`、`scripts/set_password.py` |
| 数据源 | 支持 CSV/XLSX/Parquet 上传与 S3/MinIO 受控快照导入/刷新、SQLite 表/视图、PostgreSQL schema/table、MySQL database/table 和 ClickHouse database/table 连接、字段预览、类型推断、字段治理、质量摘要、跨项目影响分析、四级分类，以及 `view`/`query`/`manage` 成员授权 | `app/main.py`、`app/data_source_access.py`、`app/lineage.py`、`app/s3_tools.py`、`app/s3_snapshots.py`、`app/postgres_tools.py`、`app/mysql_tools.py`、`app/clickhouse_tools.py`、`app/sql_tools.py`、`app/schema_tools.py`、`app/csv_tools.py`、`app/xlsx_tools.py`、`app/parquet_tools.py`、`app/sqlite_tools.py`、`app/quality_tools.py` |
| 项目管理 | 支持 SQL/Python 项目、JSON 参数、运维预置 Runtime Profile、保存不可变版本和发布运行版本 | `app/main.py`、`app/runtime_profiles.py` |
| 脚本运行 | 支持本地 subprocess runner 和单机 Docker Runner；Docker 执行容器采用非 root、只读根文件系统、网络隔离、CPU/内存/pids/tmpfs/超时限制；工作区运行槽原子领取并将满额任务保留在 queued 队列；PostgreSQL/MySQL/ClickHouse 仅使用显式受控网络，S3/MinIO 快照无凭据且无网络运行 | `app/runner.py`、`app/main.py`、`Dockerfile.runtime`、`docker-compose.yml` |
| SQL 查询 | 使用 DuckDB 查询上传或 S3/MinIO 快照导入的 CSV/Parquet，使用 SQLite、PostgreSQL、MySQL 或 ClickHouse 查询已连接表，统一表名为 `data`，并安全绑定 `$name` 参数 | `app/runner.py` |
| Python 脚本 | 支持 `load_csv()`、`load_data()`、`params` 和 `result` 输出约定；自定义依赖使用运维预置、版本固化的 Docker Runtime Profile | `app/runner.py`、`app/runtime_profiles.py` |
| 定时任务 | 支持 interval/cron schedule、时区计算、暂停恢复、手动触发、失败重试和后台扫描 | `app/main.py` |
| 运行历史 | 支持状态、触发方式、重试尝试、参数快照、耗时、错误、详情页、结果/日志分页、CSV/JSON 下载，以及工作区权限范围内的运行与日志检索 | `app/run_search.py`、`app/main.py`、`templates/run.html`、`templates/runs.html` |
| 报表 | 支持成功/失败快照、显式刷新和定时自动更新；可配置指标、表格、柱/折/饼图、基础 Markdown、快照筛选器、站内订阅，以及项目/版本/数据源/快照运行血缘展示 | `templates/report.html`、`templates/index.html`、`app/main.py`、`app/report_subscriptions.py`、`app/db.py`、`app/runner.py` |
| 审计 | 支持关键操作审计和工作台审计面板 | `app/db.py`、`templates/index.html` |
| 工作区配额 | 支持数据源、项目、定时任务、报表数量、运行并发和数据源存储配额，Owner/Admin 可配置 | `app/db.py`、`app/storage_usage.py`、`app/main.py`、`templates/index.html` |
| Secret 引用 | 支持外部部署环境变量引用、项目绑定/版本/运行快照、未绑定值隔离和日志/结果/错误脱敏 | `app/secret_tools.py`、`app/db.py`、`app/main.py`、`app/runner.py` |
| 通知 | 支持站内通知、SMTP 邮件、通用 HTTPS Webhook、Slack、Microsoft Teams、持久化投递队列、指数退避重试、去重与投递审计 | `app/notification_delivery.py`、`app/db.py`、`app/main.py`、`app/report_subscriptions.py` |
| 测试 | 有 smoke test、后端流程测试、前端渲染测试、登录/工作区/RBAC 测试，并完成桌面与 390px 移动端真实浏览器验收 | `tests/` |
| 部署 | 有本地运行、Dockerfile、Compose 健康检查、Prometheus `/metrics`、可选 Prometheus/Grafana 叠加、预置 dashboard、告警规则和 SQLite 数据卷备份/恢复脚本 | `README.md`、`Dockerfile`、`docker-compose.yml`、`docker-compose.monitoring.yml`、`monitoring/`、`scripts/backup.py`、`scripts/restore.py` |

当前仓库已经完成单机 MVP/试点版交付基线。功能、模块边界和质量门禁见 [13 实现验收清单](13-implementation-acceptance.md)；后续开发聚焦更大规模、多机高可用和企业身份治理，不再作为单机 MVP 完成条件。

## 3. 总体技术路线

### 3.1 MVP 到产品化的技术路线

| 阶段 | 默认部署 | 执行后端 | 数据存储 | 适用场景 |
| --- | --- | --- | --- | --- |
| 当前单机版 | Docker Compose 或本地 Python | DockerRunner，开发环境可用 LocalSubprocessRunner | SQLite + 本地托管文件，可接 S3/MinIO 快照 | 单台服务器试点 |
| Alpha | Docker Compose | DockerRunner | PostgreSQL + MinIO + Redis 可选演进 | 更高并发单机部署 |
| Beta | Docker Compose + 独立执行机可选 | DockerRunner + 多 Worker | PostgreSQL + MinIO + ClickHouse 可选 | 小团队生产 |
| GA | 单机优先，支持拆分服务 | 多 Runner、多队列 | PostgreSQL + MinIO/S3 + ClickHouse | 商业交付 |
| Enterprise | Kubernetes 可选 | Kubernetes Jobs + gVisor | 云托管或私有化 HA | 高隔离/高并发/大客户 |

### 3.2 推荐技术栈

| 层级 | 推荐 |
| --- | --- |
| 前端 | 当前保留 Jinja/FastAPI 页面，产品化阶段迁移到 Next.js + TypeScript + Monaco Editor |
| 后端 | FastAPI + Pydantic + SQLAlchemy/Alembic |
| 元数据 | PostgreSQL |
| 文件和产物 | MinIO，兼容 S3 |
| 队列和缓存 | Redis |
| 文件分析 | DuckDB |
| 结果和日志分析 | PostgreSQL 起步，ClickHouse 可选 |
| 执行隔离 | Docker Runner，后续 rootless Docker/gVisor/Kubernetes Jobs |
| 观测 | 结构化日志 + Prometheus + Grafana |
| 部署 | Docker Compose 起步，企业版 Helm 可选 |

## 4. 产品范围规划

### 4.1 用户角色

| 角色 | 权限和诉求 |
| --- | --- |
| Owner | 组织设置、成员、账单、安全策略、所有资源 |
| Admin | 数据源、配额、审计、任务管理 |
| Analyst | 上传数据、创建项目、运行脚本、发布报表 |
| Viewer | 查看授权报表和快照 |
| Service Account | API 调用、自动化集成 |

### 4.2 核心产品域

1. 工作区: 组织、成员、角色、项目、数据源、报表。
2. 数据源: 文件、数据库连接、对象存储、连接器。
3. 分析项目: SQL/Python 脚本、参数、版本、运行环境。
4. 执行运行: 手动运行、定时运行、日志、产物、资源限制。
5. 报表: 指标卡、表格、图表、Markdown、过滤器、快照、订阅。
6. 治理: RBAC、密钥、审计、配额、成本、安全策略。
7. 运维: 部署、备份、监控、告警、升级、故障恢复。

## 5. 分阶段开发路线

## Phase 0: 原型整理和技术债收口

周期: 1 到 2 周

目标: 把当前 MVP 从“能跑的原型”整理成可继续迭代的工程基线。

### 任务

- 整理 FastAPI 模块结构，拆分 route、service、repository、runner。
- 引入配置层，集中管理路径、runner 类型、超时、上传限制。
- 将 SQLite schema 管理抽象出来，为 PostgreSQL 迁移做准备。
- 完善 smoke test，覆盖 SQL、Python、报表、定时任务、失败任务。
- 增加基础错误页和表单校验。
- 明确当前 LocalSubprocessRunner 仅用于开发环境。

### 验收标准

- `python tests/smoke_test.py` 稳定通过。
- 失败脚本能被标记为 `failed` 并展示错误。
- 代码结构支持后续替换数据库和 runner。
- README 能让新开发者 15 分钟内启动项目。

## Phase 1: 单机 Alpha

周期: 3 到 5 周

目标: 形成可在单台服务器部署的 Alpha 版本，支持小团队真实试用。

### 产品任务

- 增加登录、用户、工作区和基础角色。
- 上传 CSV、XLSX、Parquet，展示 schema、预览和质量摘要。
- 项目支持草稿保存、版本保存、已发布版本。
- 手动运行必须绑定项目版本。
- 报表支持工作区内只读分享；当前 MVP 支持 `workspace` 可见性，以及 `private` 报表的指定成员授权。
- 运行历史展示日志、错误摘要、结果表和产物下载。

### 技术任务

- 从 SQLite 迁移到 PostgreSQL。
- 从本地文件迁移到 MinIO。
- 引入 Redis Queue。
- 当前 MVP Compose 已以 DockerRunner 为默认执行后端，开发环境仍可使用 LocalSubprocessRunner。
- Docker Runner 当前支持 CPU、内存、pids、tmpfs、超时、只读文件系统、非 root、禁 privileged 和无网络；后续补独立 worker/队列。
- 引入 Alembic 数据库迁移。
- Docker Compose 一键部署: web/api、postgres、redis、minio。

### 验收标准

- 单台 Linux 服务器可通过 Docker Compose 部署。
- 上传 CSV 后，SQL 和 Python 项目都能运行成功。
- 用户代码失败不会影响 API 服务。
- 所有运行都可追溯到项目版本、数据源、触发方式和产物。
- 运行容器默认不能访问控制平面数据库。

## Phase 2: Beta 核心生产能力

周期: 6 到 8 周

目标: 支持 5 到 10 个试点团队稳定使用。

### 产品任务

- 数据库连接: PostgreSQL、MySQL、ClickHouse 外部只读数据源当前已实现；ClickHouse 作为内部结果库后续实现。
- 数据源权限: 当前已支持 `view`、`query`、`manage`、私有共享、派生资源访问收敛、四级源分类、字段分类、非管理者导出脱敏和 Restricted 导出限制；后续补组织级、源数据库行列级和外链策略。
- SQL 编辑器增强: 语法高亮、格式化、运行前校验。
- Python 运行环境: 依赖声明、预置镜像、包缓存策略。
- 定时任务增强: 当前已实现重试次数、基础延迟、指数退避、最终失败站内通知、skip/queue_one/queue_all/cancel_previous 自动触发并发策略，以及按时间范围的 backfill；更复杂工作流后续实现。
- 并发策略: 当前支持工作区级运行上限，以及 skip、queue_one、queue_all、cancel_previous；queue_all 保留每个到期触发并按同一 schedule 串行释放，cancel_previous 会审计并终止旧 run，最新 run 同时等待同一 schedule 和工作区执行槽位释放。
- 通知增强: 当前已支持定向站内报表订阅、用户级邮件/通用 HTTPS Webhook/Slack/Teams 渠道偏好、持久化投递状态、重试、去重和失败投递重入队。
- 报表组件: 当前已支持快照绑定的指标卡、表格、柱状图、折线图、散点图、饼图、基础 Markdown、表格数值条件格式、select/contains/range 筛选器，以及桌面拖拽/按钮排序与 1/4、1/2、全宽配置式栅格布局。
- 报表快照: 当前已实现最近成功快照、刷新状态、失败提示，以及 interval/cron/手动 schedule 的自动更新。

### 技术任务

- Scheduler Service 独立化。
- Redis Queue 支持重试、死信和任务去重。
- Runner Worker 支持多进程或多实例。
- 引入审计日志表。
- 当前 MVP 已实现外部 Secret 引用，不回显明文；后续接入 KMS、动态凭证和轮换。
- 当前 MVP 已实现运行结果与日志分页，以及按关键词、状态、触发类型、项目和日期范围的工作区权限化检索；集中日志后端和长期留存后续实现。
- 当前单机版已提供 preview-first 运行保留脚本，清除过期终态运行 payload 与工作目录，保留运行元数据、审计、报表最新状态及最近成功快照；对象存储版本后续映射到生命周期策略。
- 增加 API 测试和端到端测试。
- 当前 MVP 已提供受部署侧 Bearer token/共享 token 文件保护的 Prometheus `/metrics`，覆盖控制平面、scheduler、运行、数据源和通知投递，并提供可选 Prometheus/Grafana 单机叠加和预置 dashboard；后续补集中日志。

### 验收标准

- 调度触发成功率大于 99%，用户代码错误单独统计。
- 单工作区并发可配置。
- 失败任务有明确错误、日志和重试记录。
- 报表打开默认读取最近成功快照，p95 小于 3 秒。
- 试点团队能自助完成文件到定时报表流程。

## Phase 3: 报表和协作产品化

周期: 6 到 8 周

目标: 让平台从“能跑脚本”升级为“能交付团队报表”。

### 产品任务

- 报表编辑器: 当前已支持配置式栅格布局、持久化拖拽/按钮排序和宽度控制。
- 组件库: 当前已支持指标卡、表格、折线图、柱状图、散点图、饼图、筛选器、Markdown 和表格数值条件格式。
- 报表权限: 当前 MVP 已实现私有、工作区可见和私有报表的指定成员授权；后续补 Editor/Subscriber 角色和外链 token。
- 报表订阅: 当前支持可访问报表的站内订阅和取消订阅，并由每位订阅者选择工作区允许的邮件、通用 Webhook、Slack 或 Teams 渠道投递刷新事件。
- 报表导出: 当前 MVP 已支持最近成功快照的完整 CSV/JSON/XLSX，以及带单机资源保护和截断提示的 PNG/PDF 下载；后续可拆分独立渲染服务。
- 参数化报表: 日期、枚举、多选、数字范围。
- 当前 MVP 已展示项目到可访问报表的血缘、报表的项目/运行版本/数据源/最近快照运行，以及数据源到草稿/发布版本、调度、可见报表和历史运行的影响分析；字段级血缘后续实现。

### 技术任务

- 定义 chart spec 和 widget spec。
- 结果表以 Parquet 存储，必要时同步到 ClickHouse。
- 报表快照存储到 MinIO。
- 引入前端应用框架: Next.js + TypeScript。
- 使用 Monaco Editor 替换 textarea。
- 使用 ECharts/Vega-Lite 实现图表。

### 验收标准

- 非作者用户可查看授权报表。
- 报表能显示最近刷新时间、run id、状态和失败原因。
- 报表组件绑定运行结果后可稳定刷新。
- 导出内容和页面内容一致。

## Phase 4: 安全、治理和运维

周期: 6 到 8 周

目标: 达到可商业试点和私有化交付的安全运维基线。

### 产品任务

- RBAC 完善。
- 管理员审计日志。
- 工作区资源配额。当前已实现数据源、项目、定时任务、报表数量、运行并发和数据源存储额度；后续增加工作区级 CPU 与内存预算。
- 运行成本和资源用量面板。当前已实现 Owner/Admin 可见的 24 小时、7 天和保留期运行量、状态、成功率、计算小时、平均耗时及可配置人民币成本估算；后续接入容器 CPU/内存实际计量和供应商账单。
- 密钥创建、轮换、删除。
- 数据分类标签: 当前已实现 Public、Internal、Confidential、Restricted 源级标签，以及 PII、财务、客户、敏感字段标签和 redact/partial/hash 导出脱敏；Restricted 派生导出要求 manage 权限。
- 管理员暂停高风险任务。

### 技术任务

- Docker 网络隔离和 egress allowlist。
- rootless Docker 或 gVisor 可选增强。
- 日志脱敏。
- 上传文件大小限制、类型校验、生命周期策略。
- PostgreSQL 备份和恢复脚本。
- MinIO bucket 备份策略。
- CI/CD: lint、unit test、migration check、smoke test、镜像扫描。
- 监控告警: API、Scheduler、Worker、PostgreSQL、Redis、MinIO、执行容器。

### 验收标准

- 用户不能访问未授权数据源、项目、报表和运行产物。
- 日志中不出现明文密钥。
- 超时、OOM、取消、失败状态可准确展示。
- 管理员可以审计谁在什么时间运行了什么代码、读取了什么数据源。
- 有标准备份恢复文档并完成恢复演练。

## Phase 5: GA 和商业化

周期: 8 到 12 周

目标: 具备对外销售、部署和持续运营能力。

### 产品任务

- SSO/OIDC。
- API token 和 Service Account。当前已支持只存摘要、可过期/撤销、实时继承成员角色且区分 read/full scope 的个人 token，以及可轮换凭证和整体停用的独立 Viewer/Analyst Service Account；后续补资源级 scope 和外部密钥托管。
- 嵌入式报表。
- 更多数据源: Snowflake、BigQuery、Redshift、Google Sheets。
- 数据连接器集成: dlt/Airbyte。
- 项目发布审批。
- 数据血缘和影响分析。
- 客户级使用量和套餐管理。

### 技术任务

- 多 Runner、多队列、独立执行机。
- ClickHouse 引入运行事件、大结果集和审计分析。
- MinIO 可替换云 S3。
- 企业部署包和升级脚本。
- SLA、SLO、错误预算和事故响应流程。
- 安全白皮书和合规材料。

### 验收标准

- 可支持首批付费客户。
- 单机部署和升级流程可复制。
- 关键路径有自动化测试。
- 具备标准运维、备份、恢复、监控和安全文档。

## Phase 6: 企业版和规模化

周期: 按客户需求启动

目标: 面向高并发、高隔离、大客户私有化或 SaaS 多租户场景。

### 产品任务

- SCIM 用户同步。
- 细粒度权限: 行级、列级、字段脱敏。
- 租户级 KMS。
- 审批流和合规报表。
- 多区域部署。

### 技术任务

- RunnerBackend 抽象切换到 KubernetesJobRunnerBackend。
- Temporal 管理高可用调度、补跑和复杂工作流。
- Kubernetes Jobs + gVisor 作为强隔离执行层。
- 租户独立 namespace、独立对象桶、独立执行节点。
- 高可用 PostgreSQL、Redis、MinIO/S3、ClickHouse。

### 验收标准

- 大客户可选择独立执行环境。
- 单租户故障不影响其他租户。
- 支持高可用和灾备部署。
- 支持更严格的安全审计和合规检查。

## 6. 详细模块开发计划

### 6.1 数据源模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | CSV 上传 | 当前已实现，支持上传大小限制、编码和异常处理 |
| P0 | schema 推断 | 当前已支持字段名、类型、描述、空值比例、样本与预览；超大文件异步推断后续实现 |
| P0 | PostgreSQL/MySQL 连接 | 当前已支持外部 Secret URL、schema/database + table 预检、read-only transaction、运行脱敏和 Docker 受控网络；MySQL 额外设置 `MAX_EXECUTION_TIME` 并拒绝可执行注释 |
| P1 | XLSX/Parquet | 文件上传扩展 |
| P1 | ClickHouse 外部源 | 当前已支持 Secret URL、database/table 预检、类型化参数、只读查询设置、运行脱敏和 Docker 受控网络 |
| P1 | S3/MinIO 文件快照 | 当前已支持 Secret JSON、bucket/key、CSV/XLSX/Parquet、VersionId/ETag 一致性、大小限制、显式刷新和无凭据 Runner |
| P1 | S3/ClickHouse 结果库 | 上传文件、运行产物、报表快照和内部大结果存储后续实现 |
| P2 | Snowflake/BigQuery/Google Sheets | 企业和云数仓场景 |
| P2 | Airbyte/dlt | 连接器生态 |

### 6.2 分析项目模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | SQL/Python 项目 | 当前已实现基础版 |
| P0 | 项目版本 | 运行绑定不可变版本 |
| P0 | 参数 | 已实现 JSON 参数对象；可表达日期字符串、枚举、文本、数字，并随版本和运行快照固化 |
| P1 | 脚本编辑器 | 当前已提供不依赖 CDN/Node 构建的 SQL/Python 语法高亮、语言切换、Tab 缩进、安全空白格式化和保存快捷键；复杂补全/诊断可在后续替换为 Monaco |
| P1 | 依赖管理 | 当前已支持运维白名单 Runtime Profile、不可变版本固化和 Docker 镜像选择；任意 requirements 与镜像构建服务后续实现 |
| P2 | Notebook 模式 | 多 cell、交互式运行 |
| P2 | Git 同步 | 审查、回滚、协作 |

### 6.3 执行运行模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | Docker Runner | 单机默认执行后端 |
| P0 | 运行状态 | 当前已支持 queued、running、succeeded、failed、canceling、canceled，并记录取消请求与最终状态 |
| P0 | 日志和错误摘要 | 当前已支持结果 100 行分页、日志 200 行分页、Secret 脱敏，以及按关键词、状态、触发类型、项目和日期范围的权限化集中检索；Loki/ClickHouse 日志库后续实现 |
| P0 | 资源限制 | CPU、内存、pids、超时、临时目录 |
| P1 | Redis Queue | 多 Worker 和重试 |
| P1 | 并发策略 | 当前已支持工作区运行上限、skip、queue_one、queue_all、cancel_previous；queue_all 保留每个到期触发并串行执行 |
| P1 | 取消运行 | 当前已支持 queued 即时取消、LocalSubprocessRunner 子进程终止和 Docker 命名容器强制清理；后续接入分布式 worker 取消 |
| P2 | 多执行机 | Runner 横向扩展 |

### 6.4 调度模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | interval schedule | 当前已实现 |
| P0 | cron schedule | 当前已实现基础版，支持五段表达式 |
| P0 | 时区 | 当前已实现基础版 |
| P1 | 重试策略 | 当前已实现次数、基础间隔和指数退避；后续接入队列死信与更复杂策略 |
| P1 | 补跑 backfill | 当前已支持指定时间范围、1 到 100 个 occurrence、逻辑触发时间参数快照和审计 |
| P1 | 失败通知 | 当前已实现站内、SMTP 邮件、通用 HTTPS Webhook、Slack 和 Teams，支持持久化重试与审计 |
| P2 | Temporal | 高可用和复杂工作流 |

### 6.5 报表模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | 最新成功快照 | 当前已实现基础版 |
| P0 | 表格和指标卡 | MVP 核心组件 |
| P0 | 基础图表 | 当前已支持折线图、柱状图、散点图、饼图和表格数值条件格式 |
| P1 | 报表编辑器 | 当前已支持持久化拖拽/按钮排序和 1/4、1/2、全宽配置式栅格布局 |
| P1 | 报表权限 | 私有、工作区、指定成员 |
| P1 | 订阅通知 | 当前已支持站内订阅及用户级工作区邮件/通用 Webhook/Slack/Teams 渠道偏好，权限回收和取消订阅会清理关联 |
| P1 | 导出 | 当前已支持 CSV、JSON、XLSX、PNG、PDF；后续可拆分独立渲染服务 |
| P2 | 嵌入式报表 | iframe/SDK |

### 6.6 权限与安全模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | 登录和工作区 | 用户、组织、成员 |
| P0 | RBAC | Owner、Admin、Analyst、Viewer |
| P0 | Secret 引用 | 当前已支持外部环境变量引用、版本/运行快照、未绑定值隔离和日志/结果/错误脱敏；后续接 KMS 与动态凭证 |
| P0 | 审计日志 | 数据源、脚本、运行、报表、权限 |
| P1 | Docker egress allowlist | 默认禁公网 |
| P1 | 日志脱敏 | token、密码、access key |
| P1 | 数据分类 | 当前已支持四级源标签、字段级 PII/财务/客户/敏感标签、非管理者 redact/partial/hash 导出脱敏；Restricted 导出限制为 source manage |
| P2 | SSO/OIDC/SCIM | 企业版 |

### 6.7 运维模块

| 优先级 | 功能 | 说明 |
| --- | --- | --- |
| P0 | Docker Compose | 当前已支持单机启动、`/healthz`/`/readyz` 健康检查和 SQLite 数据卷备份/恢复脚本 |
| P0 | 数据卷规划 | PostgreSQL、MinIO、Redis、运行产物 |
| P0 | 备份脚本 | 当前 MVP 已支持 SQLite 与本地数据卷备份/恢复；PostgreSQL 和 MinIO 后续实现 |
| P1 | Prometheus/Grafana | 当前已提供聚合指标、共享 token 文件、可选单机 Compose 叠加、30 天 Prometheus 留存、预置数据源与 dashboard |
| P1 | 告警 | 当前已提供 API、Scheduler、通知最终失败和积压的规则模板；Worker、存储、队列告警后续补齐 |
| P1 | 升级脚本 | 数据库迁移和镜像更新 |
| P2 | Helm Chart | 企业版 Kubernetes |

## 7. 里程碑计划

| 里程碑 | 周期 | 主要交付 |
| --- | --- | --- |
| M0 原型基线 | 已完成 | 当前 MVP、smoke test、本地服务 |
| M1 工程化 Alpha | 第 1 到 4 周 | PostgreSQL、MinIO、DockerRunner、登录、工作区 |
| M2 分析闭环 Beta | 第 5 到 10 周 | 数据库连接、版本化项目、调度增强、运行治理 |
| M3 报表协作 Beta | 第 11 到 16 周 | 报表编辑器、组件库、权限、订阅 |
| M4 安全运维 RC | 第 17 到 22 周 | 审计、Secret、配额、备份、监控、告警 |
| M5 GA | 第 23 到 30 周 | 部署包、文档、连接器扩展、商业试点 |

## 8. 团队配置

### MVP 到 Beta

- 产品经理 1 人。
- 设计师 1 人。
- 前端工程师 1 到 2 人。
- 后端/平台工程师 2 人。
- 测试开发或 QA 1 人，可兼职但需要明确负责人。

### GA 到企业版

- 数据平台工程师 1 人。
- DevOps/SRE 1 人。
- 安全工程师或顾问 1 人。
- 解决方案工程师 1 人。

## 9. 测试计划

### 自动化测试

| 类型 | 覆盖 |
| --- | --- |
| 单元测试 | schema 推断、权限判断、调度计算、runner 参数构造 |
| API 测试 | 数据源、项目、运行、调度、报表、权限 |
| 端到端测试 | 上传 CSV、创建 SQL/Python、运行、定时、报表 |
| 安全测试 | 越权访问、密钥泄露、网络隔离、超时/OOM |
| 部署测试 | Docker Compose 启动、数据卷、备份恢复 |

### 发布门禁

- 当前仓库已通过 GitHub Actions 在每次 push 和 pull request 上执行源码编译、完整 pytest、端到端 smoke 和 Docker Compose 配置校验。
- 所有 migration 可正向执行。
- smoke test 通过。
- 关键 API 测试通过。
- 手动验证文件到定时报表流程。
- 无 P0/P1 安全缺陷。
- 文档同步更新。

## 10. 运维计划

### 单机部署基线

- 反向代理: Caddy 或 Nginx。
- 应用服务: web/api、scheduler、worker。
- 依赖服务: PostgreSQL、Redis、MinIO。
- 可选服务: ClickHouse、Prometheus、Grafana。
- 数据目录: 独立磁盘或挂载卷。
- 备份: 每日 PostgreSQL + MinIO，至少每周恢复演练。

### SLO 初始目标

| 服务 | 指标 | 目标 |
| --- | --- | --- |
| API | 可用性 | 99.5% |
| API | p95 响应时间 | 小于 500ms，排除长任务 |
| 调度 | p95 触发延迟 | 小于 60 秒 |
| 报表 | 缓存报表 p95 打开时间 | 小于 3 秒 |
| 运行状态 | p95 状态更新延迟 | 小于 10 秒 |

## 11. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Docker 执行隔离不足 | 安全风险 | 默认禁公网、非 root、禁 privileged、资源限制，企业版上 gVisor/Kubernetes |
| 单机资源瓶颈 | 任务排队、成本失控 | 配额、并发限制、任务超时、独立执行机 |
| 报表范围膨胀 | 延期 | MVP 只做固定组件和栅格布局 |
| 数据源需求发散 | 维护成本高 | 文件 + PostgreSQL/MySQL/ClickHouse 优先，后续 dlt/Airbyte |
| 用户把产品当传统 BI | 定位模糊 | 强调脚本生产化、调度、快照、运行追溯 |
| 私有化部署复杂 | 交付慢 | 单机 Docker Compose 优先，Helm 延后 |

## 12. 当前优先级建议

接下来最建议按这个顺序推进:

1. 重构当前 MVP 工程结构，保证可持续迭代。
2. 替换 SQLite 为 PostgreSQL，引入 Alembic。
3. 替换本地文件为 MinIO。
4. 将 DockerRunner 作为单机部署默认 runner。
5. 增加登录、工作区、RBAC。
6. 实现项目版本和运行绑定版本。
7. 增强调度: 重试、补跑、并发策略、失败通知。
8. 增强报表: 指标卡、表格、图表、权限；当前已完成工作区/私有可见性、指定成员授权、站内订阅、可拖拽配置式栅格布局和 CSV/JSON/XLSX/PNG/PDF 快照导出，后续可拆分独立渲染服务。
9. 当前已完成外部 Secret 引用、资源配额、邮件、通用 HTTPS Webhook、Slack 和 Teams 通知；继续增加 KMS/动态凭证、密钥轮换和死信队列。
10. 做 Docker Compose 生产部署包和备份恢复文档。

## 13. 完成定义

当以下条件满足时，可认为 AnyDatas 进入 GA 可交付状态:

- 单台服务器可完成标准部署、升级、备份和恢复。当前 Compose 部署已配套一致性备份、安全恢复和带配置校验、升级前备份、镜像重建、服务重建及 readiness 门禁的标准升级脚本。
- 团队用户可完成文件/数据库到定时报表的完整流程。
- SQL/Python 脚本在隔离执行环境中运行，具备资源限制和超时。
- 每次运行可追溯到用户、代码版本、数据源、参数、日志和产物。
- 报表具备权限、快照、订阅和导出能力。
- 管理员可以查看审计、配额、运行成本和系统健康状态。当前工作台已提供审计、配额和运行成本估算，`/healthz`、`/readyz`、`/metrics` 提供系统健康与聚合指标。
- 关键流程有自动化测试和发布门禁。
- 安全、运维、用户使用文档齐备。

上述单机范围已完成实现和自动化验证；目标 Linux 服务器仍需按 [13 实现验收清单](13-implementation-acceptance.md) 执行容器启动、Docker Runner 和备份恢复发布演练。多机高可用、Kubernetes、SSO、Notebook 和内部对象存储不属于本轮单机 MVP 完成条件。
