<script setup lang="ts">
import { computed } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckOutlined,
  CloseOutlined,
  LinkOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { useAssistantStore } from '@/stores/assistant'
import type { AssistantAction } from '@/types'

const props = defineProps<{ action: AssistantAction; compact?: boolean }>()
const store = useAssistantStore()

const pending = computed(() => props.action.status === 'proposed')
const preview = computed(() => props.action.preview ?? {})
const kind = computed(() => String(preview.value.kind ?? 'tool'))

const title = computed(() => {
  if (kind.value === 'email_draft') return '待保存的邮件草稿'
  if (kind.value === 'email_send') return '待发送的邮件回复'
  if (kind.value === 'email_send_draft') return '待发送的飞书邮箱草稿'
  if (kind.value === 'calendar_event') return '待创建的日历会议'
  return '待确认操作'
})

const resultLink = computed(() => {
  const value = props.action.result?.html_link
  return typeof value === 'string' && value.startsWith('https://') ? value : null
})

const resultLabel = computed(() => {
  const result = props.action.result
  if (!result) return ''
  if (result.draft_id) return `草稿 ID：${result.draft_id}`
  if (result.message_id) return `邮件 ID：${result.message_id}`
  if (result.id) return `事件 ID：${result.id}`
  return '操作已完成'
})

async function apply() {
  try {
    await store.applyAction(props.action.id)
    message.success('已执行并保存结果')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function reject() {
  try {
    await store.rejectAction(props.action.id)
    message.info('已拒绝，未执行任何外部写入')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="assistant-action" :class="[action.status, { compact }]">
    <div class="head">
      <WarningOutlined v-if="action.status === 'proposed'" class="pending" />
      <CheckOutlined v-else-if="action.status === 'applied'" class="applied" />
      <CloseOutlined v-else class="closed" />
      <div>
        <div class="title">{{ title }}</div>
        <div class="summary">{{ action.summary }}</div>
      </div>
    </div>

    <div v-if="kind === 'email_draft' || kind === 'email_send'" class="preview">
      <div><span>收件人</span>{{ preview.to }}</div>
      <div><span>抄送</span>{{ preview.cc || '无' }}</div>
      <div><span>主题</span>{{ preview.subject }}</div>
      <pre class="body">{{ preview.body }}</pre>
    </div>

    <div v-else-if="kind === 'email_send_draft'" class="preview">
      <div><span>草稿 ID</span>{{ preview.draft_id }}</div>
    </div>

    <div v-else-if="kind === 'calendar_event'" class="preview">
      <div><span>会议</span>{{ preview.summary }}</div>
      <div><span>开始</span>{{ preview.start }}</div>
      <div><span>结束</span>{{ preview.end }}</div>
      <div><span>时区</span>{{ preview.timezone }}</div>
      <div><span>参会人</span>{{ (preview.attendees as string[])?.join('、') || '无' }}</div>
      <div v-if="preview.description"><span>说明</span>{{ preview.description }}</div>
      <div v-if="preview.location"><span>地点</span>{{ preview.location }}</div>
      <div v-if="preview.invitations" class="invite">批准后将向参会人发送邀请</div>
    </div>

    <div v-if="action.error" class="error">{{ action.error }}</div>
    <div v-if="action.status === 'applied'" class="result">
      {{ resultLabel }}
      <a v-if="resultLink" :href="resultLink" target="_blank" rel="noreferrer">
        <LinkOutlined /> 打开日历
      </a>
    </div>

    <div v-if="pending" class="actions">
      <a-button type="primary" size="small" @click="apply">
        <template #icon><CheckOutlined /></template>
        批准并执行
      </a-button>
      <a-button size="small" @click="reject">
        <template #icon><CloseOutlined /></template>
        拒绝
      </a-button>
    </div>
  </div>
</template>

<style scoped>
.assistant-action {
  border: 1px solid var(--border);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius);
  background: #fffdf7;
  padding: 12px 14px;
  margin-top: 10px;
}

.assistant-action.applied {
  border-left-color: var(--success);
  background: #f6fdf8;
}

.assistant-action.rejected,
.assistant-action.expired,
.assistant-action.failed {
  border-left-color: var(--text-muted);
  background: var(--surface-muted);
}

.assistant-action.compact {
  margin-top: 8px;
}

.head {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.head > :first-child {
  margin-top: 3px;
}

.pending {
  color: var(--warning);
}

.applied {
  color: var(--success);
}

.closed {
  color: var(--text-muted);
}

.title {
  font-size: 13px;
  font-weight: 600;
}

.summary {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-muted);
}

.preview {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 11px;
  background: white;
  font-size: 12px;
  line-height: 1.7;
}

.preview > div {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 8px;
}

.preview span {
  color: var(--text-muted);
}

.body {
  margin: 8px 0 0;
  padding: 9px;
  border-radius: 6px;
  background: var(--surface-muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  max-height: 260px;
  overflow: auto;
}

.invite {
  margin-top: 5px;
  color: var(--warning);
  font-weight: 500;
}

.actions {
  display: flex;
  gap: 8px;
  margin-top: 11px;
}

.error {
  margin-top: 8px;
  color: var(--danger);
  font-size: 12px;
}

.result {
  margin-top: 8px;
  font-size: 12px;
  color: var(--success);
  display: flex;
  gap: 10px;
}
</style>
