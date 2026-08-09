-- 助手消息可携带一份图表建议（AI 按数据内容推荐的图表类型与列映射，纯数据白名单）。
ALTER TABLE ai_messages ADD COLUMN chart_spec_json TEXT;
