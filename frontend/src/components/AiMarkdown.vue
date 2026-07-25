<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { Marked, Renderer } from 'marked'

const props = defineProps<{ content: string }>()

const renderer = new Renderer()
const renderDefaultTable = renderer.table.bind(renderer)

/**
 * 给 GFM 表格增加独立滚动边界，宽表只在自身区域横向滚动，不会撑宽整条聊天消息。
 * 保留 Marked 的默认单元格解析可以继续正确处理表内粗体、代码和链接。
 */
renderer.table = (token) => (
  `<div class="ai-markdown-table-wrap">${renderDefaultTable(token)}</div>`
)

const markdown = new Marked()
markdown.setOptions({
  async: false,
  breaks: true,
  gfm: true,
  renderer,
})

const renderedContent = computed(() => renderMarkdown(props.content))

/**
 * 将模型 Markdown 转为经过清洗的 HTML。
 * 解析和净化分离可以保留列表与代码格式，同时阻止模型内容注入脚本或危险属性。
 */
function renderMarkdown(content: string): string {
  const html = markdown.parse(content, { async: false })
  return DOMPurify.sanitize(html, {
    FORBID_ATTR: ['style'],
    FORBID_TAGS: ['form', 'iframe', 'style'],
    USE_PROFILES: { html: true },
  })
}
</script>

<template>
  <div class="ai-markdown" v-html="renderedContent" />
</template>
