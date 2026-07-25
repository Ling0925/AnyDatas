# 07 安全治理

## 安全目标

AnyDatas 同时处理敏感数据和用户代码，安全目标必须从第一版开始内置:

- 防止跨租户数据访问。
- 防止用户代码逃逸执行环境。
- 防止密钥泄露。
- 防止未授权数据外发。
- 保证关键操作可审计。
- 支持企业客户逐步合规。

## 威胁模型

| 威胁 | 场景 | 防护 |
| --- | --- | --- |
| 跨租户访问 | 用户通过 API 或脚本读取其他工作区数据 | 应用层权限、PostgreSQL RLS、对象存储路径隔离、短期凭证 |
| 容器逃逸 | 恶意 Python 代码尝试访问宿主机 | Docker 资源限制、非 root、禁 privileged、只读 rootfs、rootless Docker/gVisor 可选 |
| 数据外泄 | 脚本把数据发到外部 API | 文件/SQLite 默认禁公网 egress；PostgreSQL 仅加入运维显式批准的数据库网络、审计 |
| 密钥泄露 | 用户打印环境变量或读取连接密码 | secret 引用、最小权限、日志脱敏、不回显 |
| 资源滥用 | 无限循环、挖矿、超大查询 | CPU/内存/时长限制、并发配额、成本面板 |
| 报表越权 | 外链被转发或权限变化后仍可访问 | token 过期、访问审计、权限实时校验 |

## 身份认证

MVP:

- 当前单机版支持显式 `demo` 与 `password` 两种模式，未知模式启动失败而不回退。`password` 模式使用 PBKDF2-HMAC-SHA256 随机盐哈希、数据库仅存 SHA-256 会话 token 摘要、会话过期时间、HttpOnly/SameSite Cookie，并忽略可伪造的旧用户 id Cookie。同一邮箱/客户端哈希键在 15 分钟内失败 5 次后锁定 15 分钟，记录不含尝试密码或原始邮箱键。
- 当前 password 模式由 Owner/Admin 创建一次性邀请；原始 token 只在创建响应显示，数据库仅存 SHA-256 摘要，默认 7 天过期、接受后不可重放、可提前撤销，现有账号连续 5 次密码错误会自动撤销邀请。新用户在接受时设置密码，现有用户用当前密码确认。运维 CLI 仍可用于密码恢复并撤销旧会话。
- 自助注册默认关闭，仅 `ANYDATAS_ALLOW_SIGNUP=1` 时开放；注册创建独立工作区和 Owner 角色，复用密码、opaque 会话和审计策略。当前不验证邮箱所有权，公网部署应保持邀请制或由外部访问层限制入口。
- Owner 可签发任意人工成员的一次性密码重置链接，Admin 仅可签发 Analyst/Viewer；数据库只存摘要和到期时间，新链接撤销旧链接，成功后不可重放并撤销目标全部会话及个人 API token。该流程不自动发邮件，锁定在外的 Owner 仍使用运维 CLI。
- 已登录用户可验证当前密码后自助轮换密码；平台撤销该用户全部旧会话、签发一个新会话并记录不含密码材料的审计事件。
- 已登录用户可创建 1 到 365 天有效的个人 API token；原值只显示一次，数据库仅存 SHA-256 摘要、scope、到期、撤销和最近使用时间。`read` scope 默认只允许 GET/HEAD/OPTIONS，`full` 才允许写请求；两者每次 Bearer 请求都实时联查成员角色，角色降级或成员移除立即生效，且 token 不能创建或撤销 token。
- Owner/Admin 可创建没有密码、不可网页登录的独立 Viewer/Analyst Service Account，并签发或轮换同策略 token。机器身份独立承担资源和审计归属；停用时原子撤销全部 token 并移除 membership。
- magic link 和 MFA 预留。

P1/企业版:

- OIDC/SAML SSO。
- SCIM 用户同步。
- API token。
- 服务账号。

## 权限模型

角色建议:

| 角色 | 权限 |
| --- | --- |
| Owner | 组织设置、账单、安全、成员、所有资源 |
| Admin | 成员、数据源、配额、审计、任务管理 |
| Analyst | 创建项目、运行脚本、创建报表 |
| Viewer | 查看授权报表 |
| Service Account | API 访问和自动化任务 |

权限对象:

- workspace。
- data_source。
- project。
- schedule。
- report。
- secret。
- run_artifact。

### 当前报表授权边界

MVP 的 `private` 报表默认允许创建者以及 Owner/Admin 查看，创建者或 Owner/Admin 可以为选定成员授予或撤销查看权限；刷新仍要求使用者具有 Analyst 或更高角色。工作区成员访问未获授权的私有报表页面会得到 `404`，而不是可枚举的权限错误。首页、通知 API、审计 API、导出接口和通知已读接口均会对引用报表实时执行同一可见性判断。外链 token、批量授权和订阅收件人级授权仍属于后续阶段。

## 数据治理

### 数据分类

至少支持:

- Public。
- Internal。
- Confidential。
- Restricted。

当前单机 MVP 已将 `public`、`internal`、`confidential`、`restricted` 保存为数据源分类（默认 `internal`），并在工作台和详情页展示。具备 `query` 权限的成员仍可分析 `restricted` 数据，但其派生运行结果和报表快照导出必须具备数据源 `manage` 权限；分类变更和允许的导出都会审计。字段级已支持 PII、财务、客户、敏感标签，以及对非管理者导出的 redact、partial、hash 脱敏策略。

### 行列级控制

MVP 先做资源级权限。P1/P2 支持:

- 行级过滤策略。
- 列级隐藏或脱敏。
- 报表 drilldown 权限。
- 数据源继承策略。

PostgreSQL RLS 可用于内部元数据隔离；外部数据源的行列权限优先依赖源数据库账号或查询代理。

