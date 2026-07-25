# 03 技术选型

## 选型原则

1. MVP 要快，但不能牺牲用户代码执行安全。
2. 控制平面和执行平面分离，便于扩展、限流和故障隔离。
3. 优先使用成熟开源组件，避免早期自研调度、编辑器、对象存储和图表底层能力。
4. 数据存储分层: 元数据、原始文件、分析结果、日志和审计分开。
5. 默认支持单台服务器部署，同时保留 SaaS 和企业集群化演进路径。

## 推荐技术栈总览: 单机 MVP

| 层级 | 推荐技术 | 用途 | 理由 |
| --- | --- | --- | --- |
| 前端 | Next.js、TypeScript、TanStack Query、Monaco Editor、ECharts/Vega-Lite | Web 控制台、编辑器、报表 | React 生态成熟，编辑器和图表能力强 |
| 后端 API | FastAPI、SQLAlchemy、Alembic、Pydantic | 控制平面 API、鉴权、元数据服务 | Python 贴近数据分析生态，FastAPI 开发快 |
| 元数据库 | PostgreSQL | 组织、权限、项目、任务、运行、报表元数据 | 事务、JSON、RLS、生态成熟 |
| 对象存储 | MinIO，接口兼容 S3 | 上传文件、脚本包、运行产物、报表快照 | 单机可部署，后续可替换为云 S3 |
| 分析文件引擎 | DuckDB | CSV/Parquet/本地文件交互式分析 | 对文件和 Parquet 分析友好，MVP 成本低 |
| 结果和日志 | PostgreSQL + MinIO，ClickHouse 可选 | 运行事件、日志、结果快照 | 单机先少组件，数据量上来后再引入 ClickHouse |
| 调度 | DB-backed Scheduler Service + Redis Queue | 定时计划、重试、状态、补跑、任务生命周期 | 比 Kubernetes/Temporal 更轻，适合单机 MVP |
| 执行平台 | Docker Engine + Runner Worker | 每次运行一个短生命周期容器 | 单台服务器易部署，仍能做 CPU/内存/网络限制 |
| 沙箱 | Docker resource limits + rootless Docker/gVisor 可选 | 降低用户代码逃逸和资源滥用风险 | MVP 足够轻，企业版可增强隔离 |
| 缓存和队列 | Redis | 短期缓存、限流、会话、轻量队列 | 简单稳定，后续可替换 |
| 观测 | 结构化日志、Prometheus、Grafana | metrics、logs、基础告警 | 单机先降低运维面，后续再接 Loki/OTel |
| 连接器 | dlt、Airbyte | 数据源接入扩展 | 避免自研所有 connector |

## 架构取舍

### 为什么不用 Airflow 作为核心调度器

Airflow 很适合数据工程 DAG 和批处理编排，但 AnyDatas 的核心对象是用户创建的动态脚本、报表刷新和多租户运行。直接把每个用户任务映射成 Airflow DAG 会带来动态 DAG 管理、权限隔离、用户体验和平台抽象泄漏问题。

建议:

- MVP 使用独立 Scheduler Service 管理计划、重试、状态和补跑，任务进入 Redis Queue。
- Runner Worker 从队列取任务，并通过 Docker Engine 创建一次性运行容器。
- 如果后续需要高可用调度、分布式执行、复杂依赖或大规模 SaaS，再迁移到 Temporal、Dagster 或 Airflow。

### 为什么 MVP 不默认用 Kubernetes Jobs

Kubernetes Jobs 的隔离、资源限制和扩缩容能力很好，但会引入集群、网络策略、Ingress、存储、镜像仓库、监控和升级等运维负担。你的目标是单台服务器即可部署，因此 Kubernetes 不适合作为 MVP 默认依赖。

建议:

- MVP 使用 Docker Compose 部署所有平台服务。
- 用户脚本由 Runner Worker 启动独立 Docker 容器执行。
- 执行容器使用 CPU、内存、pids、只读文件系统、非 root、网络隔离和超时限制。
- 当并发运行数、租户数量或安全要求超过单机边界后，再将 Runner 抽象替换为 Kubernetes Jobs。

