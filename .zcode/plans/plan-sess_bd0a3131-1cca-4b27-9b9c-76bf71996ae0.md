# 前端重构方案：完整可用 + 简单大气

## 现状结论

分支 `grok/rebuild-web` 已有一轮工作区重设计（侧栏 SPA、中英文、概览台、懒加载编辑器），功能面与验收文档基本齐备。  
真正卡住「完整可用 + 简单大气」的是结构与设计系统债务，而不是缺后端能力。

### 必须修的结构问题

- **Runs / Schedules / Reports 共用一个无 id 的 `section.band`**（`templates/index.html` 约 1191–1355 行）。  
  侧栏 hash 是 `#runs` / `#schedules` / `#reports`，但 `workspace.js` 按「最近的 `section.band`」切换，三者永远落在同一屏，无法独立导航。
- **空状态不完整**：runs、schedules、projects 列表、members、audit 等空数据时只有空表体或无提示。
- **样式双轨**：`static/styles.css` 先有蓝色 `:root`，后有青绿 redesign 覆盖；大量选择器重复，`--ink` 未定义，细节页/认证页与工作区气质不完全统一。

### 测试硬约束（不可随意改）

`tests/test_frontend_rendering.py` / `tests/test_auth_workspace.py` 用**子串断言**保护：

- body 类：`workspace-page`、`auth-body`、`auth-page`、`detail-page`、`report-page`
- 脚本标签：`/static/i18n.js`、`workspace.js`、`code-editor.js`（含 defer）
- 区块 id：`overview`、`data`、`projects`、`runs`、`reports`、`audit`、`secrets`…  
- 表单 `action` / `name`、大量英文文案、i18n 中 `"Workspace overview": "工作区概览"`  
- `workspace.js` 中 `candidate.hidden = candidate !== section` 等片段

**策略：改结构与视觉，但保留上述契约字符串与字段名。**

---

## 目标交付物

1. **完整可用**：侧栏每一项都能独立打开对应模块；空态清晰；认证 / 工作区 / 详情三壳视觉一致；现有功能表单不丢。
2. **简单大气**：单一设计 token、青绿主色、少边框层级、统一字重与圆角、侧栏深色 + 主区留白。
3. **不破坏测试**：`pytest tests/test_frontend_rendering.py` 与相关 auth 渲染断言继续绿。

---

## 实施方案

### 1. 修正工作区导航结构（最高优先级）

文件：`templates/index.html`、`static/workspace.js`（仅在必要时微调）

- 将 execution 区拆成 **三个独立** `section.band`：
  - `<section id="runs" class="band">…</section>`
  - `<section id="schedules" class="band">…</section>`
  - `<section id="reports" class="band">…</section>`
- 去掉把三者塞进同一 `section` 的 `execution-grid` 依赖（可用页内布局类，但 id 必须在 section 上）。
- 保持侧栏链接 `#runs` / `#schedules` / `#reports` 不变，使 `workspace.js` 现有逻辑自然生效。
- 补齐空状态文案（英文源文案，并在 `i18n.js` 增加对应中文键）。

### 2. 统一设计系统 CSS（简单大气）

文件：`static/styles.css`

- **合并为单一 `:root`**（以当前青绿 redesign 为准），补全 token：
  - surfaces：`--bg` / `--panel` / `--panel-soft` / sidebar 色
  - type：`--text`（并 `--ink: var(--text)`）
  - brand：`--accent` / `--accent-dark` / `--accent-soft`
  - semantic：info / good / warn / bad + soft 变体
  - `--radius`、`--shadow`、线色
- 删除被 redesign 完全覆盖的蓝色遗留按钮/状态/notice 重复块。
- 保留并精修三壳：
  - Workspace：深色侧栏 + 主区留白（已有骨架）
  - Auth：左侧色带 + 居中卡片（token 化硬编码色）
  - Detail：顶栏更干净，与主区间距/表格/badge 同源
- 视觉原则：一种主色、6px 圆角、badge 降噪、字重收敛到 400–700、少用硬编码蓝灰。
- **不改**测试依赖的 class 名。

### 3. 三壳视觉对齐（模板轻改）

文件：`templates/login.html`、`register.html`、`invitation*.html`、`password_reset*.html`、`api_token_created.html`、`data_source.html`、`run.html`、`runs.html`、`report.html`、`schedule_backfill.html`

- 保持现有 body class / form action / 字段名。
- 统一 topbar 信息层级、主按钮/次按钮、notice、空态、表格密度。
- 详情页顶栏：品牌感更轻、返回 Workspace 更清晰；不引入新 SPA。
- **暂不做** Jinja 公共 base 强制抽取（风险高、测试对整页 HTML 敏感）；若抽取 partial，仅 head/脚本块，且保证最终 HTML 子串不变。

### 4. 工作区内容体验补齐

文件：`templates/index.html`、`static/i18n.js`

- 空状态：No runs / No schedules / No projects / No members / No audit events 等（英文保持可断言友好的自然语言，并翻译）。
- Overview 与列表区的 spacing、卡片标题、指标行对齐。
- 数据源连接表单网格在宽屏 3 列、中屏 2 列、窄屏 1 列（沿用现有 breakpoint，去掉多余装饰条冲突）。
- Projects 折叠编辑器保持 `data-code-editor` 与懒加载行为。

### 5. 验证门禁

```bash
.venv/bin/python -m pytest tests/test_frontend_rendering.py tests/test_auth_workspace.py -q
.venv/bin/python tests/smoke_test.py   # 若时间允许
```

- 本地 `TestClient` 抽检 `/`、`/login`、详情页关键 class/id。
- 重点手测：侧栏切换 Overview / Data / Projects / **Runs / Schedules / Reports** 是否真正分屏；390px 抽屉开关。

---

## 明确不做（本轮）

- 不上 React/Vue/Node 构建；继续服务端模板 + 本地 static。
- 不改后端 API、表单字段名、路由 path。
- 不拆成多页面应用（避免重写 workspace 导航模型与大量测试）。
- 不做暗色主题全站切换（侧栏深色保留即可）。

---

## 实施顺序

1. 拆分 Runs/Schedules/Reports section + 空状态  
2. 合并 CSS token 与去重，修复 `--ink`  
3. Auth / Detail 壳样式对齐  
4. i18n 补词  
5. 跑前端/认证渲染测试并修回归  

## 成功标准

- 侧栏每一项对应独立可视模块；Runs / Schedules / Reports 可分别进入。  
- 全站主色/组件一致，观感简洁大气。  
- `test_frontend_rendering` + 相关 auth 渲染测试通过。  
- 现有上传、项目、调度、报表、登录表单仍完整可用。