## 密钥管理

原则:

- 业务数据库不存明文密钥。
- 前端永不读取密钥明文。
- 执行环境只拿到本次运行需要的短期凭证。
- 日志脱敏。
- 密钥轮换有审计。

实现:

- 当前 MVP 的 `secret_references` 只保存工作区、引用名、部署环境变量名和说明；不提供录入或回显明文的接口。
- Owner/Admin 可以创建和删除未绑定的引用，Analyst 只能将已有引用绑定到项目的 `ANYDATAS_USER_SECRET_*` 运行时变量。
- 绑定和解绑会产生新项目版本；项目版本、运行、调度重试仅保存引用 id 与目标变量名快照。
- 引用仍被项目绑定、未结束运行或已发布项目版本使用时不可删除；解绑后必须发布新版本，避免已发布运行配置被静默破坏。
- Runner 启动前移除所有继承的 `ANYDATAS_SECRET_*` / `ANYDATAS_USER_SECRET_*` 值，再只注入本次运行需要的值；日志、结果和错误摘要保存前脱敏。
- 单机 Compose 可用 `docker-compose.secrets.example.yml` 作为 `env_file` 覆盖层，将权限为 `0600` 的 `.env.secrets` 仅挂入控制平面环境。Docker host 是受信任边界，具备 Docker 管理权限的运维者仍可能查看容器环境。
- PostgreSQL、MySQL 和 ClickHouse 数据源的完整连接 URL 都只能来自 Secret Reference；数据源元数据保存引用 id、schema/database、table 和生成的运行时变量名。SQL 只允许单条只读查询；PostgreSQL/MySQL 使用 read-only transaction，MySQL 额外拒绝可执行注释并设置 `MAX_EXECUTION_TIME`，ClickHouse 使用 `readonly=1`、查询超时和 500 行结果上限。Docker Runner 必须显式设置 `ANYDATAS_DOCKER_DATABASE_NETWORK` 才会放行这些来源。
- S3/MinIO 凭据 JSON 只从 Secret Reference 解析。控制平面以最小只读身份按 bucket/key 下载受大小限制且绑定 VersionId/ETag 的快照；数据库和审计只保存引用 id 与非密钥溯源元数据。刷新成功前不替换旧快照。Runner 只读挂载本地副本，既不接收凭据也不开放对象存储网络。
- 数据源记录创建者、`workspace`/`private` 可见性和分类，并使用 `data_source_access_grants` 保存 `view`/`query`/`manage` 授权。Owner/Admin 与创建者天然管理；私有源的直接详情、项目、运行、结果下载、报表、通知和审计都必须复核数据源权限，不能仅依赖前端隐藏。`restricted` 的结果导出额外要求 `manage` 权限。权限降级或撤销会清理失效的报表订阅与定向通知，报表投递前再次校验订阅者权限。
- 通用 Webhook、Slack 和 Microsoft Teams 渠道保存 Secret Reference id 而不是 URL，默认只接受 HTTPS；发送时临时解析 URL，投递记录和审计只保存渠道、事件、状态和脱敏错误。SMTP 主机、发件人和可选凭据只存在部署环境，错误处理会脱敏 SMTP 密码。Owner/Admin 才能管理外部投递渠道。
- `/metrics` 只输出平台聚合数和固定维度，不输出工作区 id、用户邮箱、资源名称、脚本、结果或 Secret。生产部署应通过 `ANYDATAS_METRICS_TOKEN` 启用 Bearer 鉴权，并在反向代理或私有监控网络中限制访问；该 token 以及所有 `ANYDATAS_SMTP_*` 配置都会在 Runner 启动前从用户代码环境中剔除。
- SaaS 或更高隔离要求应改接云 KMS/Secret Manager、短期凭证和独立执行环境；Compose 原生 secrets 文件挂载和动态凭证轮换仍是后续增强。

## 审计事件

必须审计:

- 登录、登出、失败登录。
- 成员邀请、角色修改。
- 数据源创建、修改、删除、测试连接。
- 密钥创建、轮换、删除。
- 脚本保存、发布、运行、取消。
- schedule 创建、暂停、恢复、删除。
- 报表发布、权限变更、外链创建。
- 管理员查看审计和导出数据。
- 工作区资源配额修改。

审计事件应包含:

- actor。
- action。
- resource type/id。
- organization/workspace。
- timestamp。
- IP/user agent。
- request id。
- before/after 摘要，敏感字段脱敏。

## 合规路线

MVP 不承诺完整认证，但按未来 SOC 2 / ISO 27001 准备:

- 访问控制。
- 变更管理。
- 审计日志。
- 备份恢复。
- 漏洞管理。
- 事件响应流程。
- 数据删除和导出流程。
- 供应商和开源依赖清单。

如果面向美国和欧洲客户，需要准备 GDPR/CCPA 相关的数据删除、导出、DPA 和子处理方清单。

## 安全验收清单

- 用户 A 不能通过 API 查询用户 B 工作区资源。
- 用户脚本不能访问控制平面数据库地址。
- 用户脚本不能读取未授权对象存储路径。
- 用户脚本默认不能访问公网。
- 未绑定的 `ANYDATAS_SECRET_*` 不会出现在用户脚本环境中，已绑定值不出现在运行日志、结果、错误、审计详情或业务数据库。
- PostgreSQL、MySQL 和 ClickHouse 的连接 URL 不出现在数据源记录、项目版本、运行记录、审计详情或浏览器页面；使用者仍必须依赖数据库侧最小权限账号和网络防火墙。
- 运行日志中不出现数据库密码、token、access key。
- 超时和 OOM 能被正确标记，并清理资源。
- 报表权限变化后旧链接立即失效或按策略过期。
- 所有高风险管理操作进入审计日志。