### 为什么不用 JupyterHub 作为核心

JupyterHub 适合多人 Notebook 环境，但 AnyDatas 的核心是从脚本到定时任务再到报表的生产闭环。直接基于 JupyterHub 容易让产品形态被 Notebook 会话绑定，难以管理报表快照、任务版本、调度和权限。

建议:

- MVP 使用 Web 代码编辑器 + 脚本运行模型。
- P2 再加入 Notebook 多 cell 和实时协作。

### 为什么报表不直接全量采用 Superset 或 Metabase

Metabase 和 Superset 都有成熟 BI 能力。它们适合嵌入或作为可选高级 BI，但 AnyDatas 的报表需要紧密绑定“脚本运行产物、版本、参数、快照和调度”。完全外包给 BI 工具会让核心工作流割裂。

建议:

- MVP 原生实现轻量报表: 指标卡、图表、表格、过滤器、订阅。
- 对企业客户提供 Superset/Metabase 嵌入或数据导出集成。

## 关键组件说明

### Next.js + Monaco + ECharts

Next.js 用于构建 Web 控制台，Monaco Editor 提供 SQL/Python 编辑体验，ECharts 或 Vega-Lite 用于报表图表。ECharts 图表类型丰富，Vega-Lite 的声明式规格适合将脚本输出转成可版本化图表配置。

### FastAPI + PostgreSQL

FastAPI 负责控制平面 API。PostgreSQL 存放租户、权限、项目、数据源、任务、运行和报表元数据。PostgreSQL Row Level Security 可用于增强多租户隔离，但应用层仍要做明确权限校验。

### S3/MinIO + DuckDB

上传文件进入对象存储，平台生成元数据和预览。DuckDB 在执行容器内读取 CSV/Parquet 或对象存储路径，适合早期低成本交互式分析。随着数据规模提升，可把结果落到 ClickHouse 或外部仓库。

### Scheduler + Docker Runner

Scheduler Service 负责读取数据库中的计划、生成运行记录、处理重试和补跑，并把运行任务写入 Redis Queue。Runner Worker 负责创建短生命周期 Docker 容器执行用户代码。这样 API 服务不会直接运行用户脚本，用户代码崩溃也不会拖垮控制平面。

### Docker 沙箱

用户代码默认不可信。MVP 使用 Docker 容器边界和资源限制: 非 root、禁 privileged、`--cap-drop=ALL`、`--security-opt no-new-privileges`、只读 root filesystem、CPU/内存/pids/临时目录限制、默认隔离网络。对更高安全等级的部署，可启用 rootless Docker、gVisor `runsc` 或迁移到 Kubernetes + gVisor。

## 版本路线

| 阶段 | 计算/执行 | 数据 | 报表 | 治理 |
| --- | --- | --- | --- | --- |
| MVP | Docker Runner + Python/SQL 镜像 | 文件、SQLite、PostgreSQL、MySQL 和 ClickHouse 只读表连接 | 原生轻量报表、站内/邮件/Webhook 通知、Prometheus 文本指标 | RBAC、基础审计、配额、外部 Secret 引用 |
| Beta | 当前已支持运维预置、版本固化的 Python Runtime Profile；后续补镜像缓存和 Slack/Teams | ClickHouse、S3 快照已实现，后续 Snowflake/BigQuery | 订阅、导出、快照 | 密钥管理、成本面板 |
| GA | 多 Runner、多队列、专用执行机 | Airbyte/dlt 连接器 | 嵌入、白标、参数化报告 | SSO、行列权限、合规日志 |
| Enterprise | Temporal + Kubernetes Jobs + gVisor | 数据血缘、语义层 | 审批发布、审计报表 | SCIM、KMS、区域隔离 |

## 不建议早期引入

- Spark: 对 MVP 过重，除非目标用户一开始就是 TB/PB 级数据。
- 自研 Notebook 内核管理: 复杂度高，先用脚本 Job 模型。
- 自研连接器生态: 维护成本大，优先接 dlt/Airbyte。
- 复杂低代码画布: 会稀释核心差异，先做好脚本到报表。
- 单一 BI 工具深度绑定: 会限制产品路线。
