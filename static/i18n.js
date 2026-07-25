(() => {
  "use strict";

  const STORAGE_KEY = "anydatas.locale";
  const CHINESE = "zh-CN";
  const ENGLISH = "en";
  const zh = {
    "AnyDatas MVP": "AnyDatas 数据平台",
    "Analysis workspace": "数据分析工作区",
    "Workspace": "工作区",
    "Workspace overview": "工作区概览",
    "Data operations, scheduled analysis and reporting in one place.": "在一个平台中完成数据处理、定时分析与报表发布。",
    "Overview": "概览",
    "Data Sources": "数据源",
    "Data sources": "数据源",
    "Projects": "项目",
    "Analysis Projects": "分析项目",
    "Runs": "运行记录",
    "Recent runs": "最近运行",
    "Schedules": "调度任务",
    "Reports": "报表",
    "Operations": "运维",
    "Notifications": "通知",
    "Usage": "用量",
    "Delivery": "投递",
    "Audit": "审计",
    "Administration": "管理",
    "Members": "成员",
    "Account": "账户",
    "Automation": "自动化",
    "Secrets": "密钥",
    "Limits": "配额",
    "Menu": "菜单",
    "Sign out": "退出登录",
    "Sign in": "登录",
    "Signed in": "登录成功",
    "Signed out": "已退出登录",
    "Create Account": "创建账户",
    "Create account": "创建账户",
    "Connect Data": "接入数据",
    "New Analysis": "新建分析",
    "View all": "查看全部",
    "Manage": "管理",
    "No runs yet.": "暂无运行记录。",
    "No connected sources.": "暂无已连接的数据源。",
    "No data sources yet.": "暂无数据源。",
    "No notifications.": "暂无通知。",
    "No reports yet.": "暂无报表。",
    "No schedules yet.": "暂无调度任务。",
    "No projects yet. Create an analysis project above.": "暂无项目。请先在上方创建分析项目。",
    "No members yet.": "暂无成员。",
    "No audit events yet.": "暂无审计事件。",
    "No matching runs.": "没有匹配的运行记录。",
    "No result": "暂无结果",
    "Account Security": "账户安全",
    "Current password": "当前密码",
    "Password": "密码",
    "New password": "新密码",
    "Confirm password": "确认密码",
    "Confirm new password": "确认新密码",
    "Change Password": "修改密码",
    "Reset Password": "重置密码",
    "Create API Token": "创建 API 令牌",
    "API token created": "API 令牌已创建",
    "API Token Created · AnyDatas": "API 令牌已创建 · AnyDatas",
    "Active API Tokens": "有效的 API 令牌",
    "Expires in days": "有效天数",
    "Expires": "过期时间",
    "Last Used": "最后使用",
    "Scope": "权限范围",
    "Read only": "只读",
    "Full access": "完整权限",
    "Create Token": "创建令牌",
    "Token": "令牌",
    "Revoke": "撤销",
    "No active API tokens.": "暂无有效的 API 令牌。",
    "Service Accounts": "服务账户",
    "Create Service Account": "创建服务账户",
    "Initial token scope": "初始令牌权限",
    "Active Service Accounts": "有效的服务账户",
    "New Token": "新建令牌",
    "No service accounts.": "暂无服务账户。",
    "Status": "状态",
    "Message": "消息",
    "Resource": "资源",
    "Created": "创建时间",
    "Mark Read": "标记已读",
    "Delivery Channels": "投递渠道",
    "Configured Channels": "已配置渠道",
    "Add Delivery Channel": "添加投递渠道",
    "Channel": "渠道",
    "Type": "类型",
    "Target": "目标",
    "Events": "事件",
    "Remove": "移除",
    "No delivery channels.": "暂无投递渠道。",
    "Email Recipients": "邮件收件人",
    "Webhook URL Reference": "Webhook 地址引用",
    "Select Secret Reference": "选择密钥引用",
    "Event Types": "事件类型",
    "Run failures": "运行失败",
    "Report refresh succeeded": "报表刷新成功",
    "Report refresh failed": "报表刷新失败",
    "Retry Count": "重试次数",
    "Save Channel": "保存渠道",
    "Recent Deliveries": "最近投递",
    "Attempts": "尝试次数",
    "Last Error": "最近错误",
    "Retry": "重试",
    "Channel inactive": "渠道未启用",
    "No delivery attempts.": "暂无投递记录。",
    "Workspace Members": "工作区成员",
    "Pending Invitations": "待处理邀请",
    "Name": "名称",
    "Email": "邮箱",
    "Role": "角色",
    "Viewer": "查看者",
    "Analyst": "分析员",
    "Admin": "管理员",
    "Owner": "所有者",
    "No pending invitations.": "暂无待处理邀请。",
    "Workspace Limits": "工作区配额",
    "Resource Usage": "资源使用量",
    "Used": "已使用",
    "Limit": "上限",
    "Concurrent Runs": "并发运行",
    "Data Source Storage": "数据源存储",
    "Storage (MiB)": "存储空间 (MiB)",
    "Save Limits": "保存配额",
    "Runtime Usage": "运行用量",
    "Workspace aggregate": "工作区汇总",
    "Period": "周期",
    "Succeeded": "成功",
    "Failed": "失败",
    "Canceled": "已取消",
    "Active": "运行中",
    "succeeded": "成功",
    "failed": "失败",
    "canceled": "已取消",
    "queued": "排队中",
    "running": "运行中",
    "schedule": "调度",
    "manual": "手动",
    "report": "报表",
    "backfill": "回填",
    "interval": "间隔",
    "cron": "Cron",
    "workspace": "工作区",
    "private": "私有",
    "manage": "管理",
    "query": "查询",
    "view": "查看",
    "file": "文件",
    "internal": "内部",
    "public": "公开",
    "confidential": "机密",
    "restricted": "受限",
    "Success Rate": "成功率",
    "Compute Hours": "计算小时",
    "Average Duration": "平均耗时",
    "Estimated Cost": "预估成本",
    "Secret References": "密钥引用",
    "References": "引用列表",
    "Source Variable": "来源变量",
    "Description": "说明",
    "No secret references.": "暂无密钥引用。",
    "Add Reference": "添加引用",
    "Save Reference": "保存引用",
    "Upload File": "上传文件",
    "Upload": "上传",
    "File": "文件",
    "Max MB": "最大 MB",
    "Internal": "内部",
    "Public": "公开",
    "Confidential": "机密",
    "Restricted": "受限",
    "Connect SQLite": "连接 SQLite",
    "Connect PostgreSQL": "连接 PostgreSQL",
    "Connect MySQL": "连接 MySQL",
    "Connect ClickHouse": "连接 ClickHouse",
    "Connect": "连接",
    "Connection Reference": "连接引用",
    "Database path": "数据库路径",
    "Database": "数据库",
    "Schema": "结构",
    "Table": "数据表",
    "Table or view": "数据表或视图",
    "Import S3 / MinIO": "导入 S3 / MinIO",
    "Bucket": "存储桶",
    "Object Key": "对象键",
    "Import Snapshot": "导入快照",
    "Available Tables": "可用数据表",
    "Rows": "行数",
    "Columns": "字段",
    "Access": "访问权限",
    "Classification": "分类",
    "Quality": "质量",
    "Issues": "问题",
    "Project": "项目",
    "Language": "语言",
    "Format": "格式化",
    "Data source": "数据源",
    "Runtime": "运行环境",
    "Parameters (JSON; SQL uses $name)": "参数（JSON；SQL 使用 $name）",
    "Create Project": "创建项目",
    "Save Version": "保存版本",
    "Draft": "草稿",
    "Not published": "未发布",
    "Linked Reports": "关联报表",
    "No linked reports.": "暂无关联报表。",
    "Secret Bindings": "密钥绑定",
    "Reference": "引用",
    "Runtime Variable": "运行变量",
    "Bind Secret": "绑定密钥",
    "Publish": "发布",
    "Run": "运行",
    "Visibility": "可见范围",
    "Private": "私有",
    "Report": "报表",
    "Search all": "搜索全部",
    "Trigger": "触发方式",
    "Attempt": "尝试次数",
    "Started": "开始时间",
    "Duration": "耗时",
    "Details": "详情",
    "Interval": "间隔",
    "Cron": "Cron 表达式",
    "Retries": "重试",
    "Delay min": "延迟分钟",
    "Concurrency": "并发策略",
    "Skip": "跳过",
    "Queue one": "排队一个",
    "Queue all": "全部排队",
    "Cancel previous": "取消上次运行",
    "Create": "创建",
    "Rule": "规则",
    "Next": "下次运行",
    "Pause": "暂停",
    "Resume": "恢复",
    "Run Now": "立即运行",
    "Backfill": "回填",
    "Save": "保存",
    "Action": "操作",
    "Time": "时间",
    "S3 Snapshot": "S3 快照",
    "Size": "大小",
    "Version": "版本",
    "Modified": "修改时间",
    "Refresh Snapshot": "刷新快照",
    "Completeness": "完整度",
    "Handling Level": "处理级别",
    "Save Visibility": "保存可见范围",
    "Save Classification": "保存分类",
    "Member Access": "成员权限",
    "Member": "成员",
    "Permission": "权限",
    "Save Member Access": "保存成员权限",
    "Impact Analysis": "影响分析",
    "Active Schedules": "有效调度",
    "Historical Runs": "历史运行",
    "Published": "已发布",
    "Versions": "版本数",
    "No project dependencies.": "无项目依赖。",
    "Export Masking": "导出脱敏",
    "Empty": "空值",
    "Unique": "唯一值",
    "Samples": "样例",
    "Save Schema": "保存结构",
    "Preview": "预览",
    "Run Search": "运行检索",
    "Search logs or errors": "搜索日志或错误",
    "All": "全部",
    "Started from": "开始日期从",
    "Started to": "开始日期至",
    "Search": "搜索",
    "Clear": "清除",
    "Results": "结果",
    "Log excerpt": "日志摘要",
    "Execution": "执行信息",
    "Run Details": "运行详情",
    "Cancel Run": "取消运行",
    "Scheduled For": "计划时间",
    "Retry Of": "重试来源",
    "Finished": "结束时间",
    "Pending": "等待中",
    "Parameters": "参数",
    "Cancellation": "取消信息",
    "Error Summary": "错误摘要",
    "Result Artifacts": "结果产物",
    "Execution Logs": "执行日志",
    "Previous": "上一页",
    "Page": "第",
    "of": "共",
    "This run has not produced a table result.": "此运行尚未生成表格结果。",
    "Download CSV": "下载 CSV",
    "Download JSON": "下载 JSON",
    "Download XLSX": "下载 XLSX",
    "Download PNG": "下载 PNG",
    "Download PDF": "下载 PDF",
    "Lineage": "数据血缘",
    "latest snapshot": "最新快照",
    "awaiting first snapshot": "等待首次快照",
    "Data Source": "数据源",
    "Unavailable": "不可用",
    "Snapshot Run": "快照运行",
    "Latest Snapshot": "最新快照",
    "No successful snapshot": "暂无成功快照",
    "Subscribed": "已订阅",
    "Save Delivery": "保存投递设置",
    "Subscribe": "订阅",
    "Unsubscribe": "取消订阅",
    "Export": "导出",
    "Filters": "筛选器",
    "Apply": "应用",
    "Private Access": "私有访问权限",
    "Grant Access": "授予权限",
    "No additional eligible members are available.": "没有其他可授权成员。",
    "Components": "组件",
    "Component": "组件",
    "Metric": "指标",
    "Bar Chart": "柱状图",
    "Line Chart": "折线图",
    "Scatter Chart": "散点图",
    "Pie Chart": "饼图",
    "Markdown": "Markdown",
    "Title": "标题",
    "Width": "宽度",
    "Automatic": "自动",
    "Quarter": "四分之一",
    "Half": "一半",
    "Full": "全宽",
    "Aggregate": "聚合方式",
    "Sum": "求和",
    "Average": "平均值",
    "Minimum": "最小值",
    "Maximum": "最大值",
    "Count": "计数",
    "Row count": "行数",
    "Column count": "字段数",
    "Value column": "值字段",
    "Label column": "标签字段",
    "X column": "X 轴字段",
    "Table rows": "表格行数",
    "Highlight column": "高亮字段",
    "Highlight rule": "高亮规则",
    "None": "无",
    "Positive": "正数",
    "Negative": "负数",
    "At or above": "大于等于",
    "At or below": "小于等于",
    "Highlight threshold": "高亮阈值",
    "Add Component": "添加组件",
    "Filter": "筛选器",
    "Column": "字段",
    "Select": "选择",
    "Contains": "包含",
    "Number range": "数值范围",
    "Default": "默认值",
    "Add Filter": "添加筛选器",
    "Refresh Status": "刷新状态",
    "No chartable numeric data.": "没有可绘制的数值数据。",
    "Choose two numeric columns to render a scatter chart.": "请选择两个数值字段绘制散点图。",
    "No positive numeric data for a pie chart.": "没有可用于饼图的正数数据。",
    "Refresh the report after a successful project run to publish a snapshot.": "项目成功运行后刷新报表即可发布快照。",
    "Backfill Schedule": "调度回填",
    "State": "状态",
    "Start": "开始",
    "End": "结束",
    "Run limit": "运行上限",
    "Queue Backfill": "加入回填队列",
    "Join Workspace": "加入工作区",
    "Invitation created": "邀请已创建",
    "Invitation Created · AnyDatas": "邀请已创建 · AnyDatas",
    "Invitation Link": "邀请链接",
    "One-time link": "一次性链接",
    "Open Invitation": "打开邀请",
    "Password reset created": "密码重置链接已创建",
    "Password Reset Created · AnyDatas": "密码重置链接已创建 · AnyDatas",
    "Open Reset": "打开重置页面",
    "Done": "完成",
    "Create Account · AnyDatas": "创建账户 · AnyDatas",
    "Reset Password · AnyDatas": "重置密码 · AnyDatas",
    "Join · AnyDatas": "加入工作区 · AnyDatas",
    "Sign in · AnyDatas": "登录 · AnyDatas",
    "Continue": "继续",
    "Restricted sources can be analyzed by query-authorized members, but derived CSV and JSON exports require source manage access.": "拥有查询权限的成员可以分析受限数据源，但导出派生 CSV 和 JSON 需要数据源管理权限。",
    "Downloads require manage access to this restricted data source.": "下载此受限数据源需要管理权限。",
    "Private reports are visible to their creator, workspace owners and admins, plus selected members below.": "私有报表仅对创建者、工作区所有者、管理员及下方指定成员可见。",
    "Set this report to Private from the workspace report list before granting selected members access.": "请先在工作区报表列表中将此报表设为私有，再为指定成员授权。"
  };

  const patterns = [
    [/^(\d+) workspace runs$/, "$1 条工作区运行记录"],
    [/^(\d+) projects$/, "$1 个项目"],
    [/^(\d+) reports$/, "$1 个报表"],
    [/^(\d+) schedules$/, "$1 个调度任务"],
    [/^(\d+) sources$/, "$1 个数据源"],
    [/^(\d+) members$/, "$1 个成员"],
    [/^(\d+) fields$/, "$1 个字段"],
    [/^(\d+) rows$/, "$1 行"],
    [/^(\d+) values$/, "$1 个值"],
    [/^(\d+) configured$/, "已配置 $1 个"],
    [/^(\d+) active$/, "$1 个有效项"],
    [/^(\d+) unread$/, "$1 条未读"],
    [/^(\d+) recent$/, "最近 $1 条"],
    [/^Page (\d+) of (\d+)$/, "第 $1 页，共 $2 页"],
    [/^(\d+)[–-](\d+) of (\d+) rows$/, "第 $1–$2 行，共 $3 行"],
    [/^(\d+)[–-](\d+) of (\d+) log lines$/, "第 $1–$2 行，共 $3 行日志"],
    [/^Every (\d+) min$/, "每 $1 分钟"],
    [/^(\d+) matching rows$/, "$1 行匹配结果"],
    [/^(\d+) MiB managed$/, "已管理 $1 MiB"],
    [/^(\d+) active$/, "$1 个运行中"],
    [/^(\d+) unread updates$/, "$1 条未读更新"],
    [/^Latest v(\d+)$/, "最新版本 v$1"],
    [/^Published v(\d+)$/, "已发布 v$1"],
    [/^v(\d+) published$/, "v$1 已发布"],
    [/^(\d+) params$/, "$1 个参数"],
    [/^(\d+) secrets$/, "$1 个密钥"],
    [/^(\d+) visible$/, "$1 个可见项"],
    [/^schedule · (.+)$/, "调度 · $1"],
    [/^manual · (.+)$/, "手动 · $1"],
    [/^(\d+) rows · manage$/, "$1 行 · 管理"],
    [/^(\d+) rows · query$/, "$1 行 · 查询"],
    [/^(\d+) rows · view$/, "$1 行 · 查看"],
    [/^Run · (.+)$/, "运行 · $1"],
    [/^Run ([a-f0-9]+)$/, "运行 $1"],
    [/^Line (\d+)$/, "第 $1 行"],
    [/^Reset Password$/, "重置密码"]
  ];

  const attributeTranslations = {
    "Filter data sources": "筛选数据源",
    "Filter projects": "筛选项目",
    "Daily revenue check": "每日收入检查",
    "Daily refresh": "每日刷新",
    "Read-only warehouse credential": "只读数据仓库凭证",
    "Revenue total": "收入总额",
    "Region": "区域",
    "# Notes": "# 备注",
    "min,max for range": "范围最小值,最大值",
    "Drag to reorder": "拖动排序",
    "Move up": "上移",
    "Move down": "下移",
    "Grant private report access": "授予私有报表访问权限",
    "Result pages": "结果分页",
    "Log pages": "日志分页",
    "Report visibility": "报表可见范围",
    "timeout, order id, run id": "超时、订单号、运行 ID"
  };

  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  const skipped = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA", "SVG"]);
  const tableEnums = new Set([
    "succeeded", "failed", "canceled", "queued", "running",
    "schedule", "manual", "report", "backfill", "interval", "cron",
    "workspace", "private", "manage", "query", "view", "file",
    "internal", "public", "confidential", "restricted"
  ]);

  function translateText(value) {
    if (zh[value]) return zh[value];
    for (const [pattern, replacement] of patterns) {
      if (pattern.test(value)) return value.replace(pattern, replacement);
    }
    return value;
  }

  function translateNode(node, locale) {
    if (!originalText.has(node)) originalText.set(node, node.nodeValue);
    const source = originalText.get(node);
    if (locale === ENGLISH) {
      node.nodeValue = source;
      return;
    }
    const value = source.trim();
    if (!value) return;
    const translated = translateText(value);
    if (translated === value) return;
    node.nodeValue = source.replace(value, translated);
  }

  function translateAttributes(element, locale) {
    const names = ["placeholder", "title", "aria-label"];
    if (!originalAttributes.has(element)) {
      const values = {};
      names.forEach((name) => {
        if (element.hasAttribute(name)) values[name] = element.getAttribute(name);
      });
      originalAttributes.set(element, values);
    }
    const values = originalAttributes.get(element);
    Object.entries(values).forEach(([name, value]) => {
      if (locale === ENGLISH) {
        element.setAttribute(name, value);
        return;
      }
      const translated = attributeTranslations[value] || translateText(value);
      element.setAttribute(name, translated);
    });
  }

  function shouldSkip(node) {
    const parent = node.parentElement;
    if (!parent || skipped.has(parent.tagName)) return true;
    if (parent.closest("[data-i18n-skip], .code-editor-surface, .log-block")) return true;
    if (parent.closest("td") && !parent.closest(".status, button, .button-link")) {
      return !tableEnums.has(node.nodeValue.trim());
    }
    return false;
  }

  let currentLocale = ENGLISH;

  function applyLocale(locale) {
    const normalized = locale === CHINESE ? CHINESE : ENGLISH;
    currentLocale = normalized;
    document.documentElement.lang = normalized;
    document.title = normalized === CHINESE ? translateText(originalTitle) : originalTitle;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      if (!shouldSkip(node)) translateNode(node, normalized);
    });
    document.querySelectorAll("[placeholder], [title], [aria-label]").forEach((element) => {
      if (!element.closest("[data-i18n-skip], .code-editor-surface, .log-block")) {
        translateAttributes(element, normalized);
      }
    });
    document.querySelectorAll("[data-locale]").forEach((button) => {
      const active = button.dataset.locale === normalized;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    localStorage.setItem(STORAGE_KEY, normalized);
  }

  function createSwitcher() {
    const switcher = document.createElement("div");
    switcher.className = "language-switcher";
    switcher.setAttribute("role", "group");
    switcher.setAttribute("aria-label", "Language");
    switcher.innerHTML = `
      <button type="button" data-locale="zh-CN">中文</button>
      <button type="button" data-locale="en">EN</button>
    `;
    switcher.addEventListener("click", (event) => {
      const button = event.target.closest("[data-locale]");
      if (button) applyLocale(button.dataset.locale);
    });
    const sidebarUser = document.querySelector(".sidebar-user");
    const detailNav = document.querySelector(".detail-page .topbar nav");
    const authNav = document.querySelector(".auth-body .topbar nav");
    if (sidebarUser) sidebarUser.before(switcher);
    else if (detailNav) detailNav.prepend(switcher);
    else if (authNav) authNav.prepend(switcher);
    else document.body.append(switcher);
  }

  const originalTitle = document.title;
  createSwitcher();
  const saved = localStorage.getItem(STORAGE_KEY);
  const preferred = saved || (navigator.language.toLowerCase().startsWith("zh") ? CHINESE : ENGLISH);
  applyLocale(preferred);

  const observer = new MutationObserver((records) => {
    records.forEach((record) => {
      record.addedNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE) {
          if (!shouldSkip(node)) translateNode(node, currentLocale);
          return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          if (!shouldSkip(walker.currentNode)) translateNode(walker.currentNode, currentLocale);
        }
        if (node.matches("[placeholder], [title], [aria-label]")) translateAttributes(node, currentLocale);
        node.querySelectorAll("[placeholder], [title], [aria-label]").forEach((element) => {
          translateAttributes(element, currentLocale);
        });
      });
    });
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
