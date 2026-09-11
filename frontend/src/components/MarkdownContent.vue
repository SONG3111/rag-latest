<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import cjkFriendly from 'markdown-it-cjk-friendly'

const props = defineProps<{ text: string }>()

// html: false —— 模型输出的原始 HTML 一律转义，不参与渲染
// cjkFriendly —— 修正中文标点紧邻 ** 时（如 **"销售"**）加粗失效的 CommonMark 规则问题
const md = new MarkdownIt({ html: false, linkify: true, breaks: true }).use(cjkFriendly)

// 让渲染出来的链接在新标签页打开，并且不允许反向拿窗口引用
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node instanceof Element && node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

const FENCE_PATTERN = /^ {0,3}(?:```|~~~)/gm

// 流式输出时可能停在半个代码块中间，补一个结束标记，
// 否则剩下的正文会被吸进代码块，看起来像卡住了
function balanceOpenFence(source: string): string {
  const fences = source.match(FENCE_PATTERN)
  return fences && fences.length % 2 === 1 ? `${source}\n\`\`\`` : source
}

const html = computed(() => {
  const source = props.text ?? ''
  if (!source.trim()) return ''
  return DOMPurify.sanitize(md.render(balanceOpenFence(source)), { USE_PROFILES: { html: true } })
})
</script>

<template>
  <!-- 内容来自模型输出，已经过 DOMPurify 清洗 -->
  <div class="markdown" v-html="html" />
</template>

<style scoped>
.markdown {
  word-break: break-word;
}

.markdown :deep(> :first-child) {
  margin-top: 0;
}

.markdown :deep(> :last-child) {
  margin-bottom: 0;
}

.markdown :deep(p) {
  margin: 0 0 8px;
}

.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4) {
  margin: 14px 0 8px;
  font-weight: 600;
  line-height: 1.4;
}

.markdown :deep(h1) {
  font-size: 17px;
}

.markdown :deep(h2) {
  font-size: 16px;
}

.markdown :deep(h3),
.markdown :deep(h4) {
  font-size: 15px;
}

.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}

.markdown :deep(li) {
  margin: 3px 0;
}

.markdown :deep(li > p) {
  margin: 0;
}

.markdown :deep(code) {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 12.5px;
  background: rgba(15, 23, 42, 0.07);
  border-radius: 4px;
  padding: 1px 5px;
}

.markdown :deep(pre) {
  margin: 8px 0;
  padding: 10px 12px;
  background: #0f172a;
  border-radius: 8px;
  overflow-x: auto;
}

.markdown :deep(pre code) {
  background: none;
  color: #e2e8f0;
  padding: 0;
  font-size: 12.5px;
  line-height: 1.6;
}

.markdown :deep(blockquote) {
  margin: 8px 0;
  padding: 2px 0 2px 12px;
  border-left: 3px solid var(--border);
  color: var(--text-muted);
}

.markdown :deep(table) {
  width: 100%;
  margin: 8px 0;
  border-collapse: collapse;
  font-size: 13px;
}

.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid var(--border);
  padding: 5px 8px;
  text-align: left;
}

.markdown :deep(th) {
  background: rgba(15, 23, 42, 0.04);
  font-weight: 600;
}

.markdown :deep(a) {
  color: var(--accent);
}

.markdown :deep(hr) {
  margin: 12px 0;
  border: none;
  border-top: 1px solid var(--border);
}
</style>
