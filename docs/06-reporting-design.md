# 06 报表功能设计

## 报表定位

AnyDatas 的报表不是传统 BI 的完整替代，而是脚本和定时任务的交付层。报表应回答三个问题:

1. 这份结果来自哪次运行。
2. 结果是否已经按计划刷新。
3. 谁可以看、订阅、导出和再次运行。

## 报表类型

| 类型 | 说明 | MVP |
| --- | --- | --- |
| Dashboard | 多图表、多指标卡、过滤器 | 是 |
| Report | 图表 + 表格 + Markdown 叙述，适合周报/月报 | 是 |
| Data App | 带输入参数和交互控件的应用 | P1/P2 |
| Scheduled Snapshot | 定时刷新后的只读快照 | 是 |
| Embedded Report | iframe/SDK 嵌入外部系统 | P1 |
| Export | PDF、PNG、CSV、XLSX | P1 |

## 报表组件

MVP 组件:

- 指标卡: 标题、数值、环比/同比、状态色、说明。
- 表格: 排序、分页、列格式、条件格式。
- 折线图: 趋势。
- 柱状图: 对比。
- 饼图/环图: 占比，限制分类数量。
- 散点图: 当前单机 MVP 已支持指定 X/Y 数值列，最多绘制 100 个点。
- Markdown 文本: 解释、结论、行动建议。
- 过滤器: 日期、枚举、多选、数字范围。

不要过早做复杂自由画布。MVP 使用栅格布局即可，让用户可靠发布结果。

当前单机 MVP 已实现持久化 `report_widget` 和 `report_filter` 列表。新报表默认创建行数、列数、柱状图和结果表组件；创建者、Owner 和 Admin 可新增或删除 metric、table、bar、line、scatter、pie、Markdown 组件，通过桌面拖放或可访问的上/下按钮调整持久化顺序，并选择 1/4、1/2、全宽，移动端自动折叠为单列。拖放提交完整且唯一的组件 ID 集合，服务端重新校验报表归属并记录排序审计。也可增加 select、contains、range 三种筛选器。筛选器直接作用于最近成功快照的内存结果，所有组件共享同一筛选后的行集；pie 会聚合同名分类、忽略非正值并将超出颜色上限的分类归到 Other。指标和图表可指定快照列名，未指定时会选择可用的数值列；scatter 使用两个数值列，最多绘制 100 个点。表格可将一个数值列按正负值或有限阈值上下界着色；Markdown 使用受限的安全渲染。项目工作台会列出当前用户可访问的关联报表和其最新快照状态，报表页会展示项目、运行版本、数据源和最近快照运行，便于回溯。

报表订阅始终保留按用户定向的站内通知。订阅者还可选择工作区中启用且接受报表事件的邮件、通用 Webhook、Slack 或 Teams 渠道；外部队列只使用本人选择，取消订阅、权限回收或渠道删除会级联清理关联。相同渠道、报表和运行继续使用唯一去重键，避免多人选择同一渠道时重复发送。

## 报表数据模型

| 实体 | 字段示例 |
| --- | --- |
| report | name、description、owner、workspace、visibility、published_version |
| report_page | report_id、title、order |
| report_widget | type、position、size、data_binding、style_config |
| report_filter | name、type、default_value、binding |
| report_snapshot | report_id、run_id、created_at、artifact_uri、status |
| report_subscription | report_id、recipient、schedule、channel、format |

## 数据绑定

每个 widget 绑定一个数据源:

- 运行产物中的结果表。
- SQL 查询。
- 预定义 metric。
- 静态 Markdown。

推荐 MVP 默认绑定运行产物，而不是每次打开报表实时查询。这样可以保证报表打开快、口径可追溯、定时刷新可审计。

当前 MVP 的导出直接读取最近一次成功快照，支持完整 CSV/JSON/XLSX 以及便于分享的表格型 PNG/PDF。导出不会触发新运行，沿用报表可见性和数据分类校验，并在文件成功生成后写入 `report.exported` 审计事件。XLSX 将文本写为非公式单元格，避免用户数据触发公式注入。为保护单机控制平面，PDF 最多呈现 500 行、PNG 最多呈现 100 行，超出时在文件中明确提示改用 CSV/JSON/XLSX 获取完整数据；Compose 镜像提供 CJK 字体，非容器部署可通过 `ANYDATAS_EXPORT_FONT_PATH` 指定 PNG 字体。

当前组件和筛选器都只读取该最近成功快照。编辑组件或筛选器不会触发项目运行，删除不会删除快照或运行记录；新增和删除分别写入 `report.widget_created` / `report.widget_deleted` 与 `report.filter_created` / `report.filter_deleted` 审计事件。

## 刷新策略

| 策略 | 说明 | 适用场景 |
| --- | --- | --- |
| Manual | 用户手动刷新 | 临时分析 |
| Scheduled | 跟随项目 schedule 刷新 | 日报、周报 |
| On open | 打开时报表触发查询或运行 | 小数据、低成本查询 |
| Cached | 使用最近成功快照 | 默认策略 |

当前 MVP 的 interval、cron、手动 schedule 运行以及成功 retry 会自动更新关联报表的快照。最终失败会记录失败快照和错误摘要，同时保留页面上的最近成功数据；普通手动项目运行仍需用户点击报表刷新，避免试验性运行意外影响已发布结果。

报表上应显示:

- 最近成功刷新时间。
- 对应 run id。
- 关联项目、运行版本和数据源。
- 下次计划刷新时间。
- 最近失败状态和错误摘要。

## 权限模型

报表权限:

- Owner: 管理报表和权限。
- Editor: 修改布局、组件和绑定。
- Viewer: 查看报表。
- Subscriber: 接收订阅，可与 Viewer 合并。

当前 MVP 已落地两档可见性:

- `workspace`: 工作区成员可查看，创建新报表时默认使用该模式。
- `private`: 创建者、Owner 和 Admin 默认可查看；创建者、Owner 或 Admin 可以向选定的非管理员工作区成员授予或撤销查看权限。未授权成员访问报表直链时返回 `404`。
- 创建者、Owner 和 Admin 可以在 `workspace` 与 `private` 间切换。私有授权记录保存在 `report_access_grants`，通知和审计 API 中引用私有报表的记录采用相同过滤规则，避免标题或详情从旁路泄露。
- 后续仍需扩展独立 Editor/Subscriber 角色、批量授权和外链 token 策略。

数据权限不能被报表绕过:

- 如果报表读取快照，用户必须有报表权限。
- 如果报表支持 drilldown 到源数据，用户还必须有对应数据权限。
- 对外分享必须使用独立 token、过期时间、访问域名限制和审计。

## 订阅和通知

订阅配置:

- 频率: 每日、每周、每月、自定义 cron。
- 渠道: 站内、邮件、Webhook，P1 接 Slack/Teams。
- 格式: 链接、PDF、PNG、CSV 附件。
- 条件: 成功后发送、失败时发送、指标超过阈值时发送。

失败通知应包含:

- 报表名称。
- 计划触发时间。
- 失败步骤。
- 错误摘要。
- 查看日志链接。

## 报表编辑体验

推荐布局:

- 左侧: 组件库。
- 中间: 报表画布。
- 右侧: 数据绑定、样式和权限设置。
- 顶部: 保存、预览、发布、刷新、订阅。

发布语义:

- 草稿可随时编辑。
- 发布后生成稳定版本。
- 定时任务更新发布版本的快照。
- 未发布草稿不影响生产报表。

## 与外部 BI 的关系

AnyDatas 可以在 P1/P2 提供:

- 将运行结果写入 ClickHouse/PostgreSQL，供 Metabase/Superset 查询。
- 嵌入 Superset/Metabase dashboard。
- 从报表导出 chart data 和 semantic model。

但核心报表层仍建议原生实现，因为它必须深度理解脚本版本、运行历史、快照和调度状态。
